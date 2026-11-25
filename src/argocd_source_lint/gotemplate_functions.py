"""The sprig and Argo CD template functions that gotemplate.py implements.

Each function takes the arguments text/template has already converted to its
parameter types: `None` is Go's nil, strings and ints are checked. A Go error
or panic raises GoCallError, which fails Argo CD's rendering. What this module
does not reproduce exactly raises NotSupported instead of guessing.

Sources: sprig v3.3.0 (strings.go, defaults.go, dict.go), Argo CD v3.3.3
(applicationset/utils/template_functions.go, utils.go), gosimple/slug v1.15.0,
Go 1.25 (fmt, strconv, encoding/json) and go.yaml.in/yaml/v2 v2.4.2, which
sigs.k8s.io/yaml v1.6.0 uses for toYaml.
"""

from __future__ import annotations

import datetime
import decimal
import functools
import math
import re
from typing import Any

from argocd_source_lint.appset_params import (
    ParamsError,
    _go_base,
    _go_float,
    _go_resolve,
    go_format,
    sanitize_name,
)
from argocd_source_lint.unicode_unstable import is_unstable


class GoCallError(Exception):
    """The Go function returns an error or panics: Argo CD's rendering fails."""


class TooLarge(Exception):
    """The result would go over MAX_STRING; it is not built."""


# The largest string a function builds, the bound of gotemplate's output.
MAX_STRING = 256 * 1024


def _check_size(size: int) -> None:
    if size > MAX_STRING:
        raise TooLarge(f"rendering one string outputs more than {MAX_STRING // 1024} KiB")


def _join(pieces: Any, separator: str = " ") -> str:
    out: list[str] = []
    total = 0
    for piece in pieces:
        total += len(piece) + len(separator)
        _check_size(total - len(separator))
        out.append(piece)
    return separator.join(out)


class NotSupported(Exception):
    """This module does not reproduce the Go function's result for these arguments."""


def go_print(value: Any) -> str:
    """fmt's %v for a template value; ints only come from template literals."""
    if type(value) is int:
        return str(value)
    return go_format(value)


# ---- sprig: defaults.go ----


def empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict)):
        return len(value) == 0
    if isinstance(value, bool):
        return not value
    return bool(value == 0)


def default(fallback: Any, *given: Any) -> Any:
    if not given or empty(given[0]):
        return fallback
    return given[0]


# ---- sprig: strings.go ----


def lower(text: str) -> str:
    if not text.isascii():
        # Go maps each rune on its own; Python applies full case mappings.
        raise NotSupported("`lower` of a non-ASCII string is not supported")
    return text.lower()


def upper(text: str) -> str:
    if not text.isascii():
        raise NotSupported("`upper` of a non-ASCII string is not supported")
    return text.upper()


def replace(old: str, new: str, text: str) -> str:
    count = text.count(old) if old else len(text) + 1
    _check_size(len(text) + count * (len(new) - len(old)))
    return text.replace(old, new)


def trim_prefix(prefix: str, text: str) -> str:
    return text.removeprefix(prefix)


def trim_suffix(suffix: str, text: str) -> str:
    return text.removesuffix(suffix)


def _strval(value: Any) -> str:
    return value if isinstance(value, str) else go_print(value)


def quote(*values: Any) -> str:
    return _join(go_quote(_strval(value)) for value in values if value is not None)


def cat(*values: Any) -> str:
    return _join(go_print(value) for value in values if value is not None)


def indent(spaces: int, text: str) -> str:
    if spaces < 0:
        raise GoCallError("strings: negative Repeat count")
    _check_size(spaces * (text.count("\n") + 1) + len(text))
    pad = " " * spaces
    return pad + text.replace("\n", "\n" + pad)


def base(path: str) -> str:
    return _go_base(path)


# ---- sprig: dict.go ----


def dig(*args: Any) -> Any:
    if len(args) < 3:
        raise GoCallError("dig needs at least three arguments")
    current = args[-1]
    fallback = args[-2]
    keys = args[:-2]
    if not isinstance(current, dict) or not all(isinstance(key, str) for key in keys):
        raise GoCallError("interface conversion")
    for index, key in enumerate(keys):
        if key not in current:
            return fallback
        step = current[key]
        if index == len(keys) - 1:
            return step
        if not isinstance(step, dict):
            raise GoCallError("interface conversion")
        current = step
    raise AssertionError("unreachable")


# ---- Argo CD: names ----


def normalize(name: str) -> str:
    return sanitize_name(name)


def slugify(*args: Any) -> str:
    """SlugifyName in applicationset/utils/utils.go, then gosimple/slug's Make."""
    max_size, smart, name = 50, True, ""
    for index, arg in enumerate(args):
        if index == len(args) - 1:
            if not isinstance(arg, str):
                raise GoCallError("interface conversion: the name is not a string")
            name = arg
        elif index == 0:
            if type(arg) is not int:
                raise GoCallError("interface conversion: the size is not an int")
            max_size = arg
        elif index == 1:
            if not isinstance(arg, bool):
                raise GoCallError("interface conversion: the flag is not a bool")
            smart = arg
    slug = sanitize_name(name)
    # Make: after SanitizeName, the text is lowercase [-a-z0-9.], which the
    # substitutions, unidecode and ToLower leave unchanged.
    if not smart and len(slug) >= max_size:
        if max_size < 0:
            raise GoCallError("slice bounds out of range")
        slug = slug[:max_size]
    slug = re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9\-_]", "-", slug)).strip("-_")
    if max_size > 0 and smart and len(slug) > max_size:
        cut = slug.rfind("-", 0, max_size + 1)
        slug = slug[:cut] if cut >= 0 else slug[:max_size]
    return slug


# ---- Go strconv.Quote, fmt's %q ----

_QUOTE_ESCAPES = {
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}


def check_stable(text: str) -> None:
    """Refuse characters whose Unicode category Python and Go may not share."""
    if not text.isascii() and any(is_unstable(char) for char in text):
        raise NotSupported("characters from recent Unicode versions are not supported")


def go_quote(text: str) -> str:
    check_stable(text)
    out = ['"']
    for char in text:
        if char in '"\\':
            out.append("\\" + char)
        elif char.isprintable():
            out.append(char)
        elif char in _QUOTE_ESCAPES:
            out.append(_QUOTE_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            raise NotSupported("quoting this character is not supported")
    out.append('"')
    return "".join(out)


# ---- Go fmt.Sprintf, for printf ----

_VERB = re.compile(r"%(?P<flags>[-0]*)(?P<width>[0-9]*)(?:\.(?P<precision>[0-9]*))?(?P<verb>.?)")


def sprintf(format_text: str, *args: Any) -> str:
    """fmt.Sprintf for %s %d %v %q %t %f and %%, with `-`, `0`, width and precision.

    Anything Go would print as an error, such as `%!d(float64=1)`, is not
    supported: Argo CD renders it, but not as a value.
    """
    out: list[str] = []
    pos = 0
    arg_index = 0
    while pos < len(format_text):
        percent = format_text.find("%", pos)
        if percent < 0:
            out.append(format_text[pos:])
            break
        out.append(format_text[pos:percent])
        match = _VERB.match(format_text, percent)
        assert match is not None
        pos = match.end()
        flags, width, precision, verb = match.group("flags", "width", "precision", "verb")
        if verb == "%" and not flags and not width and precision is None:
            out.append("%")
            continue
        if verb not in ("s", "d", "v", "q", "t", "f") or len(set(flags)) != len(flags):
            raise NotSupported(f"the printf verb `{match.group(0)}` is not supported")
        if "-" in flags and "0" in flags:
            raise NotSupported(f"the printf verb `{match.group(0)}` is not supported")
        if arg_index >= len(args):
            raise NotSupported("printf with a missing argument is not supported")
        if int(width or 0) > MAX_STRING or int(precision or 0) > MAX_STRING:
            _check_size(max(int(width or 0), int(precision or 0)))
        text = _format_verb(verb, args[arg_index], "0" in flags, precision)
        arg_index += 1
        if width and len(text) < int(width):
            padding = int(width) - len(text)
            if "-" in flags:
                text += " " * padding
            elif "0" in flags:
                # Zeros go after the sign, as zfill does.
                text = text.zfill(int(width))
            else:
                text = " " * padding + text
        out.append(text)
        _check_size(sum(map(len, out)))
    if arg_index != len(args):
        raise NotSupported("printf with extra arguments is not supported")
    return _join(out, "")


def _format_verb(verb: str, value: Any, zero: bool, precision: str | None) -> str:
    """One verb; zero padding is applied here, for numbers only."""
    unsupported = NotSupported(f"printf `%{verb}` of this value is not supported")
    if zero and verb not in ("d", "f"):
        raise unsupported
    if isinstance(value, str):
        if verb not in ("s", "v", "q") or (precision is not None and verb == "v"):
            raise unsupported
        if precision is not None:
            value = value[: int(precision or 0)]
        return go_quote(value) if verb == "q" else value
    if isinstance(value, bool):
        if verb not in ("t", "v") or precision is not None:
            raise unsupported
        return "true" if value else "false"
    if type(value) is int:
        if verb not in ("d", "v") or precision is not None:
            raise unsupported
        return str(value)
    if isinstance(value, float):
        if verb == "v" and precision is None and not zero:
            return _go_float(value)
        if verb == "f":
            digits = 6 if precision is None else int(precision or 0)
            return f"{value:.{digits}f}"
        raise unsupported
    if isinstance(value, (list, dict)) and verb == "v" and precision is None:
        return go_format(value)
    raise unsupported


# ---- Go encoding/json, for toJson and mustToPrettyJson ----

_JSON_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n"}
_JSON_SHORT_ESCAPES.update({"\r": "\\r", "\t": "\\t"})


def _json_string(text: str) -> str:
    out = ['"']
    for char in text:
        code = ord(char)
        if char in _JSON_SHORT_ESCAPES:
            out.append(_JSON_SHORT_ESCAPES[char])
        elif code < 0x20 or char in "<>&" or char in "\u2028\u2029":
            # HTML-sensitive characters are escaped too (escapeHTML).
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise NotSupported("strings with surrogates are not supported")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _shortest_digits(value: float) -> tuple[str, int]:
    """The shortest round-tripping digits of abs(value), and the decimal
    point's position, as strconv's shortest formatting computes them."""
    _, digit_tuple, exponent = decimal.Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(map(str, digit_tuple))
    stripped = digits.rstrip("0") or "0"
    return stripped, len(digits) + exponent


def json_float(value: float) -> str:
    """floatEncoder.encode in encoding/json: like ES6 number to string."""
    if math.isinf(value) or math.isnan(value):
        raise GoCallError("json: unsupported value")
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    sign = "-" if value < 0 else ""
    digits, point = _shortest_digits(value)
    if abs(value) < 1e-6 or abs(value) >= 1e21:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        exp10 = point - 1
        exponent = f"{'-' if exp10 < 0 else '+'}{abs(exp10):02d}"
        if exponent.startswith("-0"):
            exponent = "-" + exponent[2:]
        return f"{sign}{mantissa}e{exponent}"
    if point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    if point >= len(digits):
        return sign + digits + "0" * (point - len(digits))
    return f"{sign}{digits[:point]}.{digits[point:]}"


def to_json(value: Any, indent_level: int | None = None, depth: int = 0) -> str:
    """json.Marshal, or json.MarshalIndent(v, "", "  ") with indent_level set."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, float):
        return json_float(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, list):
        items = [to_json(item, indent_level, depth + 1) for item in value]
        return _json_container("[", "]", items, indent_level, depth)
    if isinstance(value, dict):
        items = [
            f"{_json_string(key)}:{' ' if indent_level is not None else ''}"
            + to_json(value[key], indent_level, depth + 1)
            for key in sorted(value, key=lambda key: key.encode("utf-8", "surrogatepass"))
        ]
        return _json_container("{", "}", items, indent_level, depth)
    raise NotSupported("this value can't be encoded")


def _json_container(
    open_: str, close: str, items: list[str], indent_level: int | None, depth: int
) -> str:
    if not items:
        return open_ + close
    if indent_level is None:
        return open_ + ",".join(items) + close
    inner = "\n" + "  " * (depth + 1)
    return open_ + inner + ("," + inner).join(items) + "\n" + "  " * depth + close


def to_pretty_json(value: Any) -> str:
    return to_json(value, indent_level=2)


# ---- sigs.k8s.io/yaml Marshal (JSON, then go-yaml v2), for toYaml ----

_BEST_WIDTH = 80
_BASE60_FLOAT = re.compile(r"[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?")
_TIMESTAMP_LIKE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")


def to_yaml(value: Any) -> str:
    """toYAML in applicationset/utils/template_functions.go."""
    lines = _yaml_block(_json_roundtrip(value), 0, root=True)
    return "\n".join(lines)


def _json_roundtrip(value: Any) -> Any:
    """What go-yaml decodes from the JSON of `value`: integral numbers
    printed without an exponent become ints."""
    if isinstance(value, float):
        text = json_float(value)
        if re.fullmatch(r"-?[0-9]+", text):
            number = int(text)
            if -(2**63) <= number < 2**64:
                return number
            return float(text)
        return value
    if isinstance(value, list):
        return [_json_roundtrip(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_roundtrip(item) for key, item in value.items()}
    return value


def _yaml_key_less(a: str, b: str) -> int:
    """keyList.Less in go-yaml v2 sorter.go, as a cmp function."""
    if a == b:
        return 0
    # unicode.IsLetter and IsDigit decide the order.
    check_stable(a)
    check_stable(b)
    ar, br = a, b
    for i in range(min(len(ar), len(br))):
        if ar[i] == br[i]:
            continue
        al, bl = ar[i].isalpha(), br[i].isalpha()
        if al and bl:
            return -1 if ar[i] < br[i] else 1
        if al or bl:
            return -1 if bl else 1
        an = bn = 0
        if ar[i] == "0" or br[i] == "0":
            j = i - 1
            while j >= 0 and ar[j].isdecimal():
                if ar[j] != "0":
                    an = bn = 1
                    break
                j -= 1
        ai = i
        while ai < len(ar) and ar[ai].isdecimal():
            an = an * 10 + (ord(ar[ai]) - 48)
            ai += 1
        bi = i
        while bi < len(br) and br[bi].isdecimal():
            bn = bn * 10 + (ord(br[bi]) - 48)
            bi += 1
        if max(an, bn) >= 2**63:
            raise NotSupported("toYaml with such long numbers in keys is not supported")
        if an != bn:
            return -1 if an < bn else 1
        if ai != bi:
            return -1 if ai < bi else 1
        return -1 if ar[i] < br[i] else 1
    return -1 if len(ar) < len(br) else 1


def _yaml_block(value: Any, indent: int, root: bool = False) -> list[str]:
    """The lines of a value emitted as a block at `indent` columns."""
    pad = " " * indent
    if isinstance(value, dict) and value:
        lines: list[str] = []
        for key in sorted(value, key=functools.cmp_to_key(_yaml_key_less)):
            key_text = _yaml_scalar_text(key, len(pad), simple_key=True)
            lines.extend(_yaml_entry(f"{pad}{key_text}:", value[key], indent, in_sequence=False))
        return lines
    if isinstance(value, list) and value:
        lines = []
        for item in value:
            lines.extend(_yaml_sequence_item(pad, item, indent))
        return lines
    if isinstance(value, str) and "\n" in value:
        return _yaml_literal(pad, value, indent + 2 if root else indent)
    return [pad + _yaml_scalar_text(value, len(pad), simple_key=False)]


def _yaml_entry(prefix: str, value: Any, indent: int, in_sequence: bool) -> list[str]:
    """`key:` followed by its value, in a mapping at `indent`."""
    if isinstance(value, dict) and value:
        return [prefix, *_yaml_block(value, indent + 2)]
    if isinstance(value, list) and value:
        # go-yaml v2 does not indent a sequence under its key.
        return [prefix, *_yaml_block(value, indent)]
    if isinstance(value, str) and "\n" in value:
        if in_sequence:
            raise NotSupported("toYaml of a multiline string in a list is not supported")
        return _yaml_literal(prefix + " ", value, indent + 2)
    text = _yaml_scalar_text(value, len(prefix) + 1, simple_key=False, fold_indent=indent + 2)
    return [prefix + " " + text]


def _yaml_sequence_item(pad: str, item: Any, indent: int) -> list[str]:
    if isinstance(item, dict) and item:
        lines = _yaml_block(item, indent + 2)
        return [pad + "- " + lines[0][indent + 2 :], *lines[1:]]
    if isinstance(item, list) and item:
        lines = _yaml_block(item, indent + 2)
        return [pad + "- " + lines[0][indent + 2 :], *lines[1:]]
    if isinstance(item, str) and "\n" in item:
        raise NotSupported("toYaml of a multiline string in a list is not supported")
    return [pad + "- " + _yaml_scalar_text(item, len(pad) + 2, simple_key=False)]


def _yaml_literal(prefix: str, text: str, content_indent: int) -> list[str]:
    """A literal block scalar (yaml_emitter_write_literal_scalar)."""
    if text[0] in " \n" or not _yaml_block_allowed(text):
        raise NotSupported("toYaml of this multiline string is not supported")
    if text.endswith("\n\n") or text == "\n":
        raise NotSupported("toYaml of a string ending with several line breaks is not supported")
    chomp = "" if text.endswith("\n") else "-"
    body = text[:-1] if text.endswith("\n") else text
    pad = " " * content_indent
    lines = [prefix + "|" + chomp]
    lines.extend(pad + line if line else "" for line in body.split("\n"))
    return lines


def _yaml_block_allowed(text: str) -> bool:
    """block_allowed in yaml_emitter_analyze_scalar, for text with line breaks."""
    if text.endswith(" ") or " \n" in text:
        return False
    return all(char == "\n" or _yaml_printable(char) for char in text)


def _yaml_scalar_text(
    value: Any, column: int, simple_key: bool, fold_indent: int | None = None
) -> str:
    """A scalar as go-yaml v2 writes it, starting at `column`. A long plain
    string is folded only where `fold_indent` gives its continuation indent;
    elsewhere folding is not supported."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, float):
        return _go_float(value)
    if value == []:
        return "[]"
    if value == {}:
        return "{}"
    if not isinstance(value, str):
        raise NotSupported("toYaml of this value is not supported")
    if "\n" in value or not all(_yaml_printable(char) for char in value):
        raise NotSupported("toYaml of this string is not supported")
    if simple_key and len(value.encode("utf-8")) > 128:
        raise NotSupported("toYaml of a key over 128 bytes is not supported")
    # encoder.stringv: double quotes when the plain text would read back as
    # another type; otherwise plain, or single quotes where plain text is not
    # allowed (yaml_emitter_select_scalar_style).
    if not _resolves_to_str(value):
        text = _double_quoted(value)
    elif _plain_allowed(value):
        text = value
    else:
        text = "'" + value.replace("'", "''") + "'"
    if not simple_key:
        if fold_indent is not None and text is value:
            return _fold_plain(text, column, fold_indent)
        _check_no_folding(text, column)
    return text


def _fold_plain(text: str, column: int, indent: int) -> str:
    """yaml_emitter_write_plain_scalar: a space past column 80, followed by a
    non-space, becomes a line break and the indentation."""
    out: list[str] = []
    previous_space = False
    for index, char in enumerate(text):
        if char == " ":
            next_is_space = index + 1 < len(text) and text[index + 1] == " "
            if not previous_space and column > _BEST_WIDTH and not next_is_space:
                out.append("\n" + " " * indent)
                column = indent
            else:
                out.append(" ")
                column += 1
            previous_space = True
        else:
            out.append(char)
            column += 1
            previous_space = False
    return "".join(out)


def _yaml_printable(char: str) -> bool:
    """is_printable in go-yaml v2 (yamlprivateh.go), which tests UTF-8 bytes,
    not Unicode categories. Line breaks other than LF (is_break) are refused."""
    code = ord(char)
    return (
        0x20 <= code <= 0x7E
        or (0xA0 <= code <= 0xD7FF and code not in (0x85, 0x2028, 0x2029))
        or (0xE000 <= code <= 0xFFFD and code != 0xFEFF)
    )


def _double_quoted(value: str) -> str:
    if '"' in value or "\\" in value or not value.isascii():
        raise NotSupported("toYaml of this quoted string is not supported")
    return f'"{value}"'


def _check_no_folding(text: str, column: int) -> None:
    """go-yaml v2 breaks a long scalar at a space past column 80."""
    previous_space = False
    for index, char in enumerate(text):
        if char == " ":
            next_is_space = index + 1 < len(text) and text[index + 1] == " "
            if not previous_space and column + index > _BEST_WIDTH and not next_is_space:
                raise NotSupported("toYaml of a string wrapped over several lines")
            previous_space = True
        else:
            previous_space = False


def _resolves_to_str(text: str) -> bool:
    """Whether go-yaml v2 reads the text, unquoted, back as this string."""
    if text == "" or _BASE60_FLOAT.fullmatch(text):
        return False
    if _TIMESTAMP_LIKE.match(text):
        # parseTimestamp in resolve.go: a valid date is a timestamp; dates
        # with a time part are not supported here.
        if not _TIMESTAMP_LIKE.fullmatch(text):
            raise NotSupported("toYaml of a timestamp-like string is not supported")
        year, month, day = map(int, text.split("-"))
        try:
            datetime.date(year, month, day)
        except ValueError:
            if year == 0:
                raise NotSupported("toYaml of a year-0 date is not supported") from None
            return True
        return False
    try:
        resolved = _go_resolve(text)
    except ParamsError:
        return False
    return isinstance(resolved, str) and resolved == text


def _plain_allowed(value: str) -> bool:
    """block_plain_allowed in yaml_emitter_analyze_scalar, for one-line text."""
    if not value or value[0] == " " or value[-1] == " ":
        return False
    if value.startswith(("---", "...")):
        return False
    first = value[0]
    followed_by_space = len(value) == 1 or value[1] == " "
    if first in "#,[]{}&*!|>'\"%@`":
        return False
    if first in "?:-" and followed_by_space:
        return False
    for index in range(1, len(value)):
        char = value[index]
        followed_by_space = index + 1 == len(value) or value[index + 1] == " "
        if char == ":" and followed_by_space:
            return False
        if char == "#" and value[index - 1] == " ":
            return False
    return True
