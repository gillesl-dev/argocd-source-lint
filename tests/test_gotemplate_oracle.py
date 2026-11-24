"""Go template rendering compared with Argo CD's own Render.Replace.

The cases are in fixtures/gotemplate_render_cases.yaml. The expected results in
fixtures/gotemplate_render_expected.json come from tools/argocd-oracle, which
calls Argo CD's applicationset/utils Render.Replace (see CONTRIBUTING.md).

Rendering must give Argo CD's output, fail where Argo CD fails, or refuse:
`<no value>` or a printf error such as `%!d(float64=1)` in Argo CD's output,
a case marked `unsupported`, or a function this tool does not implement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from argocd_source_lint import gotemplate
from argocd_source_lint.gotemplate import (
    ArgoTemplateError,
    NoValueError,
    UnsupportedFunctionError,
    UnsupportedTemplateError,
)

FIXTURES = Path(__file__).parent / "fixtures"
DATA = YAML(typ="safe").load(
    (FIXTURES / "gotemplate_render_cases.yaml").read_text(encoding="utf-8")
)
CASES: list[dict[str, Any]] = DATA["cases"]
EXPECTED = json.loads((FIXTURES / "gotemplate_render_expected.json").read_text(encoding="utf-8"))


def _as_json(value: Any) -> Any:
    """Params reach the controller as JSON: numbers are float64."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_as_json(item) for item in value]
    return {str(key): _as_json(item) for key, item in value.items()}


def _params(case: dict[str, Any]) -> dict[str, Any]:
    return _as_json(DATA["params"][case.get("params", "default")])


def _outcome(case: dict[str, Any]) -> tuple[str, Any]:
    try:
        return "output", gotemplate.render(
            case["template"], _params(case), case.get("options") or []
        )
    except NoValueError:
        return "no value", None
    except UnsupportedFunctionError as exc:
        return "unsupported function", exc.name
    except UnsupportedTemplateError as exc:
        return "unsupported", str(exc)
    except ArgoTemplateError as exc:
        return "error", str(exc)


def test_every_case_has_an_expected_result():
    renders = EXPECTED["renders"]
    assert len(renders) == len(CASES)
    for case, expected in zip(CASES, renders, strict=True):
        assert expected["template"] == case["template"]
        assert expected["options"] == (case.get("options") or [])
        assert expected["params"] == DATA["params"][case.get("params", "default")]


def test_known_functions_are_argo_cds():
    assert sorted(gotemplate.ARGO_CD_FUNCTIONS) == EXPECTED["functions"]


def test_supported_functions_are_the_planned_ones():
    planned = {
        "index", "cat", "slugify", "normalize", "base", "toJson", "mustToPrettyJson", "dig",
        "toYaml", "indent", "quote", "printf", "default", "lower", "upper", "replace",
        "trimPrefix", "trimSuffix", "eq", "ne", "and", "or", "not",
    }  # fmt: skip
    assert gotemplate.SUPPORTED_FUNCTIONS == planned
    assert planned <= gotemplate.ARGO_CD_FUNCTIONS


@pytest.mark.parametrize(
    "index", range(len(CASES)), ids=[f"{i}:{case['template']!r}" for i, case in enumerate(CASES)]
)
def test_render_matches_argo_cd(index):
    case = CASES[index]
    expected = EXPECTED["renders"][index]
    kind, detail = _outcome(case)

    if kind == "unsupported function":
        # Allowed until the function is implemented; then the case must match.
        assert detail not in gotemplate.SUPPORTED_FUNCTIONS
        assert "unsupported" not in case
        return
    if "unsupported" in case:
        assert kind == "unsupported", detail
    elif "error" in expected:
        assert kind == "error", (detail, expected["error"])
    elif "<no value>" in expected["output"]:
        assert kind == "no value", detail
    elif "%!" in expected["output"]:
        # fmt's text for a bad verb or argument: rendered, but not a value.
        assert kind == "unsupported", detail
    else:
        assert (kind, detail) == ("output", expected["output"])
