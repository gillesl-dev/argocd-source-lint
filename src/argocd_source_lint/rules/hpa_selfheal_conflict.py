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

# The Kubernetes API group of a HorizontalPodAutoscaler's usual targets,
# used only when the HPA doesn't spell out `scaleTargetRef.apiVersion`
# itself (it's optional in practice even though the CrossVersionObjectReference
# schema marks it required). A kind outside this map is skipped rather than
# guessed at (e.g. a custom scalable CRD like Argo Rollouts' `Rollout`).
_KNOWN_TARGET_GROUPS = {
    "Deployment": "apps",
    "StatefulSet": "apps",
    "ReplicaSet": "apps",
    "ReplicationController": None,
}


class HpaSelfHealConflictRule(Rule):
    """A `HorizontalPodAutoscaler` and `selfHeal: true` both managing the
    same `spec.replicas` field is a well-known ArgoCD footgun (see
    DESIGN.md): `ignoreDifferences` alone only affects diff calculation,
    not the sync itself; the `RespectIgnoreDifferences` sync option is
    also required, and is easy to miss."""

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
            # Same heuristic scoping as missing-ignore-diff: without
            # selfHeal, ArgoCD never re-applies an out-of-Git-managed
            # field on its own, so the HPA and the desired state can't
            # actually fight through this app's sync loop.
            if not app.sync_policy_self_heal:
                continue

            for source in external_path_sources(app, local_origin):
                findings.append(external_source_finding(RULE_ID, app, source))

            respects_ignore_differences = _RESPECT_IGNORE_DIFFERENCES in app.sync_options

            for doc in covered_documents_for_application(app, repo_root, local_origin):
                findings.extend(_check_document(app, doc, severity, respects_ignore_differences))

        return findings


def _check_document(
    app: Application,
    doc: dict[str, Any],
    severity: Severity,
    respects_ignore_differences: bool,
) -> list[Finding]:
    scale_target = _hpa_scale_target(doc)
    if scale_target is None:
        return []
    target_kind, target_group, target_name = scale_target
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
