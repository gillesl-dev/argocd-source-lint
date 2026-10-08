from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.hpa_selfheal_conflict import HpaSelfHealConflictRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return HpaSelfHealConflictRule().check(applications, repo_root, policy, local_origin)


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
    automated:
      selfHeal: true
"""

_HPA = """\
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
"""


def test_no_ignore_differences_at_all_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER,
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "hpa-selfheal-conflict"
    assert finding.severity == Severity.WARNING
    assert "no `ignoreDifferences` covers it" in finding.message
    assert "Deployment" in finding.message
    assert "`web`" in finding.message


def test_ignore_differences_without_respect_sync_option_is_still_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
  ignoreDifferences:
    - group: apps
      kind: Deployment
      name: web
      jsonPointers:
        - /spec/replicas
""",
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "RespectIgnoreDifferences" in findings[0].message
    assert "sync option is not set" in findings[0].message


def test_ignore_differences_and_respect_sync_option_together_is_clean(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - RespectIgnoreDifferences=true
  ignoreDifferences:
    - group: apps
      kind: Deployment
      name: web
      jsonPointers:
        - /spec/replicas
""",
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_wildcard_kind_ignore_diff_rule_matches(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - RespectIgnoreDifferences=true
  ignoreDifferences:
    - group: apps
      kind: "*"
      jsonPointers:
        - /spec/replicas
""",
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_ignore_diff_rule_for_a_different_group_does_not_match(git_repo):
    """`group` empty/omitted means the *core* API group, distinct from
    `apps`: a rule that forgets `group: apps` silently never covers a
    Deployment/StatefulSet, ArgoCD-side. Confirms we don't treat an empty
    group as a wildcard."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
      syncOptions:
        - RespectIgnoreDifferences=true
  ignoreDifferences:
    - kind: Deployment
      name: web
      jsonPointers:
        - /spec/replicas
""",
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "no `ignoreDifferences` covers it" in findings[0].message


def test_no_self_heal_is_not_flagged(git_repo):
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
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_unrecognized_target_kind_without_api_version_is_skipped_not_guessed(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER,
            "manifests/web/hpa.yaml": """\
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web
spec:
  scaleTargetRef:
    kind: Rollout
    name: web
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_non_hpa_resource_is_ignored(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER,
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


def test_namespace_scoped_ignore_diff_rule_requires_matching_namespace(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": _APP_HEADER
            + """\
    syncOptions:
      - RespectIgnoreDifferences=true
  ignoreDifferences:
    - group: apps
      kind: Deployment
      name: web
      namespace: other-namespace
      jsonPointers:
        - /spec/replicas
""",
            "manifests/web/hpa.yaml": _HPA,
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "no `ignoreDifferences` covers it" in findings[0].message
