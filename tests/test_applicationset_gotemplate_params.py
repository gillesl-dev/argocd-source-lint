"""goTemplate param sets compared with Argo CD's generators.Transform.

The goTemplate cases of fixtures/appset_oracle_cases.yaml; the oracle records,
in fixtures/appset_oracle_expected.json, the param sets of each top-level
generator. They are compared as unordered lists, numbers as float64.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from argocd_source_lint import applicationset, appset_go_params
from argocd_source_lint.appset_go_params import UnsupportedParamsError
from argocd_source_lint.appset_params import ParamsError
from argocd_source_lint.fsutil import load_yaml_documents
from argocd_source_lint.git_context import get_origin_url
from argocd_source_lint.models import Severity

FIXTURES = Path(__file__).parent / "fixtures"
CASES = [
    case
    for case in YAML(typ="safe").load(
        (FIXTURES / "appset_oracle_cases.yaml").read_text(encoding="utf-8")
    )
    if "goTemplate: true" in case["appset"]
]
EXPECTED = json.loads((FIXTURES / "appset_oracle_expected.json").read_text(encoding="utf-8"))


def _normalized(value: Any) -> Any:
    """JSON values with every number as a float, lists of param sets sorted."""
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return value


def _sorted_sets(sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(_normalized(sets), key=lambda params: json.dumps(params, sort_keys=True))


def _param_sets(repo_root: Path) -> list[list[dict[str, Any]]]:
    path = repo_root / "bootstrap" / "appset.yaml"
    doc = load_yaml_documents(path)[0]
    spec = doc["spec"]
    ctx = applicationset.GeneratorContext(
        repo_root=repo_root,
        local_origin=get_origin_url(repo_root),
        appset_name="s",
        source_file=Path("bootstrap/appset.yaml"),
        severity=Severity.INFO,
        new_git_file_globbing=False,
        go_template=True,
        go_template_options=tuple(spec.get("goTemplateOptions") or ()),
        source_lines=applicationset._source_lines(path),
    )
    sets = []
    for generator in spec["generators"]:
        params, findings = applicationset._resolve_generator(generator, ctx)
        assert findings == []
        sets.append(json.loads(json.dumps(params)))
    return sets


def test_cases_exist():
    assert len(CASES) >= 15


def _element(text: str) -> dict[str, Any]:
    # Read as manifests are (fsutil: ruamel, YAML 1.2).
    return YAML(typ="rt").load(text)


@pytest.mark.parametrize(
    "text",
    ["a: 0", "a: 00", "a: 42", "a: -3", "a: 1_000", "a: 0x1F", 'a: ""', "a: 'x'", "a: 1.5"],
)
def test_elements_read_alike_in_yaml_11_are_kept(text):
    assert appset_go_params.list_param_sets([_element(text)], None) != []


@pytest.mark.parametrize(
    "text",
    ["a: 0755", "a: -012", "a: yes", "a: on", "y: 1", "a: 2024-01-02", "a: [no]", "a: {n: 1}"],
)
def test_elements_read_differently_in_yaml_11_are_refused(text):
    with pytest.raises(UnsupportedParamsError, match="YAML 1.1"):
        appset_go_params.list_param_sets([_element(text)], None, text.split("\n"))


@pytest.mark.parametrize(
    "text",
    ["a: '1'", 'a: "true"', "a: 'yes'", "'y': 1", 'a: ["no", "0755"]', "a: {'n': 'off'}"],
)
def test_quoted_strings_are_the_same_in_yaml_11(text):
    lines = text.split("\n")
    assert appset_go_params.list_param_sets([_element(text)], None, lines) != []
    # Without the manifest's text, the quotes can't be told: refused.
    with pytest.raises(UnsupportedParamsError, match="YAML 1.1"):
        appset_go_params.list_param_sets([_element(text)], None)


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_params_match_argo_cd(git_repo, case):
    repo_root = git_repo({**(case.get("files") or {}), "bootstrap/appset.yaml": case["appset"]})
    expected = EXPECTED[case["name"]]

    if case.get("unsupported_params"):
        with pytest.raises(UnsupportedParamsError):
            _param_sets(repo_root)
    elif expected.get("paramsError"):
        # Argo CD fails, or panics, before rendering anything.
        with pytest.raises(ParamsError):
            _param_sets(repo_root)
    elif "<no value>" in json.dumps(expected["params"]):
        # Argo CD renders `<no value>` into a param; this tool doesn't.
        with pytest.raises(UnsupportedParamsError):
            _param_sets(repo_root)
    else:
        actual = _param_sets(repo_root)
        assert [_sorted_sets(sets) for sets in actual] == [
            _sorted_sets(sets) for sets in expected["params"]
        ]
