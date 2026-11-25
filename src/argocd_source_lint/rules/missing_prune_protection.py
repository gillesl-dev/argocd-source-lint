from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from argocd_source_lint.argocd_versions import (
    APPLICATION_SYNC_OPTION_INTRODUCED,
    Version,
    format_version,
    introduced_at_or_before,
    parse_version,
)
from argocd_source_lint.coverage import (
    _resolved_source_root,
    covered_files_for_source,
    find_kustomization_file,
    is_opaque_tool_directory,
)
from argocd_source_lint.fsutil import load_yaml_documents
from argocd_source_lint.git_context import local_path_sources
from argocd_source_lint.models import Application, Finding, Severity, Source
from argocd_source_lint.policy import Policy
from argocd_source_lint.rules.base import Rule

RULE_ID = "missing-prune-protection"

_SYNC_OPTIONS_ANNOTATION = "argocd.argoproj.io/sync-options"
# The values of the `Prune` option that stop a prune: `pruneObject` skips the
# resource, or the sync waits for a confirmation (gitops-engine sync_context.go).
_PROTECTING_VALUES = ("false", "confirm")

# Kustomization fields that can add, change or remove a resource's annotations
# in a way this tool does not render. A source using one is not checked.
_KUSTOMIZE_TRANSFORMS = (
    "patches",
    "patchesStrategicMerge",
    "patchesJson6902",
    "transformers",
    "components",
    "replacements",
    "helmCharts",
    "generators",
)

_CONSEQUENCES = {
    "Namespace": "everything in it, including resources that are not in Git",
    "CustomResourceDefinition": "every custom resource of that kind in the cluster",
}


class MissingPruneProtectionRule(Rule):
    """Detect a Namespace or CRD that automated prune would delete.

    Under `automated.prune`, Argo CD deletes a resource that left the rendered
    manifests unless the live object carries `Prune=false` or `Prune=confirm`
    in its sync-options annotation. Deleting a Namespace or a CRD also deletes
    what it holds, including objects that are not in Git. `Delete=false`,
    `PruneLast` and `helm.sh/resource-policy: keep` do not stop a prune (see
    DESIGN.md).
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
        declared = parse_version(policy.argocd_version) if policy.argocd_version else None
        app_level_works = declared is None or introduced_at_or_before(
            APPLICATION_SYNC_OPTION_INTRODUCED["Prune"], declared
        )
        findings: list[Finding] = []

        for app in applications:
            if not app.sync_policy_prune:
                continue

            # External sources are not reported here: orphan-source and
            # phantom-target already report each one, and a third `info` per
            # pruning Application only adds noise (DESIGN.md).
            # `SyncOptions.GetOptionValue`: the first `Prune=` entry, untrimmed.
            app_level = next(iter(_prune_values(app.sync_options)), None)
            seen: set[tuple[Path, int | None]] = set()
            for source in local_path_sources(app, local_origin):
                for identity, doc, annotation in _deployed_documents(repo_root, source):
                    kind = _risky_kind(doc)
                    line = _document_line(doc)
                    if kind is None or (identity, line) in seen:
                        continue
                    seen.add((identity, line))
                    # A `Prune` option on the resource wins over the Application's.
                    resource_level = _resource_prune_value(_split(annotation))
                    if resource_level is None and app_level in _PROTECTING_VALUES:
                        if app_level_works:
                            continue
                        message_end = _app_level_too_old(declared)
                    elif resource_level in _PROTECTING_VALUES:
                        continue
                    else:
                        message_end = ""
                    findings.append(_finding(app, kind, doc, identity, line, severity, message_end))

        return findings


def _split(annotation: Any) -> list[str]:
    # Argo CD splits on commas and trims each item (resource/annotations.go).
    if not isinstance(annotation, str):
        return []
    return [item.strip() for item in annotation.split(",") if item.strip()]


def _prune_values(options: list[str]) -> list[str]:
    return [option.removeprefix("Prune=") for option in options if option.startswith("Prune=")]


def _resource_prune_value(options: list[str]) -> str | None:
    """The resource's `Prune` option, protecting if any of its values protects.

    Argo CD 3.3 checks whether `Prune=false` or `Prune=confirm` is in the list,
    and 3.4 and later read the first `Prune=` value. They only differ with two
    contradictory options, read here as protecting so as not to warn wrongly.
    """
    values = _prune_values(options)
    protecting = [value for value in values if value in _PROTECTING_VALUES]
    return next(iter(protecting or values), None)


def _risky_kind(doc: dict[str, Any]) -> str | None:
    kind = doc.get("kind")
    api_version = str(doc.get("apiVersion", ""))
    if kind == "Namespace" and api_version == "v1":
        return kind
    if kind == "CustomResourceDefinition" and api_version.startswith("apiextensions.k8s.io/"):
        return kind
    return None


def _document_line(doc: Any) -> int | None:
    lc = getattr(doc, "lc", None)
    return None if lc is None or lc.line is None else lc.line + 1


def _annotation(doc: dict[str, Any]) -> Any:
    annotations = (doc.get("metadata") or {}).get("annotations")
    return annotations.get(_SYNC_OPTIONS_ANNOTATION) if isinstance(annotations, dict) else None


def _deployed_documents(
    repo_root: Path, source: Source
) -> Iterator[tuple[Path, dict[str, Any], Any]]:
    """`(repo-relative file, document, sync-options annotation)` for each
    document a source deploys as written in Git.

    Sources rendered by a tool this one does not run (Helm, plugins, Kustomize
    transforms) yield nothing.
    """
    resolved = _resolved_source_root(repo_root, source)
    if resolved is None or source.explicit_type in ("helm", "plugin"):
        return
    if source.kustomize_transforms:
        return
    base_root, source_dir = resolved

    files: list[tuple[Path, Any]]
    kustomization = find_kustomization_file(source_dir)
    if source.explicit_type == "kustomize" or (
        source.explicit_type != "directory" and kustomization is not None
    ):
        if kustomization is None:
            return
        listed = _kustomize_resources(source_dir, base_root, set())
        if listed is None:
            return
        files = listed
    elif source.explicit_type != "directory" and is_opaque_tool_directory(source_dir):
        return
    else:
        files = [(path, None) for path in covered_files_for_source(source_dir, source, base_root)]

    for path, override in files:
        identity = path.resolve().relative_to(base_root)
        for doc in load_yaml_documents(path):
            annotation = override if override is not None else _annotation(doc)
            yield identity, doc, annotation


def _kustomize_resources(
    directory: Path, base_root: Path, seen: set[Path]
) -> list[tuple[Path, str | None]] | None:
    """Files a kustomization lists under `resources` (and `bases`), followed
    into nested kustomizations, each with the sync-options value its
    `commonAnnotations` set, if any. None when this kustomization transforms
    its resources in another way.

    Kustomize sets a `commonAnnotations` key on every resource of its
    subtree, replacing the resource's own value; the outermost one is applied
    last.
    """
    kustomization = find_kustomization_file(directory)
    resolved_dir = directory.resolve()
    if kustomization is None or resolved_dir in seen:
        return []
    seen.add(resolved_dir)

    docs = load_yaml_documents(kustomization)
    doc = docs[0] if docs else {}
    if any(doc.get(field) for field in _KUSTOMIZE_TRANSFORMS):
        return None
    common = doc.get("commonAnnotations")
    own = (
        str(common[_SYNC_OPTIONS_ANNOTATION])
        if isinstance(common, dict) and common.get(_SYNC_OPTIONS_ANNOTATION) is not None
        else None
    )

    listed: list[tuple[Path, str | None]] = []
    for key in ("resources", "bases"):
        entries = doc.get(key)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, str):
                continue
            # A remote base is outside this repository, like an external source.
            target = (directory / entry).resolve()
            if not target.is_relative_to(base_root):
                continue
            if target.is_dir():
                # A nested kustomization's transforms only reach its own resources.
                listed += _kustomize_resources(target, base_root, seen) or []
            elif target.is_file():
                listed.append((target, None))

    return [(path, own if own is not None else inner) for path, inner in listed]


def _app_level_too_old(declared: Version | None) -> str:
    introduced = APPLICATION_SYNC_OPTION_INTRODUCED["Prune"]
    shown = format_version(declared) if declared is not None else "?"
    return (
        f" The Application's `Prune` sync option only applies from ArgoCD "
        f"{format_version(introduced)}, and `argocd_version` is {shown}."
    )


def _finding(
    app: Application,
    kind: str,
    doc: dict[str, Any],
    identity: Path,
    line: int | None,
    severity: Severity,
    message_end: str,
) -> Finding:
    name = (doc.get("metadata") or {}).get("name", "?")
    message = (
        f"{kind} `{name}` can be pruned: Application `{app.name}` syncs with automated "
        f"prune and the {kind} has no `{_SYNC_OPTIONS_ANNOTATION}: Prune=false` annotation. "
        f"If it leaves the manifests, ArgoCD deletes it with {_CONSEQUENCES[kind]}. "
        "`Prune=confirm` also protects it; `Delete=false` alone does not." + message_end
    )
    return Finding(
        rule_id=RULE_ID,
        severity=severity,
        application=app.name,
        message=message,
        file=identity,
        line=line,
    )
