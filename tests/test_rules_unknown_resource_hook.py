from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.unknown_resource_hook import UnknownResourceHookRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return UnknownResourceHookRule().check(applications, repo_root, policy, local_origin)


_APP = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/web
"""


def test_misspelled_hook_value_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: presync
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "unknown-resource-hook"
    assert finding.severity == Severity.WARNING
    assert "presync" in finding.message
    assert "Job" in finding.message
    assert "migrate" in finding.message


def test_all_known_hook_values_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreSync,Sync,Skip,PostSync,SyncFail,PreDelete,PostDelete
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_misspelled_hook_delete_policy_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: HookSuceeded
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "HookSuceeded" in findings[0].message


def test_all_known_hook_delete_policy_values_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: HookSucceeded,HookFailed,BeforeHookCreation
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_predelete_hook_flagged_below_introducing_version(git_repo):
    """`PreDelete` only exists since ArgoCD 3.3.0."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '3.2.0'\n",
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreDelete
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "3.3.0" in findings[0].message
    assert "3.2.0" in findings[0].message


def test_predelete_hook_not_flagged_at_or_above_introducing_version(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '3.3.0'\n",
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreDelete
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_all_known_hook_values_not_flagged_even_with_old_declared_version(git_repo):
    """The oldest hooks (PreSync/Sync/Skip/PostSync) predate ArgoCD's own
    versioning scheme (0.6.1): any realistic declared version accepts them."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '1.0.0'\n",
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreSync,Sync,Skip,PostSync
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_no_hook_annotations_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP,
            "manifests/web/deployment.yaml": """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []
