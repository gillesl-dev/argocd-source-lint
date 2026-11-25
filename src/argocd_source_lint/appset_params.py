"""Generator parameters as the Argo CD ApplicationSet controller computes them.

Argo CD reads params files and `elementsYaml` with sigs.k8s.io/yaml, a YAML
1.1 parser whose result goes through JSON: every number becomes a float64,
`yes`/`on`/`y` become booleans, timestamps stay text. Values are then turned
into parameters with Go's `fmt.Sprintf("%v")`, which prints `true`, `<nil>`,
`1e+06`. Each function below cites the Argo CD v3.3 code it follows.
"""

from __future__ import annotations

import decimal
import json
import math
import posixpath
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.error import YAMLError
from ruamel.yaml.resolver import BaseResolver

from argocd_source_lint.fsutil import (
    MAX_FLOW_DEPTH,
    FlowDepthError,
    check_flow_depth,
    is_within_budget,
)


class ParamsError(ValueError):
    """Argo CD fails the generator, so the ApplicationSet generates nothing."""


class LimitError(Exception):
    """The input is beyond what this tool processes, though Argo CD may accept it."""


# ---- YAML, read as sigs.k8s.io/yaml does ----

_STR_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _GoYamlResolver(BaseResolver):
    """Plain scalars are left as text, for `_go_resolve`; only `<<` keeps its
    meaning, so merge keys still merge."""

    def __init__(self, version: Any = None, loader: Any = None) -> None:
        super().__init__(loadumper=loader)

    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 1)  # go-yaml v2 scans YAML 1.1


_GoYamlResolver.add_implicit_resolver(_MERGE_TAG, re.compile(r"^<<$"), ["<"])


class _GoYamlConstructor(SafeConstructor):
    def construct_go_scalar(self, node: Any) -> Any:
        text = self.construct_scalar(node)
        # Quoted and block scalars are always strings.
        return _go_resolve(text) if node.style is None else text

    def construct_merge_value(self, node: Any) -> Any:
        return self.construct_scalar(node)  # `<<` as a value is the text "<<"

    def check_mapping_key(self, *args: Any) -> bool:
        return True  # a repeated key is not an error: go-yaml v2 keeps the last value


_GoYamlConstructor.add_constructor(_STR_TAG, _GoYamlConstructor.construct_go_scalar)
_GoYamlConstructor.add_constructor(_MERGE_TAG, _GoYamlConstructor.construct_merge_value)

_GO_YAML_CONSTANTS: dict[str, Any] = {
    **dict.fromkeys(["y", "Y", "yes", "Yes", "YES", "true", "True", "TRUE"], True),
    **dict.fromkeys(["on", "On", "ON"], True),
    **dict.fromkeys(["n", "N", "no", "No", "NO", "false", "False", "FALSE"], False),
    **dict.fromkeys(["off", "Off", "OFF"], False),
    **dict.fromkeys(["", "~", "null", "Null", "NULL"], None),
}
_GO_YAML_NON_FINITE = {
    ".nan", ".NaN", ".NAN", ".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF",
    "-.inf", "-.Inf", "-.INF",
}  # fmt: skip
_YAML_STYLE_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")


def _go_resolve(text: str) -> Any:
    """A plain scalar's value (`resolve` in go-yaml v2 resolve.go)."""
    if text in _GO_YAML_CONSTANTS:
        return _GO_YAML_CONSTANTS[text]
    if text in _GO_YAML_NON_FINITE:
        # Valid YAML, but JSON has no NaN or infinity: the conversion fails.
        raise ParamsError(f"unsupported value: {text}")
    first = text[:1]
    if first == ".":
        return _go_parse_float(text, text)
    if first and first in "+-0123456789":
        # A timestamp is kept as text, which is also what this returns for it.
        plain = text.replace("_", "")
        number = _go_parse_int(plain)
        if number is not None:
            return number
        if _YAML_STYLE_FLOAT.fullmatch(plain):
            return _go_parse_float(plain, text)
    return text


def _go_parse_int(text: str) -> int | None:
    """strconv.ParseInt(text, 0, 64), then strconv.ParseUint for larger values."""
    sign, body = (text[0], text[1:]) if text[:1] in "+-" else ("", text)
    base, digits = 10, body
    prefix = body[:2].lower()
    if prefix in ("0x", "0o", "0b"):
        base, digits = {"0x": 16, "0o": 8, "0b": 2}[prefix], body[2:]
    elif body.startswith("0") and len(body) > 1:
        base, digits = 8, body[1:]
    valid = "0123456789abcdef"[:base]
    if not digits or any(char not in valid for char in digits.lower()):
        return None
    value = int(digits, base) * (-1 if sign == "-" else 1)
    if -(2**63) <= value < 2**63 or (not sign and value < 2**64):
        return value
    return None


def _go_parse_float(text: str, original: str) -> Any:
    """strconv.ParseFloat, or the text itself when Go can't parse it."""
    if "_" in text:
        return original
    try:
        value = float(text)
    except ValueError:
        return original
    return original if math.isinf(value) else value


_YAML_DIRECTIVE = re.compile(r"^%YAML[ \t]+(\S+)", re.MULTILINE)


def _go_yaml() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.Resolver = _GoYamlResolver
    yaml.Constructor = _GoYamlConstructor
    return yaml


def load_go_yaml(text: str) -> Any:
    """The first YAML document of `text`, as sigs.k8s.io/yaml.Unmarshal reads it."""
    directive = _YAML_DIRECTIVE.search(text)
    if directive and directive.group(1) != "1.1":
        raise ParamsError("found incompatible YAML document")
    try:
        check_flow_depth(text)
    except FlowDepthError as exc:
        raise LimitError(f"the document nests deeper than {MAX_FLOW_DEPTH} levels") from exc
    try:
        data = next(iter(_go_yaml().load_all(text)), None)
        if not is_within_budget(data):
            # go-yaml rejects documents with excessive aliasing.
            raise ParamsError("document has excessive aliasing")
        return _as_json(data)
    except RecursionError as exc:
        raise LimitError("the document is nested too deeply") from exc
    except (YAMLError, ValueError) as exc:
        if isinstance(exc, ParamsError):
            raise
        raise ParamsError(str(exc).splitlines()[0] if str(exc) else "invalid YAML") from exc


def _as_json(value: Any) -> Any:
    """`value` after YAMLToJSON and json.Unmarshal: string keys, float numbers."""
    if isinstance(value, dict):
        return {_key_text(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_json(item) for item in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _key_text(key: Any) -> str:
    if key is None or isinstance(key, (dict, list, tuple)):
        raise ParamsError("unsupported map key")
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float):
        return _go_float(key)
    return str(key)


# ---- Go formatting: fmt.Sprintf("%v", v) on JSON-decoded values ----


def go_format(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "<nil>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _go_float(float(value))
    if isinstance(value, dict):
        items = " ".join(f"{key}:{go_format(value[key])}" for key in sorted(value))
        return f"map[{items}]"
    if isinstance(value, list):
        return "[" + " ".join(go_format(item) for item in value) + "]"
    return str(value)


def _go_float(value: float) -> str:
    """strconv.FormatFloat(value, 'g', -1, 64), what `%v` prints for a float64."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign = "-" if value < 0 else ""
    # repr() gives the shortest digits that round-trip, as Go's shortest mode.
    _, digit_tuple, exponent = decimal.Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0") or "0"
    assert isinstance(exponent, int)
    point = len(digit_tuple) + exponent  # digits before the decimal point
    exp10 = point - 1
    # With the shortest precision, Go switches to %e from an exponent of 6.
    if exp10 < -4 or exp10 >= 6:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'-' if exp10 < 0 else '+'}{abs(exp10):02d}"
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return f"{sign}{digits[:point]}.{digits[point:]}"


# ---- Names and paths ----

_INVALID_DNS_CHARS = re.compile(r"[^-a-z0-9.]")


def sanitize_name(name: str) -> str:
    """`normalize` and the `*Normalized` params (SanitizeName in
    applicationset/utils/template_functions.go)."""
    # Go's ToLower maps U+0130 to "i"; Python's lower() gives "i" + U+0307.
    name = _INVALID_DNS_CHARS.sub("-", name.replace("\u0130", "i").lower())[:253]
    return name.strip("-.")


def directory_params(
    rel_path: str, prefix: str = "", values: dict[str, Any] | None = None
) -> dict[str, str]:
    """Params of one directory (generateParamsFromApps in generators/git.go).
    `prefix` is the generator's `pathParamPrefix`, `values` its `values`."""
    params = _path_params(rel_path, prefix)
    return _with_values(params, values)


def _path_params(rel_path: str, prefix: str) -> dict[str, str]:
    name = f"{prefix}.path" if prefix else "path"
    params = {
        name: rel_path,
        f"{name}.basename": _go_base(rel_path),
        f"{name}.basenameNormalized": sanitize_name(_go_base(rel_path)),
    }
    # `{{path[0]}}`, `{{path[1]}}`...: empty segments are skipped, not renumbered.
    for index, segment in enumerate(rel_path.split("/")):
        if segment:
            params[f"{name}[{index}]"] = segment
    return params


def _with_values(params: dict[str, str], values: dict[str, Any] | None) -> dict[str, str]:
    """`values.<key>`, each rendered with the generator's other params
    (appendTemplatedValues in generators/value_interpolation.go)."""
    rendered = {}
    for key, value in (values or {}).items():
        if not isinstance(value, str):
            raise ParamsError(f"git generator value `{key}` is not a string")
        rendered[f"values.{key}"] = replace_placeholders(value, params)
    return {**params, **rendered}


_IS_TEMPLATED = re.compile(r".*\{\{.*\}\}.*")  # no DOTALL, as Go's isTemplatedRegex


def replace_placeholders(text: str, params: dict[str, Any]) -> str:
    """Classic `{{key}}` substitution (Replace in applicationset/utils/utils.go,
    valyala/fasttemplate): only a string param fills a tag, others are left."""
    if not _IS_TEMPLATED.search(text):
        return text
    parts: list[str] = []
    position = 0
    while (start := text.find("{{", position)) != -1:
        end = text.find("}}", start + 2)
        if end == -1:
            raise ParamsError(f"cannot find the end of a `{{{{` tag in {text!r}")
        tag = text[start + 2 : end]
        value = params.get(tag.strip())
        parts.append(text[position:start])
        parts.append(value if tag.strip() and isinstance(value, str) else "{{" + tag + "}}")
        position = end + 2
    parts.append(text[position:])
    return "".join(parts)


def _go_base(path: str) -> str:
    """Go's path.Base."""
    if not path:
        return "."
    stripped = path.rstrip("/")
    return stripped.rsplit("/", 1)[-1] if stripped else "/"


def _go_dir(path: str) -> str:
    """Go's path.Dir, for the clean relative paths `git ls-files` returns."""
    return posixpath.dirname(path) or "."


# ---- Generators, classic (non-goTemplate) mode ----


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """jeremywohl/flatten with DotStyle: nested keys joined by dots, list items
    by their index, empty maps and lists dropped."""
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        children = ((str(key), item) for key, item in value.items())
    elif isinstance(value, list):
        children = ((str(index), item) for index, item in enumerate(value))
    else:
        return flat
    for key, item in children:
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(item, (dict, list)):
            flat.update(flatten(item, dotted))
        else:
            flat[dotted] = item
    return flat


def git_file_param_sets(
    rel_path: str, text: str, prefix: str = "", values: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """Params of one file of a git `files` generator, one set per object
    (generateParamsFromGitFile in generators/git.go)."""
    data = load_go_yaml(text)
    if data is None or isinstance(data, dict):
        objects: list[Any] = [data or {}]
    elif isinstance(data, list) and all(item is None or isinstance(item, dict) for item in data):
        objects = [item or {} for item in data]
    else:
        raise ParamsError("the file is neither an object nor a list of objects")

    name = f"{prefix}.path" if prefix else "path"
    filename = _go_base(rel_path)
    param_sets = []
    for obj in objects:
        try:
            params = {key: go_format(value) for key, value in flatten(obj).items()}
        except RecursionError as exc:
            raise LimitError("the file is nested too deeply") from exc
        params.update(_path_params(_go_dir(rel_path), prefix))
        params[f"{name}.filename"] = filename
        params[f"{name}.filenameNormalized"] = sanitize_name(filename)
        param_sets.append(_with_values(params, values))
    return param_sets


def list_param_sets(elements: list[Any], elements_yaml: Any) -> list[dict[str, Any]]:
    """Params of a list generator (ListGenerator.GenerateParams in
    generators/list.go). `elements` take string values only, and a `values`
    map of strings; `elementsYaml` entries are kept as they are, so only their
    top-level string values can fill a `{{key}}`."""
    param_sets: list[dict[str, Any]] = []
    for element in elements:
        if element is None:
            param_sets.append({})
            continue
        if not isinstance(element, dict):
            raise ParamsError("a list element is not an object")
        params: dict[str, Any] = {}
        for key, value in element.items():
            if key == "values":
                if not isinstance(value, dict):
                    raise ParamsError("`values` of a list element is not a map")
                for name, item in value.items():
                    if not isinstance(item, str):
                        raise ParamsError(f"list element value `values.{name}` is not a string")
                    params[f"values.{name}"] = item
            elif isinstance(value, str):
                params[str(key)] = value
            else:
                raise ParamsError(f"list element value `{key}` is not a string")
        param_sets.append(params)

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


def combine_string_maps(first: dict[str, Any], second: dict[str, Any]) -> dict[str, str]:
    """A matrix entry: both param sets as strings, a key in both must agree
    (CombineStringMaps in applicationset/utils/map.go)."""
    combined = {key: go_format(value) for key, value in first.items()}
    for key, value in second.items():
        text = go_format(value)
        if key in combined and combined[key] != text:
            raise ParamsError(f"matrix children set `{key}` to different values")
        combined[key] = text
    return combined


def merge_key(params: dict[str, Any], merge_keys: list[str]) -> str:
    """The identity of a param set for a merge generator: its merge keys'
    values, a missing key counting as null (getParamSetsByMergeKey in
    generators/merge.go)."""
    return json.dumps({key: params.get(key) for key in sorted(set(merge_keys))}, sort_keys=True)
