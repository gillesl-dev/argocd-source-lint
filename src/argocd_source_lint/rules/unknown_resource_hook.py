from __future__ import annotations

from pathlib import Path
from typing import Any

from argocd_source_lint.argocd_versions import (
    HOOK_DELETE_POLICY_INTRODUCED,
    HOOK_VALUE_INTRODUCED,
    Version,
    format_version,
    is_known,
    parse_version,
)
from argocd_source_lint.coverage import covered_documents_for_application
from argocd_source_lint.git_context import external_path_sources
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule, external_source_finding

RULE_ID = "unknown-resource-hook"

_HOOK_ANNOTATION = "argocd.argoproj.io/hook"
_HOOK_DELETE_POLICY_ANNOTATION = "argocd.argoproj.io/hook-delete-policy"


class UnknownResourceHookRule(Rule):
    """`argocd.argoproj.io/hook`/`hook-delete-policy` are plain string
    annotations read by the controller at reconcile time; there's no
    schema enforcing their value against the closed set of recognized
    ones. A misspelled value (`presync`, `HookSuceeded`) most likely
    falls through silently to "not a hook"/"no delete policy", the same
    architecture already confirmed for `syncOptions`
    (`unknown-sync-option`) and `sync-wave`. Slightly lower confidence
    than `unknown-sync-option`: the official docs list the valid values
    but don't spell out the silent-fallthrough behavior for this
    specific pair the way they do for sync options (see DESIGN.md).

    The closed set itself is also narrowed by the policy's optional
    `argocd_version`, e.g. `PreDelete` only exists since ArgoCD 3.3.0
    (see argocd_versions.py)."""

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.WARNING)
        declared = parse_version(policy.argocd_version) if policy.argocd_version else None
        findings: list[Finding] = []

        for app in applications:
            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            for doc in covered_documents_for_application(app, repo_root, local_origin):
                findings.extend(_check_document(app, doc, severity, declared))

        return findings


def _check_document(
    app: Application, doc: dict[str, Any], severity: Severity, declared: Version | None
) -> list[Finding]:
    annotations = (doc.get("metadata") or {}).get("annotations")
    if not isinstance(annotations, dict):
        return []

    findings = _check_annotation(
        app, doc, annotations, _HOOK_ANNOTATION, HOOK_VALUE_INTRODUCED, severity, declared
    )
    findings += _check_annotation(
        app,
        doc,
        annotations,
        _HOOK_DELETE_POLICY_ANNOTATION,
        HOOK_DELETE_POLICY_INTRODUCED,
        severity,
        declared,
    )
    return findings


def _check_annotation(
    app: Application,
    doc: dict[str, Any],
    annotations: dict[str, Any],
    annotation_key: str,
    table: dict[str, tuple[int, ...]],
    severity: Severity,
    declared: Version | None,
) -> list[Finding]:
    raw_value = annotations.get(annotation_key)
    if not isinstance(raw_value, str):
        return []

    findings: list[Finding] = []
    for value in (entry.strip() for entry in raw_value.split(",")):
        if value and not is_known(value, table, declared):
            findings.append(_finding(app, doc, annotation_key, value, table, severity, declared))
    return findings


def _finding(
    app: Application,
    doc: dict[str, Any],
    annotation_key: str,
    value: str,
    table: dict[str, tuple[int, ...]],
    severity: Severity,
    declared: Version | None,
) -> Finding:
    kind = doc.get("kind", "?")
    name = (doc.get("metadata") or {}).get("name", "?")
    introduced = table.get(value)
    if introduced is not None and declared is not None:
        message = (
            f"`{annotation_key}: {value}` on `{kind}` `{name}` -- `{value}` is a real ArgoCD "
            f"value, but it only exists since ArgoCD {format_version(introduced)}. Your "
            f"configured `argocd_version: {format_version(declared)}` is older, so ArgoCD "
            "falls through silently (treated as a plain, non-hook resource) there."
        )
    else:
        message = (
            f"`{annotation_key}: {value}` on `{kind}` `{name}` doesn't match any "
            "ArgoCD-recognized value -- likely a typo; an unrecognized value falls "
            "through silently (the resource is treated as a plain, non-hook "
            "resource) instead of erroring."
        )
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=app.source_file,
    )
