"""ApplicationSet params in goTemplate mode, as Argo CD computes them.

With `goTemplate: true`, generators keep their values as they are instead of
flattening them to strings: nested maps, lists, float64 numbers, booleans
(generators/list.go, git.go). The `matrix` generator renders its second child
as a Go template with each entry of the first, and both `matrix` and `merge`
combine entries with mergo.Merge(..., WithOverride) (dario.cat/mergo v1.0.2).
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml.scalarint import ScalarInt

from argocd_source_lint import gotemplate
from argocd_source_lint.appset_params import (
    LimitError,
    ParamsError,
    _as_json,
    _go_base,
    _go_dir,
    _go_resolve,
    load_go_yaml,
    sanitize_name,
)
from argocd_source_lint.gotemplate import GoStringMap


class UnsupportedParamsError(Exception):
    """This tool can't tell which params Argo CD computes."""


@dataclass(frozen=True)
class Rendering:
    """The goTemplateOptions of an ApplicationSet, and what its rendering may
    still spend."""

    options: tuple[str, ...] = ()
    budget: gotemplate.Budget = field(default_factory=gotemplate.Budget)


def render(text: str, params: dict[str, Any], rendering: Rendering) -> str:
    """A string rendered as Go template, with errors as params errors."""
    try:
        return gotemplate.render(text, params, rendering.options, rendering.budget)
    except gotemplate.TemplateLimitError as exc:
        raise LimitError(str(exc)) from exc
    except gotemplate.ArgoTemplateError as exc:
        raise ParamsError(f"rendering `{text}` fails: {exc}") from exc
    except gotemplate.UnsupportedTemplateError as exc:
        raise UnsupportedParamsError(f"rendering `{text}`: {exc}") from exc


# ---- list ----

_TIMESTAMP_PREFIX = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")


def _check_yaml11(value: Any, lines: Sequence[str] = ()) -> None:
    """Refuse what Kubernetes, which reads manifests as YAML 1.1, may read
    differently from this tool's YAML 1.2: `yes`, `y` or `on` (booleans),
    `0755` (octal), dates (timestamps), and such map keys. A string written
    in quotes is the same string in both; `lines`, the manifest's text, tells."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not (
                _same_in_yaml11(key) or _quoted(lines, value, "key", key)
            ):
                raise UnsupportedParamsError(
                    f"the list element key `{key}` may read differently in Kubernetes (YAML 1.1)"
                )
            _check_scalar_or_container(item, lines, value, "value", key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_scalar_or_container(item, lines, value, "item", index)


def _check_scalar_or_container(
    item: Any, lines: Sequence[str], container: Any, kind: str, where: Any
) -> None:
    if isinstance(item, (dict, list)):
        _check_yaml11(item, lines)
    elif isinstance(item, str):
        if not (_same_in_yaml11(item) or _quoted(lines, container, kind, where, item)):
            raise UnsupportedParamsError(
                f"the list element value `{item}` may read differently in Kubernetes (YAML 1.1)"
            )
    elif _leading_zero(item) or isinstance(item, (datetime.date, datetime.datetime)):
        # YAML 1.1 reads 0755 as octal, and a date as a timestamp.
        raise UnsupportedParamsError(
            f"the list element value `{item}` may read differently in Kubernetes (YAML 1.1)"
        )
    elif isinstance(item, float) and not math.isfinite(item):
        raise UnsupportedParamsError("a non-finite number in a list element")


def _quoted(
    lines: Sequence[str], container: Any, kind: str, where: Any, text: str | None = None
) -> bool:
    """Whether ruamel's position of a key, value or item points at a quoted
    scalar holding `text` (the key itself for keys). In doubt, False."""
    positions = getattr(container, "lc", None)
    if positions is None or not lines:
        return False
    try:
        line, column = getattr(positions, kind)(where)
        source = lines[line]
    except (AttributeError, IndexError, KeyError, TypeError):
        return False
    quote = source[column : column + 1]
    content = where if text is None else text
    return quote in ("'", '"') and source[column + 1 :].startswith(content + quote)


def _leading_zero(value: Any) -> bool:
    """A decimal integer written with leading zeros, such as 0755: ruamel
    keeps its written width; go-yaml reads it as octal. All zeros read 0 in both."""
    if type(value) is not ScalarInt or value == 0:
        return False
    width = getattr(value, "_width", None)
    return width is not None and width > len(str(abs(int(value))))


def _same_in_yaml11(text: str) -> bool:
    if text == "":
        # Only a quoted scalar is an empty string, in both versions.
        return True
    if _TIMESTAMP_PREFIX.match(text):
        return False
    try:
        resolved = _go_resolve(text)
    except ParamsError:
        return False
    return isinstance(resolved, str) and resolved == text


def check_manifest_elements(generator: Any, lines: Sequence[str]) -> None:
    """The YAML 1.1 check of every `list` element in a generator, nested ones
    included, as the manifest holds them: before a matrix renders them, which
    loses the positions that tell quoted scalars."""
    if isinstance(generator, dict):
        for key, value in generator.items():
            if key == "list" and isinstance(value, dict):
                for element in value.get("elements") or []:
                    if isinstance(element, dict):
                        _check_yaml11(element, lines)
            check_manifest_elements(value, lines)
    elif isinstance(generator, list):
        for item in generator:
            check_manifest_elements(item, lines)


def list_param_sets(
    elements: list[Any], elements_yaml: Any, lines: Sequence[str] | None = ()
) -> list[dict[str, Any]]:
    """ListGenerator.GenerateParams in goTemplate mode: each element as it is.
    `lines` is the text of the manifest the elements were read from, or None
    when check_manifest_elements already checked them."""
    param_sets: list[dict[str, Any]] = []
    for element in elements:
        if element is None:
            param_sets.append({})
            continue
        if not isinstance(element, dict):
            raise ParamsError("a list element is not an object")
        if lines is not None:
            _check_yaml11(element, lines)
        param_sets.append(_as_json(element))
    if elements_yaml:
        parsed = load_go_yaml(str(elements_yaml))
        if parsed is None:
            parsed = []
        if not isinstance(parsed, list) or not all(
            item is None or isinstance(item, dict) for item in parsed
        ):
            raise ParamsError("`elementsYaml` is not a list of objects")
        param_sets.extend(item or {} for item in parsed)
    return param_sets


# ---- git ----


def directory_params(
    rel_path: str, prefix: str, values: dict[str, Any] | None, rendering: Rendering
) -> dict[str, Any]:
    """generateParamsFromApps in generators/git.go, goTemplate mode."""
    path = {
        "path": rel_path,
        "basename": _go_base(rel_path),
        "basenameNormalized": sanitize_name(_go_base(rel_path)),
        "segments": rel_path.split("/"),
    }
    params: dict[str, Any] = {prefix: {"path": path}} if prefix else {"path": path}
    return _with_values(params, values, rendering)


def git_file_param_sets(
    rel_path: str,
    text: str,
    prefix: str,
    values: dict[str, Any] | None,
    rendering: Rendering,
) -> list[dict[str, Any]]:
    """generateParamsFromGitFile in generators/git.go, goTemplate mode: the
    file's objects as they are, then `path`, which replaces a `path` key."""
    data = load_go_yaml(text)
    if data is None or isinstance(data, dict):
        objects: list[Any] = [data or {}]
    elif isinstance(data, list) and all(item is None or isinstance(item, dict) for item in data):
        objects = [item or {} for item in data]
    else:
        raise ParamsError("the file is neither an object nor a list of objects")
    directory = _go_dir(rel_path)
    filename = _go_base(rel_path)
    path = {
        "path": directory,
        "basename": _go_base(directory),
        "filename": filename,
        "basenameNormalized": sanitize_name(_go_base(directory)),
        "filenameNormalized": sanitize_name(filename),
        "segments": directory.split("/"),
    }
    param_sets = []
    for obj in objects:
        params = dict(obj)
        if prefix:
            params[prefix] = {"path": dict(path)}
        else:
            params["path"] = dict(path)
        param_sets.append(_with_values(params, values, rendering))
    return param_sets


def _with_values(
    params: dict[str, Any], values: dict[str, Any] | None, rendering: Rendering
) -> dict[str, Any]:
    """appendTemplatedValues in generators/value_interpolation.go: `values`,
    each rendered with the other params, replaces any `values` key."""
    if not values:
        return params
    rendered = GoStringMap()
    for key, value in values.items():
        if not isinstance(value, str):
            raise ParamsError(f"git generator value `{key}` is not a string")
        rendered[str(key)] = render(value, params, rendering)
    return {**params, "values": rendered}


# ---- matrix and merge ----


# ---- Rendering the template and the generators ----
#
# deeplyReplace (applicationset/utils/utils.go) renders the strings of Argo
# CD's typed objects: string fields, map keys and values, and raw JSON fields,
# whose strings and keys are all rendered. The API server drops unknown
# fields, so they are never rendered. argocd_schema.json, written by
# tools/argocd-oracle/regenerate.py, describes these types.

_SCHEMA: dict[str, Any] = json.loads(
    (Path(__file__).parent / "argocd_schema.json").read_text(encoding="utf-8")
)


def render_template(template: Any, params: dict[str, Any], rendering: Rendering) -> Any:
    """An ApplicationSet template rendered with one param set."""
    return _render_known(template, _SCHEMA["template"], params, rendering)


def render_generator(generator: Any, params: dict[str, Any], rendering: Rendering) -> Any:
    """A child generator rendered with an entry of the matrix's first child
    (InterpolateGenerator)."""
    return _render_known(generator, _SCHEMA["generator"], params, rendering)


def _render_known(
    value: Any, schema: dict[str, Any], params: dict[str, Any], rendering: Rendering
) -> Any:
    node = _SCHEMA["definitions"][schema["ref"]] if "ref" in schema else schema
    kind = node["kind"]
    if kind == "struct":
        if not isinstance(value, dict):
            return value
        return {
            key: _render_known(item, node["fields"][key], params, rendering)
            for key, item in value.items()
            if isinstance(key, str) and key in node["fields"]
        }
    if kind == "string":
        return render(value, params, rendering) if isinstance(value, str) else value
    if kind == "list":
        if not isinstance(value, list):
            return value
        return [_render_known(item, node["item"], params, rendering) for item in value]
    if kind == "map":
        if not isinstance(value, dict):
            return value
        return {
            render(key, params, rendering) if isinstance(key, str) else key: _render_known(
                item, node["value"], params, rendering
            )
            for key, item in value.items()
        }
    if kind == "raw":
        return _render_raw(value, params, rendering)
    return value


def _render_raw(value: Any, params: dict[str, Any], rendering: Rendering) -> Any:
    """Raw JSON: every string and map key; a null map value is dropped, as
    deeplyReplace skips nil values."""
    if isinstance(value, dict):
        return {
            render(key, params, rendering) if isinstance(key, str) else key: _render_raw(
                item, params, rendering
            )
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_render_raw(item, params, rendering) for item in value]
    if isinstance(value, str):
        return render(value, params, rendering)
    return value


def mergo_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """mergo.Merge(&dst, src, mergo.WithOverride) on JSON-like maps, returned
    as a new map: a nil, a scalar or a list in `src` replaces the value in
    `dst`; a map is merged into a map, and replaces anything else, except that
    an empty map leaves a non-empty one as it is."""
    result: dict[str, Any] = type(dst)(dst)
    if isinstance(dst, GoStringMap) and not isinstance(src, GoStringMap) and src:
        # SetMapIndex of an interface{} value into a map[string]string.
        raise ParamsError("merging the params panics (mergo: a map of any into a map of strings)")
    for key, value in src.items():
        current = result.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = mergo_merge(current, value)
        else:
            result[key] = value
    return result
