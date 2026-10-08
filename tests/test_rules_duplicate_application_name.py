from __future__ import annotations

from pathlib import Path

from argocd_source_lint import applicationset
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.duplicate_application_name import DuplicateApplicationNameRule


def _run_rule(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    generated_apps, _ = applicationset.discover(repo_root, local_origin, Severity.INFO)
    applications += generated_apps
    return DuplicateApplicationNameRule().check(applications, repo_root, policy, local_origin)


def test_two_plain_manifests_with_the_same_name_are_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments
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
  name: payments
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/b
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "duplicate-application-name"
    assert finding.severity == Severity.ERROR
    assert finding.application == "payments"
    assert "2 Applications named `payments`" in finding.message
    assert "bootstrap/argocd-apps/app-a.yaml" in finding.message
    assert "bootstrap/argocd-apps/app-b.yaml" in finding.message


def test_different_names_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments
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
  name: billing
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/b
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_same_name_different_namespace_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments
  namespace: argocd
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
  name: payments
  namespace: argocd-team-b
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/b
""",
        }
    )

    findings = _run_rule(repo_root)

    assert findings == []


def test_applicationset_generated_entry_colliding_with_a_plain_manifest_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/payments.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: payments
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/payments
""",
            "bootstrap/appsets/team-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: team-appset
spec:
  generators:
    - list:
        elements:
          - name: payments
  template:
    metadata:
      name: '{{name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/generated
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "2 Applications named `payments`" in findings[0].message


def test_applicationset_generator_colliding_with_itself_is_flagged(git_repo):
    """Two generator entries rendering to the same name via the template
    (e.g. a typo'd `elements` list) share the same source_file (the
    ApplicationSet's own manifest), listed only once (see
    `_finding`'s de-duplication)."""
    repo_root = git_repo(
        {
            "bootstrap/appsets/team-appset.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: team-appset
spec:
  generators:
    - list:
        elements:
          - name: payments
            region: eu
          - name: payments
            region: us
  template:
    metadata:
      name: '{{name}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/generated
""",
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert findings[0].message.count("bootstrap/appsets/team-appset.yaml") == 1
