from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.revision_mismatch import RevisionMismatchRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return RevisionMismatchRule().check(applications, repo_root, policy, local_origin)


def test_target_revision_matching_head_is_not_flagged(git_repo):
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
            "manifests/app/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_target_revision_pinned_to_a_diverged_tag_is_flagged(git_repo, git_tag, git_commit):
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
    targetRevision: v1.0
    path: manifests/app
""",
            "manifests/app/deployment.yaml": "kind: Deployment\n",
        }
    )
    git_tag(repo_root, "v1.0")
    git_commit(repo_root, {})  # HEAD now diverges from v1.0

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "revision-mismatch"
    assert finding.severity == Severity.INFO
    assert finding.application == "app"
    assert "v1.0" in finding.message


def test_severity_is_configurable_via_policy(git_repo, git_tag, git_commit):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "rules:\n  revision-mismatch: warning\n",
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: v1.0
    path: manifests/app
""",
            "manifests/app/deployment.yaml": "kind: Deployment\n",
        }
    )
    git_tag(repo_root, "v1.0")
    git_commit(repo_root, {})

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING


def test_external_source_is_not_checked(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: v9.9
    path: some/path
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_unresolvable_revision_is_left_to_phantom_target(git_repo):
    """A revision that is not fetched at all is the shallow-clone case of
    phantom-target and broken-values-ref (`is_revision_resolvable`)."""
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
    targetRevision: does-not-exist-anywhere
    path: manifests/app
""",
            "manifests/app/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_two_applications_pinned_to_the_same_diverged_revision_are_both_flagged(
    git_repo, git_tag, git_commit
):
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
    targetRevision: v1.0
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
    targetRevision: v1.0
    path: manifests/b
""",
            "manifests/a/deployment.yaml": "kind: Deployment\n",
            "manifests/b/deployment.yaml": "kind: Deployment\n",
        }
    )
    git_tag(repo_root, "v1.0")
    git_commit(repo_root, {})

    findings = _run_rule(repo_root)

    assert len(findings) == 2
    assert {f.application for f in findings} == {"app-a", "app-b"}
