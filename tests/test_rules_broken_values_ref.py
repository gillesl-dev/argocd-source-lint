from __future__ import annotations

from pathlib import Path

from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.loader import RawManifestDiscovery
from argocd_source_lint.models import Finding, Severity
from argocd_source_lint.policy import load_policy
from argocd_source_lint.rules.broken_values_ref import BrokenValuesRefRule


def _run_broken_values_ref(repo_root: Path) -> list[Finding]:
    policy = load_policy(repo_root)
    applications = RawManifestDiscovery().discover(repo_root)
    local_origin = get_origin_url(repo_root)
    return BrokenValuesRefRule().check(applications, repo_root, policy, local_origin)


def test_good_repo_has_no_broken_values_ref_findings(fixture_repo):
    repo_root = fixture_repo("good_repo")

    findings = _run_broken_values_ref(repo_root)

    assert findings == []


def test_valid_ref_and_existing_file_produces_no_finding(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  sources:
    - repoURL: https://charts.example.invalid/repo
      chart: something
      targetRevision: "1.0.0"
      helm:
        valueFiles:
          - $values/applications/demo-app/values.yaml
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      ref: values
""",
            "applications/demo-app/values.yaml": "key: value\n",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert findings == []


def test_broken_ref_name_is_flagged(fixture_repo):
    repo_root = fixture_repo("bad_broken_values_ref")

    findings = _run_broken_values_ref(repo_root)

    name_findings = [f for f in findings if f.application == "broken-ref-name"]
    assert len(name_findings) == 1
    finding = name_findings[0]
    assert finding.rule_id == "broken-values-ref"
    assert finding.severity == Severity.ERROR
    assert "ref: missing" in finding.message


def test_broken_ref_file_is_flagged_distinctly_from_broken_ref_name(fixture_repo):
    repo_root = fixture_repo("bad_broken_values_ref")

    findings = _run_broken_values_ref(repo_root)

    file_findings = [f for f in findings if f.application == "broken-ref-file"]
    assert len(file_findings) == 1
    finding = file_findings[0]
    assert finding.rule_id == "broken-values-ref"
    assert finding.severity == Severity.ERROR
    assert "does-not-exist.yaml" in finding.message
    assert "ref: missing" not in finding.message
    assert finding.line == 12  # the `- $values/...` valueFiles entry's line


def test_bad_fixture_reports_exactly_two_distinct_findings(fixture_repo):
    repo_root = fixture_repo("bad_broken_values_ref")

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 2


def test_external_ref_source_is_flagged_info_not_checked(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  sources:
    - repoURL: https://charts.example.invalid/repo
      chart: something
      targetRevision: "1.0.0"
      helm:
        valueFiles:
          - $values/whatever.yaml
    - repoURL: https://example.invalid/some-other-repo.git
      targetRevision: HEAD
      ref: values
""",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    assert findings[0].severity == Severity.INFO
    assert findings[0].application == "demo-app"


def test_unresolvable_ref_revision_is_unverifiable(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  sources:
    - repoURL: https://charts.example.invalid/repo
      chart: something
      targetRevision: "1.0.0"
      helm:
        valueFiles:
          - $values/whatever.yaml
    - repoURL: https://example.invalid/repo.git
      targetRevision: never-fetched-branch
      ref: values
""",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.UNVERIFIABLE
    assert "never-fetched-branch" in finding.message
    assert "fetch-depth" in finding.message


def test_rule_severity_is_configurable_via_policy(fixture_repo):
    repo_root = fixture_repo("bad_broken_values_ref")
    (repo_root / ".argocd-lint.yaml").write_text(
        "rules:\n  broken-values-ref: warning\n",
        encoding="utf-8",
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 2
    assert all(f.severity == Severity.WARNING for f in findings)


def test_plain_entry_existing_file_produces_no_finding(git_repo):
    """A plain `valueFiles` entry (no `$ref/...`) is resolved relative to
    its own source's `path`: the common case, not a `$ref`-only rule."""
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
    helm:
      valueFiles:
        - values.yaml
""",
            "manifests/demo-app/values.yaml": "key: value\n",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert findings == []


def test_plain_entry_missing_file_is_flagged(git_repo):
    """A plain `valueFiles` entry pointing at a missing file is the common case
    (see argoproj/argo-cd#4558, "New Applications with misconfiguration show up
    as Healthy")."""
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
    helm:
      valueFiles:
        - values.yaml
        - does-not-exist-either.yaml
""",
            "manifests/demo-app/values.yaml": "key: value\n",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "broken-values-ref"
    assert finding.severity == Severity.ERROR
    assert "does-not-exist-either.yaml" in finding.message
    assert "manifests/demo-app/does-not-exist-either.yaml" in finding.message


def test_plain_entry_with_ignore_missing_value_files_is_not_flagged(git_repo):
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
    helm:
      ignoreMissingValueFiles: true
      valueFiles:
        - does-not-exist.yaml
""",
            "manifests/demo-app/.gitkeep": "",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert findings == []


def test_plain_entry_on_external_source_is_not_checked(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/demo-app.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: demo-app
spec:
  source:
    repoURL: https://example.invalid/some-other-repo.git
    targetRevision: HEAD
    path: manifests/demo-app
    helm:
      valueFiles:
        - does-not-exist.yaml
""",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert findings == []


def test_plain_entry_unresolvable_revision_is_unverifiable(git_repo):
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
    targetRevision: never-fetched-branch
    path: manifests/demo-app
    helm:
      valueFiles:
        - values.yaml
""",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == Severity.UNVERIFIABLE
    assert "never-fetched-branch" in finding.message
    assert "fetch-depth" in finding.message


def test_value_files_relative_to_the_chart_path_are_resolved(git_repo):
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
    path: charts/app
    helm:
      valueFiles:
        - ./values.yaml
        - ../shared/values.yaml
        - https://example.invalid/remote-values.yaml
        - ../shared/missing.yaml
""",
            "charts/app/Chart.yaml": "apiVersion: v2\nname: app\nversion: 0.1.0\n",
            "charts/app/values.yaml": "a: 1\n",
            "charts/shared/values.yaml": "b: 2\n",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    assert "charts/shared/missing.yaml" in findings[0].message


def test_ignore_missing_value_files_also_covers_ref_entries(git_repo):
    """Argo CD's repo-server checks the flag after resolving `$ref` paths too
    (getResolvedValueFiles in reposerver/repository/repository.go)."""
    repo_root = git_repo(
        {
            "bootstrap/argocd-apps/web.yaml": """\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: web
spec:
  sources:
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      ref: values
    - repoURL: https://example.invalid/repo.git
      targetRevision: HEAD
      path: charts/web
      helm:
        ignoreMissingValueFiles: true
        valueFiles:
          - $values/envs/missing.yaml
""",
            "charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
        }
    )

    assert _run_broken_values_ref(repo_root) == []


def test_absolute_value_file_path_is_relative_to_the_repo_root(git_repo):
    """Argo CD: "A path starting with `/` is treated as relative to the
    repository root" (user guide, Helm)."""
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
    path: charts/web
    helm:
      valueFiles:
        - /values/common.yaml
        - /values/missing.yaml
""",
            "charts/web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 0.1.0\n",
            "values/common.yaml": "a: 1\n",
        }
    )

    findings = _run_broken_values_ref(repo_root)

    assert len(findings) == 1
    assert "values/missing.yaml" in findings[0].message
