from __future__ import annotations

from pathlib import Path

from argocd_source_lint import applicationset
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.double_coverage import DoubleCoverageRule

IN_CLUSTER = "https://kubernetes.default.svc"


def _run_double_coverage(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    generated, _ = applicationset.discover(repo_root, local_origin, Severity.INFO)
    applications += generated
    return DoubleCoverageRule().check(applications, repo_root, policy, local_origin)


def _app(
    name: str,
    path: str = "manifests/shared",
    *,
    server: str | None = IN_CLUSTER,
    cluster_name: str | None = None,
    namespace: str | None = "shared",
) -> str:
    destination = ""
    if server or cluster_name or namespace:
        destination = "  destination:\n"
        if server:
            destination += f"    server: {server}\n"
        if cluster_name:
            destination += f"    name: {cluster_name}\n"
        if namespace:
            destination += f"    namespace: {namespace}\n"
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: {path}
{destination}"""


DEPLOYMENT = "kind: Deployment\nmetadata:\n  name: web\n"


def test_good_repo_has_no_double_coverage_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_double_coverage(repo_root)

    assert findings == []


def test_two_applications_deploying_the_same_file_to_the_same_place_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a"),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
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
            "bootstrap/argocd-apps/app-a.yaml": f"""\
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
  destination:
    server: {IN_CLUSTER}
    namespace: shared
""",
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_three_applications_covering_the_same_file_lists_all_three(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a"),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b"),
            "bootstrap/argocd-apps/app-c.yaml": _app("app-c"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    for name in ("app-a", "app-b", "app-c"):
        assert name in findings[0].message


def test_disjoint_applications_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a", "manifests/a"),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b", "manifests/b"),
            "manifests/a/deployment.yaml": DEPLOYMENT,
            "manifests/b/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_rule_severity_is_configurable_via_policy(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "rules:\n  double-coverage: warning\n",
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a"),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING


def test_same_file_deployed_to_different_namespaces_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/dev.yaml": _app("web-dev", namespace="dev"),
            "bootstrap/argocd-apps/prod.yaml": _app("web-prod", namespace="prod"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_same_file_deployed_to_different_clusters_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/eu.yaml": _app("web-eu", server="https://eu.example.invalid"),
            "bootstrap/argocd-apps/us.yaml": _app("web-us", server="https://us.example.invalid"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_applicationset_fanning_out_to_one_namespace_per_env_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appsets/web.yaml": f"""\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: web
spec:
  generators:
    - list:
        elements:
          - env: dev
          - env: prod
  template:
    metadata:
      name: 'web-{{{{env}}}}'
    spec:
      source:
        repoURL: https://example.invalid/repo.git
        targetRevision: HEAD
        path: manifests/shared
      destination:
        server: {IN_CLUSTER}
        namespace: 'web-{{{{env}}}}'
""",
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_manifest_pinning_its_own_namespace_is_flagged_even_with_different_destinations(
    git_repo,
):
    """ArgoCD only applies the destination namespace to resources that don't
    set metadata.namespace, so both Applications write the same object."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/dev.yaml": _app("web-dev", namespace="dev"),
            "bootstrap/argocd-apps/prod.yaml": _app("web-prod", namespace="prod"),
            "manifests/shared/deployment.yaml": (
                "kind: Deployment\nmetadata:\n  name: web\n  namespace: shared\n"
            ),
        }
    )

    findings = _run_double_coverage(repo_root)

    assert len(findings) == 1
    assert "web-dev" in findings[0].message
    assert "web-prod" in findings[0].message


def test_cluster_scoped_resource_from_two_namespaces_is_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/dev.yaml": _app("web-dev", namespace="dev"),
            "bootstrap/argocd-apps/prod.yaml": _app("web-prod", namespace="prod"),
            "manifests/shared/role.yaml": "kind: ClusterRole\nmetadata:\n  name: reader\n",
        }
    )

    assert len(_run_double_coverage(repo_root)) == 1


def test_mixed_file_is_flagged_only_because_of_its_cluster_scoped_document(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/dev.yaml": _app("web-dev", namespace="dev"),
            "bootstrap/argocd-apps/prod.yaml": _app("web-prod", namespace="prod"),
            "manifests/shared/all.yaml": (
                DEPLOYMENT + "---\nkind: ClusterRole\nmetadata:\n  name: reader\n"
            ),
        }
    )

    assert len(_run_double_coverage(repo_root)) == 1


def test_unknown_kind_from_two_namespaces_is_not_flagged(git_repo):
    """A custom resource may be cluster-scoped, but its scope isn't knowable
    from the repo alone, so it's treated as namespaced: missed, never
    falsely flagged."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/dev.yaml": _app("web-dev", namespace="dev"),
            "bootstrap/argocd-apps/prod.yaml": _app("web-prod", namespace="prod"),
            "manifests/shared/thing.yaml": "kind: SomeCustomKind\nmetadata:\n  name: x\n",
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_applications_without_destination_are_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a", server=None, namespace=None),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b", server=None, namespace=None),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_cluster_named_by_url_and_by_name_is_not_compared(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a"),
            "bootstrap/argocd-apps/app-b.yaml": _app(
                "app-b", server=None, cluster_name="in-cluster"
            ),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []


def test_unresolved_template_namespace_is_not_flagged(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/app-a.yaml": _app("app-a", namespace="'{{env}}'"),
            "bootstrap/argocd-apps/app-b.yaml": _app("app-b", namespace="'{{env}}'"),
            "manifests/shared/deployment.yaml": DEPLOYMENT,
        }
    )

    assert _run_double_coverage(repo_root) == []
