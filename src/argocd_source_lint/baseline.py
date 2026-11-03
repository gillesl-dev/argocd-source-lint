from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from argocd_source_lint.models import Finding

DEFAULT_BASELINE_FILENAME = ".argocd-lint-baseline.yaml"

_HEADER = (
    "# Findings accepted by `argocd-source-lint --write-baseline`.\n"
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
    """Load the baseline from the repo root. A missing file is an empty baseline."""
    baseline_file = repo_root / filename
    if not baseline_file.exists():
        return set()

    yaml = YAML(typ="safe")
    with baseline_file.open("r", encoding="utf-8") as f:
        raw_entries = yaml.load(f) or []
    if not isinstance(raw_entries, list) or not all(isinstance(e, dict) for e in raw_entries):
        raise ValueError("expected a list of entries, as written by --write-baseline")
    return {_key_from_entry(entry) for entry in raw_entries}


def write_baseline(
    repo_root: Path, findings: list[Finding], filename: str = DEFAULT_BASELINE_FILENAME
) -> Path:
    """Write every finding to the baseline file, replacing any previous baseline."""
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
    """Split `findings` into `(new, known)`, where `known` are in the baseline."""
    new: list[Finding] = []
    known: list[Finding] = []
    for finding in findings:
        (known if _key(finding) in baseline else new).append(finding)
    return new, known


def stale_baseline_entries(findings: list[Finding], baseline: set[FindingKey]) -> set[FindingKey]:
    """Baseline entries that match no current finding.

    Reported for cleanup only; they never affect the exit code (see DESIGN.md).
    """
    current = {_key(finding) for finding in findings}
    return baseline - current
