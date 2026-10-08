from __future__ import annotations

from pathlib import Path
from typing import Any

from argocd_source_lint.argocd_versions import (
    APPLICATION_SYNC_OPTION_INTRODUCED,
    RESOURCE_SYNC_OPTION_INTRODUCED,
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

RULE_ID = "unknown-sync-option"

_RESOURCE_SYNC_OPTIONS_ANNOTATION = "argocd.argoproj.io/sync-options"


class UnknownSyncOptionRule(Rule):
    """A `syncOptions` entry, at the Application level
    (`spec.syncPolicy.syncOptions`) or per-resource (the
    `argocd.argoproj.io/sync-options` annotation), is matched by
    ArgoCD as a literal `Key=Value` string; there's no schema
    validation on it, so a wrong case (`respectIgnoreDifferences=true`)
    or a misspelled key (`PruneLatest=true`) is never rejected, just
    silently a no-op. The key portion is checked against the closed,
    documented set rather than the value: the exact accepted
    values/casing per key are less consistently documented, so
    validating only the key keeps this an objective check instead of a
    guess (same principle as `malformed-ignore-diff-pointer`).

    The closed set itself is also narrowed by the policy's optional
    `argocd_version`: a key can be a real, correctly-spelled ArgoCD sync
    option and still be a silent no-op if it wasn't introduced yet on the
    declared version (see argocd_versions.py)."""

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
            for option in app.sync_options:
                key = option.split("=", 1)[0]
                if not is_known(key, APPLICATION_SYNC_OPTION_INTRODUCED, declared):
                    findings.append(_app_level_finding(app, option, key, severity, declared))

            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            for doc in covered_documents_for_application(app, repo_root, local_origin):
                findings.extend(_check_resource_annotation(app, doc, severity, declared))

        return findings


def _check_resource_annotation(
    app: Application, doc: dict[str, Any], severity: Severity, declared: Version | None
) -> list[Finding]:
    annotations = (doc.get("metadata") or {}).get("annotations")
    if not isinstance(annotations, dict):
        return []

    raw_value = annotations.get(_RESOURCE_SYNC_OPTIONS_ANNOTATION)
    if not isinstance(raw_value, str):
        return []

    findings: list[Finding] = []
    for option in (entry.strip() for entry in raw_value.split(",")):
        key = option.split("=", 1)[0]
        if option and not is_known(key, RESOURCE_SYNC_OPTION_INTRODUCED, declared):
            findings.append(_resource_level_finding(app, doc, option, key, severity, declared))
    return findings


def _app_level_finding(
    app: Application,
    option: str,
    key: str,
    severity: Severity,
    declared: Version | None,
) -> Finding:
    introduced = APPLICATION_SYNC_OPTION_INTRODUCED.get(key)
    if introduced is not None and declared is not None:
        message = (
            f"`{option}` in `syncOptions` -- `{key}` is a real ArgoCD sync option, but it "
            f"only became valid at the Application level in ArgoCD {format_version(introduced)}. "
            f"Your configured `argocd_version: {format_version(declared)}` is older, so ArgoCD "
            "silently ignores it there."
        )
    else:
        message = (
            f"`{option}` in `syncOptions` doesn't match any of the "
            f"{len(APPLICATION_SYNC_OPTION_INTRODUCED)} ArgoCD-recognized sync option keys "
            "(case-sensitive, e.g. `RespectIgnoreDifferences`, `CreateNamespace`) -- "
            "likely a typo; ArgoCD silently ignores an unrecognized sync option "
            "instead of erroring, so this has no effect at all."
        )
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=app.source_file,
        line=app.sync_options_line,
    )


def _resource_level_finding(
    app: Application,
    doc: dict[str, Any],
    option: str,
    key: str,
    severity: Severity,
    declared: Version | None,
) -> Finding:
    kind = doc.get("kind", "?")
    name = (doc.get("metadata") or {}).get("name", "?")
    introduced = RESOURCE_SYNC_OPTION_INTRODUCED.get(key)
    if introduced is not None and declared is not None:
        message = (
            f"`{option}` in the `{_RESOURCE_SYNC_OPTIONS_ANNOTATION}` annotation on "
            f"`{kind}` `{name}` -- `{key}` is a real per-resource ArgoCD sync option, but it "
            f"only became valid in ArgoCD {format_version(introduced)}. Your configured "
            f"`argocd_version: {format_version(declared)}` is older, so ArgoCD silently "
            "ignores it there."
        )
    else:
        message = (
            f"`{option}` in the `{_RESOURCE_SYNC_OPTIONS_ANNOTATION}` annotation on "
            f"`{kind}` `{name}` doesn't match any of the "
            f"{len(RESOURCE_SYNC_OPTION_INTRODUCED)} ArgoCD-recognized per-resource "
            "sync option keys (case-sensitive, e.g. `Force`, `ServerSideApply`) -- "
            "likely a typo; ArgoCD silently ignores an unrecognized sync option "
            "instead of erroring, so this has no effect at all."
        )
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=app.source_file,
    )
