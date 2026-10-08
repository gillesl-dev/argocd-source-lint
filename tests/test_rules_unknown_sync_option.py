from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.unknown_sync_option import UnknownSyncOptionRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return UnknownSyncOptionRule().check(applications, repo_root, policy, local_origin)


_APP_HEADER = """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/web
  syncPolicy:
"""


def test_wrong_case_key_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - respectIgnoreDifferences=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "unknown-sync-option"
    assert finding.severity == Severity.WARNING
    assert "respectIgnoreDifferences=true" in finding.message
    assert finding.line == 11


def test_misspelled_key_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - PruneLatest=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "PruneLatest=true" in findings[0].message


def test_all_known_keys_are_not_flagged(git_repo):
    known_options = [
        "Prune=false",
        "Validate=false",
        "SkipDryRunOnMissingResource=true",
        "Delete=confirm",
        "ApplyOutOfSyncOnly=true",
        "PrunePropagationPolicy=foreground",
        "PruneLast=true",
        "Replace=true",
        "ServerSideApply=true",
        "ClientSideApplyMigration=false",
        "FailOnSharedResource=true",
        "RespectIgnoreDifferences=true",
        "CreateNamespace=true",
    ]
    options_yaml = "".join(f"      - {option}\n" for option in known_options)
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER + "    syncOptions:\n" + options_yaml,
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_no_sync_options_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/web
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_unknown_bare_entry_without_equals_sign_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - AutoPrune
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "AutoPrune" in findings[0].message


_PLAIN_APP = """\
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


def test_resource_level_unknown_key_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _PLAIN_APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/sync-options: forceApply=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "unknown-sync-option"
    assert "forceApply=true" in finding.message
    assert "Job" in finding.message
    assert "migrate" in finding.message


def test_resource_level_known_keys_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _PLAIN_APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/sync-options: Force=true,ServerSideApply=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_prune_at_application_level_flagged_below_introducing_version(git_repo):
    """`Prune`/`Delete` only became valid Application-level `syncOptions` keys
    in ArgoCD 3.4.0; before that they only existed as the per-resource
    annotation. A repo declaring an older `argocd_version` should still catch
    this real silent no-op."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '3.3.0'\n",
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - Prune=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "3.4.0" in findings[0].message
    assert "3.3.0" in findings[0].message


def test_prune_at_application_level_not_flagged_at_or_above_introducing_version(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '3.4.0'\n",
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - Prune=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_prune_at_application_level_not_flagged_without_declared_version(git_repo):
    """No `argocd_version` declared keeps today's behavior: every key ArgoCD
    has ever recognized is accepted, regardless of when it shipped."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - Prune=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_resource_level_prune_not_flagged_even_with_old_declared_version(git_repo):
    """`Prune` has existed as the per-resource annotation since ArgoCD 1.1.0 --
    long before it became valid at the Application level (3.4.0, see above).
    An old declared `argocd_version` must not gate the two contexts together."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "argocd_version: '1.2.0'\n",
            "bootstrap/argocd-apps/web.yaml": _PLAIN_APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/sync-options: Prune=false
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_resource_level_key_only_valid_at_application_level_is_flagged(git_repo):
    """`RespectIgnoreDifferences` is a real ArgoCD sync option key, but
    only at the Application level: it doesn't exist in the smaller
    per-resource annotation key set, so it's still a no-op there."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _PLAIN_APP,
            "manifests/web/job.yaml": """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/sync-options: RespectIgnoreDifferences=true
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "RespectIgnoreDifferences=true" in findings[0].message
