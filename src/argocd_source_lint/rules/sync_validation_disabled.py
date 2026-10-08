from __future__ import annotations

from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "sync-validation-disabled"

_VALIDATE_FALSE = "Validate=false"


class SyncValidationDisabledRule(Rule):
    """`Validate=false` skips the API server's schema validation on
    sync (legitimate for a CRD with a known-broken OpenAPI schema), but
    also a common way to silence a validation error on a manifest that's
    actually wrong, and one that's easy to add once and forget. Purely
    static: the flag is right there in the Application manifest, no
    cluster access needed."""

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
