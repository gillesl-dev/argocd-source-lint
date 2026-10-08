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

# ArgoCD's GetSyncWave parses this annotation with Go's strconv.Atoi
# (confirmed against the argo-cd source, not assumed): a signed
# integer literal, no whitespace, no decimal point. A value that
# doesn't match falls through the same unvalidated-annotation
# architecture as sync-options/hooks: the parse error isn't surfaced,
# the resource silently gets treated as wave 0.
_INTEGER_LITERAL = re.compile(r"[+-]?[0-9]+")


class MalformedSyncWaveRule(Rule):
    """A copy-paste mistake (e.g. `PreSync` pasted into `sync-wave`
    instead of `hook`) or a stray non-numeric value silently collapses
    that resource back to wave 0 instead of erroring, the same
    unvalidated-annotation-string architecture already confirmed for
    `unknown-sync-option`/`unknown-resource-hook`, this time on the
    numeric side rather than a closed set of keywords."""

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
