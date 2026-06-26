# Evaluation Harness

This repository now has two evaluation layers:

1. `scripts/audit-evaluations.py` audits the published evaluation artifacts already committed to the repository.
2. `scripts/run-llm-evaluations.py` runs fresh with-skill versus without-skill LLM evaluations and produces raw artifacts.

The audit is deterministic. The LLM harness is the source of new benchmark data.

## What The Harness Measures

For each selected eval case, the harness:

1. Loads the user prompt and assertions from `skills/<skill>/evals/evals.json`.
2. Calls the generation model once without the skill context.
3. Calls the same generation model once with the target skill context.
4. Scores each assertion with a separate blind judge call.
5. Writes raw prompts, responses, blind candidate ids, judge rationales, and summary scores to `artifacts/evaluations/<run-id>/`.

The judge does not receive the candidate variant label. It only sees the task, the candidate answer, optional expected/trap text, and one assertion.

## Why GitHub Actions Instead Of Harness.io

Harness.io has a free plan, but a reusable fork cannot run Harness pipelines without a Harness account, project setup, and provider-specific secrets. GitHub Actions is free for public repositories, already available on GitHub forks, and can store the exact same artifacts.

If a team already uses Harness.io, wire the same commands from this document into a Harness pipeline stage. The benchmark source of truth remains the checked-in Python harness and the generated artifacts, not the CI vendor.

## Required Secrets

For OpenAI runs, configure:

- `OPENAI_API_KEY`

For Anthropic runs, configure:

- `ANTHROPIC_API_KEY`

For Gemini runs, configure:

- `GEMINI_API_KEY`

The workflow supports using one provider for generation and another provider for judging.

## Local Smoke Test

This validates parsing and artifact generation without model calls:

```bash
python scripts/run-llm-evaluations.py \
  --dry-run \
  --skills golang-samber-lo \
  --limit 1
```

## Real Local Run

Example OpenAI run:

```bash
export OPENAI_API_KEY=...
python scripts/run-llm-evaluations.py \
  --provider openai \
  --model gpt-4.1 \
  --judge-provider openai \
  --judge-model gpt-4.1 \
  --skills golang-security,golang-samber-lo \
  --context-mode skill-only \
  --max-workers 1 \
  --fail-on-errors
```

Example Anthropic run:

```bash
export ANTHROPIC_API_KEY=...
python scripts/run-llm-evaluations.py \
  --provider anthropic \
  --model claude-opus-4-1-20250805 \
  --judge-provider anthropic \
  --judge-model claude-opus-4-1-20250805 \
  --skills golang-security \
  --context-mode skill-only \
  --fail-on-errors
```

Example Gemini free-tier smoke run:

```bash
export GEMINI_API_KEY=...
python scripts/run-llm-evaluations.py \
  --provider gemini \
  --model gemini-2.5-flash \
  --judge-provider gemini \
  --judge-model gemini-2.5-flash \
  --skills golang-samber-lo \
  --limit 2 \
  --context-mode skill-only \
  --fail-on-errors
```

Do not publish Gemini free-tier results as Codex or Claude results. Use them to prove the harness, CI, artifacts, and blind judging flow works at low or zero cost.

## CI Usage

Use the `LLM Evaluations` workflow manually from GitHub Actions.

Recommended first run:

- `dry_run`: `true`
- `skills`: `golang-samber-lo`
- `limit`: `1`

Recommended real smoke run:

- `dry_run`: `false`
- `provider`: `gemini`
- `model`: `gemini-2.5-flash`
- `judge_provider`: `gemini`
- `judge_model`: `gemini-2.5-flash`
- `skills`: `golang-samber-lo`
- `limit`: `2`
- `context_mode`: `skill-only`
- `max_workers`: `1`

Run full suites only when cost is acceptable. The harness writes full transcripts and judge rationales as workflow artifacts.

## GitHub Secret Setup

Never commit API keys. Store them as repository secrets.

With the GitHub CLI:

```bash
gh secret set GEMINI_API_KEY --repo ClementG91/cc-skills-golang
```

Paste the key into the hidden prompt when `gh` asks for it.

Or use the GitHub web UI:

1. Open the fork on GitHub.
2. Go to `Settings` -> `Secrets and variables` -> `Actions`.
3. Select `New repository secret`.
4. Name it `GEMINI_API_KEY`.
5. Paste the key and save.

If a key was pasted into chat, rotate it from the provider console before using it for longer-lived automation.

## Context Modes

`skill-only` injects only `SKILL.md`. This is stricter and closer to a skill trigger.

`skill-with-references` injects `SKILL.md` plus every Markdown reference file under the skill directory. This is useful for testing full skill knowledge, but it can overstate what an agent would read lazily in a normal session.

Use the same mode across baseline comparisons and report it with every published score.

## Validity Rules

Treat a score as publishable only when:

- The run artifact is preserved.
- The generation and judge model names are recorded.
- The git commit SHA is recorded.
- The judge is blind to with-skill versus without-skill labels.
- Provider and judge errors are zero, or explicitly explained.
- The selected eval set is declared.
- The context mode is declared.
- The same prompt set is used for both variants.

Do not compare scores from different model versions, context modes, or eval subsets as if they were the same benchmark.
