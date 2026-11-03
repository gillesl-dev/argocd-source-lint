from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from argocd_source_lint.coverage import covered_documents_for_application
from argocd_source_lint.git_context import external_path_sources
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule, external_source_finding

RULE_ID = "malformed-sync-wave"

_SYNC_WAVE_ANNOTATION = "argocd.argoproj.io/sync-wave"

# What Go's strconv.Atoi accepts, which Argo CD's GetSyncWave uses: a signed
# integer with no whitespace or decimal point.
_INTEGER_LITERAL = re.compile(r"[+-]?[0-9]+")


class MalformedSyncWaveRule(Rule):
    """Detect a `sync-wave` annotation that is not an integer.

    Argo CD's `GetSyncWave` parses it with `strconv.Atoi` and does not
    surface the error, so a value such as `PreSync` puts the resource in
    wave 0 instead of failing the sync.
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
            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            for doc in covered_documents_for_application(app, repo_root, local_origin):
                finding = _check_document(app, doc, severity)
                if finding is not None:
                    findings.append(finding)

        return findings


def _check_document(app: Application, doc: dict[str, Any], severity: Severity) -> Finding | None:
    annotations = (doc.get("metadata") or {}).get("annotations")
    if not isinstance(annotations, dict):
        return None

    raw_value = annotations.get(_SYNC_WAVE_ANNOTATION)
    if not isinstance(raw_value, str) or _INTEGER_LITERAL.fullmatch(raw_value):
        return None

    kind = doc.get("kind", "?")
    name = (doc.get("metadata") or {}).get("name", "?")
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=(
            f"`{_SYNC_WAVE_ANNOTATION}: {raw_value}` on `{kind}` `{name}` isn't a valid "
            "integer -- ArgoCD parses this value with Go's strconv.Atoi, so a value that "
            "fails to parse silently falls back to wave 0 instead of erroring."
        ),
        file=app.source_file,
    )
