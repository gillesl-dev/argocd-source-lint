from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.double_coverage import DoubleCoverageRule


def _run_double_coverage(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return DoubleCoverageRule().check(applications, repo_root, policy, local_origin)


def test_good_repo_has_no_double_coverage_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_double_coverage(repo_root)

    assert findings == []


def test_two_different_applications_covering_the_same_file_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-a
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "bootstrap/argocd-apps/app-b.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-b
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "manifests/shared/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "double-coverage"
    assert finding.severity == Severity.ERROR
    assert finding.file.as_posix() == "manifests/shared/deployment.yaml"
    assert "app-a" in finding.message
    assert "app-b" in finding.message


def test_same_application_covering_a_file_twice_is_not_flagged(git_repo):
    """Two sources of the SAME Application overlapping isn't a conflict:
    it's one sync loop, not two fighting each other."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-a
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/shared
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/shared
      directory:
        recurse: true
""",
            "manifests/shared/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_double_coverage(repo_root)

    assert findings == []


def test_three_applications_covering_the_same_file_lists_all_three(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-a
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "bootstrap/argocd-apps/app-b.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-b
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "bootstrap/argocd-apps/app-c.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-c
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "manifests/shared/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    assert "app-a" in findings[0].message
    assert "app-b" in findings[0].message
    assert "app-c" in findings[0].message


def test_disjoint_applications_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-a
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/a
""",
            "bootstrap/argocd-apps/app-b.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-b
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/b
""",
            "manifests/a/deployment.yaml": "kind: Deployment\n",
            "manifests/b/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_double_coverage(repo_root)

    assert findings == []


def test_rule_severity_is_configurable_via_policy(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "rules:\n  double-coverage: warning\n",
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-a
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "bootstrap/argocd-apps/app-b.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app-b
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/shared
""",
            "manifests/shared/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING
