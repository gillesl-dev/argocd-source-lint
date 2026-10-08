from __future__ import annotations

_STAR = "star"
_DOUBLE_STAR = "double_star"
_ANY = "any"
_LITERAL = "literal"


def match_glob(pattern: str, candidate: str) -> bool:
    """ArgoCD's glob semantics, shared by the `git` ApplicationSet
    generator (`directories`/`files`) and `project-scope-violation`
    (`sourceRepos`/`destinations`): `*` matches within one `/`-separated
    segment, `**` crosses `/`, unlike `fnmatch`, where a lone `*`
    already crosses `/`. `namespace`/`server`/`name` patterns never
    contain `/` in practice, so this is equally correct there.

    Matched with a dynamic-programming scan over `candidate`, not by
    translating to a backtracking regex (the previous implementation,
    `re.match("^" + ".*".join(escaped) + "$", candidate)`); confirmed
    for real, not theoretical: a pattern with ~25 `*`s and a ~40-character
    non-matching candidate (either one entirely repo-controlled: an
    ApplicationSet generator's own `path:`, or an `AppProject`'s
    `sourceRepos`/`destinations`) hung indefinitely, a classic ReDoS
    shape (`literal*literal*literal*...`, ambiguous about which `*`
    consumed which character, forcing exponential backtracking on a
    string that ultimately doesn't match). This scan is O(len(pattern)
    x len(candidate)) worst case, regardless of how many wildcards the
    pattern has."""
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
