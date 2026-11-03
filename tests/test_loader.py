from __future__ import annotations

from argocd_source_lint.loader import RawManifestDiscovery


def test_discovers_legacy_single_source_application(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/legacy-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: legacy-app
  namespace: argocd
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/legacy-app
  syncPolicy:
    automated:
      selfHeal: true
"""
        }
    )

    applications = RawManifestDiscovery().discover(repo_root)

    assert len(applications) == 1
    app = applications[0]
    assert app.name == "legacy-app"
    assert app.namespace == "argocd"
    assert app.sync_policy_self_heal is True
    assert len(app.sources) == 1
    assert app.sources[0].path == "manifests/legacy-app"
    assert app.sources[0].target_revision == "HEAD"


def test_empty_target_revision_defaults_to_head(git_repo):
    """An explicit `targetRevision: \"\"` (emitted by some generators to
    mean "track the default branch") must default to HEAD the same as a
    missing key, not be treated as a literally empty revision."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/empty-revision-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: empty-revision-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: ""
    path: manifests/empty-revision-app
"""
        }
    )

    source = RawManifestDiscovery().discover(repo_root)[0].sources[0]

    assert source.target_revision == "HEAD"


def test_parses_directory_recurse_include_exclude(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/filtered-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: filtered-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/filtered-app
    directory:
      recurse: true
      include: "{httproute.yaml,foo-*.yaml}"
      exclude: internal.yaml
"""
        }
    )

    source = RawManifestDiscovery().discover(repo_root)[0].sources[0]

    assert source.directory_recurse is True
    assert source.directory_include == "{httproute.yaml,foo-*.yaml}"
    assert source.directory_exclude == "internal.yaml"


def test_directory_defaults_when_absent(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/plain-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: plain-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/plain-app
"""
        }
    )

    source = RawManifestDiscovery().discover(repo_root)[0].sources[0]

    assert source.directory_recurse is False
    assert source.directory_include is None
    assert source.directory_exclude is None


def test_normalizes_multi_source_with_ref_and_value_files(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/multi-source-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: multi-source-app
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/multi-source-app
      helm:
        valueFiles:
          - values.yaml
          - $values/manifests/multi-source-app/env-values.yaml
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/multi-source-app
      ref: values
"""
        }
    )

    applications = RawManifestDiscovery().discover(repo_root)

    assert len(applications) == 1
    app = applications[0]
    assert len(app.sources) == 2
    chart_source, ref_source = app.sources
    assert chart_source.helm_value_files == [
        "values.yaml",
        "$values/manifests/multi-source-app/env-values.yaml",
    ]
    assert ref_source.ref == "values"


def test_parses_ignore_differences(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/with-ignore.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: with-ignore
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/with-ignore
  ignoreDifferences:
    - group: ""
      kind: Secret
      name: pg-app
      jsonPointers:
        - /data
"""
        }
    )

    app = RawManifestDiscovery().discover(repo_root)[0]

    assert len(app.ignore_differences) == 1
    rule = app.ignore_differences[0]
    assert rule.kind == "Secret"
    assert rule.name == "pg-app"
    assert rule.json_pointers == ["/data"]


def test_ignores_non_application_and_unparsable_yaml_files(git_repo):
    repo_root = git_repo(
        {
            "manifests/configmap.yaml": """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: not-an-application
""",
            "charts/foo/templates/deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Values.name | quote }}
""",
        }
    )

    applications = RawManifestDiscovery().discover(repo_root)

    assert applications == []


def test_ignores_a_manifest_that_is_a_yaml_alias_bomb(git_repo):
    """A "billion laughs" document is treated like any unparseable file and never
    reaches `str(doc.get("apiVersion"))` on the expanded value, which hangs."""
    layers = 30
    lines = ['a0: &a0 ["x"]']
    for i in range(1, layers):
        lines.append(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]")
    lines.append(f"apiVersion: *a{layers - 1}")
    lines.append("kind: Application")
    lines.append("metadata:")
    lines.append("  name: bomb")
    bomb_yaml = "\n".join(lines) + "\n"

    repo_root = git_repo({"bootstrap/argocd-apps/bomb-app.yaml": bomb_yaml})

    applications = RawManifestDiscovery().discover(repo_root)

    assert applications == []


def test_source_and_self_heal_line_numbers(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/legacy-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: legacy-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/legacy-app
  syncPolicy:
    automated:
      selfHeal: true
"""
        }
    )

    app = RawManifestDiscovery().discover(repo_root)[0]

    assert app.sources[0].line == 7
    assert app.self_heal_line == 12


def test_multi_source_and_value_files_line_numbers(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/multi-source-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: multi-source-app
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/multi-source-app
      helm:
        valueFiles:
          - values.yaml
          - $values/manifests/multi-source-app/env-values.yaml
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      ref: values
"""
        }
    )

    app = RawManifestDiscovery().discover(repo_root)[0]
    chart_source, ref_source = app.sources

    assert chart_source.line == 7
    assert chart_source.helm_value_files_lines == [12, 13]
    assert ref_source.line == 14


def test_self_heal_line_is_none_when_self_heal_absent(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/plain-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: plain-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/plain-app
"""
        }
    )

    app = RawManifestDiscovery().discover(repo_root)[0]

    assert app.self_heal_line is None


def test_discovers_multiple_documents_in_one_file(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/apps.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-one
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app-one
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-two
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app-two
"""
        }
    )

    applications = RawManifestDiscovery().discover(repo_root)

    assert {app.name for app in applications} == {"app-one", "app-two"}
