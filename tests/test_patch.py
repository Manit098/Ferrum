"""Tests for exact-text patches."""

import pytest

from ferrum.patch import (
    AmbiguousPatchError,
    Patch,
    StaleSourceError,
    format_unified_diff,
)

SOURCE = "int main(void)\n{\n    return 0;\n}\n"


def test_apply_replaces_exactly_once():
    patch = Patch(rel="main.c", old="return 0;", new="return 1;")
    assert patch.apply(SOURCE) == SOURCE.replace("return 0;", "return 1;")


def test_occurrences():
    patch = Patch(rel="f.c", old="x", new="y")
    assert patch.occurrences("axbxc") == 2


def test_stale_source_raises():
    patch = Patch(rel="main.c", old="return 9;", new="return 1;")
    with pytest.raises(StaleSourceError, match="changed since analysis"):
        patch.verify(SOURCE)


def test_ambiguous_source_raises():
    patch = Patch(rel="f.c", old="x = 1;", new="x = 2;")
    with pytest.raises(AmbiguousPatchError, match="matches 2 times"):
        patch.verify("x = 1;\nx = 1;\n")


def test_roundtrip_diff():
    patch = Patch(rel="main.c", old="return 0;", new="return 1;")
    updated = patch.apply(SOURCE)
    diff = format_unified_diff(SOURCE, updated, "main.c")
    assert diff.startswith("--- a/main.c")
    assert "+++ b/main.c" in diff
    assert "-    return 0;" in diff
    assert "+    return 1;" in diff
    assert diff.endswith("\n")


def test_no_change_diff_is_explained():
    assert format_unified_diff("same", "same", "f.c") == "(no text change in f.c)"
