from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from argocd_source_lint.coverage import covered_file_paths_for_application, renamed_by_kustomize
from argocd_source_lint.fsutil import load_yaml_documents
from argocd_source_lint.models import Application, Finding, Severity
from argocd_source_lint.policy import Policy, argocd_instance
from argocd_source_lint.rules.base import Rule

RULE_ID = "double-coverage"

# Only kinds the Kubernetes docs themselves state are not namespaced. Any other
# kind, CRDs included, is treated as namespaced: a cluster-scoped custom
# resource deployed from two namespaces is then missed, never falsely flagged.
CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Namespace",
        "Node",
        "PersistentVolume",
        "StorageClass",
        "ClusterRole",
        "ClusterRoleBinding",
        "PriorityClass",
        "CustomResourceDefinition",
    }
)

_ResourceKey = tuple[tuple[str, str], str | None, str, str | None]


class DoubleCoverageRule(Rule):
    """Detect a resource deployed to the same place by two different Applications.

    Argo CD syncs it from two independent loops, so it flaps between
    whatever each Application last applied. The same file sent to different
    clusters or namespaces is two resources, and two sources of one
    Application share a single sync loop, so neither is flagged.
    """

    rule_id = RULE_ID

    def check(
        self,
        applications: list[Application],
        repo_root: Path,
        policy: Policy,
        local_origin: str | None,
    ) -> list[Finding]:
        severity = policy.rules.get(RULE_ID, Severity.ERROR)
        owners: dict[Path, dict[_ResourceKey, set[str]]] = defaultdict(lambda: defaultdict(set))

        for app in applications:
            cluster = _cluster(app)
            if cluster is None:
                continue
            # The same server URL seen from two Argo CD instances is two clusters.
            instance = argocd_instance(app.source_file, policy.argocd_instances)
            cluster = (f"{instance}:{cluster[0]}", cluster[1])
            renamed = renamed_by_kustomize(app, repo_root, local_origin)
            for identity, readable in covered_file_paths_for_application(
                app, repo_root, local_origin
            ):
                if identity in renamed:
                    continue
                for doc in load_yaml_documents(readable):
                    key = _resource_key(doc, cluster, app.destination_namespace)
                    if key is not None:
                        owners[identity][key].add(app.name)

        findings = []
        for file_path, by_resource in owners.items():
            conflicting = set().union(*(names for names in by_resource.values() if len(names) > 1))
            if conflicting:
                findings.append(_finding(file_path, repo_root, conflicting, severity))
        findings.sort(key=lambda f: f.file.as_posix())
        return findings


def _determined(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "{{" in value:
        return None
    return value


def _cluster(app: Application) -> tuple[str, str] | None:
    """Return the destination cluster as `("server", url)` or `("name", name)`.

    The two forms are never compared, so a server/name pair is not flagged.
    """
    if server := _determined(app.destination_server):
        return ("server", server)
    if name := _determined(app.destination_name):
        return ("name", name)
    return None


def _resource_key(
    doc: dict[str, Any], cluster: tuple[str, str], destination_namespace: str | None
) -> _ResourceKey | None:
    kind = doc.get("kind")
    if not isinstance(kind, str):
        return None
    metadata = doc.get("metadata") or {}
    name = metadata.get("name") if isinstance(metadata.get("name"), str) else None
    if kind in CLUSTER_SCOPED_KINDS:
        return (cluster, None, kind, name)
    # Argo CD applies the destination namespace only when the resource
    # does not set metadata.namespace itself.
    own_namespace = metadata.get("namespace")
    namespace = _determined(own_namespace if own_namespace else destination_namespace)
    if namespace is None:
        return None
    return (cluster, namespace, kind, name)


def _finding(file_path: Path, repo_root: Path, app_names: set[str], severity: Severity) -> Finding:
    try:
        relative = file_path.relative_to(repo_root)
    except ValueError:
        relative = file_path

    names = ", ".join(sorted(app_names))
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application="",
        message=(
            f"File covered by {len(app_names)} different Applications: {names} — "
            "ArgoCD would sync it from independent loops, risking it flapping "
            "between whichever Application last applied it."
        ),
        file=relative,
    )
