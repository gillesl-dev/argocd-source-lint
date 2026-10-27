"""End-to-end mutation tests: one known defect injected into an otherwise
clean synthetic GitOps repo per case, run through the real CLI and read
back from its JSON report.

Positive cases must fire their rule at its documented default severity and
no other blocking rule. Negative cases are legitimate setups that once
produced false positives and must stay clean. Released 1.2.2 passed every
positive case and failed all four negative ones.

Adapted from a standalone script written from the README alone, without
reading the rules' code."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argocd_source_lint import applicationset
from argocd_source_lint.cli import RULES, app

runner = CliRunner()

ORIGIN = "https://github.com/example/gitops.git"
CHART_REPO = "https://charts.example.com"
BLOCKING = {"error", "warning", "unverifiable"}

# Default severities as documented in the README, on purpose not imported
# from policy.py: changing a default must break this test until the README
# and this table are updated together.
DOCUMENTED_SEVERITIES = {
    "orphan-source": "error",
    "broken-values-ref": "error",
    "missing-ignore-diff": "warning",
    "phantom-target": "error",
    "unresolvable-generator": "info",
    "double-coverage": "error",
    "revision-mismatch": "info",
    "project-scope-violation": "error",
    "hpa-selfheal-conflict": "warning",
    "sync-validation-disabled": "info",
    "duplicate-application-name": "error",
    "malformed-ignore-diff-pointer": "warning",
    "malformed-ignore-diff-jq-expression": "warning",
    "unknown-sync-option": "warning",
    "unknown-resource-hook": "warning",
    "malformed-sync-wave": "warning",
    "floating-target-revision": "warning",
}


def _app(
    name: str,
    *,
    path: str = "manifests/web",
    project: str = "team",
    repo: str = ORIGIN,
    revision: str = "main",
    dest_ns: str = "team-ns",
    sync_options: tuple[str, ...] = (),
    ignore_diff: list[str] | None = None,
    chart: str | None = None,
    helm_values: list[str] | None = None,
) -> str:
    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: Application",
        "metadata:",
        f'  name: "{name}"',
        "  namespace: argocd",
        "spec:",
        f"  project: {project}",
        "  source:",
        f"    repoURL: {repo}",
        f'    targetRevision: "{revision}"',
        f"    chart: {chart}" if chart else f"    path: {path}",
    ]
    if helm_values is not None:
        lines += ["    helm:", "      valueFiles:"] + [f"        - {v}" for v in helm_values]
    lines += [
        "  destination:",
        "    server: https://kubernetes.default.svc",
        f"    namespace: {dest_ns}",
    ]
    if ignore_diff:
        lines += ["  ignoreDifferences:", *ignore_diff]
    lines += ["  syncPolicy:", "    automated:", "      prune: true", "      selfHeal: true"]
    if sync_options:
        lines += ["    syncOptions:"] + [f"      - {option}" for option in sync_options]
    return "\n".join(lines) + "\n"


def _project(name: str, namespaces: list[str]) -> str:
    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: AppProject",
        "metadata:",
        f"  name: {name}",
        "  namespace: argocd",
        "spec:",
        "  sourceRepos:",
        f"    - {ORIGIN}",
        f"    - {CHART_REPO}",
        "  destinations:",
    ]
    for namespace in namespaces:
        lines += ["    - server: https://kubernetes.default.svc", f"      namespace: {namespace}"]
    return "\n".join(lines) + "\n"


def _configmap(name: str) -> str:
    return f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\ndata:\n  key: value\n"


def _list_appset(name: str, namespace: str, elements: list[tuple[str, str]], path: str) -> str:
    lines = [
        "apiVersion: argoproj.io/v1alpha1",
        "kind: ApplicationSet",
        "metadata:",
        f"  name: {name}",
        f"  namespace: {namespace}",
        "spec:",
        "  generators:",
        "    - list:",
        "        elements:",
    ]
    for app_name, app_namespace in elements:
        lines += [f"          - name: {app_name}", f"            ns: {app_namespace}"]
    lines += [
        "  template:",
        "    metadata:",
        "      name: '{{name}}'",
        "    spec:",
        "      project: team",
        "      source:",
        f"        repoURL: {ORIGIN}",
        "        targetRevision: main",
        f"        path: {path}",
        "      destination:",
        "        server: https://kubernetes.default.svc",
        "        namespace: '{{ns}}'",
        "      syncPolicy:",
        "        automated:",
        "          prune: true",
        "          selfHeal: true",
    ]
    return "\n".join(lines) + "\n"


def _ignore_diff(key: str, value: str) -> list[str]:
    return ["    - group: apps", "      kind: Deployment", f"      {key}:", f'        - "{value}"']


DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 2
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.27
"""

SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector:
    app: web
  ports:
    - port: 80
"""

HPA = """\
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: web
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: web
  minReplicas: 2
  maxReplicas: 5
"""

HOOK_JOB = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: migrate
  annotations:
    argocd.argoproj.io/hook: PreSyncc
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: m
          image: busybox:1.36
"""

CNPG_CLUSTER = """\
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: db
spec:
  instances: 1
  storage:
    size: 1Gi
"""

CLUSTERS_APPSET = f"""\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: clusters-set
  namespace: argocd
spec:
  generators:
    - clusters: {{}}
  template:
    metadata:
      name: '{{{{name}}}}-web'
    spec:
      project: team
      source:
        repoURL: {ORIGIN}
        targetRevision: main
        path: manifests/web
      destination:
        server: '{{{{server}}}}'
        namespace: team-ns
"""


def _base_files() -> dict[str, str]:
    return {
        ".argocd-lint.yaml": "scan_roots:\n  - manifests\n",
        "projects/team.yaml": _project("team", ["team-ns", "ns-a", "ns-b"]),
        "apps/web.yaml": _app("web"),
        "apps/helm-app.yaml": _app("helm-app", path="charts/app", helm_values=["values.yaml"]),
        "manifests/web/deployment.yaml": DEPLOYMENT,
        "manifests/web/service.yaml": SERVICE,
        "charts/app/Chart.yaml": "apiVersion: v2\nname: app\nversion: 0.1.0\n",
        "charts/app/values.yaml": "replicaCount: 1\n",
    }


@dataclass
class Case:
    name: str
    files: dict[str, str] = field(default_factory=dict)
    # None: negative case, nothing blocking may fire.
    rule: str | None = None
    # Run after the initial commit, from the repo root.
    git_after: list[list[str]] = field(default_factory=list)


POSITIVE_CASES = [
    Case("orphan-source", {"manifests/stray/orphan.yaml": _configmap("orphan")}, "orphan-source"),
    Case(
        "broken-values-ref",
        {
            "apps/helm-app.yaml": _app(
                "helm-app", path="charts/app", helm_values=["missing-values.yaml"]
            )
        },
        "broken-values-ref",
    ),
    Case(
        "missing-ignore-diff",
        {"manifests/web/db.yaml": CNPG_CLUSTER},
        "missing-ignore-diff",
    ),
    Case(
        "phantom-target",
        {"apps/helm-app.yaml": _app("helm-app", path="charts/nowhere")},
        "phantom-target",
    ),
    Case(
        "unresolvable-generator",
        {"apps/clusters-set.yaml": CLUSTERS_APPSET},
        "unresolvable-generator",
    ),
    Case(
        "double-coverage",
        {"apps/web-twin.yaml": _app("web-twin")},
        "double-coverage",
    ),
    # The rule compares commits, not branch names: `other` must point at a
    # different commit than the checked-out one.
    Case(
        "revision-mismatch",
        {"apps/web.yaml": _app("web", revision="other")},
        "revision-mismatch",
        git_after=[["branch", "other"], ["commit", "-q", "--allow-empty", "-m", "advance main"]],
    ),
    Case(
        "project-scope-violation",
        {"apps/web.yaml": _app("web", dest_ns="forbidden")},
        "project-scope-violation",
    ),
    Case("hpa-selfheal-conflict", {"manifests/web/hpa.yaml": HPA}, "hpa-selfheal-conflict"),
    Case(
        "sync-validation-disabled",
        {"apps/web.yaml": _app("web", sync_options=("Validate=false",))},
        "sync-validation-disabled",
    ),
    Case(
        "duplicate-application-name",
        {
            "manifests/api/cm.yaml": _configmap("api"),
            "apps/web-dup.yaml": _app("web", path="manifests/api"),
        },
        "duplicate-application-name",
    ),
    Case(
        "malformed-ignore-diff-pointer",
        {"apps/web.yaml": _app("web", ignore_diff=_ignore_diff("jsonPointers", "spec/replicas"))},
        "malformed-ignore-diff-pointer",
    ),
    Case(
        "malformed-ignore-diff-jq-expression",
        {
            "apps/web.yaml": _app(
                "web", ignore_diff=_ignore_diff("jqPathExpressions", "spec.replicas")
            )
        },
        "malformed-ignore-diff-jq-expression",
    ),
    Case(
        "unknown-sync-option",
        {"apps/web.yaml": _app("web", sync_options=("createnamespace=true",))},
        "unknown-sync-option",
    ),
    Case("unknown-resource-hook", {"manifests/web/job.yaml": HOOK_JOB}, "unknown-resource-hook"),
    Case(
        "malformed-sync-wave",
        {
            "manifests/web/service.yaml": SERVICE.replace(
                "metadata:\n  name: web\n",
                "metadata:\n  name: web\n  annotations:\n"
                '    argocd.argoproj.io/sync-wave: "first"\n',
            )
        },
        "malformed-sync-wave",
    ),
    Case(
        "floating-target-revision",
        {
            "apps/ext-chart.yaml": _app(
                "ext-chart", repo=CHART_REPO, chart="nginx", revision="1.2.*"
            )
        },
        "floating-target-revision",
    ),
]

# Each one guards a false positive fixed after 1.2.2: keep them even if they
# look redundant with the rules' unit tests.
NEGATIVE_CASES = [
    Case("clean base"),
    # 2469b56: the same file deployed to two namespaces is two resources.
    Case(
        "applicationset fan-out to two namespaces",
        {
            "manifests/fanout/cm.yaml": _configmap("fanout"),
            "apps/fanout-set.yaml": _list_appset(
                "fanout", "argocd", [("fan-a", "ns-a"), ("fan-b", "ns-b")], "manifests/fanout"
            ),
        },
    ),
    # 2469b56: generated Applications live in their ApplicationSet's namespace.
    Case(
        "two applicationsets in different namespaces generating the same name",
        {
            "manifests/svc/cm.yaml": _configmap("svc"),
            "apps/set-1.yaml": _list_appset(
                "set-1", "argocd", [("svc-one", "ns-a")], "manifests/svc"
            ),
            "apps/set-2.yaml": _list_appset(
                "set-2", "argocd-2", [("svc-one", "ns-b")], "manifests/svc"
            ),
        },
    ),
    # 8def70d: a name the API server would refuse can never collide.
    Case(
        "two applications named with an unfilled placeholder",
        {
            "manifests/tpl-1/cm.yaml": _configmap("tpl-1"),
            "apps/tpl-1.yaml": _app("<CLUSTER_NAME>-app", path="manifests/tpl-1"),
            "manifests/tpl-2/cm.yaml": _configmap("tpl-2"),
            "apps/tpl-2.yaml": _app("<CLUSTER_NAME>-app", path="manifests/tpl-2"),
        },
    ),
    # 8def70d: same-named AppProjects used to be checked against the last one read.
    Case(
        "two same-named appprojects in two folders",
        {
            "projects/a/shared.yaml": _project("shared", ["team-ns"]),
            "projects/b/shared.yaml": _project("shared", ["ns-a"]),
            "manifests/proj/cm.yaml": _configmap("proj"),
            "apps/proj-app.yaml": _app("proj-app", path="manifests/proj", project="shared"),
        },
    ),
]


def _lint(git_repo: Callable[..., Path], case: Case) -> tuple[int, list[dict]]:
    repo_root = git_repo({**_base_files(), **case.files}, origin_url=ORIGIN)
    # Every Application targets `main`; `git init`'s default branch name
    # depends on the machine's git config.
    for args in [["branch", "-M", "main"], *case.git_after]:
        subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)

    result = runner.invoke(app, [str(repo_root), "--format", "json", "--fail-on-empty"])
    # Warnings go to stderr; stdout must be the JSON report alone.
    return result.exit_code, json.loads(result.stdout)["findings"]


def _signatures(findings: list[dict]) -> set[str]:
    return {f"{f['rule_id']}:{f['severity']}" for f in findings}


def test_every_registered_rule_has_a_positive_case():
    registered = {rule.rule_id for rule in RULES} | {applicationset.RULE_ID}
    assert {case.rule for case in POSITIVE_CASES} == registered == set(DOCUMENTED_SEVERITIES)


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=lambda case: case.name)
def test_injected_defect_fires_its_rule_and_no_other_blocking_rule(git_repo, case):
    exit_code, findings = _lint(git_repo, case)
    severity = DOCUMENTED_SEVERITIES[case.rule]

    own = [f for f in findings if f["rule_id"] == case.rule]
    assert own, f"{case.rule} did not fire"
    assert {f["severity"] for f in own} == {severity}
    other_blocking = [
        f for f in findings if f["severity"] in BLOCKING and f["rule_id"] != case.rule
    ]
    assert not other_blocking, _signatures(other_blocking)
    assert exit_code == (1 if severity in ("error", "unverifiable") else 0)


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=lambda case: case.name)
def test_legitimate_setup_produces_nothing_blocking(git_repo, case):
    exit_code, findings = _lint(git_repo, case)

    blocking = [f for f in findings if f["severity"] in BLOCKING]
    assert not blocking, _signatures(blocking)
    assert exit_code == 0
