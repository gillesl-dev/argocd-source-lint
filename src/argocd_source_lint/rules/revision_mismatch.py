from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import local_path_sources, revision_matches_checkout
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "revision-mismatch"


class RevisionMismatchRule(Rule):
    """orphan-source, missing-ignore-diff and double-coverage resolve a
    local source's files against a snapshot of its own `targetRevision`
    when that differs from what's checked out (see
    `git_context.materialize_revision`, DESIGN.md "targetRevision drift"),
    so their result stays correct either way. This is purely an FYI
    that a source is pinned away from HEAD, for a human reading the
    report, not a correctness caveat."""

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
                    # True: matches HEAD, nothing to report. None: not
                    # resolvable at all; phantom-target/broken-values-ref
                    # already flag that shallow-clone case on their own.
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
