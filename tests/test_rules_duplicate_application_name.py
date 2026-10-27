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


def _appset(name: str, namespace: str, generated: str, template_namespace: str = "") -> str:
    template_ns = f"      namespace: {template_namespace}\n" if template_namespace else ""
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: {name}
  namespace: {namespace}
spec:
  generators:
    - list:
        elements:
          - service: {generated}
  template:
    metadata:
      name: '{{{{service}}}}'
{template_ns}    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/{name}
"""


def test_applicationsets_in_different_namespaces_generating_the_same_name_are_not_flagged(
    git_repo,
):
    """Generated Applications land in their ApplicationSet's namespace, so
    `team-a/api` and `team-b/api` are two distinct Applications."""
    repo_root = git_repo(
        {
            "appsets/team-a.yaml": _appset("team-a", "team-a", "api"),
            "appsets/team-b.yaml": _appset("team-b", "team-b", "api"),
        }
    )

    assert _run_rule(repo_root) == []


def test_generated_application_does_not_collide_with_a_plain_one_in_argocd(git_repo):
    repo_root = git_repo(
        {
            "appsets/team-a.yaml": _appset("team-a", "team-a", "api"),
            "bootstrap/argocd-apps/api.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: api
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/api
""",
        }
    )

    assert _run_rule(repo_root) == []


def test_template_namespace_is_ignored_like_argocd_does(git_repo):
    """ArgoCD overwrites the template's metadata.namespace with the
    ApplicationSet's own, so two ApplicationSets in the same namespace still
    collide even if their templates claim different namespaces."""
    repo_root = git_repo(
        {
            "appsets/one.yaml": _appset("one", "team-a", "api", template_namespace="ns-one"),
            "appsets/two.yaml": _appset("two", "team-a", "api", template_namespace="ns-two"),
        }
    )

    findings = _run_rule(repo_root)

    assert len(findings) == 1
    assert "in namespace `team-a`" in findings[0].message


def _named(name: str) -> str:
    return f"""apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: '{name}'
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/app
"""


def test_names_the_api_server_would_refuse_are_not_compared(git_repo):
    """A template's unfilled `<CLUSTER_NAME>-app` can never be created
    (custom resource names must be DNS subdomains), so two copies of it
    can't collide: the real names depend on what fills the placeholder."""
    repo_root = git_repo(
        {
            "templates/cluster/app.yaml": _named("<CLUSTER_NAME>-cert-manager"),
            "templates/vcluster/app.yaml": _named("<CLUSTER_NAME>-cert-manager"),
            "templates/a/upper.yaml": _named("Web"),
            "templates/b/upper.yaml": _named("Web"),
        }
    )

    assert _run_rule(repo_root) == []


def test_valid_dotted_names_are_still_compared(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/a.yaml": _named("web.team-a"),
            "bootstrap/argocd-apps/b.yaml": _named("web.team-a"),
        }
    )

    assert len(_run_rule(repo_root)) == 1
