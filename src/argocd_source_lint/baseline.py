from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from argocd_source_lint.models import Finding

DEFAULT_BASELINE_FILENAME = ".argocd-lint-baseline.yaml"

_HEADER = (
    "# Findings accepted at a point in time — see DESIGN.md.\n"
    "# New findings still block CI; regenerate with `--write-baseline`.\n"
)

FindingKey = tuple[str, str, str, str]


def _key(finding: Finding) -> FindingKey:
    return (finding.rule_id, finding.file.as_posix(), finding.application, finding.message)


def _key_from_entry(entry: dict[str, Any]) -> FindingKey:
    return (
        entry.get("rule_id", ""),
        entry.get("file", ""),
        entry.get("application", ""),
        entry.get("message", ""),
    )


def load_baseline(repo_root: Path, filename: str = DEFAULT_BASELINE_FILENAME) -> set[FindingKey]:
    """Loads the baseline file from the repo root. Missing file means an
    empty baseline (nothing accepted yet), same convention as
    `policy.load_policy`."""
    baseline_file = repo_root / filename
    if not baseline_file.exists():
        return set()

    yaml = YAML(typ="safe")
    with baseline_file.open("r", encoding="utf-8") as f:
        raw_entries = yaml.load(f) or []
    return {_key_from_entry(entry) for entry in raw_entries}


def write_baseline(
    repo_root: Path, findings: list[Finding], filename: str = DEFAULT_BASELINE_FILENAME
) -> Path:
    """Writes every current finding to the baseline file, accepting them
    all at once: this is the onboarding path for an existing repo. Overwrites any
    previous baseline outright (it is meant to be regenerated, not
    hand-merged)."""
    entries = [
        {
            "rule_id": finding.rule_id,
            "file": finding.file.as_posix(),
            "application": finding.application,
            "message": finding.message,
        }
        for finding in findings
    ]

    baseline_file = repo_root / filename
    yaml = YAML()
    yaml.default_flow_style = False
    with baseline_file.open("w", encoding="utf-8") as f:
        f.write(_HEADER)
        yaml.dump(entries, f)
    return baseline_file


def split_by_baseline(
    findings: list[Finding], baseline: set[FindingKey]
) -> tuple[list[Finding], list[Finding]]:
    """Splits `findings` into (new, known): `known` are already accepted
    in the baseline and should be suppressed from the report and the exit
    code, `new` are everything else."""
    new: list[Finding] = []
    known: list[Finding] = []
    for finding in findings:
        (known if _key(finding) in baseline else new).append(finding)
    return new, known


def stale_baseline_entries(findings: list[Finding], baseline: set[FindingKey]) -> set[FindingKey]:
    """Baseline entries matching none of the current `findings`: the
    underlying issue was fixed, renamed, or the file/Application removed,
    so the entry no longer suppresses anything. Purely informational
    (never affects the exit code): a stale entry is dead weight, not a
    new risk, but left to grow forever it stops being something a
    reviewer can actually read (see DESIGN.md)."""
    current = {_key(finding) for finding in findings}
    return baseline - current
