"""Go templates, as Argo CD renders the strings of a `goTemplate: true` ApplicationSet.

Argo CD renders every string of the template with Go's text/template and
sprig's functions, minus env, expandenv and getHostByName, plus normalize,
slugify, toYaml, fromYaml and fromYamlArray (`Render.Replace` in
applicationset/utils/utils.go). This module follows text/template's lexer,
parser and executor (Go 1.25, text/template/parse and text/template/exec.go)
for the subset that ApplicationSets use.

Rendering never guesses. A template on which Argo CD's own rendering fails
raises ArgoTemplateError. One this module does not render the way Argo CD does
raises UnsupportedTemplateError, and so does one that prints `<no value>`
(NoValueError): Argo CD renders it, but with that text in place of a value.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from argocd_source_lint import gotemplate_functions as functions
from argocd_source_lint.unicode_unstable import is_unstable

__all__ = [
    "ARGO_CD_FUNCTIONS",
    "ArgoTemplateError",
    "GoStringMap",
    "NoValueError",
    "SUPPORTED_FUNCTIONS",
    "TemplateError",
    "UnsupportedFunctionError",
    "UnsupportedTemplateError",
    "render",
]


class TemplateError(Exception):
    """A template this module does not render."""


class ArgoTemplateError(TemplateError):
    """Argo CD's own rendering fails on this template."""


class UnsupportedTemplateError(TemplateError):
    """This module does not render this template the way Argo CD does."""


class UnsupportedFunctionError(UnsupportedTemplateError):
    """The template calls a function Argo CD has and this module does not implement."""

    def __init__(self, name: str) -> None:
        super().__init__(f"the function `{name}` is not supported by this tool")
        self.name = name


class TemplateLimitError(TemplateError):
    """Rendering would go over one of this module's bounds."""


# Templates come from the repository, which may not be trusted. Bounds per
# rendered string, then per ApplicationSet (Budget).
MAX_TEMPLATE_LENGTH = 64 * 1024
MAX_OUTPUT = functions.MAX_STRING
MAX_RANGE_ITERATIONS = 10_000
MAX_NESTING = 100
MAX_OPERATIONS = 1_000_000
MAX_TOTAL_OUTPUT = 16 * 1024 * 1024
# Operation costs, measured so that one operation takes about 2 microseconds
# and MAX_OPERATIONS a few seconds at most: a node or a command costs 1, a
# function call 10, plus 1 per 64 characters it reads or writes, times the
# weight of the functions that do more work per character.
_CALL_COST = 10
_BYTES_PER_OPERATION = 64
_BYTE_WEIGHTS = {"toYaml": 16, "mustToPrettyJson": 6, "toJson": 5, "quote": 2}


class Budget:
    """Template operations and output left to one ApplicationSet."""

    def __init__(self, operations: int = MAX_OPERATIONS, output: int = MAX_TOTAL_OUTPUT) -> None:
        self.operations = operations
        self.output = output

    def spend(self, cost: int = 1) -> None:
        self.operations -= cost
        if self.operations < 0:
            raise TemplateLimitError(
                f"rendering takes more than {MAX_OPERATIONS} template operations"
            )

    def write(self, size: int) -> None:
        self.output -= size
        if self.output < 0:
            raise TemplateLimitError(
                f"the ApplicationSet renders more than {MAX_TOTAL_OUTPUT // (1024 * 1024)} MiB "
                "of output"
            )


class NoValueError(UnsupportedTemplateError):
    """The template prints `<no value>`, Go's text for a missing or null value."""


# The functions Argo CD's Go templates accept: sprig.GenericFuncMap, minus
# env, expandenv and getHostByName, plus Argo CD's five and text/template's
# builtins. tests/test_gotemplate_oracle.py checks this list against Argo CD.
ARGO_CD_FUNCTIONS = frozenset(
    """
    abbrev abbrevboth add add1 add1f addf adler32sum ago all and any append atoi
    b32dec b32enc b64dec b64enc base bcrypt biggest buildCustomCert call camelcase
    cat ceil chunk clean coalesce compact concat contains date dateInZone
    dateModify date_in_zone date_modify decryptAES deepCopy deepEqual default
    derivePassword dict dig dir div divf duration durationRound empty encryptAES eq
    ext fail first float64 floor fromJson fromYaml fromYamlArray ge genCA
    genCAWithKey genPrivateKey genSelfSignedCert genSelfSignedCertWithKey
    genSignedCert genSignedCertWithKey get gt has hasKey hasPrefix hasSuffix hello
    html htmlDate htmlDateInZone htpasswd indent index initial initials int int64
    isAbs join js kebabcase keys kindIs kindOf last le len list lower lt max maxf
    merge mergeOverwrite min minf mod mul mulf mustAppend mustChunk mustCompact
    mustDateModify mustDeepCopy mustFirst mustFromJson mustHas mustInitial mustLast
    mustMerge mustMergeOverwrite mustPrepend mustPush mustRegexFind
    mustRegexFindAll mustRegexMatch mustRegexReplaceAll mustRegexReplaceAllLiteral
    mustRegexSplit mustRest mustReverse mustSlice mustToDate mustToJson
    mustToPrettyJson mustToRawJson mustUniq mustWithout must_date_modify ne nindent
    normalize nospace not now omit or osBase osClean osDir osExt osIsAbs pick pluck
    plural prepend print printf println push quote randAlpha randAlphaNum randAscii
    randBytes randInt randNumeric regexFind regexFindAll regexMatch regexQuoteMeta
    regexReplaceAll regexReplaceAllLiteral regexSplit repeat replace rest reverse
    round semver semverCompare seq set sha1sum sha256sum sha512sum shuffle slice
    slugify snakecase sortAlpha split splitList splitn squote sub subf substr
    swapcase ternary title toDate toDecimal toJson toPrettyJson toRawJson toString
    toStrings toYaml trim trimAll trimPrefix trimSuffix trimall trunc tuple typeIs
    typeIsLike typeOf uniq unixEpoch unset until untilStep untitle upper urlJoin
    urlParse urlquery uuidv4 values without wrap wrapWith
    """.split()
)


class GoStringMap(dict[str, str]):
    """A Go map[string]string: the git generator's `values` in goTemplate mode.

    It differs from the other maps (map[string]interface{}) in `index` of a
    missing key, which gives "" instead of nil, and in mergo, which panics
    when merging a map[string]interface{} into it.
    """


# ---- Values ----
#
# Params are JSON values: dict (string keys), list, str, float, bool and None.
# Template literals add int. MISSING is Go's invalid reflect.Value, what a
# missing map key evaluates to; None is a nil interface{} held by a map, a
# list or dot. A pipeline's result is unwrapped one level, as exec.go's
# evalPipeline does, so None becomes MISSING there.


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()
# exec.go's missingVal: no final argument was given to a command.
_NOT_GIVEN: Any = object()


# fmt.Fprint of a value, as printValue writes an action's result.
_go_print = functions.go_print


def _is_true(value: Any) -> bool:
    """isTrue in exec.go, for the values a template can hold."""
    if value is MISSING or value is None:
        return False
    if isinstance(value, (bool, int, float)):
        return bool(value)
    return len(value) > 0


# ---- Lexer (text/template/parse/lex.go) ----

_TEXT = "text"
_LEFT_DELIM = "{{"
_RIGHT_DELIM = "}}"
_SPACE = "space"
_FIELD = "field"
_VARIABLE = "variable"
_IDENTIFIER = "identifier"
_KEYWORD = "keyword"
_BOOL = "bool"
_NUMBER = "number"
_STRING = "string"
_RAW_STRING = "raw string"
_ASSIGN = "="
_DECLARE = ":="
_PIPE = "|"
_LEFT_PAREN = "("
_RIGHT_PAREN = ")"
_CHAR = "char"
_DOT = "."
_NIL = "nil"
_EOF = "EOF"

_KEYWORDS = frozenset(
    {"block", "break", "continue", "define", "else", "end", "if", "range", "template", "with"}
)
_SPACE_CHARS = " \t\r\n"


@dataclass(frozen=True, slots=True)
class _Item:
    typ: str
    val: str


def _is_alphanumeric(char: str) -> bool:
    """isAlphaNumeric in lex.go: unicode.IsLetter or unicode.IsDigit."""
    if char and not char.isascii() and is_unstable(char):
        raise UnsupportedTemplateError("characters from recent Unicode versions are not supported")
    return char == "_" or char.isalpha() or char.isdecimal()


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.items: list[_Item] = []
        self.paren_depth = 0

    def lex(self) -> list[_Item]:
        while self._lex_text():
            self._lex_inside_action()
        return self.items

    def _error(self, message: str) -> ArgoTemplateError:
        return ArgoTemplateError(f"template: {message}")

    def _lex_text(self) -> bool:
        """Emit the text up to the next action; False at the end of the input."""
        text = self.text
        while True:
            start = self.pos
            index = text.find("{{", start)
            if index < 0:
                if start < len(text):
                    self.items.append(_Item(_TEXT, text[start:]))
                self.items.append(_Item(_EOF, ""))
                return False
            after = index + 2
            trim = _has_left_trim_marker(text, after)
            chunk = text[start:index]
            if trim:
                chunk = chunk.rstrip(_SPACE_CHARS)
            if chunk:
                self.items.append(_Item(_TEXT, chunk))
            after_marker = after + 2 if trim else after
            if text.startswith("/*", after_marker):
                self._lex_comment(after_marker)
                continue
            self.items.append(_Item(_LEFT_DELIM, "{{"))
            self.pos = after_marker
            self.paren_depth = 0
            return True

    def _lex_comment(self, pos: int) -> None:
        end = self.text.find("*/", pos + 2)
        if end < 0:
            raise self._error("unclosed comment")
        self.pos = end + 2
        delim, trim = self._at_right_delim()
        if not delim:
            raise self._error("comment ends before closing delimiter")
        self._skip_right_delim(trim)

    def _at_right_delim(self) -> tuple[bool, bool]:
        text, pos = self.text, self.pos
        if _has_right_trim_marker(text, pos) and text.startswith("}}", pos + 2):
            return True, True
        return text.startswith("}}", pos), False

    def _skip_right_delim(self, trim: bool) -> None:
        self.pos += 4 if trim else 2
        if trim:
            while self.pos < len(self.text) and self.text[self.pos] in _SPACE_CHARS:
                self.pos += 1

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _at_terminator(self) -> bool:
        char = self._peek()
        if char == "" or char in _SPACE_CHARS or char in ".,|:)(":
            return True
        return self.text.startswith("}}", self.pos)

    def _lex_inside_action(self) -> None:
        text = self.text
        while True:
            delim, trim = self._at_right_delim()
            if delim:
                if self.paren_depth:
                    raise self._error("unclosed left paren")
                self._skip_right_delim(trim)
                self.items.append(_Item(_RIGHT_DELIM, "}}"))
                return
            if self.pos >= len(text):
                raise self._error("unclosed action")
            start = self.pos
            char = text[start]
            self.pos += 1
            if char in _SPACE_CHARS:
                self.pos = start
                self._lex_space()
            elif char == "=":
                self.items.append(_Item(_ASSIGN, "="))
            elif char == ":":
                if self._peek() != "=":
                    raise self._error("expected :=")
                self.pos += 1
                self.items.append(_Item(_DECLARE, ":="))
            elif char == "|":
                self.items.append(_Item(_PIPE, "|"))
            elif char == '"':
                self._lex_quote(start)
            elif char == "`":
                end = text.find("`", self.pos)
                if end < 0:
                    raise self._error("unterminated raw quoted string")
                self.pos = end + 1
                self.items.append(_Item(_RAW_STRING, text[start : self.pos]))
            elif char == "$":
                self._lex_field_or_variable(start, _VARIABLE)
            elif char == "'":
                raise UnsupportedTemplateError("character constants are not supported")
            elif char == "." and not ("0" <= self._peek() <= "9" and self._peek() != ""):
                self._lex_field_or_variable(start, _FIELD)
            elif char in "+-." or "0" <= char <= "9":
                self.pos = start
                self._lex_number()
            elif _is_alphanumeric(char):
                self.pos = start
                self._lex_identifier()
            elif char == "(":
                self.paren_depth += 1
                self.items.append(_Item(_LEFT_PAREN, "("))
            elif char == ")":
                self.paren_depth -= 1
                if self.paren_depth < 0:
                    raise self._error("unexpected right paren")
                self.items.append(_Item(_RIGHT_PAREN, ")"))
            elif char.isascii() and char.isprintable():
                self.items.append(_Item(_CHAR, char))
            else:
                raise self._error(f"unrecognized character in action: {char!r}")

    def _lex_space(self) -> None:
        text = self.text
        start = self.pos
        while self.pos < len(text) and text[self.pos] in _SPACE_CHARS:
            self.pos += 1
        # A space followed by "-}}" belongs to the trim-marked right delimiter.
        if text.startswith("-}}", self.pos):
            self.pos -= 1
            if self.pos == start:
                return
        self.items.append(_Item(_SPACE, text[start : self.pos]))

    def _lex_quote(self, start: int) -> None:
        text = self.text
        while True:
            char = self._peek()
            self.pos += 1
            if char == "\\":
                char = self._peek()
                self.pos += 1
                if char not in ("", "\n"):
                    continue
            if char in ("", "\n"):
                raise self._error("unterminated quoted string")
            if char == '"':
                break
        self.items.append(_Item(_STRING, text[start : self.pos]))

    def _lex_field_or_variable(self, start: int, typ: str) -> None:
        if self._at_terminator():
            self.items.append(_Item(_VARIABLE if typ == _VARIABLE else _DOT, self.text[start]))
            return
        while _is_alphanumeric(self._peek()):
            self.pos += 1
        if not self._at_terminator():
            raise self._error(f"bad character {self._peek()!r}")
        self.items.append(_Item(typ, self.text[start : self.pos]))

    def _lex_identifier(self) -> None:
        start = self.pos
        while _is_alphanumeric(self._peek()):
            self.pos += 1
        word = self.text[start : self.pos]
        if not self._at_terminator():
            raise self._error(f"bad character {self._peek()!r}")
        if word in _KEYWORDS:
            self.items.append(_Item(_KEYWORD, word))
        elif word == "nil":
            self.items.append(_Item(_NIL, word))
        elif word in ("true", "false"):
            self.items.append(_Item(_BOOL, word))
        else:
            self.items.append(_Item(_IDENTIFIER, word))

    def _lex_number(self) -> None:
        start = self.pos
        if not self._scan_number():
            raise self._error(f"bad number syntax: {self.text[start : self.pos]!r}")
        if self._peek() in ("+", "-") and self._peek():
            # A complex number such as 1+2i, which must end in "i".
            if not self._scan_number() or self.text[self.pos - 1] != "i":
                raise self._error(f"bad number syntax: {self.text[start : self.pos]!r}")
            raise UnsupportedTemplateError("complex numbers are not supported")
        text = self.text[start : self.pos]
        if not any(c.isdigit() for c in text):
            # No strconv parser accepts a number without digits (newNumber in node.go).
            raise self._error(f"illegal number syntax: {text!r}")
        self.items.append(_Item(_NUMBER, text))

    def _accept(self, chars: str) -> bool:
        if self._peek() and self._peek() in chars:
            self.pos += 1
            return True
        return False

    def _accept_run(self, chars: str) -> None:
        while self._accept(chars):
            pass

    def _scan_number(self) -> bool:
        """scanNumber in lex.go."""
        self._accept("+-")
        digits = "0123456789_"
        if self._accept("0"):
            if self._accept("xX"):
                digits = "0123456789abcdefABCDEF_"
            elif self._accept("oO"):
                digits = "01234567_"
            elif self._accept("bB"):
                digits = "01_"
        self._accept_run(digits)
        if self._accept("."):
            self._accept_run(digits)
        if len(digits) == 11 and self._accept("eE"):
            self._accept("+-")
            self._accept_run("0123456789_")
        if len(digits) == 23 and self._accept("pP"):
            self._accept("+-")
            self._accept_run("0123456789_")
        self._accept("i")
        if _is_alphanumeric(self._peek()) and self._peek():
            self.pos += 1
            return False
        return True


def _has_left_trim_marker(text: str, pos: int) -> bool:
    return len(text) >= pos + 2 and text[pos] == "-" and text[pos + 1] in _SPACE_CHARS


def _has_right_trim_marker(text: str, pos: int) -> bool:
    return len(text) >= pos + 2 and text[pos] in _SPACE_CHARS and text[pos + 1] == "-"


# ---- Nodes (text/template/parse/node.go) ----


@dataclass(frozen=True, slots=True)
class _Text:
    text: str


@dataclass(frozen=True, slots=True)
class _Dot:
    pass


@dataclass(frozen=True, slots=True)
class _Nil:
    pass


@dataclass(frozen=True, slots=True)
class _Bool:
    value: bool


@dataclass(frozen=True, slots=True)
class _Number:
    text: str
    is_int: bool
    int_value: int
    float_value: float


@dataclass(frozen=True, slots=True)
class _String:
    value: str


@dataclass(frozen=True, slots=True)
class _Field:
    idents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Variable:
    idents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Identifier:
    name: str


@dataclass(frozen=True, slots=True)
class _Command:
    args: tuple[Any, ...]


@dataclass(slots=True)
class _Pipe:
    decl: list[str] = field(default_factory=list)
    is_assign: bool = False
    cmds: list[_Command] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Action:
    pipe: _Pipe


@dataclass(frozen=True, slots=True)
class _Control:
    kind: str  # "if", "with" or "range"
    pipe: _Pipe
    body: list[Any]
    else_body: list[Any] | None


@dataclass(frozen=True, slots=True)
class _Break:
    pass


@dataclass(frozen=True, slots=True)
class _Continue:
    pass


@dataclass(frozen=True, slots=True)
class _End:
    pass


@dataclass(frozen=True, slots=True)
class _Else:
    pass


# ---- Parser (text/template/parse/parse.go) ----

_TERM_STARTS = frozenset(
    {_BOOL, _DOT, _FIELD, _IDENTIFIER, _NUMBER, _NIL, _RAW_STRING, _STRING, _VARIABLE, _LEFT_PAREN}
)


class _Parser:
    def __init__(self, items: list[_Item]) -> None:
        self.items = items
        self.index = 0
        self.vars = ["$"]
        self.range_depth = 0
        self.depth = 0

    def _error(self, message: str) -> ArgoTemplateError:
        return ArgoTemplateError(f"template: {message}")

    def _next(self) -> _Item:
        # Past the end, the EOF item repeats.
        item = self.items[min(self.index, len(self.items) - 1)]
        self.index += 1
        return item

    def _backup(self, count: int = 1) -> None:
        self.index -= count

    def _peek(self) -> _Item:
        return self.items[min(self.index, len(self.items) - 1)]

    def _next_non_space(self) -> _Item:
        item = self._next()
        while item.typ == _SPACE:
            item = self._next()
        return item

    def _peek_non_space(self) -> _Item:
        item = self._next_non_space()
        self._backup()
        return item

    def _unexpected(self, item: _Item, context: str) -> ArgoTemplateError:
        shown = "EOF" if item.typ == _EOF else f'"{item.val}"' if item.typ != _KEYWORD else item.val
        return self._error(f"unexpected {shown} in {context}")

    def parse(self) -> list[Any]:
        nodes: list[Any] = []
        while self._peek().typ != _EOF:
            node = self._text_or_action()
            if isinstance(node, (_End, _Else)):
                name = "end" if isinstance(node, _End) else "else"
                raise self._error(f"unexpected {{{{{name}}}}}")
            nodes.append(node)
        return nodes

    def _item_list(self) -> tuple[list[Any], Any]:
        nodes: list[Any] = []
        while self._peek_non_space().typ != _EOF:
            node = self._text_or_action()
            if isinstance(node, (_End, _Else)):
                return nodes, node
            nodes.append(node)
        raise self._error("unexpected EOF")

    def _text_or_action(self) -> Any:
        item = self._next_non_space()
        if item.typ == _TEXT:
            return _Text(item.val)
        if item.typ == _LEFT_DELIM:
            return self._action()
        raise self._unexpected(item, "input")

    def _action(self) -> Any:
        item = self._next_non_space()
        if item.typ == _KEYWORD:
            keyword = item.val
            if keyword in ("define", "template", "block"):
                raise UnsupportedTemplateError(f"`{keyword}` is not supported")
            if keyword in ("break", "continue"):
                end = self._next_non_space()
                if end.typ != _RIGHT_DELIM:
                    raise self._unexpected(end, f"{{{{{keyword}}}}}")
                if not self.range_depth:
                    raise self._error(f"{{{{{keyword}}}}} outside {{{{range}}}}")
                return _Break() if keyword == "break" else _Continue()
            if keyword == "else":
                following = self._peek_non_space()
                if following.typ == _KEYWORD and following.val in ("if", "with"):
                    return _Else()
                self._expect(_RIGHT_DELIM, "else")
                return _Else()
            if keyword == "end":
                self._expect(_RIGHT_DELIM, "end")
                return _End()
            return self._control(keyword)
        self._backup()
        return _Action(self._pipeline("command", _RIGHT_DELIM))

    def _expect(self, typ: str, context: str) -> None:
        item = self._next_non_space()
        if item.typ != typ:
            raise self._unexpected(item, context)

    def _nest(self) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise TemplateLimitError(f"templates nest deeper than {MAX_NESTING} levels")

    def _control(self, kind: str) -> _Control:
        self._nest()
        saved = len(self.vars)
        pipe = self._pipeline(kind, _RIGHT_DELIM)
        if kind == "range":
            if pipe.decl and pipe.is_assign:
                raise UnsupportedTemplateError("`range` assigning existing variables")
            self.range_depth += 1
        body, end = self._item_list()
        if kind == "range":
            self.range_depth -= 1
        else_body: list[Any] | None = None
        if isinstance(end, _Else):
            following = self._peek()
            if kind in ("if", "with") and following.typ == _KEYWORD and following.val == kind:
                self._next()
                else_body = [self._control(kind)]
            elif following.typ == _KEYWORD and following.val in ("if", "with"):
                # Only `if` chains `else if`, and only `with` chains `else with`.
                raise self._unexpected(following, "input")
            else:
                else_body, end = self._item_list()
                if not isinstance(end, _End):
                    raise self._error("expected end; found {{else}}")
        del self.vars[saved:]
        self.depth -= 1
        return _Control(kind, pipe, body, else_body)

    def _pipeline(self, context: str, end: str) -> _Pipe:
        pipe = _Pipe()
        while True:  # declarations, as the `decls:` label in parse.go
            variable = self._peek_non_space()
            if variable.typ != _VARIABLE:
                break
            self._next_non_space()
            after_variable = self._peek()
            following = self._peek_non_space()
            if following.typ in (_ASSIGN, _DECLARE):
                pipe.is_assign = following.typ == _ASSIGN
                self._next_non_space()
                pipe.decl.append(variable.val)
                self.vars.append(variable.val)
                break
            if following.typ == _CHAR and following.val == ",":
                self._next_non_space()
                pipe.decl.append(variable.val)
                self.vars.append(variable.val)
                if context == "range" and len(pipe.decl) < 2:
                    if self._peek_non_space().typ in (_VARIABLE, _RIGHT_DELIM, _RIGHT_PAREN):
                        continue
                    raise self._error("range can only initialize variables")
                raise self._error(f"too many declarations in {context}")
            self._backup(2 if after_variable.typ == _SPACE else 1)
            break
        while True:
            item = self._next_non_space()
            if item.typ == end:
                if not pipe.cmds:
                    raise self._error(f"missing value for {context}")
                for stage, cmd in enumerate(pipe.cmds[1:], start=2):
                    if isinstance(cmd.args[0], (_Bool, _Dot, _Nil, _Number, _String)):
                        raise self._error(f"non executable command in pipeline stage {stage}")
                return pipe
            if item.typ in _TERM_STARTS:
                self._backup()
                pipe.cmds.append(self._command())
            else:
                raise self._unexpected(item, context)

    def _command(self) -> _Command:
        args: list[Any] = []
        while True:
            self._peek_non_space()
            operand = self._operand()
            if operand is not None:
                args.append(operand)
            item = self._next()
            if item.typ == _SPACE:
                continue
            if item.typ in (_RIGHT_DELIM, _RIGHT_PAREN):
                self._backup()
            elif item.typ != _PIPE:
                raise self._unexpected(item, "operand")
            break
        if not args:
            raise self._error("empty command")
        return _Command(tuple(args))

    def _operand(self) -> Any:
        node = self._term()
        if node is None:
            return None
        if self._peek().typ == _FIELD:
            fields: list[str] = []
            while self._peek().typ == _FIELD:
                fields.append(self._next().val[1:])
            if isinstance(node, _Field):
                return _Field(node.idents + tuple(fields))
            if isinstance(node, _Variable):
                return _Variable(node.idents + tuple(fields))
            if isinstance(node, (_Bool, _String, _Number, _Nil, _Dot)):
                raise self._error("unexpected . after term")
            raise UnsupportedTemplateError("a field of a parenthesized expression or function")
        return node

    def _term(self) -> Any:
        item = self._next_non_space()
        typ = item.typ
        if typ == _IDENTIFIER:
            if item.val not in ARGO_CD_FUNCTIONS:
                raise self._error(f'function "{item.val}" not defined')
            return _Identifier(item.val)
        if typ == _DOT:
            return _Dot()
        if typ == _NIL:
            return _Nil()
        if typ == _VARIABLE:
            if item.val not in self.vars:
                raise self._error(f'undefined variable "{item.val}"')
            return _Variable((item.val,))
        if typ == _FIELD:
            return _Field((item.val[1:],))
        if typ == _BOOL:
            return _Bool(item.val == "true")
        if typ == _NUMBER:
            return _number(item.val)
        if typ == _LEFT_PAREN:
            self._nest()
            pipe = self._pipeline("parenthesized pipeline", _RIGHT_PAREN)
            self.depth -= 1
            return pipe
        if typ == _STRING:
            return _String(_unquote(item.val))
        if typ == _RAW_STRING:
            return _String(item.val[1:-1].replace("\r", ""))
        self._backup()
        return None


_INT64_LIMIT = 2**63


def _number(text: str) -> _Number:
    """A decimal number; Go's other notations are not supported."""
    body = text.lstrip("+-")
    mantissa = body.split("e")[0].split("E")[0]
    whole = mantissa.split(".")[0]
    valid = (
        body
        and all(c in "0123456789.eE+-" for c in body)
        and mantissa.count(".") <= 1
        and any(c.isdigit() for c in mantissa)
        and not (len(whole) > 1 and whole.startswith("0"))
    )
    if not valid:
        raise UnsupportedTemplateError(f"the number `{text}` is not supported")
    try:
        float_value = float(text)
    except ValueError:
        raise UnsupportedTemplateError(f"the number `{text}` is not supported") from None
    if math.isinf(float_value):
        raise UnsupportedTemplateError(f"the number `{text}` is not supported")
    if not any(c in body for c in ".eE"):
        int_value = int(text)
        if not -_INT64_LIMIT <= int_value < _INT64_LIMIT:
            raise UnsupportedTemplateError(f"the number `{text}` is not supported")
        return _Number(text, True, int_value, float(int_value))
    # NumberNode.IsInt: the float is integral and fits in an int64.
    is_int = float_value.is_integer() and -_INT64_LIMIT <= float_value < _INT64_LIMIT
    return _Number(text, is_int, int(float_value) if is_int else 0, float_value)


_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
}


def _unquote(quoted: str) -> str:
    """strconv.Unquote of a double-quoted string, without byte escapes over 0x7F."""
    body = quoted[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        kind = body[i + 1]
        i += 2
        if kind in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[kind])
            continue
        width = {"x": 2, "u": 4, "U": 8}.get(kind)
        if width is not None:
            digits = body[i : i + width]
            if len(digits) != width or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ArgoTemplateError("template: invalid syntax")
            code = int(digits, 16)
            i += width
        elif kind in "01234567":
            digits = body[i - 1 : i + 2]
            if len(digits) != 3 or any(c not in "01234567" for c in digits):
                raise ArgoTemplateError("template: invalid syntax")
            code = int(digits, 8)
            if code > 255:
                raise ArgoTemplateError("template: invalid syntax")
            i += 2
        else:
            raise ArgoTemplateError("template: invalid syntax")
        if kind in "uU":
            if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                raise ArgoTemplateError("template: invalid syntax")
        elif code > 0x7F:
            # A single byte, not valid UTF-8 on its own.
            raise UnsupportedTemplateError("byte escapes over 0x7F are not supported")
        out.append(chr(code))
    return "".join(out)


# ---- Executor (text/template/exec.go) ----


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class _State:
    def __init__(self, data: Any, missing_error: bool, budget: Budget) -> None:
        self.vars: list[list[Any]] = [["$", data]]
        self.out: list[str] = []
        self.missing_error = missing_error
        self.no_value = False
        self.budget = budget
        self.size = 0
        self.iterations = 0

    def _error(self, message: str) -> ArgoTemplateError:
        return ArgoTemplateError(f"template: {message}")

    def write(self, text: str) -> None:
        self.size += len(text)
        if self.size > MAX_OUTPUT:
            raise TemplateLimitError(
                f"rendering one string outputs more than {MAX_OUTPUT // 1024} KiB"
            )
        self.budget.write(len(text))
        self.budget.spend(len(text) // _BYTES_PER_OPERATION)
        self.out.append(text)

    def walk(self, dot: Any, nodes: list[Any]) -> None:
        for node in nodes:
            self.budget.spend()
            if isinstance(node, _Text):
                self.write(node.text)
            elif isinstance(node, _Action):
                value = self.eval_pipeline(dot, node.pipe)
                if not node.pipe.decl:
                    self.print_value(value)
            elif isinstance(node, _Control):
                if node.kind == "range":
                    self.walk_range(dot, node)
                else:
                    self.walk_if_or_with(dot, node)
            elif isinstance(node, _Break):
                raise _BreakSignal
            elif isinstance(node, _Continue):
                raise _ContinueSignal

    def print_value(self, value: Any) -> None:
        if value is MISSING:
            self.no_value = True
            self.write("<no value>")
        else:
            self.write(_go_print(value))

    def walk_if_or_with(self, dot: Any, node: _Control) -> None:
        mark = len(self.vars)
        value = self.eval_pipeline(dot, node.pipe)
        if _is_true(value):
            self.walk(value if node.kind == "with" else dot, node.body)
        elif node.else_body is not None:
            self.walk(dot, node.else_body)
        del self.vars[mark:]

    def walk_range(self, dot: Any, node: _Control) -> None:
        outer_mark = len(self.vars)
        value = self.eval_pipeline(dot, node.pipe)
        mark = len(self.vars)
        decl = node.pipe.decl

        def one_iteration(index: Any, elem: Any) -> None:
            self.iterations += 1
            if self.iterations > MAX_RANGE_ITERATIONS:
                raise TemplateLimitError(
                    f"rendering one string iterates `range` more than {MAX_RANGE_ITERATIONS} times"
                )
            # The pipeline pushed its variables: the top one gets the element,
            # the one below it (with two variables) the index.
            if decl:
                self.vars[-1][1] = elem
            if len(decl) > 1:
                self.vars[-2][1] = index
            try:
                self.walk(elem, node.body)
            except _ContinueSignal:
                pass
            finally:
                del self.vars[mark:]

        iterated = False
        try:
            if type(value) is int:
                if len(decl) > 1:
                    raise self._error(f"can't use {value} to iterate over more than one variable")
                for number in range(value):
                    iterated = True
                    one_iteration(MISSING, number)
            elif isinstance(value, list):
                for index, elem in enumerate(value):
                    iterated = True
                    one_iteration(index, elem)
            elif isinstance(value, dict):
                for key in sorted(value):
                    iterated = True
                    one_iteration(key, value[key])
            elif value is not MISSING:
                raise self._error(f"range can't iterate over {_go_print(value)}")
        except _BreakSignal:
            pass
        if not iterated and node.else_body is not None:
            self.walk(dot, node.else_body)
        del self.vars[outer_mark:]

    def eval_pipeline(self, dot: Any, pipe: _Pipe) -> Any:
        value = _NOT_GIVEN
        for cmd in pipe.cmds:
            value = self.eval_command(dot, cmd, value)
            if value is None:
                value = MISSING
        for name in pipe.decl:
            if pipe.is_assign:
                self.set_var(name, value)
            else:
                self.vars.append([name, value])
        return value

    def not_a_function(self, args: tuple[Any, ...], final: Any) -> None:
        if len(args) > 1 or final is not _NOT_GIVEN:
            raise self._error("can't give argument to non-function")

    def eval_command(self, dot: Any, cmd: _Command, final: Any) -> Any:
        self.budget.spend()
        first = cmd.args[0]
        if isinstance(first, _Field):
            return self.eval_field_chain(dot, first.idents, cmd.args, final)
        if isinstance(first, _Variable):
            value = self.var_value(first.idents[0])
            if len(first.idents) == 1:
                self.not_a_function(cmd.args, final)
                return value
            return self.eval_field_chain(value, first.idents[1:], cmd.args, final)
        if isinstance(first, _Identifier):
            return self.eval_function(dot, first.name, cmd.args, final)
        if isinstance(first, _Pipe):
            self.not_a_function(cmd.args, final)
            return self.eval_pipeline(dot, first)
        self.not_a_function(cmd.args, final)
        if isinstance(first, _Bool):
            return first.value
        if isinstance(first, _Dot):
            return dot
        if isinstance(first, _Nil):
            raise self._error("nil is not a command")
        if isinstance(first, _Number):
            return _ideal_constant(first)
        assert isinstance(first, _String)
        return first.value

    def eval_field_chain(
        self, receiver: Any, idents: tuple[str, ...], args: tuple[Any, ...], final: Any
    ) -> Any:
        for name in idents[:-1]:
            receiver = self.eval_field(name, (), _NOT_GIVEN, receiver)
        return self.eval_field(idents[-1], args, final, receiver)

    def eval_field(self, name: str, args: tuple[Any, ...], final: Any, receiver: Any) -> Any:
        if receiver is MISSING:
            if self.missing_error:
                raise self._error(f'nil data; no entry for key "{name}"')
            return MISSING
        if receiver is None:
            raise self._error(f"nil pointer evaluating interface {{}}.{name}")
        if isinstance(receiver, dict):
            if len(args) > 1 or final is not _NOT_GIVEN:
                raise self._error(f"{name} is not a method but has arguments")
            if name in receiver:
                return receiver[name]
            if self.missing_error:
                raise self._error(f'map has no entry for key "{name}"')
            return MISSING
        raise self._error(f"can't evaluate field {name} of {_go_print(receiver)}")

    def eval_function(self, dot: Any, name: str, args: tuple[Any, ...], final: Any) -> Any:
        """evalCall in exec.go: check the argument count, convert each
        argument to its parameter's type, then call."""
        signature = _FUNCTIONS.get(name)
        if signature is None:
            raise UnsupportedFunctionError(name)
        nodes = args[1:]
        count = len(nodes) + (final is not _NOT_GIVEN)
        fixed = len(signature.params)
        if count < fixed or (signature.variadic is None and count != fixed):
            raise self._error(f"wrong number of args for {name}: want {fixed} got {count}")
        if name in ("and", "or"):
            return self.eval_and_or(name, dot, nodes, final)

        def kind(index: int) -> str:
            return signature.params[index] if index < fixed else str(signature.variadic)

        values = [self.eval_arg(dot, kind(index), node) for index, node in enumerate(nodes)]
        if final is not _NOT_GIVEN:
            values.append(self.validate(final, kind(len(nodes))))
        try:
            # A call costs more than a node, and its strings cost by their size.
            size = sum(len(value) for value in values if isinstance(value, str))
            weight = _BYTE_WEIGHTS.get(name, 1)
            self.budget.spend(_CALL_COST + weight * size // _BYTES_PER_OPERATION)
            result = signature.function(*values)
            if isinstance(result, str):
                self.budget.spend(weight * len(result) // _BYTES_PER_OPERATION)
        except functions.TooLarge as exc:
            raise TemplateLimitError(str(exc)) from None
        except functions.GoCallError as exc:
            raise self._error(f"error calling {name}: {exc}") from None
        except functions.NotSupported as exc:
            raise UnsupportedTemplateError(str(exc)) from None
        if isinstance(result, str) and len(result) > MAX_OUTPUT:
            raise TemplateLimitError(
                f"rendering one string outputs more than {MAX_OUTPUT // 1024} KiB"
            )
        return result

    def eval_and_or(self, name: str, dot: Any, nodes: tuple[Any, ...], final: Any) -> Any:
        """The special case of evalCall: `and` and `or` stop at the first
        argument that decides, and return it."""
        value: Any = MISSING
        for node in nodes:
            value = self.eval_arg(dot, "value", node)
            if _is_true(value) == (name == "or"):
                return value
        if final is not _NOT_GIVEN:
            value = self.validate(final, "value")
        return value

    def eval_arg(self, dot: Any, kind: str, node: Any) -> Any:
        """evalArg in exec.go."""
        if isinstance(node, _Dot):
            return self.validate(dot, kind)
        if isinstance(node, _Nil):
            if kind in ("any", "value"):
                return None if kind == "any" else MISSING
            raise self._error(f"cannot assign nil to {_GO_TYPES[kind]}")
        if isinstance(node, _Field):
            value = self.eval_field_chain(dot, node.idents, (node,), _NOT_GIVEN)
            return self.validate(value, kind)
        if isinstance(node, _Variable):
            value = self.var_value(node.idents[0])
            if len(node.idents) > 1:
                value = self.eval_field_chain(value, node.idents[1:], (), _NOT_GIVEN)
            return self.validate(value, kind)
        if isinstance(node, _Pipe):
            return self.validate(self.eval_pipeline(dot, node), kind)
        if isinstance(node, _Identifier):
            return self.validate(self.eval_function(dot, node.name, (node,), _NOT_GIVEN), kind)
        # A literal: evalEmptyInterface for interface{} parameters, else
        # evalString, evalInteger or evalBool.
        if kind in ("any", "value"):
            if isinstance(node, _Number):
                return _ideal_constant(node)
            return node.value
        if kind == "string" and isinstance(node, _String):
            return node.value
        if kind == "int" and isinstance(node, _Number) and node.is_int:
            return node.int_value
        if kind == "bool" and isinstance(node, _Bool):
            return node.value
        expected = {"string": "string", "int": "integer", "bool": "bool"}[kind]
        raise self._error(f"expected {expected}")

    def validate(self, value: Any, kind: str) -> Any:
        """validateType in exec.go: a value converted to a parameter type."""
        if kind == "value":
            return value
        if kind == "any":
            return None if value is MISSING else value
        if value is MISSING:
            raise self._error(f"invalid value; expected {_GO_TYPES[kind]}")
        matches = {
            "string": isinstance(value, str),
            "int": type(value) is int,
            "bool": isinstance(value, bool),
        }[kind]
        if not matches:
            raise self._error(f"wrong type for value; expected {_GO_TYPES[kind]}")
        return value

    def var_value(self, name: str) -> Any:
        for entry in reversed(self.vars):
            if entry[0] == name:
                return entry[1]
        raise self._error(f"undefined variable: {name}")

    def set_var(self, name: str, value: Any) -> None:
        for entry in reversed(self.vars):
            if entry[0] == name:
                entry[1] = value
                return
        raise self._error(f"undefined variable: {name}")


def _ideal_constant(number: _Number) -> Any:
    """idealConstant in exec.go: a float if written as one, an int otherwise."""
    if any(c in number.text for c in ".eE"):
        return number.float_value
    return number.int_value


# ---- Functions ----
#
# Parameter kinds: "any" is interface{} (a missing value becomes nil),
# "value" is reflect.Value (text/template's builtins, which see a missing
# value as such), "string", "int" and "bool" are checked Go types.

_GO_TYPES = {"string": "string", "int": "int", "bool": "bool"}


@dataclass(frozen=True, slots=True)
class _Signature:
    function: Callable[..., Any]
    params: tuple[str, ...]
    variadic: str | None = None


def _indirect_interface(value: Any) -> Any:
    """indirectInterface in exec.go: a nil interface{} is a missing value."""
    return MISSING if value is None else value


def _basic_kind(value: Any) -> str:
    """basicKind in funcs.go: lists and maps have no basic kind, like a
    missing value."""
    if isinstance(value, bool):
        return "bool"
    if type(value) is int:
        return "int"
    if isinstance(value, float):
        return "float64"
    if isinstance(value, str):
        return "string"
    return "invalid"


def _eq(arg1: Any, *others: Any) -> bool:
    """eq in funcs.go."""
    arg1 = _indirect_interface(arg1)
    if not others:
        raise functions.GoCallError("missing argument for comparison")
    kind1 = _basic_kind(arg1)
    for other in others:
        other = _indirect_interface(other)
        kind2 = _basic_kind(other)
        if kind1 != kind2:
            if arg1 is not MISSING and other is not MISSING:
                raise functions.GoCallError(
                    f"incompatible types for comparison: {kind1} and {kind2}"
                )
            continue
        if kind1 == "invalid":
            # A missing value equals only another; lists and maps are not
            # comparable (canCompare, then Type().Comparable()).
            if arg1 is MISSING and other is MISSING:
                return True
            if arg1 is MISSING or other is MISSING:
                continue
            raise functions.GoCallError("non-comparable type")
        if arg1 == other:
            return True
    return False


def _ne(arg1: Any, arg2: Any) -> bool:
    return not _eq(arg1, arg2)


def _not(value: Any) -> bool:
    return not _is_true(value)


def _index(item: Any, *indexes: Any) -> Any:
    """index in funcs.go."""
    item = _indirect_interface(item)
    if item is MISSING:
        raise functions.GoCallError("index of untyped nil")
    for index in indexes:
        index = _indirect_interface(index)
        if item is None:
            raise functions.GoCallError("index of nil pointer")
        if isinstance(item, (list, str)):
            if index is MISSING:
                raise functions.GoCallError("cannot index slice/array with nil")
            if type(index) is not int:
                raise functions.GoCallError("cannot index slice/array with this type")
            if not 0 <= index < len(item):
                raise functions.GoCallError(f"index out of range: {index}")
            if isinstance(item, str):
                raise functions.NotSupported("indexing a string (a byte) is not supported")
            item = item[index]
        elif isinstance(item, dict):
            if not isinstance(index, str):
                raise functions.GoCallError("a map index must be a string")
            # A missing key gives the zero value: nil, or "" for a map of strings.
            item = item.get(index, "" if isinstance(item, GoStringMap) else None)
        else:
            raise functions.GoCallError("can't index this item")
    return item


_FUNCTIONS: dict[str, _Signature] = {
    # text/template builtins (funcs.go)
    "and": _Signature(_not, ("value",), "value"),  # evaluated by eval_and_or
    "or": _Signature(_not, ("value",), "value"),  # evaluated by eval_and_or
    "not": _Signature(_not, ("value",)),
    "eq": _Signature(_eq, ("value",), "value"),
    "ne": _Signature(_ne, ("value", "value")),
    "index": _Signature(_index, ("value",), "value"),
    "printf": _Signature(functions.sprintf, ("string",), "any"),
    # sprig
    "default": _Signature(functions.default, ("any",), "any"),
    "lower": _Signature(functions.lower, ("string",)),
    "upper": _Signature(functions.upper, ("string",)),
    "replace": _Signature(functions.replace, ("string", "string", "string")),
    "trimPrefix": _Signature(functions.trim_prefix, ("string", "string")),
    "trimSuffix": _Signature(functions.trim_suffix, ("string", "string")),
    "quote": _Signature(functions.quote, (), "any"),
    "cat": _Signature(functions.cat, (), "any"),
    "indent": _Signature(functions.indent, ("int", "string")),
    "base": _Signature(functions.base, ("string",)),
    "dig": _Signature(functions.dig, (), "any"),
    "toJson": _Signature(functions.to_json, ("any",)),
    "mustToPrettyJson": _Signature(functions.to_pretty_json, ("any",)),
    # Argo CD
    "toYaml": _Signature(functions.to_yaml, ("any",)),
    "normalize": _Signature(functions.normalize, ("string",)),
    "slugify": _Signature(functions.slugify, (), "any"),
}
SUPPORTED_FUNCTIONS = frozenset(_FUNCTIONS)


# ---- Entry point ----


def _missing_key_error(options: Sequence[str]) -> bool:
    """Whether goTemplateOptions select missingkey=error (setOption in option.go)."""
    mode = "default"
    for option in options:
        key, _, value = str(option).partition("=")
        if key == "missingkey" and value in ("default", "invalid", "zero", "error"):
            mode = "default" if value == "invalid" else value
        else:
            # text/template panics on an unknown option.
            raise ArgoTemplateError(f"unrecognized goTemplateOptions entry `{option}`")
    if mode == "zero":
        raise UnsupportedTemplateError("the option `missingkey=zero` is not supported")
    return mode == "error"


def render(
    template: str,
    params: dict[str, Any],
    options: Sequence[str] = (),
    budget: Budget | None = None,
) -> str:
    """The text Argo CD's Render.Replace produces from `template` and `params`.

    Raises ArgoTemplateError when Argo CD's rendering fails,
    UnsupportedTemplateError when this module can't tell what it produces, and
    TemplateLimitError past a bound. `budget` is shared by the strings of one
    ApplicationSet.
    """
    missing_error = _missing_key_error(options)
    if len(template) > MAX_TEMPLATE_LENGTH:
        raise TemplateLimitError(
            f"a template string is longer than {MAX_TEMPLATE_LENGTH // 1024} KiB"
        )
    state = _State(params, missing_error, budget or Budget())
    try:
        nodes = _Parser(_Lexer(template).lex()).parse()
        state.walk(params, nodes)
    except RecursionError:
        raise TemplateLimitError("template values nest too deeply") from None
    if state.no_value:
        raise NoValueError("the template prints `<no value>` for a missing or null value")
    return "".join(state.out)
