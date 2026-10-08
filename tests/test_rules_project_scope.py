from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.project_scope import ProjectScopeViolationRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return ProjectScopeViolationRule().check(applications, repo_root, policy, local_origin)


def test_no_appproject_manifests_at_all_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_app_referencing_undeclared_named_project_is_flagged_info(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: platform
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert "platform" in findings[0].message


def test_source_repo_outside_source_repos_scope_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: restricted
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
            "bootstrap/appprojects/restricted.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: restricted
spec:
  sourceRepos:
    - https://example.invalid/some-other-repo.git
  destinations:
    - server: '*'
      namespace: '*'
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "project-scope-violation"
    assert finding.severity == Severity.ERROR
    assert "sourceRepos" in finding.message


def test_source_repo_matching_wildcard_scope_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team-a
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
  destination:
    server: https://kubernetes.default.svc
    namespace: team-a-prod
""",
            "bootstrap/appprojects/team-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
spec:
  sourceRepos:
    - https://example.invalid/*
  destinations:
    - server: https://kubernetes.default.svc
      namespace: team-a-*
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_no_source_repos_declared_denies_everything(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: empty
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
            "bootstrap/appprojects/empty.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: empty
spec:
  sourceRepos: []
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "no `sourceRepos`" in findings[0].message


def test_negated_source_repo_pattern_is_flagged_info_not_evaluated(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: negated
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
            "bootstrap/appprojects/negated.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: negated
spec:
  sourceRepos:
    - '*'
    - '!https://example.invalid/forbidden.git'
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert "negated" in findings[0].message.lower()


def test_destination_server_mismatch_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team-a
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
  destination:
    server: https://other-cluster.internal
    namespace: team-a-prod
""",
            "bootstrap/appprojects/team-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
spec:
  sourceRepos:
    - '*'
  destinations:
    - server: https://kubernetes.default.svc
      namespace: '*'
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "destination" in findings[0].message
    assert "other-cluster.internal" in findings[0].message


def test_destination_namespace_outside_pattern_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team-a
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
  destination:
    server: https://kubernetes.default.svc
    namespace: team-b-prod
""",
            "bootstrap/appprojects/team-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
spec:
  sourceRepos:
    - '*'
  destinations:
    - server: https://kubernetes.default.svc
      namespace: team-a-*
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "destination" in findings[0].message


def test_destination_by_name_vs_project_by_server_is_uncomparable(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team-a
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
  destination:
    name: prod-cluster
    namespace: team-a-prod
""",
            "bootstrap/appprojects/team-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
spec:
  sourceRepos:
    - '*'
  destinations:
    - server: https://kubernetes.default.svc
      namespace: team-a-*
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert "cannot cross-check" in findings[0].message


def test_no_destination_declared_skips_destination_check(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: team-a
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
            "bootstrap/appprojects/team-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: team-a
spec:
  sourceRepos:
    - '*'
  destinations:
    - server: https://kubernetes.default.svc
      namespace: team-a-*
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_default_project_not_found_is_not_flagged(git_repo):
    """Absent a manifest for it, "default" is assumed to be ArgoCD's own
    auto-created, permissive AppProject, not something to flag."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: default
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
  destination:
    server: https://anything.internal
    namespace: anything
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_severity_is_configurable_via_policy(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "rules:\n  project-scope-violation: warning\n",
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  project: restricted
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
""",
            "bootstrap/appprojects/restricted.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: restricted
spec:
  sourceRepos:
    - https://example.invalid/some-other-repo.git
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING
