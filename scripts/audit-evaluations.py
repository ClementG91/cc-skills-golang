#!/usr/bin/env python3
"""Audit the repository evaluation data.

This script does not run LLM evaluations. It validates and audits the evaluation
artifacts already present in the repository, then reports which published
numbers are solid arithmetic and which should be treated as weaker evidence.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SUMMARY_RE = re.compile(
    r"^\| `(?P<skill>[^`]+)`\s+\|\s+(?P<version>[^|]+?)\s+\|\s+"
    r"(?P<assertions>\d+)\s+\|\s+(?P<with>[^|]+?)\s+\|\s+"
    r"(?P<without>[^|]+?)\s+\|\s+(?P<delta>[^|]+?)\s+\|\s+"
    r"(?P<uplift>[^|]+?)\s+\|\s+(?P<concern>[^|]*?)\s+\|$"
)

TOTAL_RE = re.compile(
    r"^\| \*\*Total \((?P<skills>\d+) skills\)\*\*\s+\|\s+\|\s+"
    r"\*\*(?P<assertions>\d+)\*\*\s+\|\s+\*\*(?P<with>\d+)%\*\*\s+\|\s+"
    r"\*\*(?P<without>\d+)%\*\*\s+\|\s+\*\*(?P<delta>[+-]?\d+)pp\*\*"
)

SECTION_RE = re.compile(r"^## `(?P<skill>[^`]+)` \N{EM DASH} (?P<version>.+)$")

OVERALL_RE = re.compile(
    r"^\|\s+\*\*Overall\*\*\s+\|\s+\*\*(?P<with_pass>\d+)/(?P<with_total>\d+) "
    r"\((?P<with_pct>\d+)%\)\*\*\s+\|\s+\*\*(?P<without_pass>\d+)/(?P<without_total>\d+) "
    r"\((?P<without_pct>\d+)%\)\*\*\s+\|\s+\*\*(?P<delta>[+-]\d+)pp\*\*\s+\|$"
)

ASSERTION_RE = re.compile(r"^\|\s+\d+\.\d+\s+\|")
PASS_RE = re.compile(r'<span class="g">\u2713</span>')
FAIL_RE = re.compile(r'<span class="r">\u2717</span>')


@dataclass(frozen=True)
class SummaryRow:
    skill: str
    version: str
    assertions: int
    with_pct: int
    without_pct: int
    delta: int
    uplift: str
    concern: str


@dataclass(frozen=True)
class DetailScore:
    skill: str
    version: str
    with_pass: int
    without_pass: int
    total: int
    with_pct: int
    without_pct: int
    delta: int
    grading: str
    both_pass: int
    both_fail: int
    with_only: int
    without_only: int


def repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
    )
    return Path(out.strip())


def clean_pct(value: str) -> int:
    value = value.replace("*", "").replace("%", "").strip()
    return int(value)


def clean_delta(value: str) -> int:
    value = value.replace("*", "").replace("pp", "").strip()
    return int(value)


def parse_summary(text: str) -> tuple[dict[str, SummaryRow], dict[str, int]]:
    rows: dict[str, SummaryRow] = {}
    total: dict[str, int] = {}

    for line in text.splitlines():
        match = SUMMARY_RE.match(line)
        if match:
            skill = match.group("skill")
            rows[skill] = SummaryRow(
                skill=skill,
                version=match.group("version").strip(),
                assertions=int(match.group("assertions")),
                with_pct=clean_pct(match.group("with")),
                without_pct=clean_pct(match.group("without")),
                delta=clean_delta(match.group("delta")),
                uplift=match.group("uplift").strip(),
                concern=re.sub(r"[* ]+", " ", match.group("concern")).strip(),
            )
            continue

        total_match = TOTAL_RE.match(line)
        if total_match:
            total = {key: int(value) for key, value in total_match.groupdict().items()}

    return rows, total


def parse_details(text: str) -> dict[str, DetailScore]:
    details: dict[str, DetailScore] = {}
    current_skill = ""
    current_version = ""
    current_grading = ""
    pending_score: dict[str, int] | None = None
    both_pass = both_fail = with_only = without_only = 0

    def flush() -> None:
        nonlocal pending_score, both_pass, both_fail, with_only, without_only
        if current_skill and pending_score:
            details[current_skill] = DetailScore(
                skill=current_skill,
                version=current_version,
                with_pass=pending_score["with_pass"],
                without_pass=pending_score["without_pass"],
                total=pending_score["total"],
                with_pct=pending_score["with_pct"],
                without_pct=pending_score["without_pct"],
                delta=pending_score["delta"],
                grading=current_grading,
                both_pass=both_pass,
                both_fail=both_fail,
                with_only=with_only,
                without_only=without_only,
            )
        pending_score = None
        both_pass = both_fail = with_only = without_only = 0

    for line in text.splitlines():
        section = SECTION_RE.match(line)
        if section:
            flush()
            current_skill = section.group("skill")
            current_version = section.group("version").strip()
            current_grading = ""
            continue

        if current_skill and "**Grading:**" in line:
            current_grading = line.split("**Grading:**", 1)[1].strip()

        overall = OVERALL_RE.match(line)
        if current_skill and overall:
            total = int(overall.group("with_total"))
            if total != int(overall.group("without_total")):
                raise AssertionError(f"{current_skill}: with/without totals differ")
            pending_score = {
                "with_pass": int(overall.group("with_pass")),
                "without_pass": int(overall.group("without_pass")),
                "total": total,
                "with_pct": int(overall.group("with_pct")),
                "without_pct": int(overall.group("without_pct")),
                "delta": int(overall.group("delta")),
            }
            continue

        if current_skill and ASSERTION_RE.match(line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4:
                continue
            with_cell = cells[-2]
            without_cell = cells[-1]
            with_passed = bool(PASS_RE.search(with_cell))
            without_passed = bool(PASS_RE.search(without_cell))
            with_failed = bool(FAIL_RE.search(with_cell))
            without_failed = bool(FAIL_RE.search(without_cell))
            if not (with_passed or with_failed) or not (without_passed or without_failed):
                continue

            if with_passed and without_passed:
                both_pass += 1
            elif with_failed and without_failed:
                both_fail += 1
            elif with_passed and without_failed:
                with_only += 1
            elif with_failed and without_passed:
                without_only += 1

    flush()
    return details


def audit_evals_json(root: Path) -> tuple[dict[str, int], list[str], list[str]]:
    counts: dict[str, int] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for path in sorted((root / "skills").glob("*/evals/evals.json")):
        skill = path.parents[1].name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSON: {exc}")
            continue

        if isinstance(data, dict) and isinstance(data.get("evals"), list):
            data = data["evals"]

        if not isinstance(data, list):
            errors.append(f"{path}: root must be a list or an object with evals")
            continue

        assertion_count = 0
        seen_ids: set[str] = set()
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                errors.append(f"{path}: eval {index} must be an object")
                continue
            for field in ("id", "assertions"):
                if field not in item:
                    errors.append(f"{path}: eval {index} missing {field}")
            for field in ("name", "description"):
                if field not in item:
                    warnings.append(f"{path}: eval {index} missing optional {field}")
            if "prompt" not in item and "task" not in item:
                errors.append(f"{path}: eval {index} missing prompt/task")
            assertions = item.get("assertions", [])
            if not isinstance(assertions, list) or not assertions:
                errors.append(f"{path}: eval {index} has no assertions")
                continue
            for assertion_index, assertion in enumerate(assertions, start=1):
                assertion_count += 1
                if isinstance(assertion, str):
                    assertion_id = f"{index}.{assertion_index}"
                    if not assertion.strip():
                        errors.append(f"{path}: assertion {assertion_id} is empty")
                    continue
                if not isinstance(assertion, dict):
                    errors.append(f"{path}: assertion {index}.{assertion_index} must be object or string")
                    continue
                assertion_id = str(assertion.get("id", ""))
                if not assertion_id:
                    warnings.append(f"{path}: eval {index} assertion missing id")
                if assertion_id in seen_ids:
                    warnings.append(f"{path}: duplicate assertion id {assertion_id}")
                seen_ids.add(assertion_id)
                if not assertion.get("text"):
                    errors.append(f"{path}: assertion {assertion_id} missing text")

        counts[skill] = assertion_count

    return counts, errors, warnings


def pct(numerator: int, denominator: int) -> int:
    return round(numerator * 100 / denominator)


def print_table(title: str, rows: list[tuple[object, ...]], headers: tuple[str, ...]) -> None:
    if not rows:
        return
    print(f"\n{title}")
    print("-" * len(title))
    print(" | ".join(headers))
    print(" | ".join("---" for _ in headers))
    for row in rows:
        print(" | ".join(str(cell) for cell in row))


def print_limited(title: str, rows: list[str], limit: int = 30) -> None:
    if not rows:
        return
    visible = rows[:limit]
    print_table(title, [(item,) for item in visible], ("Problem",))
    remaining = len(rows) - len(visible)
    if remaining > 0:
        print(f"... {remaining} more not shown")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = repo_root()
    evaluation_text = (root / "EVALUATIONS.md").read_text(encoding="utf-8")

    summary, summary_total = parse_summary(evaluation_text)
    details = parse_details(evaluation_text)
    json_counts, json_errors, json_warnings = audit_evals_json(root)

    skill_dirs = sorted(path.name for path in (root / "skills").iterdir() if path.is_dir())
    skills_with_json = set(json_counts)
    skills_in_summary = set(summary)
    skills_in_details = set(details)

    detail_total_assertions = sum(score.total for score in details.values())
    detail_with_pass = sum(score.with_pass for score in details.values())
    detail_without_pass = sum(score.without_pass for score in details.values())

    print("Evaluation audit")
    print("================")
    print(f"skills directories: {len(skill_dirs)}")
    print(f"skills with evals.json: {len(skills_with_json)}")
    print(f"skills in EVALUATIONS summary: {len(skills_in_summary)}")
    print(f"skills with detailed scores: {len(skills_in_details)}")

    if summary_total:
        print("\nPublished total vs recomputed detailed total")
        print(f"- published assertions: {summary_total['assertions']}")
        print(f"- recomputed assertions: {detail_total_assertions}")
        print(f"- published with-skill: {summary_total['with']}%")
        print(f"- recomputed with-skill: {pct(detail_with_pass, detail_total_assertions)}%")
        print(f"- published without-skill: {summary_total['without']}%")
        print(f"- recomputed without-skill: {pct(detail_without_pass, detail_total_assertions)}%")
        print(f"- published delta: {summary_total['delta']}pp")
        recomputed_delta = pct(detail_with_pass, detail_total_assertions) - pct(
            detail_without_pass, detail_total_assertions
        )
        print(f"- recomputed delta: {recomputed_delta:+d}pp")

    mismatches: list[str] = []
    for skill, row in sorted(summary.items()):
        detail = details.get(skill)
        if not detail:
            mismatches.append(f"{skill}: in summary but missing detailed score")
            continue
        if row.assertions != detail.total:
            mismatches.append(f"{skill}: summary assertions {row.assertions} != detail {detail.total}")
        if row.with_pct != detail.with_pct:
            mismatches.append(f"{skill}: summary with {row.with_pct}% != detail {detail.with_pct}%")
        if row.without_pct != detail.without_pct:
            mismatches.append(
                f"{skill}: summary without {row.without_pct}% != detail {detail.without_pct}%"
            )
        if row.delta != detail.delta:
            mismatches.append(f"{skill}: summary delta {row.delta}pp != detail {detail.delta}pp")

    for skill, assertion_count in sorted(json_counts.items()):
        summary_row = summary.get(skill)
        if summary_row and summary_row.assertions != assertion_count:
            mismatches.append(
                f"{skill}: evals.json assertions {assertion_count} != summary {summary_row.assertions}"
            )

    uncovered = [skill for skill in skill_dirs if skill not in skills_with_json]
    unevaluated = [skill for skill in skill_dirs if skill not in skills_in_summary]
    json_not_reported = sorted(skills_with_json - skills_in_summary)
    reported_without_json = sorted(skills_in_summary - skills_with_json)

    if uncovered:
        print_table(
            "Skills without evals.json",
            [(skill,) for skill in uncovered],
            ("Skill",),
        )

    if unevaluated:
        print_table(
            "Skills not included in EVALUATIONS.md summary",
            [(skill,) for skill in unevaluated],
            ("Skill",),
        )

    if json_not_reported:
        print_table(
            "evals.json exists but no published score",
            [(skill,) for skill in json_not_reported],
            ("Skill",),
        )

    if reported_without_json:
        print_table(
            "Published score but no evals.json",
            [(skill,) for skill in reported_without_json],
            ("Skill",),
        )

    low_signal = []
    for skill, detail in sorted(details.items()):
        both_pass_rate = detail.both_pass / detail.total if detail.total else 0
        if detail.delta <= 32 or detail.without_pct >= 65 or detail.with_pct <= 90:
            low_signal.append(
                (
                    skill,
                    f"{detail.with_pct}%",
                    f"{detail.without_pct}%",
                    f"{detail.delta:+d}pp",
                    f"{both_pass_rate:.0%}",
                    detail.grading or "?",
                )
            )

    print_table(
        "Low-confidence or low-uplift published scores",
        low_signal,
        ("Skill", "With", "Without", "Delta", "Both-pass assertions", "Grading"),
    )

    grading_rows = []
    for skill, detail in sorted(details.items()):
        grading_lower = detail.grading.lower()
        if "self" in grading_lower or "llm" in grading_lower or "biased" in grading_lower:
            grading_rows.append((skill, detail.grading))

    print_table(
        "Scores relying on LLM/self grading",
        grading_rows,
        ("Skill", "Grading"),
    )

    small_evals = []
    for skill, row in sorted(summary.items()):
        if row.assertions < 50:
            small_evals.append((skill, row.assertions, row.with_pct, row.without_pct, f"{row.delta:+d}pp"))

    print_table(
        "Published evals below 50 assertions",
        small_evals,
        ("Skill", "Assertions", "With", "Without", "Delta"),
    )

    print_limited("Mismatches", mismatches)

    print_limited("evals.json errors", json_errors)
    print_limited("evals.json schema warnings", json_warnings)

    if mismatches or json_errors:
        return 1

    print("\nPASS: evaluation artifacts are parseable and published arithmetic is internally consistent.")
    print(
        "CAUTION: this validates artifact consistency, not whether the LLM-as-judge scores "
        "are unbiased or reproducible."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
