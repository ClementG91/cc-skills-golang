#!/usr/bin/env python3
"""Run real LLM evaluations for repository skills.

The harness executes each eval twice: once with only the base Go agent prompt and
once with the target skill injected as context. It then scores each assertion
with a separate blind judge call. Raw prompts, responses, judge decisions, and
summaries are written to an artifact directory so the published score can be
audited after the run.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_SYSTEM_PROMPT = """You are a senior Go engineer working inside an AI coding agent.
Answer the user's Go question directly and professionally.
Prefer modern Go standard library features when they are a better fit.
Do not invent APIs. If a dependency is not justified, say so.
"""

SKILL_SYSTEM_PROMPT = """You are a senior Go engineer working inside an AI coding agent.
The following skill instructions are available and should be followed when relevant.

<skill_context>
{skill_context}
</skill_context>

Answer the user's Go question directly and professionally.
Prefer modern Go standard library features when they are a better fit.
Do not invent APIs. If a dependency is not justified, say so.
"""

JUDGE_SYSTEM_PROMPT = """You are an impartial evaluator of Go engineering answers.
You will receive one user task, one candidate answer, and one assertion.
Decide whether the candidate answer satisfies the assertion.

Rules:
- Be strict but fair.
- Do not infer unstated facts.
- For assertions starting with "Does NOT", pass only when the prohibited behavior is absent.
- Ignore style unless the assertion explicitly requires style.
- Return only valid JSON with keys: pass, confidence, rationale.
"""


@dataclass(frozen=True)
class AssertionItem:
    assertion_id: str
    text: str
    kind: str


@dataclass(frozen=True)
class EvalCase:
    skill: str
    eval_id: str
    name: str
    prompt: str
    expected_output: str
    trap: str
    assertions: list[AssertionItem]


@dataclass(frozen=True)
class Candidate:
    variant: str
    blind_id: str
    response: str
    provider: str
    model: str
    latency_ms: int
    error: str


class ProviderError(RuntimeError):
    pass


def repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
    )
    return Path(out.strip())


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_run_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_eval_cases(root: Path, selected_skills: set[str] | None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted((root / "skills").glob("*/evals/evals.json")):
        skill = path.parents[1].name
        if selected_skills and skill not in selected_skills:
            continue
        data = read_json(path)
        if isinstance(data, dict):
            evals = data.get("evals", [])
            skill = str(data.get("skill_name") or skill)
        else:
            evals = data
        if not isinstance(evals, list):
            raise ValueError(f"{path}: evals root must be a list or an object with evals")

        for item_index, item in enumerate(evals, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"{path}: eval {item_index} is not an object")
            prompt = str(item.get("prompt") or item.get("task") or "").strip()
            if not prompt:
                raise ValueError(f"{path}: eval {item_index} has no prompt/task")
            raw_assertions = item.get("assertions", [])
            if not isinstance(raw_assertions, list) or not raw_assertions:
                raise ValueError(f"{path}: eval {item_index} has no assertions")
            assertions: list[AssertionItem] = []
            for assertion_index, assertion in enumerate(raw_assertions, start=1):
                fallback_id = f"{item.get('id', item_index)}.{assertion_index}"
                if isinstance(assertion, str):
                    assertions.append(AssertionItem(fallback_id, assertion.strip(), "semantic"))
                    continue
                if not isinstance(assertion, dict):
                    raise ValueError(f"{path}: assertion {fallback_id} must be string or object")
                text = str(assertion.get("text") or "").strip()
                if not text:
                    raise ValueError(f"{path}: assertion {fallback_id} has no text")
                assertions.append(
                    AssertionItem(
                        str(assertion.get("id") or fallback_id),
                        text,
                        str(assertion.get("type") or "semantic"),
                    )
                )

            cases.append(
                EvalCase(
                    skill=skill,
                    eval_id=str(item.get("id") or item_index),
                    name=str(item.get("name") or f"eval-{item_index}"),
                    prompt=prompt,
                    expected_output=str(item.get("expected_output") or ""),
                    trap=str(item.get("trap") or ""),
                    assertions=assertions,
                )
            )
    return cases


def select_cases(cases: list[EvalCase], eval_ids: set[str] | None, limit: int | None) -> list[EvalCase]:
    selected = [case for case in cases if not eval_ids or case.eval_id in eval_ids or case.name in eval_ids]
    if limit is not None:
        selected = selected[:limit]
    return selected


def load_skill_context(root: Path, skill: str, context_mode: str) -> str:
    skill_dir = root / "skills" / skill
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        raise FileNotFoundError(f"{skill_file} not found")

    parts = [f"# {skill}/SKILL.md\n\n{skill_file.read_text(encoding='utf-8')}"]
    if context_mode == "skill-with-references":
        for path in sorted(skill_dir.rglob("*.md")):
            if path.name == "SKILL.md":
                continue
            rel = path.relative_to(skill_dir).as_posix()
            parts.append(f"\n\n# {skill}/{rel}\n\n{path.read_text(encoding='utf-8')}")
    return "\n".join(parts)


def request_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(str(exc)) from exc


def call_openai(model: str, system_prompt: str, user_prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ProviderError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_output_tokens": max_tokens,
    }
    data = request_json(
        "https://api.openai.com/v1/responses",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload,
        timeout,
    )
    if data.get("output_text"):
        return str(data["output_text"])
    fragments: list[str] = []
    for output in data.get("output", []):
        for content in output.get("content", []):
            if "text" in content:
                fragments.append(str(content["text"]))
    if fragments:
        return "\n".join(fragments)
    raise ProviderError(f"OpenAI response did not contain text: {data}")


def call_anthropic(model: str, system_prompt: str, user_prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ProviderError("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    data = request_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload,
        timeout,
    )
    fragments = [str(part.get("text", "")) for part in data.get("content", []) if part.get("type") == "text"]
    if fragments:
        return "\n".join(fragments)
    raise ProviderError(f"Anthropic response did not contain text: {data}")


def call_provider(
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    timeout: int,
) -> str:
    if provider == "openai":
        return call_openai(model, system_prompt, user_prompt, max_tokens, timeout)
    if provider == "anthropic":
        return call_anthropic(model, system_prompt, user_prompt, max_tokens, timeout)
    raise ProviderError(f"unsupported provider: {provider}")


def retry_call(fn: Any, attempts: int, base_sleep: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - CLI must record provider failures.
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(base_sleep * attempt)
    raise ProviderError(str(last_error)) from last_error


def generate_candidate(
    case: EvalCase,
    variant: str,
    root: Path,
    args: argparse.Namespace,
    blind_id: str,
) -> Candidate:
    if args.dry_run:
        return Candidate(
            variant=variant,
            blind_id=blind_id,
            response=f"DRY RUN: {variant} response for {case.skill}/{case.eval_id}",
            provider=args.provider,
            model=args.model,
            latency_ms=0,
            error="",
        )

    if variant == "with_skill":
        skill_context = load_skill_context(root, case.skill, args.context_mode)
        system_prompt = SKILL_SYSTEM_PROMPT.format(skill_context=skill_context)
    else:
        system_prompt = BASE_SYSTEM_PROMPT

    started = time.perf_counter()
    try:
        response = retry_call(
            lambda: call_provider(
                args.provider,
                args.model,
                system_prompt,
                case.prompt,
                args.max_output_tokens,
                args.timeout_seconds,
            ),
            args.retry_attempts,
            args.retry_sleep_seconds,
        )
        error = ""
    except Exception as exc:  # noqa: BLE001 - persisted as artifact.
        response = ""
        error = str(exc)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return Candidate(variant, blind_id, response, args.provider, args.model, latency_ms, error)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"judge did not return JSON: {text[:500]}")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("judge JSON is not an object")
    return value


def score_native_assertion(assertion: AssertionItem, response: str) -> dict[str, Any] | None:
    kind = assertion.kind.lower()
    if kind not in {"contains", "not_contains", "regex", "not_regex"}:
        return None
    passed = False
    if kind == "contains":
        passed = assertion.text.lower() in response.lower()
    elif kind == "not_contains":
        passed = assertion.text.lower() not in response.lower()
    elif kind == "regex":
        passed = bool(re.search(assertion.text, response, flags=re.IGNORECASE | re.DOTALL))
    elif kind == "not_regex":
        passed = not re.search(assertion.text, response, flags=re.IGNORECASE | re.DOTALL)
    return {
        "pass": bool(passed),
        "confidence": 1.0,
        "rationale": f"native {kind} assertion",
        "judge_provider": "native",
        "judge_model": "native",
        "latency_ms": 0,
        "error": "",
    }


def judge_assertion(
    case: EvalCase,
    candidate: Candidate,
    assertion: AssertionItem,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if candidate.error:
        return {
            "pass": False,
            "confidence": 1.0,
            "rationale": f"candidate generation failed: {candidate.error}",
            "judge_provider": "none",
            "judge_model": "none",
            "latency_ms": 0,
            "error": candidate.error,
        }

    native = score_native_assertion(assertion, candidate.response)
    if native is not None:
        return native

    if args.dry_run:
        return {
            "pass": False,
            "confidence": 0.0,
            "rationale": "dry run: judge not called",
            "judge_provider": args.judge_provider,
            "judge_model": args.judge_model,
            "latency_ms": 0,
            "error": "",
        }

    user_prompt = f"""User task:
{case.prompt}

Expected output summary, if provided:
{case.expected_output or "(none)"}

Known trap, if provided:
{case.trap or "(none)"}

Candidate answer:
{candidate.response}

Assertion to evaluate:
{assertion.text}
"""
    started = time.perf_counter()
    try:
        raw = retry_call(
            lambda: call_provider(
                args.judge_provider,
                args.judge_model,
                JUDGE_SYSTEM_PROMPT,
                user_prompt,
                args.judge_max_output_tokens,
                args.timeout_seconds,
            ),
            args.retry_attempts,
            args.retry_sleep_seconds,
        )
        data = extract_json_object(raw)
        passed = bool(data.get("pass"))
        confidence = float(data.get("confidence", 0))
        rationale = str(data.get("rationale", ""))
        error = ""
    except Exception as exc:  # noqa: BLE001 - persisted as artifact.
        passed = False
        confidence = 0.0
        rationale = "judge failed"
        error = str(exc)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {
        "pass": passed,
        "confidence": confidence,
        "rationale": rationale,
        "judge_provider": args.judge_provider,
        "judge_model": args.judge_model,
        "latency_ms": latency_ms,
        "error": error,
    }


def run_case(case: EvalCase, root: Path, args: argparse.Namespace) -> dict[str, Any]:
    blind_ids = {"with_skill": uuid.uuid4().hex[:10], "without_skill": uuid.uuid4().hex[:10]}
    variants = ["with_skill", "without_skill"]
    random.shuffle(variants)

    candidates = [
        generate_candidate(case, variant, root, args, blind_ids[variant])
        for variant in variants
    ]
    results = []
    for candidate in candidates:
        assertion_results = []
        for assertion in case.assertions:
            score = judge_assertion(case, candidate, assertion, args)
            assertion_results.append(
                {
                    "id": assertion.assertion_id,
                    "text": assertion.text,
                    "type": assertion.kind,
                    **score,
                }
            )
        passed = sum(1 for item in assertion_results if item["pass"])
        results.append(
            {
                "variant": candidate.variant,
                "blind_id": candidate.blind_id,
                "provider": candidate.provider,
                "model": candidate.model,
                "latency_ms": candidate.latency_ms,
                "response_sha256": sha256_text(candidate.response),
                "response": candidate.response,
                "error": candidate.error,
                "passed_assertions": passed,
                "total_assertions": len(assertion_results),
                "assertions": assertion_results,
            }
        )

    return {
        "skill": case.skill,
        "eval_id": case.eval_id,
        "name": case.name,
        "prompt": case.prompt,
        "expected_output": case.expected_output,
        "trap": case.trap,
        "assertion_count": len(case.assertions),
        "results": results,
    }


def summarize(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    by_skill: dict[str, dict[str, int]] = {}
    totals = {
        "with_skill_pass": 0,
        "without_skill_pass": 0,
        "with_skill_total": 0,
        "without_skill_total": 0,
        "generation_errors": 0,
        "judge_errors": 0,
    }
    for case in case_results:
        skill = case["skill"]
        bucket = by_skill.setdefault(
            skill,
            {
                "with_skill_pass": 0,
                "without_skill_pass": 0,
                "with_skill_total": 0,
                "without_skill_total": 0,
                "evals": 0,
            },
        )
        bucket["evals"] += 1
        for result in case["results"]:
            variant = result["variant"]
            pass_key = f"{variant}_pass"
            total_key = f"{variant}_total"
            bucket[pass_key] += int(result["passed_assertions"])
            bucket[total_key] += int(result["total_assertions"])
            totals[pass_key] += int(result["passed_assertions"])
            totals[total_key] += int(result["total_assertions"])
            if result["error"]:
                totals["generation_errors"] += 1
            for assertion in result["assertions"]:
                if assertion["error"]:
                    totals["judge_errors"] += 1
    return {"totals": totals, "skills": by_skill}


def pct(passed: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round(passed * 100 / total, 2)


def write_summary_markdown(path: Path, run: dict[str, Any]) -> None:
    summary = run["summary"]
    totals = summary["totals"]
    with_pct = pct(totals["with_skill_pass"], totals["with_skill_total"])
    without_pct = pct(totals["without_skill_pass"], totals["without_skill_total"])
    delta = round(with_pct - without_pct, 2)
    lines = [
        "# LLM Evaluation Run",
        "",
        f"- Run id: `{run['run_id']}`",
        f"- Created at: `{run['created_at']}`",
        f"- Generation provider/model: `{run['provider']}` / `{run['model']}`",
        f"- Judge provider/model: `{run['judge_provider']}` / `{run['judge_model']}`",
        f"- Context mode: `{run['context_mode']}`",
        f"- Dry run: `{run['dry_run']}`",
        "",
        "## Totals",
        "",
        "| Variant | Passed | Total | Score |",
        "| --- | ---: | ---: | ---: |",
        f"| with skill | {totals['with_skill_pass']} | {totals['with_skill_total']} | {with_pct}% |",
        f"| without skill | {totals['without_skill_pass']} | {totals['without_skill_total']} | {without_pct}% |",
        "",
        f"Delta: `{delta:+.2f}pp`",
        "",
        f"Generation errors: `{totals['generation_errors']}`",
        f"Judge errors: `{totals['judge_errors']}`",
        "",
        "## By Skill",
        "",
        "| Skill | Evals | With | Without | Delta |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for skill, row in sorted(summary["skills"].items()):
        skill_with = pct(row["with_skill_pass"], row["with_skill_total"])
        skill_without = pct(row["without_skill_pass"], row["without_skill_total"])
        skill_delta = round(skill_with - skill_without, 2)
        lines.append(f"| `{skill}` | {row['evals']} | {skill_with}% | {skill_without}% | {skill_delta:+.2f}pp |")
    lines.append("")
    lines.append("Raw prompts, responses, blind ids, and judge rationales are in `results.json`.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills", default="", help="Comma-separated skill names. Default: all skills with evals.")
    parser.add_argument("--eval-ids", default="", help="Comma-separated eval ids or names to run.")
    parser.add_argument("--limit", type=int, default=None, help="Limit selected eval cases after filtering.")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default=os.environ.get("EVAL_PROVIDER", "openai"))
    parser.add_argument("--model", default=os.environ.get("EVAL_MODEL", "gpt-4.1"))
    parser.add_argument(
        "--judge-provider",
        choices=["openai", "anthropic"],
        default=os.environ.get("EVAL_JUDGE_PROVIDER", os.environ.get("EVAL_PROVIDER", "openai")),
    )
    parser.add_argument("--judge-model", default=os.environ.get("EVAL_JUDGE_MODEL", "gpt-4.1"))
    parser.add_argument(
        "--context-mode",
        choices=["skill-only", "skill-with-references"],
        default=os.environ.get("EVAL_CONTEXT_MODE", "skill-only"),
    )
    parser.add_argument("--output-dir", default="artifacts/evaluations")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("EVAL_MAX_WORKERS", "1")))
    parser.add_argument("--max-output-tokens", type=int, default=int(os.environ.get("EVAL_MAX_OUTPUT_TOKENS", "4096")))
    parser.add_argument(
        "--judge-max-output-tokens",
        type=int,
        default=int(os.environ.get("EVAL_JUDGE_MAX_OUTPUT_TOKENS", "512")),
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("EVAL_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--retry-attempts", type=int, default=int(os.environ.get("EVAL_RETRY_ATTEMPTS", "3")))
    parser.add_argument(
        "--retry-sleep-seconds",
        type=float,
        default=float(os.environ.get("EVAL_RETRY_SLEEP_SECONDS", "2")),
    )
    parser.add_argument("--min-delta-pp", type=float, default=None, help="Fail if with-skill delta is below this.")
    parser.add_argument("--fail-on-errors", action="store_true", help="Fail when generation or judge errors occur.")
    parser.add_argument("--dry-run", action="store_true", help="Validate selection and artifact writing without API calls.")
    return parser.parse_args()


def split_csv(value: str) -> set[str] | None:
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def main() -> int:
    args = parse_args()
    root = repo_root()
    run_id = args.run_id or now_run_id()
    output_dir = root / args.output_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=False)

    selected_skills = split_csv(args.skills)
    selected_eval_ids = split_csv(args.eval_ids)
    cases = select_cases(load_eval_cases(root, selected_skills), selected_eval_ids, args.limit)
    if not cases:
        raise SystemExit("no eval cases selected")

    manifest = {
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, encoding="utf-8").strip(),
        "provider": args.provider,
        "model": args.model,
        "judge_provider": args.judge_provider,
        "judge_model": args.judge_model,
        "context_mode": args.context_mode,
        "dry_run": args.dry_run,
        "selected_skills": sorted({case.skill for case in cases}),
        "selected_eval_count": len(cases),
        "selected_assertion_count": sum(len(case.assertions) for case in cases),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Run id: {run_id}")
    print(f"Selected evals: {len(cases)}")
    print(f"Selected assertions: {manifest['selected_assertion_count']}")
    print(f"Output dir: {output_dir}")
    if args.dry_run:
        print("Dry run: API calls disabled")

    case_results: list[dict[str, Any]] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            futures = [executor.submit(run_case, case, root, args) for case in cases]
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                result = future.result()
                case_results.append(result)
                print(f"[{index}/{len(cases)}] {result['skill']}/{result['eval_id']} {result['name']}")
    except Exception:
        (output_dir / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise

    case_results.sort(key=lambda item: (item["skill"], item["eval_id"], item["name"]))
    run = {
        **manifest,
        "completed_at": dt.datetime.now(dt.UTC).isoformat(),
        "summary": summarize(case_results),
        "cases": case_results,
    }
    (output_dir / "results.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    write_summary_markdown(output_dir / "summary.md", run)

    totals = run["summary"]["totals"]
    with_pct = pct(totals["with_skill_pass"], totals["with_skill_total"])
    without_pct = pct(totals["without_skill_pass"], totals["without_skill_total"])
    delta = round(with_pct - without_pct, 2)
    print(f"with skill: {with_pct}%")
    print(f"without skill: {without_pct}%")
    print(f"delta: {delta:+.2f}pp")
    print(f"generation errors: {totals['generation_errors']}")
    print(f"judge errors: {totals['judge_errors']}")

    if args.fail_on_errors and (totals["generation_errors"] or totals["judge_errors"]):
        return 1
    if args.min_delta_pp is not None and delta < args.min_delta_pp:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
