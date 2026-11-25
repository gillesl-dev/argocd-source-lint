from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argocd_source_lint.cli import app
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.missing_prune_protection import MissingPruneProtectionRule

runner = CliRunner()

RULE = "missing-prune-protection"


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return MissingPruneProtectionRule().check(applications, repo_root, policy, local_origin)


def _app(
    *,
    path: str = "manifests/team",
    automated: str = "      prune: true\n",
    sync_options: tuple[str, ...] = (),
    source_extra: str = "",
    annotations: str = "",
    repo: str = "https://example.invalid/repo.git",
) -> str:
    text = "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: team\n"
    if annotations:
        text += "  annotations:\n" + annotations
    text += (
        "spec:\n"
        "  source:\n"
        f"    repoURL: {repo}\n"
        "    targetRevision: HEAD\n"
        f"    path: {path}\n" + source_extra + "  syncPolicy:\n"
    )
    if automated is not None:
        text += "    automated:\n" + automated
    if sync_options:
        text += "    syncOptions:\n" + "".join(f"      - {o}\n" for o in sync_options)
    return text


def _namespace(name: str = "team", sync_options: str | None = None, extra: str = "") -> str:
    text = f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {name}\n"
    if sync_options is not None:
        text += f"  annotations:\n    argocd.argoproj.io/sync-options: {sync_options}\n"
    return text + extra


_CRD = """\
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.example.com
spec:
  group: example.com
  names:
    kind: Widget
    plural: widgets
  scope: Namespaced
"""

_CONFIGMAP = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\n"


# --- The case of the issue --------------------------------------------------


def test_unprotected_namespace_under_automated_prune_is_flagged(git_repo):
    repo_root = git_repo({"apps/team.yaml": _app(), "manifests/team/namespace.yaml": _namespace()})

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == RULE
    assert finding.severity == Severity.WARNING
    assert finding.application == "team"
    assert finding.file == Path("manifests/team/namespace.yaml")
    assert finding.line == 1
    assert "Namespace `team`" in finding.message
    assert "Prune=false" in finding.message


def test_unprotected_crd_is_flagged_with_its_own_consequence(git_repo):
    repo_root = git_repo({"apps/team.yaml": _app(), "manifests/team/crd.yaml": _CRD})

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "CustomResourceDefinition `widgets.example.com`" in findings[0].message
    assert "every custom resource of that kind" in findings[0].message


def test_finding_points_to_the_document_in_a_multi_document_file(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/all.yaml": _CONFIGMAP
            + "---\n"
            + _namespace("a", "Prune=false")
            + "---\n"
            + _namespace("b"),
        }
    )

    findings = _run_rule(repo_root)

    assert [(f.file.as_posix(), f.line) for f in findings] == [("manifests/team/all.yaml", 13)]
    assert "Namespace `b`" in findings[0].message


# --- What protects, what does not ------------------------------------------


@pytest.mark.parametrize(
    "sync_options",
    [
        "Prune=false",
        "Prune=confirm",
        "ServerSideApply=true, Prune=false",
        "Prune=false,Delete=false",
    ],
)
def test_prune_false_or_confirm_on_the_resource_protects_it(git_repo, sync_options):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/namespace.yaml": _namespace(sync_options=f'"{sync_options}"'),
            "manifests/team/crd.yaml": _CRD.replace(
                "  name: widgets.example.com\n",
                "  name: widgets.example.com\n  annotations:\n"
                f'    argocd.argoproj.io/sync-options: "{sync_options}"\n',
            ),
        }
    )

    assert _run_rule(repo_root) == []


@pytest.mark.parametrize(
    "sync_options",
    # Argo CD compares the options case-sensitively (resource/annotations.go).
    ["Prune=False", "Delete=false", "PruneLast=true", "Delete=confirm", "Prune=true"],
)
def test_options_that_do_not_stop_a_prune_leave_it_flagged(git_repo, sync_options):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/namespace.yaml": _namespace(sync_options=sync_options),
        }
    )

    assert len(_run_rule(repo_root)) == 1


def test_helm_resource_policy_keep_does_not_stop_a_prune(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/namespace.yaml": _namespace(
                extra="  annotations:\n    helm.sh/resource-policy: keep\n"
            ),
        }
    )

    assert len(_run_rule(repo_root)) == 1


@pytest.mark.parametrize("sync_options", [("PruneLast=true",), ("Delete=false",)])
def test_application_level_options_that_do_not_stop_a_prune(git_repo, sync_options):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(sync_options=sync_options),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert len(_run_rule(repo_root)) == 1


@pytest.mark.parametrize("option", ["Prune=false", "Prune=confirm"])
def test_application_level_prune_option_protects_unless_argocd_is_older_than_3_4(git_repo, option):
    files = {
        "apps/team.yaml": _app(sync_options=(option,)),
        "manifests/team/namespace.yaml": _namespace(),
    }
    assert _run_rule(git_repo(files)) == []


def test_application_level_prune_option_is_ignored_before_argocd_3_4(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: 3.3.3\n",
            "apps/team.yaml": _app(sync_options=("Prune=false",)),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "3.4" in findings[0].message


def test_application_level_prune_option_counts_from_argocd_3_4(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: 3.4.0\n",
            "apps/team.yaml": _app(sync_options=("Prune=false",)),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


def test_a_prune_option_on_the_resource_overrides_the_application_level_one(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(sync_options=("Prune=false",)),
            "manifests/team/namespace.yaml": _namespace(sync_options="Prune=true"),
        }
    )

    assert len(_run_rule(repo_root)) == 1


# --- Which Applications ----------------------------------------------------


@pytest.mark.parametrize(
    "automated",
    [
        None,
        "      selfHeal: true\n",
        "      prune: false\n",
        # Auto-sync turned off (`IsAutomatedSyncEnabled` in Argo CD's types.go).
        "      prune: true\n      enabled: false\n",
    ],
)
def test_no_automated_prune_means_no_finding(git_repo, automated):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(automated=automated),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


def test_other_cluster_scoped_kinds_are_not_flagged(git_repo):
    cluster_role = (
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: r\n"
    )
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/role.yaml": cluster_role,
            "manifests/team/cm.yaml": _CONFIGMAP,
            # Same kind name, other API group.
            "manifests/team/other.yaml": _namespace().replace("apiVersion: v1", "apiVersion: x/v1"),
        }
    )

    assert _run_rule(repo_root) == []


def test_directory_source_honors_recurse_and_exclude(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(
                source_extra="    directory:\n      recurse: true\n      exclude: 'skip/*'\n"
            ),
            "manifests/team/deep/namespace.yaml": _namespace("deep"),
            "manifests/team/skip/namespace.yaml": _namespace("skipped"),
        }
    )

    findings = _run_rule(repo_root)

    assert [f.file.as_posix() for f in findings] == ["manifests/team/deep/namespace.yaml"]


def test_external_source_is_not_reported(git_repo):
    # orphan-source and phantom-target already report every external source.
    repo_root = git_repo(
        {"apps/team.yaml": _app(repo="https://example.invalid/other.git")},
    )

    findings = _run_rule(repo_root)

    assert findings == []


# --- Kustomize -------------------------------------------------------------


def test_kustomize_resources_are_followed_and_unlisted_files_ignored(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": "resources:\n  - ../base\n  - namespace.yaml\n",
            "manifests/team/namespace.yaml": _namespace("listed"),
            "manifests/team/unlisted.yaml": _namespace("unlisted"),
            "manifests/base/kustomization.yaml": "resources:\n  - crd.yaml\n",
            "manifests/base/crd.yaml": _CRD,
        }
    )

    findings = _run_rule(repo_root)

    assert sorted(f.file.as_posix() for f in findings) == [
        "manifests/base/crd.yaml",
        "manifests/team/namespace.yaml",
    ]


def test_a_namespace_used_as_a_patch_is_not_deployed(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": (
                "resources:\n  - cm.yaml\npatches:\n  - path: namespace.yaml\n"
            ),
            "manifests/team/cm.yaml": _CONFIGMAP,
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


@pytest.mark.parametrize(
    "transform",
    [
        "patches:\n  - path: patch.yaml\n",
        "patchesStrategicMerge:\n  - patch.yaml\n",
        "components:\n  - ../component\n",
        "transformers:\n  - transformer.yaml\n",
        "replacements:\n  - path: r.yaml\n",
    ],
)
def test_kustomization_that_transforms_resources_is_not_checked(git_repo, transform):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": "resources:\n  - namespace.yaml\n" + transform,
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


def test_transform_in_a_nested_kustomization_only_hides_its_own_resources(git_repo):
    # Kustomize applies a kustomization's patches to the resources it lists,
    # not to those of the kustomization that includes it.
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": "resources:\n  - ../base\n  - crd.yaml\n",
            "manifests/team/crd.yaml": _CRD,
            "manifests/base/kustomization.yaml": (
                "resources:\n  - namespace.yaml\npatches:\n  - path: p.yaml\n"
            ),
            "manifests/base/namespace.yaml": _namespace(),
        }
    )

    findings = _run_rule(repo_root)

    assert [f.file.as_posix() for f in findings] == ["manifests/team/crd.yaml"]


def test_common_annotations_with_prune_false_protect_the_resources(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": (
                "resources:\n  - namespace.yaml\n  - ../base\ncommonAnnotations:\n"
                '  argocd.argoproj.io/sync-options: "Prune=false,Delete=false"\n'
            ),
            "manifests/team/namespace.yaml": _namespace(),
            "manifests/base/kustomization.yaml": "resources:\n  - crd.yaml\n",
            "manifests/base/crd.yaml": _CRD,
        }
    )

    assert _run_rule(repo_root) == []


def test_common_annotations_replace_the_resource_annotation(git_repo):
    # Kustomize sets the key on every resource, replacing the resource's own value.
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": (
                "resources:\n  - namespace.yaml\ncommonAnnotations:\n"
                "  argocd.argoproj.io/sync-options: ServerSideApply=true\n"
            ),
            "manifests/team/namespace.yaml": _namespace(sync_options="Prune=false"),
        }
    )

    assert len(_run_rule(repo_root)) == 1


def test_outer_common_annotations_win_over_inner_ones(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": (
                "resources:\n  - ../base\ncommonAnnotations:\n"
                "  argocd.argoproj.io/sync-options: Prune=false\n"
            ),
            "manifests/base/kustomization.yaml": (
                "resources:\n  - namespace.yaml\ncommonAnnotations:\n"
                "  argocd.argoproj.io/sync-options: ServerSideApply=true\n"
            ),
            "manifests/base/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


def test_common_annotations_without_the_key_keep_the_resource_annotation(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/kustomization.yaml": (
                "resources:\n  - namespace.yaml\ncommonAnnotations:\n  team: a\n"
            ),
            "manifests/team/namespace.yaml": _namespace(sync_options="Prune=false"),
        }
    )

    assert _run_rule(repo_root) == []


@pytest.mark.parametrize(
    "kustomize",
    [
        "    kustomize:\n      commonAnnotations:\n        a: b\n",
        "    kustomize:\n      patches:\n        - patch: '[]'\n",
        "    kustomize:\n      components:\n        - ../c\n",
    ],
)
def test_application_kustomize_transforms_hide_the_source(git_repo, kustomize):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(source_extra=kustomize),
            "manifests/team/kustomization.yaml": "resources:\n  - namespace.yaml\n",
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


def test_application_kustomize_options_that_keep_annotations_are_checked(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(source_extra="    kustomize:\n      namePrefix: x-\n"),
            "manifests/team/kustomization.yaml": "resources:\n  - namespace.yaml\n",
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert len(_run_rule(repo_root)) == 1


def test_kustomize_source_without_a_kustomization_is_not_read_as_a_directory(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(source_extra="    kustomize:\n      namePrefix: x-\n"),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


# --- Helm and plugins ------------------------------------------------------


def test_helm_chart_is_not_rendered(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(),
            "manifests/team/Chart.yaml": "apiVersion: v2\nname: team\nversion: 0.1.0\n",
            "manifests/team/templates/namespace.yaml": _namespace(),
            "manifests/team/crds/crd.yaml": _CRD,
        }
    )

    assert _run_rule(repo_root) == []


def test_plugin_source_is_not_rendered(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(source_extra="    plugin:\n      name: cmp\n"),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _run_rule(repo_root) == []


# --- Through the CLI: ApplicationSets, suppression, --skip ----------------


def _lint(repo_root: Path, *args: str) -> list[dict]:
    result = runner.invoke(app, [str(repo_root), "--format", "json", *args])
    return [f for f in json.loads(result.stdout)["findings"] if f["rule_id"] == RULE]


_APPSET = """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: teams
spec:
  goTemplate: true
  generators:
    - list:
        elements:
          - team: a
          - team: b
  template:
    metadata:
      name: 'team-{{.team}}'
ANNOTATIONS
    spec:
      project: default
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: 'manifests/{{.team}}'
      destination:
        server: https://kubernetes.default.svc
        namespace: '{{.team}}'
      syncPolicy:
        automated:
          prune: true
"""


def test_applications_generated_by_an_applicationset_are_checked(git_repo):
    repo_root = git_repo(
        {
            "apps/teams.yaml": _APPSET.replace("ANNOTATIONS\n", ""),
            "manifests/a/namespace.yaml": _namespace("a"),
            "manifests/b/namespace.yaml": _namespace("b", "Prune=false"),
        }
    )

    findings = _lint(repo_root)

    assert [(f["application"], f["file"]) for f in findings] == [
        ("team-a", "manifests/a/namespace.yaml")
    ]


def test_applicationset_template_annotation_suppresses_the_rule(git_repo):
    repo_root = git_repo(
        {
            "apps/teams.yaml": _APPSET.replace(
                "ANNOTATIONS\n",
                "      annotations:\n"
                '        argocd-source-lint/ignore-missing-prune-protection: "data is in git"\n',
            ),
            "manifests/a/namespace.yaml": _namespace("a"),
        }
    )

    assert _lint(repo_root) == []


def test_application_annotation_suppresses_the_rule(git_repo):
    repo_root = git_repo(
        {
            "apps/team.yaml": _app(
                annotations=(
                    "    argocd-source-lint/ignore-missing-prune-protection: "
                    '"namespace is disposable"\n'
                )
            ),
            "manifests/team/namespace.yaml": _namespace(),
        }
    )

    assert _lint(repo_root) == []


def test_skip_option_disables_the_rule(git_repo):
    repo_root = git_repo({"apps/team.yaml": _app(), "manifests/team/namespace.yaml": _namespace()})

    assert len(_lint(repo_root)) == 1
    assert _lint(repo_root, "--skip", RULE) == []
