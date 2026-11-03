"""Valid ArgoCD setups and the result ArgoCD would give, checked end to end
through the CLI's JSON report."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from typer.testing import CliRunner

from argocd_source_lint import applicationset
from argocd_source_lint.cli import app
from argocd_source_lint.models import Severity
from argocd_source_lint.rules.floating_target_revision import is_constraint

runner = CliRunner()

ORIGIN = "https://example.invalid/repo.git"
CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cfg\n"
CHART = "apiVersion: v2\nname: web\nversion: 0.1.0\n"
DESTINATION = """\
  destination:
    server: https://kubernetes.default.svc
    namespace: web
"""


def _app(source: str, name: str = "web") -> str:
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
spec:
  source:
    repoURL: {ORIGIN}
{source}{DESTINATION}"""


def _appset(generators: str, path: str, name: str = "app-{{path.basename}}") -> str:
    return f"""\
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: set
spec:
  generators:
{generators}  template:
    metadata:
      name: '{name}'
    spec:
      source:
        repoURL: {ORIGIN}
        targetRevision: HEAD
        path: '{path}'
      destination:
        server: https://kubernetes.default.svc
        namespace: web
"""


def _lint(repo_root) -> tuple[int, list[tuple[str, str]], str]:
    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    findings = [
        (f["rule_id"], f["severity"])
        for f in json.loads(result.stdout)["findings"]
        if f["severity"] != "info"
    ]
    return result.exit_code, findings, " ".join(result.stderr.split())


def test_source_path_starting_with_dot_slash_is_found(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: ./manifests/web\n"),
            "manifests/web/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[:2] == (0, [])


def test_non_ascii_path_at_another_revision_is_found(git_repo, git_tag, git_commit):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app("    targetRevision: v1\n    path: manifests/café\n"),
            "manifests/café/cm.yaml": CM,
        }
    )
    git_tag(repo_root, "v1")
    git_commit(repo_root, {"README.md": "x\n"})

    assert _lint(repo_root)[:2] == (0, [])


def test_invalid_document_keeps_the_ones_before_it_and_is_reported(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: nowhere\n")
            + "---\nfoo: [unclosed\n"
        }
    )

    exit_code, findings, stderr = _lint(repo_root)

    assert findings == [("phantom-target", "error")]
    assert "`apps/web.yaml` is not valid YAML" in stderr


def test_duplicate_key_in_an_application_is_reported(git_repo):
    repo_root = git_repo(
        {"apps/web.yaml": _app("    targetRevision: HEAD\n    path: a\n    path: b\n")}
    )

    assert "`apps/web.yaml` is not valid YAML" in _lint(repo_root)[2]


def test_helm_template_with_braces_is_not_reported_as_invalid(git_repo):
    repo_root = git_repo(
        {
            "charts/web/templates/app.yaml": "apiVersion: argoproj.io/v1alpha1\n"
            "kind: Application\nmetadata:\n  name: {{ .Values.name }}\n"
        }
    )

    assert "not valid YAML" not in _lint(repo_root)[2]


@pytest.mark.parametrize(
    ("value_files", "expected"),
    [
        ("values-*.yaml", []),
        ("envs/**/*.yaml", []),
        ("values-[ab].yaml", []),
        ("nothing-*.yaml", [("broken-values-ref", "error")]),
    ],
)
def test_value_file_globs_are_matched_against_the_chart(git_repo, value_files, expected):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: charts/web\n    helm:\n"
                f"      valueFiles:\n        - '{value_files}'\n"
            ),
            "charts/web/Chart.yaml": CHART,
            "charts/web/values-prod.yaml": "a: 1\n",
            "charts/web/envs/prod.yaml": "b: 2\n",
        }
    )

    assert _lint(repo_root)[1] == expected


def test_value_file_glob_covers_its_files_for_orphan_source(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - envs/\n",
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: charts/web\n    helm:\n"
                "      valueFiles:\n        - '../../envs/*/values.yaml'\n"
            ),
            "charts/web/Chart.yaml": CHART,
            "envs/prod/values.yaml": "a: 1\n",
        }
    )

    assert _lint(repo_root)[1] == []


def test_applicationset_path_segments_are_expanded(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                "        directories:\n          - path: apps/*\n",
                "apps/{{path[1]}}",
            ),
            "apps/web/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == []


def test_generator_level_template_overrides_the_spec_template(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                "    - list:\n        elements:\n          - env: dev\n"
                "        template:\n          metadata: {}\n          spec:\n"
                "            source:\n              path: missing\n",
                "apps/web",
                name="app-{{env}}",
            ),
            "apps/web/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == [("phantom-target", "error")]


def test_placeholder_without_a_value_is_unresolvable_not_a_missing_path(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                "    - list:\n        elements:\n          - env: dev\n",
                "apps/{{region}}",
                name="app-{{env}}",
            ),
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    findings = json.loads(result.stdout)["findings"]

    assert [f["rule_id"] for f in findings] == ["unresolvable-generator"]
    assert "{{region}}" in findings[0]["message"]


def test_matrix_second_generator_can_use_the_first_ones_parameters(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                "    - matrix:\n        generators:\n"
                f"          - git:\n              repoURL: {ORIGIN}\n              revision: HEAD\n"
                "              directories:\n                - path: apps/*\n"
                f"          - git:\n              repoURL: {ORIGIN}\n              revision: HEAD\n"
                "              files:\n"
                "                - path: 'envs/{{path.basename}}/config.json'\n",
                "missing/{{env}}",
            ),
            "apps/web/cm.yaml": CM,
            "envs/web/config.json": '{"env": "prod"}\n',
        }
    )

    assert _lint(repo_root)[1] == [("phantom-target", "error")]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ("rules: [a]\n", "`rules` must map rule IDs to severities"),
        ("rules:\n  orphan-sources: warning\n", "unknown rule(s) in `rules`: orphan-sources"),
    ],
)
def test_invalid_rules_setting_exits_2(git_repo, config, message):
    repo_root = git_repo({".argocd-lint.yaml": config, "manifests/.gitkeep": ""})

    result = runner.invoke(app, [str(repo_root)])

    assert result.exit_code == 2
    assert message in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("    chart: web\n    targetRevision: 1.2.x\n", True),
        ("    chart: web\n    targetRevision: 1.2 - 1.4\n", True),
        ("    chart: web\n    targetRevision: 1.2.3\n", False),
        # A Git branch may be named `1.x`: left alone.
        ("    targetRevision: 1.x\n    path: manifests\n", False),
    ],
)
def test_chart_version_ranges_in_masterminds_syntax(git_repo, source, flagged):
    repo_root = git_repo(
        {"apps/web.yaml": _app(source).replace(ORIGIN, "https://charts.example.invalid")}
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    rules = [f["rule_id"] for f in json.loads(result.stdout)["findings"]]

    assert ("floating-target-revision" in rules) is flagged


def test_staged_files_are_seen(git_repo):
    repo_root = git_repo({"README.md": "x\n"})
    (repo_root / "apps").mkdir()
    (repo_root / "apps/web.yaml").write_text(
        _app("    targetRevision: HEAD\n    path: manifests/web\n"), encoding="utf-8"
    )
    (repo_root / "manifests/web").mkdir(parents=True)
    (repo_root / "manifests/web/cm.yaml").write_text(CM, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)

    assert _lint(repo_root)[:2] == (0, [])


@pytest.mark.parametrize(
    ("value_file", "expected"),
    [
        ("values-$ARGOCD_APP_NAME.yaml", []),
        ("values-${ARGOCD_APP_NAME}.yaml", []),
        ("$ARGOCD_APP_NAME/values.yaml", []),
        # The resolved commit isn't known here, so the entry isn't checked.
        ("values-$ARGOCD_APP_REVISION.yaml", []),
        ("values-$ARGOCD_APP_PROJECT_NAME.yaml", [("broken-values-ref", "error")]),
        ("$typo/values.yaml", [("broken-values-ref", "error")]),
    ],
)
def test_value_files_use_argo_cd_build_variables(git_repo, value_file, expected):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: charts/web\n    helm:\n"
                f"      valueFiles:\n        - '{value_file}'\n"
            ),
            "charts/web/Chart.yaml": CHART,
            "charts/web/values-web.yaml": "a: 1\n",
            "charts/web/web/values.yaml": "a: 1\n",
        }
    )

    assert _lint(repo_root)[1] == expected


@pytest.mark.parametrize(
    "source_type",
    [
        "    plugin:\n      name: cmp\n",
        # An explicit `directory` wins over a kustomization.yaml in the folder.
        "    directory:\n      recurse: true\n",
    ],
)
def test_explicit_source_type_covers_the_whole_folder(git_repo, source_type):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app(f"    targetRevision: HEAD\n    path: manifests\n{source_type}"),
            "manifests/kustomization.yaml": "resources:\n  - cm.yaml\n",
            "manifests/cm.yaml": CM,
            "manifests/sub/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == []


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        # Argo CD drops a block with no field set, so the folder stays Kustomize.
        ("    directory:\n      recurse: false\n", [("orphan-source", "error")]),
        ("    helm: {}\n", [("orphan-source", "error")]),
        ("    kustomize: {}\n", [("orphan-source", "error")]),
        # An include keeps it a plain, non-recursive directory.
        ("    directory:\n      include: '*.yaml'\n", [("orphan-source", "error")] * 2),
    ],
)
def test_empty_source_type_block_is_not_explicit(git_repo, source_type, expected):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app(f"    targetRevision: HEAD\n    path: manifests\n{source_type}"),
            "manifests/kustomization.yaml": "resources:\n  - cm.yaml\n  - base\n",
            "manifests/cm.yaml": CM,
            "manifests/base/kustomization.yaml": "resources:\n  - cm.yaml\n",
            "manifests/base/cm.yaml": CM,
            "manifests/unreferenced.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == expected


@pytest.mark.parametrize(
    ("include", "expected"),
    [
        ("*-{dev,prod}.yaml", []),
        ("{*-dev,*-qa}.yaml", [("orphan-source", "error")]),
    ],
)
def test_directory_include_braces_anywhere_in_the_pattern(git_repo, include, expected):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: manifests\n"
                f"    directory:\n      include: '{include}'\n"
            ),
            "manifests/a-dev.yaml": CM,
            "manifests/a-prod.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == expected


def test_orphans_an_unexpanded_applicationset_may_deploy_are_info(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - svc/\n  - other/\n",
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                "        directories:\n          - path: svc/*\n",
                "{{.path.path}}",
            ).replace("spec:\n  generators:", "spec:\n  goTemplate: true\n  generators:"),
            "svc/web/cm.yaml": CM,
            "other/cm.yaml": CM,
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    findings = {
        (f["file"], f["severity"])
        for f in json.loads(result.stdout)["findings"]
        if f["rule_id"] == "orphan-source"
    }

    assert findings == {("svc/web/cm.yaml", "info"), ("other/cm.yaml", "error")}


def test_list_generator_elements_yaml_is_expanded(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                "    - list:\n        elementsYaml: |\n          - env: dev\n",
                "missing/{{env}}",
                name="app-{{env}}",
            ),
        }
    )

    assert _lint(repo_root)[1] == [("phantom-target", "error")]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # `exclude: true` removes the matching files.
        (
            "          - path: envs/*/config.json\n"
            "          - path: envs/b/config.json\n            exclude: true\n",
            1,
        ),
        ("          - path: envs/*/config.json\n", 2),
    ],
)
def test_git_files_generator_exclude(git_repo, files, expected):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                f"        files:\n{files}",
                "missing",
            ),
            "envs/a/config.json": "{}\n",
            "envs/b/config.json": "{}\n",
        }
    )

    assert len(_lint(repo_root)[1]) == expected


def test_git_generator_with_directories_ignores_files(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                "        directories:\n          - path: apps/*\n"
                "        files:\n          - path: envs/*/config.json\n",
                "apps/web",
                # One Application per directory; the files would add a second `web`.
                name="web",
            ),
            "apps/web/cm.yaml": CM,
            "envs/a/config.json": "{}\n",
        }
    )

    assert _lint(repo_root)[1] == []


def test_template_spec_that_is_not_a_mapping_is_reported(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: ApplicationSet\n"
            "metadata:\n  name: set\nspec:\n  generators:\n    - list:\n"
            "        elements:\n          - a: b\n  template:\n    metadata:\n"
            "      name: x\n    spec: 5\n"
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])

    assert result.exit_code == 0
    assert [f["rule_id"] for f in json.loads(result.stdout)["findings"]] == [
        "unresolvable-generator"
    ]


@pytest.mark.parametrize(
    ("expression", "flagged"),
    [
        ('(.spec.template.spec.containers[] | select(.name == "a")).image', False),
        (".spec.replicas", False),
        ('getpath(["spec", "replicas"])', False),
        ("select(.a == 1) | .b", False),
        ("first(.spec.containers[])", False),
        ("/spec/replicas", True),
        ("spec.replicas", True),
        ("spec[0]", True),
    ],
)
def test_jq_path_expressions_that_argo_cd_accepts(git_repo, expression, flagged):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n").replace(
                "  destination:",
                "  ignoreDifferences:\n    - kind: Deployment\n      jqPathExpressions:\n"
                f"        - '{expression}'\n  destination:",
            ),
            "manifests/cm.yaml": CM,
        }
    )

    rules = [rule for rule, _ in _lint(repo_root)[1]]

    assert ("malformed-ignore-diff-jq-expression" in rules) is flagged


def test_kustomize_fields_that_name_local_files_cover_them(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n"),
            "manifests/kustomization.yaml": "resources:\n  - cm.yaml\n"
            "replacements:\n  - path: replacement.yaml\n"
            "transformers:\n  - labels.yaml\n"
            "configurations:\n  - config.yaml\n"
            "openapi:\n  path: schema.yaml\n"
            "helmCharts:\n  - name: db\n    valuesFile: db-values.yaml\n",
            "manifests/cm.yaml": CM,
            "manifests/replacement.yaml": "source: {kind: ConfigMap}\n",
            "manifests/labels.yaml": "apiVersion: builtin\nkind: LabelTransformer\n",
            "manifests/config.yaml": "nameReference: []\n",
            "manifests/schema.yaml": "definitions: {}\n",
            "manifests/db-values.yaml": "a: 1\n",
            "manifests/charts/db/Chart.yaml": CHART,
            "manifests/unused.yaml": CM,
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    orphans = [f["file"] for f in json.loads(result.stdout)["findings"]]

    assert orphans == ["manifests/unused.yaml"]


def test_git_directories_follow_go_path_match_and_skip_hidden_folders(git_repo):
    repo_root = git_repo(
        {
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                "        directories:\n          - path: svc/**\n",
                "{{path}}",
            ),
            "svc/web/cm.yaml": CM,
            "svc/web/deep/cm.yaml": CM,
            "svc/.hidden/cm.yaml": CM,
        }
    )

    generated, _ = applicationset.discover(repo_root, ORIGIN, Severity.INFO)

    assert [a.name for a in generated] == ["app-web"]


@pytest.mark.parametrize(
    ("source_repo", "flagged"),
    [
        # Argo CD keeps the scheme: an https pattern doesn't permit a git@ URL.
        ("https://example.invalid/*", True),
        ("git@example.invalid:*", False),
        ("git@example.invalid:repo.git", False),
    ],
)
def test_project_source_repos_keep_the_url_scheme(git_repo, source_repo, flagged):
    repo_root = git_repo(
        {
            "projects/team.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n"
            f"metadata:\n  name: team\nspec:\n  sourceRepos:\n    - '{source_repo}'\n"
            "  destinations:\n    - server: '*'\n      namespace: '*'\n",
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n")
            .replace(f"repoURL: {ORIGIN}", "repoURL: git@example.invalid:repo.git")
            .replace("spec:\n", "spec:\n  project: team\n", 1),
            "manifests/cm.yaml": CM,
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    rules = [f["rule_id"] for f in json.loads(result.stdout)["findings"]]

    assert ("project-scope-violation" in rules) is flagged


def test_project_destinations_accept_braces(git_repo):
    repo_root = git_repo(
        {
            "projects/team.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n"
            "metadata:\n  name: team\nspec:\n  sourceRepos:\n    - '*'\n"
            "  destinations:\n    - server: '*'\n      namespace: '{web,api}'\n",
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n").replace(
                "spec:\n", "spec:\n  project: team\n", 1
            ),
            "manifests/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == []


def test_brace_pattern_too_large_to_expand_keeps_the_files(git_repo):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: manifests\n"
                f"    directory:\n      include: '{'{a,b}' * 40}.yaml'\n"
            ),
            "manifests/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == []


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        # Argo CD tries every Git revision as a semver constraint first.
        ("    targetRevision: 1.0.0 || 2.0.0\n    path: manifests\n", True),
        ("    targetRevision: '~1.2'\n    path: manifests\n", True),
        # `=` and `<` are legal in a branch name.
        ("    targetRevision: feature/a=b\n    path: manifests\n", False),
        ("    targetRevision: '=1.2.3'\n    path: manifests\n", False),
        ("    chart: web\n    targetRevision: '=1.2.3'\n", False),
        # Masterminds can't build a version from `x64` or `1x`.
        ("    targetRevision: x64-build\n    path: manifests\n", False),
        ("    targetRevision: 1x-legacy\n    path: manifests\n", False),
        ("    targetRevision: '>=1.2 <1.3'\n    path: manifests\n", True),
    ],
)
def test_floating_revision_follows_masterminds_constraints(git_repo, source, flagged):
    repo_root = git_repo({"apps/web.yaml": _app(source), "manifests/cm.yaml": CM})

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    rules = [f["rule_id"] for f in json.loads(result.stdout)["findings"]]

    assert ("floating-target-revision" in rules) is flagged


@pytest.mark.parametrize("revision", ["1" * 22 + "g", "1  " * 22 + "!", "1,  " * 16 + "!"])
def test_constraint_check_stays_fast(revision):
    start = time.perf_counter()
    assert not is_constraint(revision)
    assert time.perf_counter() - start < 0.5


def test_project_destination_without_namespace_allows_none(git_repo):
    repo_root = git_repo(
        {
            "projects/team.yaml": "apiVersion: argoproj.io/v1alpha1\nkind: AppProject\n"
            "metadata:\n  name: team\nspec:\n  sourceRepos:\n    - '*'\n"
            "  destinations:\n    - server: https://kubernetes.default.svc\n",
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n").replace(
                "spec:\n", "spec:\n  project: team\n", 1
            ),
            "manifests/cm.yaml": CM,
        }
    )

    assert _lint(repo_root)[1] == [("project-scope-violation", "error")]


def test_directory_source_reads_json_and_skips_marked_files(git_repo):
    hook = '{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "j", '
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": "scan_roots:\n  - manifests/\n",
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: manifests\n"),
            "manifests/job.json": hook
            + '"annotations": {"argocd.argoproj.io/hook": "PreSnyc"}}}\n',
            # Argo CD doesn't apply it, and it is not an orphan either.
            "manifests/notes.yaml": "# +argocd:skip-file-rendering\n"
            "metadata:\n  annotations:\n    argocd.argoproj.io/hook: PreSnyc\n",
        }
    )

    result = runner.invoke(app, [str(repo_root), "-f", "json"])
    findings = [(f["rule_id"], f["file"]) for f in json.loads(result.stdout)["findings"]]

    assert findings == [("unknown-resource-hook", "apps/web.yaml")]


@pytest.mark.parametrize(
    ("config", "pattern", "expected"),
    [
        # `git ls-files`: `*` crosses `/`.
        ("", "config/*.yaml", ["app-config", "app-dev", "app-prod"]),
        # doublestar: `*` stays in one folder, `**` matches none too.
        ("new_git_file_globbing: true\n", "config/*.yaml", ["app-config"]),
        ("new_git_file_globbing: true\n", "config/**/top.yaml", ["app-config"]),
    ],
)
def test_git_files_generator_globbing_modes(git_repo, config, pattern, expected):
    repo_root = git_repo(
        {
            ".argocd-lint.yaml": config or "scan_roots: []\n",
            "appsets/set.yaml": _appset(
                f"    - git:\n        repoURL: {ORIGIN}\n        revision: HEAD\n"
                f"        files:\n          - path: '{pattern}'\n",
                "manifests",
            ),
            "config/top.yaml": "a: 1\n",
            "config/dev/a.yaml": "a: 1\n",
            "config/prod/a.yaml": "a: 1\n",
            "manifests/cm.yaml": CM,
        }
    )

    policy_globbing = "new_git_file_globbing" in config
    generated, _ = applicationset.discover(
        repo_root, ORIGIN, Severity.INFO, new_git_file_globbing=policy_globbing
    )

    assert sorted(a.name for a in generated) == expected


def _commit(repo_root, *args):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, *args], cwd=repo_root, check=True, capture_output=True)


def test_source_path_on_a_submodule_is_not_phantom(git_repo):
    repo_root = git_repo(
        {"apps/web.yaml": _app("    targetRevision: HEAD\n    path: vendor/lib\n")}
    )
    sha = "0123456789abcdef0123456789abcdef01234567"
    _commit(repo_root, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
    _commit(repo_root, "commit", "-qm", "submodule")
    (repo_root / "vendor" / "lib").mkdir(parents=True)

    assert _lint(repo_root)[1] == []


def test_source_path_on_a_symlink_is_not_phantom(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app("    targetRevision: HEAD\n    path: current\n"),
            "releases/v2/cm.yaml": CM,
        }
    )
    try:
        (repo_root / "current").symlink_to("releases/v2", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")
    _commit(repo_root, "add", "current")
    _commit(repo_root, "commit", "-qm", "link")

    assert _lint(repo_root)[1] == []


def test_sparse_checkout_keeps_the_files_it_leaves_out(git_repo):
    repo_root = git_repo(
        {
            "apps/web.yaml": _app(
                "    targetRevision: HEAD\n    path: charts/web\n"
                "    helm:\n      valueFiles:\n        - values.yaml\n"
            ),
            "charts/web/Chart.yaml": CHART,
            "charts/web/values.yaml": "a: 1\n",
            "charts/web/values dev.yaml": "a: 1\n",
        }
    )
    _commit(repo_root, "sparse-checkout", "set", "--cone", "apps")
    assert not (repo_root / "charts").exists()

    assert _lint(repo_root)[1] == []
