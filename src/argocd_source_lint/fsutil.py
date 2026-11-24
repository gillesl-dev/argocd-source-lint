from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# Round-trip mode is the only one that keeps line/column info (`.lc`), which
# `loader.py` needs for `Finding.line`. The results still behave as dict/list.
_yaml = YAML(typ="rt")

# Nested anchors referencing each other expand to 2^N nodes when something
# like `str()` materializes them ("billion laughs"). ruamel shares the aliased
# object, so parsing is cheap, but any later walk is not. Documents over this
# expanded size are rejected at load time (see DESIGN.md).
_MAX_EXPANDED_NODES = 1_000_000


def is_within_budget(node: Any, memo: dict[int, int] | None = None) -> bool:
    """Whether `node`, fully expanded, stays within `_MAX_EXPANDED_NODES` nodes.

    `memo`, keyed by `id()`, sizes each shared subtree once, so the cost is
    proportional to the distinct nodes, not the expanded count.
    """
    memo = {} if memo is None else memo
    try:
        return _expanded_size(node, memo, _MAX_EXPANDED_NODES) is not None
    except RecursionError:
        return False  # nested deeper than any real manifest


# Marks a node being visited. A YAML alias can only reference an anchor that is
# already resolved, so cycles should not occur, but this does not rely on it.
_VISITING = -1


def _expanded_size(node: Any, memo: dict[int, int], budget: int) -> int | None:
    if not isinstance(node, (dict, list)):
        return 1

    node_id = id(node)
    if node_id in memo:
        return None if memo[node_id] == _VISITING else memo[node_id]

    memo[node_id] = _VISITING
    total = 1
    for child in node.values() if isinstance(node, dict) else node:
        child_size = _expanded_size(child, memo, budget)
        if child_size is None:
            return None
        total += child_size
        if total > budget:
            return None

    memo[node_id] = total
    return total


def walk_tree(directory: Path) -> Iterator[Path]:
    """Every file and directory under `directory`, skipping `.git`.

    Stops at already visited directories. A Windows junction is not a
    symlink to `Path.rglob` or `os.walk(followlinks=False)`, so a junction
    to an ancestor would loop forever (see DESIGN.md).
    """
    yield from _walk(directory, set())


def _walk(directory: Path, seen: set[Path]) -> Iterator[Path]:
    try:
        resolved = directory.resolve()
    except OSError:
        return
    if resolved in seen:
        return
    seen.add(resolved)

    try:
        entries = list(directory.iterdir())
    except OSError:
        return

    for entry in entries:
        if entry.name == ".git":
            continue
        yield entry
        if entry.is_dir():
            yield from _walk(entry, seen)


def iter_yaml_files(
    directory: Path, *, recurse: bool = True, suffixes: tuple[str, ...] = (".yaml", ".yml")
) -> Iterator[Path]:
    """Files with one of `suffixes` under `directory`.

    `recurse=False`, Argo CD's default for a directory source, stays at the top level.
    """
    if not recurse:
        for path in directory.iterdir():
            if path.is_file() and path.name.endswith(suffixes):
                yield path
        return

    for path in walk_tree(directory):
        if path.is_file() and path.name.endswith(suffixes):
            yield path


_LOADED_DOCUMENTS_CACHE: dict[Path, list[dict[str, Any]]] = {}
_UNREADABLE_ARGOCD_FILES: dict[Path, str] = {}


def _looks_like_argocd_manifest(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "argoproj.io/" in text and "{{" not in text


def unreadable_argocd_files() -> dict[Path, str]:
    """Files that mention Argo CD resources but are not valid YAML, with the error."""
    return dict(_UNREADABLE_ARGOCD_FILES)


# Files skipped as nested too deeply, and whether they mention Argo CD
# resources, in which case they may hold an Application.
_SKIPPED_FILES: dict[Path, bool] = {}


def skipped_files() -> dict[Path, bool]:
    """Files skipped for `SKIPPED_REASON`: path to "may hold an Application"."""
    return dict(_SKIPPED_FILES)


class FlowDepthError(ValueError):
    pass


# ruamel can't parse `[`/`{` nested deeper than about 247 levels (it recurses
# once per level, under Python's limit of 1,000 frames), and its scanner then
# spends seconds per file before failing. Any file deeper than this was
# already unreadable, so it is skipped before parsing.
MAX_FLOW_DEPTH = 250
# Brackets counted without regard to quotes: bounds a file whose quotes would
# hide its real nesting from the count above. No file has this many unclosed.
_MAX_RAW_FLOW_DEPTH = 1_000
_FLOW_CHARS = re.compile(r"[\[\]{}]")
_BLOCK_SCALAR_HEADER = re.compile(r"(?:^|[\s:-])[|>][-+0-9]*$")
_SIGNIFICANT_CHARS = re.compile(r"['\"\[\]{}#|>]")
SKIPPED_REASON = f"nesting deeper than {MAX_FLOW_DEPTH} levels, skipped"


def check_flow_depth(text: str) -> None:
    """Raise FlowDepthError when `[`/`{` nest deeper than `MAX_FLOW_DEPTH`.

    Brackets in quoted strings, comments and block scalars (`|`, `>`) are not
    nesting and are not counted.
    """
    raw = 0
    for match in _FLOW_CHARS.finditer(text):
        if match.group() in "[{":
            raw += 1
            if raw > _MAX_RAW_FLOW_DEPTH:
                raise FlowDepthError(SKIPPED_REASON)
        elif raw:
            raw -= 1
    if _flow_depth(text) > MAX_FLOW_DEPTH:
        raise FlowDepthError(SKIPPED_REASON)


def _flow_depth(text: str) -> int:
    """The deepest `[`/`{` nesting outside quotes, comments and block scalars."""
    depth = deepest = 0
    quote = ""
    block_indent: int | None = None
    for line in text.splitlines():
        content = line.lstrip(" ")
        indent = len(line) - len(content)
        if block_indent is not None:
            if not content.strip() or indent > block_indent:
                continue  # inside a block scalar
            block_indent = None
        if not _SIGNIFICANT_CHARS.search(line):
            continue  # changes neither the depth nor the quote or block state
        kept: list[str] = []
        previous = " "  # a quote only opens a scalar at the start of a token
        i = 0
        while i < len(line):
            char = line[i]
            if quote:
                if char == quote == "'" and line[i + 1 : i + 2] == "'":
                    i += 1  # '' is an escaped quote
                elif char == "\\" and quote == '"':
                    i += 1  # skip the escaped character
                elif char == quote:
                    quote, previous = "", char
                i += 1
                continue
            if char == "#" and previous in " \t":
                break  # a comment
            if char in "'\"" and previous in " \t[{,:?-":
                quote = char
            elif char in "[{":
                depth += 1
                deepest = max(deepest, depth)
            elif char in "]}" and depth:
                depth -= 1
            if not quote:
                kept.append(char)
                previous = char
            i += 1
        if not quote and not depth and _BLOCK_SCALAR_HEADER.search("".join(kept).rstrip()):
            block_indent = indent
    return deepest


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    """Mapping documents in a YAML file, possibly multi-document.

    Unparseable files, such as Helm templates, and documents over the alias
    budget are dropped. Cached by resolved path until `clear_caches()`,
    since several rules and discovery passes read the same files.
    """
    resolved = path.resolve()
    if resolved in _LOADED_DOCUMENTS_CACHE:
        return _LOADED_DOCUMENTS_CACHE[resolved]

    docs: list[Any] = []
    try:
        text = path.read_text(encoding="utf-8")
        check_flow_depth(text)
        for doc in _yaml.load_all(text):
            docs.append(doc)
    except (FlowDepthError, RecursionError):
        # Too deep to parse: skipped whole, and said so (`skipped_files`).
        docs = []
        _SKIPPED_FILES[resolved] = "argoproj.io/" in text
    except (YAMLError, UnicodeDecodeError) as exc:
        # Documents before the error are kept. A Helm template is expected to
        # fail; a plain file declaring Argo CD resources is not, so say so.
        if _looks_like_argocd_manifest(path):
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            _UNREADABLE_ARGOCD_FILES[resolved] = first_line or type(exc).__name__
    result = [doc for doc in docs if isinstance(doc, dict) and is_within_budget(doc)]
    _LOADED_DOCUMENTS_CACHE[resolved] = result
    return result


_DISCOVERED_DOCUMENTS_CACHE: dict[Path, list[tuple[Path, dict[str, Any]]]] = {}


def discover_documents(repo_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every YAML document in the repo, as `(manifest_path, doc)` pairs.

    Shared by the Application, ApplicationSet and AppProject discovery
    passes and cached by `repo_root`. Callers that modify the repo between
    calls must call `clear_caches()`.
    """
    if repo_root in _DISCOVERED_DOCUMENTS_CACHE:
        return _DISCOVERED_DOCUMENTS_CACHE[repo_root]

    documents = [
        (manifest_path, doc)
        for manifest_path in sorted(iter_yaml_files(repo_root))
        for doc in load_yaml_documents(manifest_path)
    ]
    _DISCOVERED_DOCUMENTS_CACHE[repo_root] = documents
    return documents


def clear_caches() -> None:
    """Reset the document caches. Called at the start of each lint run."""
    _DISCOVERED_DOCUMENTS_CACHE.clear()
    _LOADED_DOCUMENTS_CACHE.clear()
    _UNREADABLE_ARGOCD_FILES.clear()
    _SKIPPED_FILES.clear()
