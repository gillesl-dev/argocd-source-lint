from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.orphan_source import OrphanSourceRule


def _run_orphan_source(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return OrphanSourceRule().check(applications, repo_root, policy, local_origin)


def test_good_repo_has_no_orphan_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_bad_orphan_source_flags_uncovered_file_but_not_ignored_one(fixture_repo):
    repo_root = fixture_repo("bad_orphan_source")

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.ERROR
    assert finding.rule_id == "orphan-source"
    assert "orphaned" in finding.file.as_posix()
    assert "ignored-orphan" not in finding.file.as_posix()
    assert finding.line is None  # whole file is the issue, not one line of it


def test_multi_repo_application_is_flagged_info_and_not_scanned(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/external-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: external-app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: HEAD
    path: manifests/external-app
""",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.INFO
    assert finding.application == "external-app"
    assert "out of scope" in finding.message
    assert finding.line == 7  # the `source:` mapping's `repoURL:` line


def test_mixed_local_and_external_sources_still_covers_the_local_source(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/mixed-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: mixed-app
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: manifests/mixed-app
    - repoURL: https://example.invalid/some-other-repo.git
      targetRevision: HEAD
      path: charts/bar
""",
            "manifests/mixed-app/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].application == "mixed-app"
    assert "charts/bar" in findings[0].message


def test_rule_severity_is_configurable_via_policy(fixture_repo):
    repo_root = fixture_repo("bad_orphan_source")
    (repo_root / ".argocd-lint.yaml").write_text(
        "scan_roots:\n  - manifests/\nrules:\n  orphan-source: warning\n",
        encoding="utf-8",
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.WARNING


def test_exclude_paths_suppresses_orphan_finding(fixture_repo):
    repo_root = fixture_repo("bad_orphan_source")
    (repo_root / ".argocd-lint.yaml").write_text(
        "scan_roots:\n  - manifests/\nexclude_paths:\n  - manifests/orphaned/**\n",
        encoding="utf-8",
    )

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_exclude_paths_is_case_sensitive(fixture_repo):
    """CI checkouts on Linux are case-sensitive, so `exclude_paths` must not
    over-match on Windows through `fnmatch`'s case-folding."""
    repo_root = fixture_repo("bad_orphan_source")
    (repo_root / ".argocd-lint.yaml").write_text(
        "scan_roots:\n  - manifests/\nexclude_paths:\n  - manifests/ORPHANED/**\n",
        encoding="utf-8",
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert "manifests/orphaned" in findings[0].file.as_posix()


def test_helm_ref_value_file_is_treated_as_covered(git_repo):
    """Common pattern: an external Helm chart with a local values.yaml referenced
    via $values, and no local `path` source at all."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - applications/\n",
            "bootstrap/argocd-apps/airbyte-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: airbyte
spec:
  sources:
    - repoURL: https://airbytehq.github.io/charts
      chart: airbyte
      targetRevision: "2.0.0"
      helm:
        valueFiles:
          - $values/applications/airbyte/values.yaml
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      ref: values
""",
            "applications/airbyte/values.yaml": "key: value\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_source_without_directory_block_does_not_cover_nested_subdirectories(git_repo):
    """ArgoCD's default for a `directory` source is `recurse: false`: only files
    at the root of `path` are covered."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
""",
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
            "manifests/demo-app/nested/configmap.yaml": "kind: ConfigMap\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].file.as_posix() == "manifests/demo-app/nested/configmap.yaml"


def test_directory_recurse_true_covers_nested_subdirectories(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
    directory:
      recurse: true
""",
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
            "manifests/demo-app/nested/configmap.yaml": "kind: ConfigMap\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_directory_include_masks_files_outside_the_pattern(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - applications/\n",
            "bootstrap/argocd-apps/airflow-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: airflow
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: applications/airflow
    directory:
      include: "{httproute.yaml,airflow-pvc-*.yaml}"
""",
            "applications/airflow/httproute.yaml": "kind: HTTPRoute\n",
            "applications/airflow/namespace.yaml": "kind: Namespace\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].file.as_posix() == "applications/airflow/namespace.yaml"


def test_directory_exclude_suppresses_coverage_of_matched_file(git_repo):
    """App-of-apps pattern: `exclude` keeps the bootstrap file out of its own
    coverage (ArgoCD can't deploy itself), so only `# argocd-lint:ignore` clears
    it, while `other-app.yaml` stays covered via `include: '*.yaml'`."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - bootstrap/argocd-apps/\n",
            "bootstrap/argocd-apps/bootstrap-apps.yaml": """\
# argocd-lint:ignore -- app-of-apps root, applied manually once
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bootstrap-apps
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: bootstrap/argocd-apps
    directory:
      recurse: false
      include: '*.yaml'
      exclude: 'bootstrap-apps.yaml'
""",
            "bootstrap/argocd-apps/other-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: other-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/other-app
""",
        }
    )

    findings = _run_orphan_source(repo_root)

    # other-app.yaml is covered via `*.yaml`; bootstrap-apps.yaml is only
    # cleared thanks to the ignore comment, not by coverage.
    assert findings == []


def test_self_excluding_bootstrap_manifest_is_flagged_without_ignore_comment(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - bootstrap/argocd-apps/\n",
            "bootstrap/argocd-apps/bootstrap-apps.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bootstrap-apps
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: bootstrap/argocd-apps
    directory:
      recurse: false
      include: '*.yaml'
      exclude: 'bootstrap-apps.yaml'
""",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].file.as_posix() == "bootstrap/argocd-apps/bootstrap-apps.yaml"


def test_kustomize_directory_with_fully_referenced_resources_has_no_finding(git_repo):
    """Every file listed in `kustomization.yaml`, however deep the `resources:`
    chain, counts as covered (see DESIGN.md)."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
""",
            "manifests/demo-app/kustomization.yaml": "resources:\n  - base/deployment.yaml\n",
            "manifests/demo-app/base/deployment.yaml": "kind: Deployment\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_kustomize_file_not_in_resources_is_flagged_as_orphan(git_repo):
    """A manifest in the overlay directory but missing from `resources:` is an
    orphan, as in a plain directory source."""
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
""",
            "manifests/demo-app/kustomization.yaml": "resources:\n  - deployment.yaml\n",
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
            "manifests/demo-app/forgotten.yaml": "kind: ConfigMap\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert len(findings) == 1
    assert findings[0].file.as_posix() == "manifests/demo-app/forgotten.yaml"


def test_no_scan_roots_configured_scans_nothing(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: manifests/demo-app
""",
            "manifests/demo-app/deployment.yaml": "kind: Deployment\n",
            "manifests/orphaned/configmap.yaml": "kind: ConfigMap\n",
        }
    )

    findings = _run_orphan_source(repo_root)

    assert findings == []


def test_value_file_outside_the_chart_counts_as_covered(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - envs/\n",
            "bootstrap/argocd-apps/web.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  source:
    repoURL: https://example.invalid/repo.git
    targetRevision: HEAD
    path: charts/web
    helm:
      valueFiles:
        - ../../envs/prod/web/values.yaml
""",
            "charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
            "envs/prod/web/values.yaml": "replicaCount: 2\n",
        }
    )

    assert _run_orphan_source(repo_root) == []
