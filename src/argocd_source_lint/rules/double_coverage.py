from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from argocd_source_lint.coverage import covered_files_for_application
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "double-coverage"


class DoubleCoverageRule(Rule):
    """The mirror image of `orphan-source`: a file covered by more than
    one *different* Application's local sources at once, which ArgoCD
    would try to sync from two independent loops; the same manifest can
    flap between whatever each Application last applied. A file covered
    twice by two sources of the *same* Application isn't flagged: that's
    one sync loop, not a conflict."""

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        owners: dict[Path, set[str]] = defaultdict(set)

        for app in applications:
            for covered_file in covered_files_for_application(app, repo_root, local_origin):
                owners[covered_file].add(app.name)

        findings = [
            _finding(file_path, repo_root, app_names, severity)
            for file_path, app_names in owners.items()
            if len(app_names) > 1
        ]
        findings.sort(key=lambda f: f.file.as_posix())
        return findings


def _finding(file_path: Path, repo_root: Path, app_names: set[str], severity: Severity) -> Finding:
    try:
        relative = file_path.relative_to(repo_root)
    except ValueError:
        relative = file_path

    names = ", ".join(sorted(app_names))
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application="",
        message=(
            f"File covered by {len(app_names)} different Applications: {names} — "
            "ArgoCD would sync it from independent loops, risking it flapping "
            "between whichever Application last applied it."
        ),
        file=relative,
    )
