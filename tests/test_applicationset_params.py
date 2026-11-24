"""Generator parameters must match what the Argo CD controller computes.

The expected values come from Argo CD v3.3.3's own code (SanitizeName,
generateParamsFromGitFile, ListGenerator, CombineStringMaps) run with its
library versions: sigs.k8s.io/yaml v1.6.0, jeremywohl/flatten, Go's fmt.
"""

from __future__ import annotations

import textwrap

import pytest

from argocd_source_lint import applicationset, appset_params
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity


def _discover(repo_root):
    return applicationset.discover(repo_root, get_origin_url(repo_root), Severity.INFO)


def _appset(generators: str, *, name: str = "app-x", path: str = "manifests") -> str:
    return (
        "apiVersion: argoproj.io/v1alpha1\n"
        "kind: ApplicationSet\n"
        "metadata:\n"
        "  name: params-appset\n"
        "spec:\n"
        "  generators:\n" + textwrap.indent(textwrap.dedent(generators).strip("\n"), "    ") + "\n"
        "  template:\n"
        "    metadata:\n"
        f"      name: '{name}'\n"
        "    spec:\n"
        "      source:\n"
        "        repoURL: https://example.invalid/repo.git\n"
        "        targetRevision: HEAD\n"
        f"        path: '{path}'\n"
    )


_GIT_FILES = """\
- git:
    repoURL: https://example.invalid/repo.git
    revision: HEAD
    files:
      - path: {pattern}
"""


# ---- Values computed by Argo CD ----


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("my.app", "my.app"),
        ("UPPER.case", "upper.case"),
        ("My_App", "my-app"),
        ("config.yaml", "config.yaml"),
        ("...", ""),
        ("", ""),
        (".", ""),
        ("-a-", "a"),
        ("Éte", "te"),
        ("a" * 300, "a" * 253),
    ],
)
def test_sanitize_name_matches_argo_cd(name, expected):
    assert appset_params.sanitize_name(name) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (None, "<nil>"),
        (8080.0, "8080"),
        (1.0, "1"),
        (0.5, "0.5"),
        (-3.0, "-3"),
        (-0.0, "-0"),
        (100000.0, "100000"),
        (1000000.0, "1e+06"),
        (123456789.0, "1.23456789e+08"),
        (0.0001, "0.0001"),
        (0.00001, "1e-05"),
        (1.5e300, "1.5e+300"),
        (9007199254740993.0, "9.007199254740992e+15"),
        ({"k": "v", "a": [1.0, 2.0]}, "map[a:[1 2] k:v]"),
    ],
)
def test_go_format_matches_fmt_sprintf_v(value, expected):
    assert appset_params.go_format(value) == expected


@pytest.mark.parametrize(
    ("scalar", "expected"),
    [
        ("y", "true"),
        ("yes", "true"),
        ("On", "true"),
        ("n", "false"),
        ("OFF", "false"),
        ("~", "<nil>"),
        ("0755", "493"),
        ("0o17", "15"),
        ("0x1F", "31"),
        ("08", "8"),
        ("1_000", "1000"),
        ("+12", "12"),
        ("+.5", "0.5"),
        ("1e3", "1000"),
        ("1:20", "1:20"),
        ("2001-12-14", "2001-12-14"),
        ("1e400", "1e400"),
        ("'yes'", "yes"),
        ("<<", "<<"),
        ("=", "="),
    ],
)
def test_git_file_scalars_are_read_as_yaml_1_1_then_json(scalar, expected):
    (params,) = appset_params.git_file_param_sets("d/f.yaml", f"v: {scalar}\n")
    assert params["v"] == expected


def test_git_file_params_flatten_lists_by_index_and_drop_empty_containers():
    (params,) = appset_params.git_file_param_sets(
        "d/f.yaml", "tags: [a, {b: c}]\nempty_list: []\nempty_map: {}\n"
    )
    assert {k: v for k, v in params.items() if not k.startswith("path")} == {
        "tags.0": "a",
        "tags.1.b": "c",
    }


def test_git_file_at_the_repository_root_has_path_dot():
    (params,) = appset_params.git_file_param_sets("config.json", "{}")
    assert params == {
        "path": ".",
        "path.basename": ".",
        "path.basenameNormalized": "",
        "path[0]": ".",
        "path.filename": "config.json",
        "path.filenameNormalized": "config.json",
    }


@pytest.mark.parametrize(
    ("content", "count"),
    [("", 1), ("# only a comment\n", 1), ("null\n", 1), ("{}\n", 1), ("[]\n", 0),
     ("- a: 1\n- null\n", 2), ("---\na: 1\n---\nb: [\n", 1), ("a: 1\na: 2\n", 1)],
)  # fmt: skip
def test_git_file_documents_give_the_same_number_of_param_sets(content, count):
    assert len(appset_params.git_file_param_sets("d/f.yaml", content)) == count


@pytest.mark.parametrize(
    "content",
    ["just-a-string\n", "a: [\n", "- a: 1\n- x\n", "v: .inf\n", "? [1, 2]\n: v\n",
     "null: v\n", "%YAML 1.2\n---\na: 1\n"],
)  # fmt: skip
def test_git_file_argo_cd_cannot_read_is_an_error(content):
    with pytest.raises(appset_params.ParamsError):
        appset_params.git_file_param_sets("d/f.yaml", content)


def test_path_param_prefix_renames_the_path_params():
    (params,) = appset_params.git_file_param_sets("envs/web/config.json", "{}", "target")
    assert params == {
        "target.path": "envs/web",
        "target.path.basename": "web",
        "target.path.basenameNormalized": "web",
        "target.path[0]": "envs",
        "target.path[1]": "web",
        "target.path.filename": "config.json",
        "target.path.filenameNormalized": "config.json",
    }


def test_git_generator_values_are_rendered_with_its_params():
    params = appset_params.directory_params(
        "apps/web",
        values={"a": "{{path.basename}}-x", "b": "{{ missing }}", "c": "{{ path }}/{{path}}"},
    )
    assert params["values.a"] == "web-x"
    assert params["values.b"] == "{{ missing }}"
    assert params["values.c"] == "apps/web/apps/web"


def test_combine_string_maps_rejects_a_key_set_to_two_values():
    assert appset_params.combine_string_maps({"x": "1", "y": "2"}, {"x": "1", "z": "3"}) == {
        "x": "1",
        "y": "2",
        "z": "3",
    }
    with pytest.raises(appset_params.ParamsError):
        appset_params.combine_string_maps({"x": "1"}, {"x": "2"})


# ---- The same values, through ApplicationSet expansion ----


def test_normalized_names_keep_dots(git_repo):
    repo_root = git_repo(
        {
            "apps/my.app/config.json": "{}",
            "bootstrap/appset.yaml": _appset(
                _GIT_FILES.format(pattern="apps/*/config.json"),
                name="{{path.basenameNormalized}}-{{path.filenameNormalized}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert [app.name for app in applications] == ["my.app-config.json"]


def test_git_file_values_are_formatted_as_argo_cd_does(git_repo):
    repo_root = git_repo(
        {
            "envs/dev.yaml": "enabled: yes\nreplicas: 1000000\nratio: 1.0\n",
            "bootstrap/appset.yaml": _appset(
                _GIT_FILES.format(pattern="envs/*.yaml"),
                path="manifests/{{enabled}}-{{replicas}}-{{ratio}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert applications[0].sources[0].path == "manifests/true-1e+06-1"


def test_git_file_holding_a_list_of_objects_generates_one_application_each(git_repo):
    repo_root = git_repo(
        {
            "envs/all.yaml": "- env: dev\n- env: prod\n",
            "bootstrap/appset.yaml": _appset(
                _GIT_FILES.format(pattern="envs/*.yaml"),
                name="app-{{env}}",
                path="manifests/{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert sorted(app.name for app in applications) == ["app-dev", "app-prod"]


def test_empty_git_file_still_generates_an_application(git_repo):
    repo_root = git_repo(
        {
            "envs/dev/config.yaml": "",
            "bootstrap/appset.yaml": _appset(
                _GIT_FILES.format(pattern="envs/*/config.yaml"),
                name="app-{{path.basename}}",
                path="{{path}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert [app.sources[0].path for app in applications] == ["envs/dev"]


def test_git_file_argo_cd_cannot_read_fails_the_whole_application_set(git_repo):
    repo_root = git_repo(
        {
            "envs/dev.yaml": "env: dev\n",
            "envs/broken.yaml": "just-a-string\n",
            "bootstrap/appset.yaml": _appset(
                _GIT_FILES.format(pattern="envs/*.yaml")
                + "- list:\n    elements:\n      - env: extra\n",
                name="app-{{env}}",
                path="manifests/{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "envs/broken.yaml" in findings[0].message
    assert findings[0].unexpanded_paths == ("manifests",)


def test_list_element_with_a_non_string_value_fails_the_application_set(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                "- list:\n    elements:\n      - env: dev\n        replicas: 3\n",
                name="app-{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "`replicas`" in findings[0].message


def test_list_element_values_map_gives_values_params(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                "- list:\n    elements:\n      - env: dev\n        values:\n          dir: d1\n",
                name="app-{{env}}",
                path="manifests/{{values.dir}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert applications[0].sources[0].path == "manifests/d1"


def test_elements_yaml_nested_values_are_not_flattened(git_repo):
    """Argo CD keeps `elementsYaml` entries as they are: `{{cluster.name}}`
    gets no value, unlike in a git files generator."""
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                "- list:\n    elementsYaml: |\n      - cluster:\n          name: prod\n",
                path="manifests/{{cluster.name}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "`{{cluster.name}}`" in findings[0].message


def test_matrix_children_setting_one_key_to_two_values_fail_the_application_set(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                """\
                - matrix:
                    generators:
                      - list:
                          elements:
                            - env: dev
                      - list:
                          elements:
                            - env: prod
                """,
                name="app-{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1
    assert "`env`" in findings[0].message


_TWO_GITS = """\
- matrix:
    generators:
      - git:
          repoURL: https://example.invalid/repo.git
          revision: HEAD
          directories:
            - path: apps/*
      - git:
          repoURL: https://example.invalid/repo.git
          revision: HEAD
{prefix}          files:
            - path: 'envs/{{{{path.basename}}}}/config.json'
"""


def test_matrix_of_two_git_generators_needs_a_path_param_prefix(git_repo):
    files = {"apps/web/cm.yaml": "kind: ConfigMap\n", "envs/web/config.json": '{"env": "prod"}'}
    without = git_repo(
        {**files, "bootstrap/appset.yaml": _appset(_TWO_GITS.format(prefix=""), name="a-{{env}}")}
    )

    applications, findings = _discover(without)

    assert applications == []
    assert "`path`" in findings[0].message


def test_matrix_of_two_git_generators_with_a_path_param_prefix(git_repo):
    repo_root = git_repo(
        {
            "apps/web/cm.yaml": "kind: ConfigMap\n",
            "envs/web/config.json": '{"env": "prod"}',
            "bootstrap/appset.yaml": _appset(
                _TWO_GITS.format(prefix="          pathParamPrefix: target\n"),
                name="{{path.basename}}-{{env}}",
                path="{{target.path}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert [(app.name, app.sources[0].path) for app in applications] == [("web-prod", "envs/web")]


def test_merge_generator_with_a_single_child_fails_the_application_set(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                """\
                - merge:
                    mergeKeys: [env]
                    generators:
                      - list:
                          elements:
                            - env: dev
                """,
                name="app-{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1


def test_merge_generator_with_a_repeated_merge_key_fails_the_application_set(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                """\
                - merge:
                    mergeKeys: [env]
                    generators:
                      - list:
                          elements:
                            - env: dev
                            - env: dev
                      - list:
                          elements:
                            - env: dev
                """,
                name="app-{{env}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert applications == []
    assert len(findings) == 1


def test_merge_generator_tells_a_missing_key_from_an_empty_one(git_repo):
    repo_root = git_repo(
        {
            "bootstrap/appset.yaml": _appset(
                """\
                - merge:
                    mergeKeys: [env]
                    generators:
                      - list:
                          elements:
                            - name: base
                              env: ''
                      - list:
                          elements:
                            - name: override
                """,
                name="app-{{name}}",
            ),
        }
    )

    applications, findings = _discover(repo_root)

    assert findings == []
    assert [app.name for app in applications] == ["app-base"]
