from __future__ import annotations

import fnmatch

_STAR = "star"
_DOUBLE_STAR = "double_star"
_ANY = "any"
_LITERAL = "literal"


def match_glob(pattern: str, candidate: str) -> bool:
    """Match Argo CD's glob syntax: `*` stays within a `/` segment, `**` crosses it.

    Patterns come from the repo, so this is a dynamic-programming scan,
    O(len(pattern) * len(candidate)), rather than a backtracking regex
    open to ReDoS.
    """
    tokens = _tokenize(pattern)
    length = len(candidate)
    reachable = [False] * (length + 1)
    reachable[0] = True

    for kind, value in tokens:
        next_reachable = [False] * (length + 1)
        if kind in (_STAR, _DOUBLE_STAR):
            next_reachable[0] = reachable[0]
            for j in range(1, length + 1):
                extends = candidate[j - 1] != "/" if kind is _STAR else True
                next_reachable[j] = reachable[j] or (next_reachable[j - 1] and extends)
        elif kind is _ANY:
            for j in range(1, length + 1):
                next_reachable[j] = reachable[j - 1]
        else:  # _LITERAL
            for j in range(1, length + 1):
                next_reachable[j] = reachable[j - 1] and candidate[j - 1] == value
        reachable = next_reachable

    return reachable[length]


def _tokenize(pattern: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                tokens.append((_DOUBLE_STAR, ""))
                i += 2
            else:
                tokens.append((_STAR, ""))
                i += 1
        elif char == "?":
            tokens.append((_ANY, ""))
            i += 1
        else:
            tokens.append((_LITERAL, char))
            i += 1
    return tokens


def match_path(pattern: str, candidate: str) -> bool:
    """Match Go's `path.Match`, used by the git directory generator: `*`, `?` and
    `[...]` stay within a `/` segment, so `**` is the same as `*`."""
    pattern_segments = pattern.split("/")
    candidate_segments = candidate.split("/")
    if len(pattern_segments) != len(candidate_segments):
        return False
    return all(
        _match_segment(segment, value)
        for segment, value in zip(pattern_segments, candidate_segments, strict=True)
    )


def _match_segment(pattern: str, value: str) -> bool:
    if "[" in pattern:
        # Go negates a class with `^`, fnmatch with `!`.
        return fnmatch.fnmatchcase(value, pattern.replace("[^", "[!"))
    return match_glob(pattern, value)


# `{a,b}{a,b}...` doubles the alternatives with each group, and the pattern
# comes from the repository. Real patterns stay far below this.
_MAX_BRACE_ALTERNATIVES = 256


def expand_braces(pattern: str) -> list[str] | None:
    """The patterns `{a,b}` alternatives stand for, as in gobwas/glob, which
    Argo CD uses for `directory.include` and AppProject destinations.

    None when they would exceed `_MAX_BRACE_ALTERNATIVES`.
    """
    expanded: list[str] = []
    pending = [pattern]
    while pending:
        current = pending.pop()
        alternatives = _expand_first_braces(current)
        if alternatives is None:
            expanded.append(current)
        else:
            pending.extend(alternatives)
        if len(expanded) + len(pending) > _MAX_BRACE_ALTERNATIVES:
            return None
    return expanded


def _expand_first_braces(pattern: str) -> list[str] | None:
    """`pattern` with its first `{...}` group expanded, or None without one."""
    start = pattern.find("{")
    if start == -1:
        return None
    depth = 0
    parts: list[str] = []
    part_start = start + 1
    for index in range(start, len(pattern)):
        char = pattern[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parts.append(pattern[part_start:index])
                head, tail = pattern[:start], pattern[index + 1 :]
                return [head + part.strip() + tail for part in parts]
        elif char == "," and depth == 1:
            parts.append(pattern[part_start:index])
            part_start = index + 1
    return None  # unbalanced: matched literally


def match_git_pathspec(pattern: str, candidate: str) -> bool:
    """Match `git ls-files -- <pattern>`, which the git files generator runs by
    default: `*` and `?` cross `/`, and a pattern without them also matches the
    files under a directory of that name."""
    if not any(char in pattern for char in "*?["):
        prefix = pattern.strip("/")
        return candidate == prefix or candidate.startswith(prefix + "/")
    return fnmatch.fnmatchcase(candidate, pattern)


def match_doublestar(pattern: str, candidate: str) -> bool:
    """Match doublestar's glob, used by the git files generator with
    `--enable-new-git-file-globbing`: `*` stays within a `/` segment, and `**`
    also matches no directory at all."""
    for alternative in expand_braces(pattern) or []:
        variants = {alternative, alternative.replace("/**/", "/")}
        if alternative.startswith("**/"):
            variants.add(alternative[3:])
        for variant in variants:
            if "[" in variant:
                if fnmatch.fnmatchcase(candidate, variant):
                    return True
            elif match_glob(variant, candidate):
                return True
    return False
