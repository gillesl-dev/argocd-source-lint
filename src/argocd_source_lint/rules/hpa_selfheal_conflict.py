from __future__ import annotations

from pathlib import Path
from typing import Any

from argocd_source_lint.coverage import covered_documents_for_application
from argocd_source_lint.git_context import external_path_sources
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule, external_source_finding

RULE_ID = "hpa-selfheal-conflict"

_RESPECT_IGNORE_DIFFERENCES = "RespectIgnoreDifferences=true"

# API group of the usual HPA targets, used when `scaleTargetRef.apiVersion`
# is omitted, which is common even though the schema marks it required.
# Other kinds, such as a custom scalable CRD, are skipped.
_KNOWN_TARGET_GROUPS = {
    "Deployment": "apps",
    "StatefulSet": "apps",
    "ReplicaSet": "apps",
    "ReplicationController": None,
}


class HpaSelfHealConflictRule(Rule):
    """Detect an HPA target whose `spec.replicas` selfHeal would reset.

    With selfHeal on, Argo CD reverts the replica count the HPA set unless
    `ignoreDifferences` covers the target. That alone only affects the diff:
    the `RespectIgnoreDifferences=true` sync option is also needed for the
    sync to honor it (see DESIGN.md).
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
            # Without selfHeal, Argo CD does not re-apply drifted fields on
            # its own, so the HPA has nothing to fight with.
            if not app.sync_policy_self_heal:
                continue

            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            respects_ignore_differences = _RESPECT_IGNORE_DIFFERENCES in app.sync_options

            documents = list(covered_documents_for_application(app, repo_root, local_origin))
            without_replicas = _workloads_without_replicas(documents)
            for doc in documents:
                findings.extend(
                    _check_document(
                        app, doc, severity, respects_ignore_differences, without_replicas
                    )
                )

        return findings


def _check_document(
    app: Application,
    doc: dict[str, Any],
    severity: Severity,
    respects_ignore_differences: bool,
    without_replicas: set[tuple[str, str]],
) -> list[Finding]:
    scale_target = _hpa_scale_target(doc)
    if scale_target is None:
        return []
    target_kind, target_group, target_name = scale_target
    if (target_kind, target_name) in without_replicas:
        # Git sets no replica count, so self-heal has nothing to restore. This is
        # the setup the Kubernetes HPA docs recommend.
        return []
    namespace = (doc.get("metadata") or {}).get("namespace")

    if not _has_matching_ignore_diff(app, target_kind, target_group, target_name, namespace):
        return [
            Finding(
                rule_id=RULE_ID,
                severity=severity,
                application=app.name,
                message=(
                    f"selfHeal active and a HorizontalPodAutoscaler targets `{target_kind}` "
                    f"`{target_name}`, but no `ignoreDifferences` covers it — ArgoCD will "
                    "reset the HPA-managed replica count on every sync."
                ),
                file=app.source_file,
                line=app.self_heal_line,
            )
        ]

    if not respects_ignore_differences:
        return [
            Finding(
                rule_id=RULE_ID,
                severity=severity,
                application=app.name,
                message=(
                    f"`ignoreDifferences` covers `{target_kind}` `{target_name}` (HPA-managed "
                    "replicas), but the `RespectIgnoreDifferences` sync option is not set — "
                    "`ignoreDifferences` alone only affects the diff, not the sync itself, so "
                    "the replica count will still be reset."
                ),
                file=app.source_file,
                line=app.self_heal_line,
            )
        ]

    return []


def _workloads_without_replicas(documents: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (doc["kind"], str((doc.get("metadata") or {}).get("name")))
        for doc in documents
        if doc.get("kind") in _KNOWN_TARGET_GROUPS and "replicas" not in (doc.get("spec") or {})
    }


def _hpa_scale_target(doc: dict[str, Any]) -> tuple[str, str | None, str] | None:
    api_version = str(doc.get("apiVersion", ""))
    if doc.get("kind") != "HorizontalPodAutoscaler" or not api_version.startswith("autoscaling/"):
        return None

    scale_target_ref = (doc.get("spec") or {}).get("scaleTargetRef") or {}
    target_name = scale_target_ref.get("name")
    target_kind = scale_target_ref.get("kind")
    if not target_name or not target_kind:
        return None

    target_api_version = scale_target_ref.get("apiVersion")
    if target_api_version:
        target_group = target_api_version.split("/", 1)[0] if "/" in target_api_version else None
    elif target_kind in _KNOWN_TARGET_GROUPS:
        target_group = _KNOWN_TARGET_GROUPS[target_kind]
    else:
        return None

    return target_kind, target_group, target_name


def _has_matching_ignore_diff(
    app: Application,
    kind: str,
    group: str | None,
    name: str,
    namespace: str | None,
) -> bool:
    for rule in app.ignore_differences:
        if rule.kind not in (kind, "*"):
            continue
        if (rule.group or None) != (group or None):
            continue
        if rule.namespace is not None and rule.namespace != namespace:
            continue
        if rule.name is None or rule.name == name:
            return True
    return False
