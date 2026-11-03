from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import local_path_sources, revision_matches_checkout
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "revision-mismatch"


class RevisionMismatchRule(Rule):
    """Report a local source whose `targetRevision` differs from the checkout.

    Informational only: rules that read file contents already resolve such
    a source against a snapshot of its own revision (DESIGN.md
    "targetRevision drift"), so their results stay correct.
    """

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.INFO)
        findings: list[Finding] = []
        checked: dict[str, bool | None] = {}

        for app in applications:
            for source in local_path_sources(app, local_origin):
                revision = source.target_revision
                if revision not in checked:
                    checked[revision] = revision_matches_checkout(repo_root, revision)
                if checked[revision] is not False:
                    # None means unresolvable, which phantom-target and
                    # broken-values-ref already report.
                    continue

                findings.append(
                    Finding(
                        rule_id=RULE_ID,
                        severity=severity,
                        application=app.name,
                        message=(
                            f"targetRevision `{revision}` differs from the checked-out "
                            "revision — resolved against a snapshot of that revision "
                            "instead of the working tree."
                        ),
                        file=app.source_file,
                        line=source.line,
                    )
                )

        return findings
