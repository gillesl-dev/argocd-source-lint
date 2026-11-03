from __future__ import annotations

from pathlib import Path

from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "malformed-ignore-diff-pointer"


class MalformedIgnoreDiffPointerRule(Rule):
    """Detect a `jsonPointers` entry that does not start with `/`.

    RFC 6901 requires the leading slash. A pointer such as `spec.replicas`
    matches nothing, so Argo CD keeps diffing the field it was meant to
    ignore without reporting an error.
    """

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.WARNING)
        findings: list[Finding] = []

        for app in applications:
            for rule in app.ignore_differences:
                for pointer in rule.json_pointers:
                    if pointer and not pointer.startswith("/"):
                        findings.append(_finding(app, rule.kind, pointer, severity))

        return findings


def _finding(app: Application, kind: str, pointer: str, severity: Severity) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=(
            f"`ignoreDifferences` jsonPointers entry `{pointer}` (on `{kind}`) doesn't "
            "start with `/` -- RFC 6901 requires a leading slash for every segment "
            f"(e.g. `/spec/replicas`, not `{pointer}`), so this entry matches nothing "
            "and the field it was meant to ignore isn't actually ignored."
        ),
        file=app.source_file,
        line=app.ignore_differences_line,
    )
