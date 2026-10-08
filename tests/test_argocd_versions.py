from __future__ import annotations

import pytest

from argocd_source_lint.argocd_versions import (
    introduced_at_or_before,
    is_known,
    parse_version,
)


def test_parse_version_handles_two_and_three_components():
    assert parse_version("3.2") == (3, 2)
    assert parse_version("3.2.0") == (3, 2, 0)


def test_parse_version_strips_leading_v():
    assert parse_version("v3.2.0") == (3, 2, 0)


def test_parse_version_rejects_non_numeric_input():
    with pytest.raises(ValueError):
        parse_version("not-a-version")


def test_introduced_at_or_before_pads_shorter_tuple():
    assert introduced_at_or_before((3, 2), (3, 2, 0)) is True
    assert introduced_at_or_before((3, 2, 0), (3, 2)) is True
    assert introduced_at_or_before((3, 3, 0), (3, 2, 9)) is False


def test_is_known_without_declared_version_accepts_any_table_entry():
    table = {"Foo": (9, 9, 9)}
    assert is_known("Foo", table, None) is True


def test_is_known_rejects_value_absent_from_table_regardless_of_version():
    table = {"Foo": (1, 0, 0)}
    assert is_known("Bar", table, parse_version("99.0.0")) is False


def test_is_known_respects_declared_version_floor():
    table = {"Foo": (3, 4, 0)}
    assert is_known("Foo", table, parse_version("3.3.0")) is False
    assert is_known("Foo", table, parse_version("3.4.0")) is True
    assert is_known("Foo", table, parse_version("3.5.0")) is True
