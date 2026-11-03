from __future__ import annotations

from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "sync-validation-disabled"

_VALIDATE_FALSE = "Validate=false"


class SyncValidationDisabledRule(Rule):
    """Report Applications that set the `Validate=false` sync option.

    It skips schema validation on every sync. That is legitimate for a CRD
    with a broken OpenAPI schema, but it also hides real manifest errors
    and is easy to forget once added.
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

        for app in applications:
            if _VALIDATE_FALSE not in app.sync_options:
                continue

            findings.append(
                Finding(
                    rule_id=RULE_ID,
                    severity=severity,
                    application=app.name,
                    message=(
                        "`Validate=false` sync option skips schema validation on every "
                        "sync — a manifest that fails validation is applied anyway "
                        "instead of blocking, silently."
                    ),
                    file=app.source_file,
                    line=app.sync_options_line,
                )
            )

        return findings
