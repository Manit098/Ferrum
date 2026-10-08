"""Tests for filesystem safety helpers."""

import pytest

from ferrum.safety import (
    PathEscapeError,
    is_denied,
    is_within,
    safe_join,
)


def test_safe_join_inside_root(tmp_path):
    joined = safe_join(tmp_path, "src", "main.c")
    assert joined == (tmp_path / "src" / "main.c").resolve()


def test_safe_join_allows_root_itself(tmp_path):
    assert safe_join(tmp_path) == tmp_path.resolve()


def test_safe_join_rejects_parent_escape(tmp_path):
    with pytest.raises(PathEscapeError, match="escapes project root"):
        safe_join(tmp_path, "..", "secrets.txt")


def test_safe_join_rejects_nested_escape(tmp_path):
    with pytest.raises(PathEscapeError):
        safe_join(tmp_path, "src", "..", "..", "elsewhere")


def test_safe_join_rejects_absolute_path(tmp_path):
    with pytest.raises(PathEscapeError, match="absolute path"):
        safe_join(tmp_path, "/etc/passwd")


def test_is_within(tmp_path):
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    assert is_within(tmp_path, inner)
    assert is_within(tmp_path, tmp_path)
    assert not is_within(tmp_path, tmp_path.parent / "other")


def test_denied_directories():
    assert is_denied(".git/config")
    assert is_denied("src/__pycache__/mod.cpython-314.pyc")
    assert is_denied("target/debug/app.c")
    assert is_denied("build/output.c")
    assert is_denied("vendor/node_modules/x/y.js")


def test_denied_sensitive_files():
    assert is_denied(".env")
    assert is_denied("config/.env.local")
    assert is_denied("certs/server.pem")
    assert is_denied("id_rsa")
    assert is_denied("keys/private.key")


def test_denied_binary_suffixes():
    assert is_denied("main.o")
    assert is_denied("app.exe")
    assert is_denied("libfoo.so")
    assert is_denied("image.png")


def test_allowed_source_files():
    assert not is_denied("main.c")
    assert not is_denied("src/lib.rs")
    assert not is_denied("include/memory.h")
    assert not is_denied("Makefile")
    assert not is_denied("Cargo.toml")
    assert not is_denied("pyproject.toml")


def test_windows_style_separators_handled():
    assert is_denied("src\\__pycache__\\x.pyc")
