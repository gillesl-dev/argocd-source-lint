"""Unicode categories shared with Go, on whichever Python runs the tests.

Go 1.25 (Argo CD v3.3.3) ships Unicode 15.0; Python 3.11 to 3.14 ship 14.0 to
16.0. Where gotemplate depends on categories (printable for quote and
printf %q, letters and digits for identifiers and toYaml's key order), it
refuses the code points of unicode_unstable.UNSTABLE_RANGES. This test checks,
on the running Python, that every other code point has Go's category
(tests/fixtures/go_unicode_categories.json, from tools/argocd-oracle).
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

import pytest

from argocd_source_lint import gotemplate
from argocd_source_lint.gotemplate import UnsupportedTemplateError
from argocd_source_lint.unicode_unstable import UNSTABLE_RANGES, is_unstable

GO_CATEGORIES = json.loads(
    (Path(__file__).parent / "fixtures" / "go_unicode_categories.json").read_text(encoding="utf-8")
)

# A letter added in Unicode 16.0 (Python 3.14), unassigned in Go 1.25.
NEW_LETTER = "\U000105c0"
# A letter added in Unicode 15.0 (Python 3.12 and Go 1.25), unassigned in Python 3.11.
NEWISH_LETTER = "\U00011f04"


def test_go_categories_cover_every_code_point():
    assert GO_CATEGORIES[0][0] == 0
    assert GO_CATEGORIES[-1][1] == sys.maxunicode
    for previous, current in zip(GO_CATEGORIES, GO_CATEGORIES[1:], strict=False):
        assert current[0] == previous[1] + 1


def test_stable_code_points_have_gos_category_on_this_python():
    unstable = {code for first, last in UNSTABLE_RANGES for code in range(first, last + 1)}
    mismatches = [
        hex(code)
        for first, last, category in GO_CATEGORIES
        for code in range(first, last + 1)
        if code not in unstable and unicodedata.category(chr(code)) != category
    ]
    assert mismatches == [], f"Unicode {unicodedata.unidata_version}: {mismatches[:20]}"


def test_unstable_ranges_are_sorted_and_disjoint():
    for (_, last), (first, _) in zip(UNSTABLE_RANGES, UNSTABLE_RANGES[1:], strict=False):
        assert first > last + 1


@pytest.mark.parametrize("char", [NEW_LETTER, NEWISH_LETTER])
def test_recent_characters_are_unstable(char):
    assert is_unstable(char)
    assert not is_unstable("é")
    assert not is_unstable("a")


@pytest.mark.parametrize(
    "template",
    [
        "{{ quote .s }}",
        '{{ printf "%q" .s }}',
        "{{ toYaml .m }}",
    ],
)
@pytest.mark.parametrize("char", [NEW_LETTER, NEWISH_LETTER])
def test_functions_refuse_unstable_characters(template, char):
    params = {"s": f"a{char}b", "m": {f"k{char}": 1.0, "a": 2.0}}
    with pytest.raises(UnsupportedTemplateError, match="recent Unicode"):
        gotemplate.render(template, params)


def test_identifiers_refuse_unstable_characters():
    with pytest.raises(UnsupportedTemplateError, match="recent Unicode"):
        gotemplate.render(f"{{{{ .a{NEW_LETTER} }}}}", {"s": "x"})


def test_stable_non_ascii_characters_still_render():
    params = {"s": "héllo", "m": {"clé": 1.0, "a": 2.0}}
    assert gotemplate.render("{{ quote .s }}", params) == '"héllo"'
    assert gotemplate.render("{{ toYaml .m }}", params) == "a: 2\nclé: 1"
