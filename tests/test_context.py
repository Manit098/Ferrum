"""Tests for project context: summary, root detection, discovery, ignores."""

import dataclasses

import pytest

from ferrum.config import Config
from ferrum.context import (
    BuildSystem,
    FileEntry,
    Language,
    ProjectContext,
    collect_files,
    discover,
    find_project_root,
    limit_entries,
)


def test_language_labels():
    assert Language.C.label == "C"
    assert Language.CPP.label == "C++"
    assert Language.RUST.label == "Rust"
    assert Language.ASM.label == "Assembly"
    assert Language.PYTHON.label == "Python"


def test_build_system_labels():
    assert BuildSystem.MAKE.label == "Make"
    assert BuildSystem.CMAKE.label == "CMake"
    assert BuildSystem.CARGO.label == "Cargo"
    assert BuildSystem.PYTEST.label == "pytest"


def test_summary_format_matches_documented_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "example"
    root.mkdir()
    ctx = ProjectContext(
        root=root,
        languages=[Language.C],
        build_systems=[BuildSystem.MAKE],
        files=[
            FileEntry(path="main.c", size=100, language=Language.C),
            FileEntry(path="memory.c", size=200, language=Language.C),
            FileEntry(path="memory.h", size=50, language=Language.C),
            FileEntry(path="Makefile", size=30),
        ],
    )
    expected = (
        "Ferrum\n"
        "──────\n"
        "\n"
        "Project: ./example\n"
        "Languages: C\n"
        "Build system: Make\n"
        "\n"
        "Files:\n"
        "  main.c\n"
        "  memory.c\n"
        "  memory.h\n"
        "  Makefile"
    )
    assert ctx.summary() == expected


def test_summary_handles_unknown_and_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = ProjectContext(root=tmp_path)
    summary = ctx.summary()
    assert "Languages: unknown" in summary
    assert "Build system: unknown" in summary
    assert "  (none)" in summary


def test_display_path_is_relative_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ProjectContext(root=tmp_path).display_path() == "."
    sub = tmp_path / "proj"
    sub.mkdir()
    assert ProjectContext(root=sub).display_path() == "./proj"


def test_name_defaults_to_directory_name(tmp_path):
    root = tmp_path / "myproj"
    root.mkdir()
    assert ProjectContext(root=root).name == "myproj"


def test_file_entry_is_immutable():
    entry = FileEntry(path="main.c")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.path = "other.c"


def test_find_project_root_uses_marker_in_start(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert find_project_root(root) == root.resolve()


def test_find_project_root_walks_up(tmp_path):
    root = tmp_path / "proj"
    deep = root / "src" / "deep"
    deep.mkdir(parents=True)
    (root / "Makefile").write_text("all:\n", encoding="utf-8")
    assert find_project_root(deep) == root.resolve()


def test_find_project_root_falls_back_to_start(tmp_path):
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert find_project_root(lonely) == lonely.resolve()


def _full_project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".gitignore").write_text(
        "*.log\n"
        "ignored_dir/\n"
        "secret.txt\n"
        "secret_src.c\n"
        "*.gen.rs\n"
        "!keep.gen.rs\n",
        encoding="utf-8",
    )
    (root / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (root / "util.h").write_text("int helper(void);\n", encoding="utf-8")
    (root / "notes.log").write_text("noise\n", encoding="utf-8")
    (root / "secret.txt").write_text("ignore me\n", encoding="utf-8")
    (root / "secret_src.c").write_text("int secret;\n", encoding="utf-8")
    (root / "data.gen.rs").write_text("fn generated() {}\n", encoding="utf-8")
    (root / "keep.gen.rs").write_text("fn kept() {}\n", encoding="utf-8")
    (root / ".env").write_text("KEY=1\n", encoding="utf-8")
    (root / "big.c").write_bytes(b"x" * (256 * 1024 + 1))
    (root / "ignored_dir").mkdir()
    (root / "ignored_dir" / "x.c").write_text("int x;\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "m.py").write_text("x = 1\n", encoding="utf-8")
    (root / "target").mkdir()
    (root / "target" / "gen.c").write_text("int gen;\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("fn borrow() {}\n", encoding="utf-8")
    (root / "Makefile").write_text("all:\n", encoding="utf-8")
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    return root


def test_collect_files_respects_gitignore_and_denies(tmp_path):
    root = _full_project(tmp_path)
    paths = {entry.path for entry in collect_files(root, Config())}
    assert {"main.c", "util.h", "src/lib.rs", "Makefile", "Cargo.toml"} <= paths
    assert "secret_src.c" not in paths
    assert "data.gen.rs" not in paths
    assert "keep.gen.rs" in paths
    assert "ignored_dir/x.c" not in paths
    assert "secret.txt" not in paths
    assert "notes.log" not in paths
    assert ".env" not in paths
    assert "big.c" not in paths
    assert "__pycache__/m.py" not in paths
    assert "target/gen.c" not in paths
    assert ".git/config" not in paths


def test_discover_detects_languages_and_build_systems(tmp_path):
    root = _full_project(tmp_path)
    ctx = discover(root, Config())
    assert ctx.languages == [Language.C, Language.RUST]
    assert ctx.build_systems == [BuildSystem.MAKE, BuildSystem.CARGO]


def test_discover_lists_files_in_path_order(tmp_path):
    root = _full_project(tmp_path)
    ctx = discover(root, Config())
    paths = [entry.path for entry in ctx.files]
    assert paths == sorted(paths)
    assert "src/lib.rs" in paths


def test_collect_files_scoped_to_subdir(tmp_path):
    root = _full_project(tmp_path)
    (root / "src" / "sub.log").write_text("x\n", encoding="utf-8")
    scoped = {e.path for e in collect_files(root, Config(), start=root / "src")}
    assert scoped == {"src/lib.rs"}
    all_files = {e.path for e in collect_files(root, Config())}
    assert "main.c" in all_files


def test_nested_gitignore_applies(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / ".gitignore").write_text("skip_root.c\n", encoding="utf-8")
    (root / "sub" / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (root / "sub" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "sub" / "keep.c").write_text("int keep;\n", encoding="utf-8")
    (root / "sub" / "skip_root.c").write_text("int skip;\n", encoding="utf-8")
    (root / "root.py").write_text("y = 2\n", encoding="utf-8")
    (root / "root.c").write_text("int root;\n", encoding="utf-8")
    paths = {e.path for e in collect_files(root, Config())}
    assert "sub/a.py" not in paths
    assert "sub/skip_root.c" not in paths
    assert "root.c" in paths
    assert "root.py" in paths
    assert "sub/keep.c" in paths


def test_start_outside_root_rejected(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="outside the project root"):
        collect_files(root, Config(), start=other)


def test_limit_entries_by_count(tmp_path):
    entries = [
        FileEntry(path="a.c", size=1),
        FileEntry(path="b.c", size=1),
        FileEntry(path="c.c", size=1),
    ]
    assert len(limit_entries(entries, Config(max_files=2))) == 2


def test_limit_entries_by_budget():
    entries = [
        FileEntry(path="a.c", size=10),
        FileEntry(path="b.c", size=10),
    ]
    assert len(limit_entries(entries, Config(max_context_bytes=15))) == 1


def test_discover_file_cap_keeps_languages(tmp_path):
    root = _full_project(tmp_path)
    ctx = discover(root, Config(max_files=2))
    assert len(ctx.files) == 2
    assert Language.RUST in ctx.languages


def test_brief_is_one_short_line(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (root / "Makefile").write_text("all:\n", encoding="utf-8")
    brief = discover(root, Config()).brief()
    assert brief == "2 files · C · Make"
    assert "\n" not in brief


def test_walk_budget_stops_a_huge_tree(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(60):
        (root / f"f{i}.c").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr("ferrum.context.MAX_EXAMINED", 5)
    entries = collect_files(root, Config(max_files=1))
    assert 0 < len(entries) <= 20  # the walk stopped, no full scan
    assert len(entries) < 60
