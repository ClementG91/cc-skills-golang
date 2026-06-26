#!/usr/bin/env python3
"""Validate stdlib-first governance guardrails.

This is a deterministic runner. It does not claim to reproduce the repository's
LLM-as-judge evaluations; it checks the structural guarantees introduced by the
stdlib-first governance branch.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SAMBER_SKILLS = ("do", "hot", "lo", "mo", "oops", "ro", "slog")

GOVERNANCE_REQUIRED = (
    "Architectural Sign-Off Required",
    "RFC Template",
    "Execution Lock",
    "Stdlib-First Dependency Gate",
    "Industry Standard Check",
    "multi-tenancy",
    "database access",
    "GOMEMLIMIT",
    "govulncheck",
    "human sign-off",
)


def repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
    )
    return Path(out.strip())


def git_show(root: Path, ref: str, path: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "show", f"{ref}:{path}"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return ""


def frontmatter(text: str, path: Path) -> str:
    if not text.startswith("---"):
        raise AssertionError(f"{path}: missing opening frontmatter")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise AssertionError(f"{path}: missing closing frontmatter")

    return parts[1]


def field_value(fm: str, field: str) -> str:
    prefix = f"{field}:"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def check_all_skill_frontmatter(root: Path) -> list[str]:
    errors: list[str] = []

    for path in sorted((root / "skills").glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        try:
            fm = frontmatter(text, path)
        except AssertionError as exc:
            errors.append(str(exc))
            continue

        name = field_value(fm, "name")
        description = field_value(fm, "description")

        if name != path.parent.name:
            errors.append(f"{path}: name {name!r} does not match directory")
        if not description:
            errors.append(f"{path}: missing description")
        elif len(description) > 1024:
            errors.append(f"{path}: description too long ({len(description)})")
        elif "Golang" not in description:
            errors.append(f"{path}: description must contain Golang")

        for required in (
            "user-invocable:",
            "license:",
            "compatibility:",
            "metadata:",
            "allowed-tools:",
        ):
            if required not in fm:
                errors.append(f"{path}: missing {required}")

    return errors


def check_evals_json(root: Path) -> list[str]:
    errors: list[str] = []

    for path in sorted((root / "skills").glob("*/evals/evals.json")):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: invalid JSON: {exc}")

    return errors


def samber_metrics(root: Path, ref: str | None = None) -> tuple[int, int]:
    broad_triggers = 0
    adoption_gates = 0

    for skill in SAMBER_SKILLS:
        rel = f"skills/golang-samber-{skill}/SKILL.md"
        text = git_show(root, ref, rel) if ref else (root / rel).read_text(encoding="utf-8")
        if not text:
            continue

        fm = frontmatter(text, root / rel)
        if "when using or adopting" in fm.lower():
            broad_triggers += 1
        if "## Adoption Gate" in text:
            adoption_gates += 1

    return broad_triggers, adoption_gates


def check_governance_branch(root: Path) -> list[str]:
    errors: list[str] = []

    governance = root / "skills/golang-architecture-governance/SKILL.md"
    if not governance.exists():
        errors.append(f"{governance}: missing governance skill")
        return errors

    governance_text = governance.read_text(encoding="utf-8")
    for phrase in GOVERNANCE_REQUIRED:
        if phrase not in governance_text:
            errors.append(f"{governance}: missing required phrase {phrase!r}")

    for skill in SAMBER_SKILLS:
        path = root / f"skills/golang-samber-{skill}/SKILL.md"
        text = path.read_text(encoding="utf-8")
        fm = frontmatter(text, path)

        if "## Adoption Gate" not in text:
            errors.append(f"{path}: missing Adoption Gate")
        if "already imports" not in fm:
            errors.append(f"{path}: trigger should require existing imports")
        if "explicitly asks" not in fm:
            errors.append(f"{path}: trigger should include explicit user request")
        if "when using or adopting" in fm.lower():
            errors.append(f"{path}: broad 'using or adopting' trigger still present")

    popular = (root / "skills/golang-popular-libraries/SKILL.md").read_text(encoding="utf-8")
    if "## Dependency Adoption Gate" not in popular:
        errors.append("golang-popular-libraries: missing Dependency Adoption Gate")

    design = (root / "skills/golang-design-patterns/SKILL.md").read_text(encoding="utf-8")
    if "produce a short RFC" not in design or "before writing application code" not in design:
        errors.append("golang-design-patterns: missing RFC-before-code guidance")

    dependency_injection = (root / "skills/golang-dependency-injection/SKILL.md").read_text(
        encoding="utf-8"
    )
    old_any_size_row = "| **Project size** | Small (< 10 services) | Medium-Large | Large | Any size |"
    if old_any_size_row in dependency_injection:
        errors.append("golang-dependency-injection: samber/do still marked Any size")

    readme = (root / "README.md").read_text(encoding="utf-8")
    if "not intended to override the repository's stdlib-first guidance" not in readme:
        errors.append("README: missing samber/* stdlib-first clarification")

    return errors


def print_metric_report(root: Path, base_ref: str | None) -> None:
    current_broad, current_gates = samber_metrics(root)
    print("Governance metrics")
    print(f"- current broad samber triggers: {current_broad}")
    print(f"- current samber adoption gates: {current_gates}")

    if base_ref:
        base_broad, base_gates = samber_metrics(root, base_ref)
        print(f"- broad samber triggers: {base_broad} -> {current_broad}")
        print(f"- samber adoption gates: {base_gates} -> {current_gates}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref used for before/after metrics. Use '' to disable.",
    )
    args = parser.parse_args()

    root = repo_root()
    base_ref = args.base_ref or None
    errors: list[str] = []

    errors.extend(check_all_skill_frontmatter(root))
    errors.extend(check_evals_json(root))
    errors.extend(check_governance_branch(root))

    print_metric_report(root, base_ref)

    if errors:
        print("\nFAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
