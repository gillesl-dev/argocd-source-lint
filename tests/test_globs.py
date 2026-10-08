from __future__ import annotations

import time

from argocd_source_lint.globs import match_glob


def test_exact_literal_match():
    assert match_glob("apps/foo", "apps/foo") is True
    assert match_glob("apps/foo", "apps/bar") is False


def test_star_matches_within_one_segment():
    assert match_glob("apps/*", "apps/foo") is True
    assert match_glob("apps/*", "apps/foo/bar") is False  # * doesn't cross /


def test_double_star_crosses_segments():
    assert match_glob("apps/**", "apps/foo/bar") is True
    assert match_glob("**/foo.yaml", "clusters/dev/foo.yaml") is True


def test_question_mark_matches_exactly_one_character():
    assert match_glob("app?.yaml", "app1.yaml") is True
    assert match_glob("app?.yaml", "app12.yaml") is False
    assert match_glob("app?.yaml", "app.yaml") is False  # requires exactly one char


def test_multiple_stars_in_one_pattern():
    assert match_glob("a*b*c", "aXXbYYc") is True
    assert match_glob("a*b*c", "abc") is True
    assert match_glob("a*b*c", "ac") is False  # missing b


def test_regex_metacharacters_in_pattern_are_treated_as_literal():
    """The pattern is matched directly, never translated into a regex:
    a literal `.`/`+`/`(` in a real path (e.g. `clusters/dev.json`) must
    never be interpreted as a regex metacharacter."""
    assert match_glob("clusters/*.json", "clusters/dev.json") is True
    assert match_glob("clusters/*.json", "clusters/devXjson") is False
    assert match_glob("a(b)+c", "a(b)+c") is True


def test_empty_pattern_matches_only_empty_candidate():
    assert match_glob("", "") is True
    assert match_glob("", "x") is False


def test_pattern_with_trailing_wildcard_after_full_match():
    assert match_glob("apps/*", "apps/") is True


def test_a_pattern_that_previously_caused_catastrophic_backtracking_is_fast():
    """Confirmed for real before the fix: `re.match("^" + ".*".join(...)
    + "$", candidate)` on a pattern shaped like `*a*a*a...*a!` against a
    non-matching candidate of `a`s hung indefinitely: a classic ReDoS
    (repeated wildcard/literal pairs, ambiguous about which wildcard
    consumed which character, on a string that ultimately can't match).
    Both the pattern and the candidate are repo-controlled (an
    ApplicationSet generator's own `path:`, or an AppProject's
    `sourceRepos`/`destinations`), so this had to be fixed with a
    matching algorithm that's polynomial by construction, not by
    bounding input size."""
    pattern = "*a" * 40 + "!"
    candidate = "a" * 60

    start = time.perf_counter()
    result = match_glob(pattern, candidate)
    elapsed = time.perf_counter() - start

    assert result is False  # candidate has no "!"; genuinely no match
    assert elapsed < 1.0
