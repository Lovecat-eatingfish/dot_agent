"""ChangePreviewer 执行前预览测试"""
from __future__ import annotations

from pathlib import Path

from dot.coding.review.previewer import (
    KIND_BINARY,
    KIND_CREATE,
    KIND_ERROR,
    KIND_UPDATE,
    build_preview,
)
from dot.coding.review.fsutil import MAX_PREVIEW_CHARS


def _write_args(rel: str, content: str) -> dict:
    return {"file_path": rel, "content": content}


def _edit_args(rel: str, old: str, new: str) -> dict:
    return {"file_path": rel, "old_text": old, "new_text": new}


# ============================================================
# write_file
# ============================================================

def test_write_new_file_is_create_with_full_diff(tmp_path: Path) -> None:
    preview = build_preview("write_file", _write_args("new.py", "a = 1\nb = 2\n"), tmp_path)
    assert preview.kind == KIND_CREATE
    assert preview.path == "new.py"
    assert "+a = 1" in preview.diff and "+b = 2" in preview.diff
    assert preview.lines_added == 2 and preview.lines_removed == 0
    assert not preview.truncated


def test_write_existing_file_is_update_with_removed_lines(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("old = 1\n", encoding="utf-8", newline="\n")
    preview = build_preview("write_file", _write_args("app.py", "new = 2\n"), tmp_path)
    assert preview.kind == KIND_UPDATE
    assert "-old = 1" in preview.diff and "+new = 2" in preview.diff
    assert (preview.lines_added, preview.lines_removed) == (1, 1)


def test_write_identical_content_yields_empty_diff(tmp_path: Path) -> None:
    (tmp_path / "same.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    preview = build_preview("write_file", _write_args("same.py", "x = 1\n"), tmp_path)
    assert preview.kind == KIND_UPDATE
    assert preview.diff == ""
    assert (preview.lines_added, preview.lines_removed) == (0, 0)


def test_write_binary_file_degrades_to_summary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    preview = build_preview("write_file", _write_args("blob.bin", "text"), tmp_path)
    assert preview.kind == KIND_BINARY
    assert "binary" in preview.error


# ============================================================
# edit_file
# ============================================================

def test_edit_replacement_preview(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    print('hi')\n", encoding="utf-8", newline="\n")
    preview = build_preview(
        "edit_file", _edit_args("app.py", "print('hi')", "print('bye')"), tmp_path,
    )
    assert preview.kind == KIND_UPDATE
    assert "-    print('hi')" in preview.diff
    assert "+    print('bye')" in preview.diff


def test_edit_zero_matches_is_preview_error(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    preview = build_preview("edit_file", _edit_args("app.py", "missing", "x"), tmp_path)
    assert preview.kind == KIND_ERROR
    assert "not found" in preview.error


def test_edit_ambiguous_match_is_preview_error(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("dup\ndup\n", encoding="utf-8", newline="\n")
    preview = build_preview("edit_file", _edit_args("app.py", "dup", "x"), tmp_path)
    assert preview.kind == KIND_ERROR
    assert "2 times" in preview.error


def test_edit_crlf_fallback_matches_tool_behavior(tmp_path: Path) -> None:
    # 与 _edit_file 一致：CRLF 文件中 LF 片段 0 命中时归一化重试
    (tmp_path / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    preview = build_preview("edit_file", _edit_args("win.py", "b = 2\n", "b = 3\n"), tmp_path)
    assert preview.kind == KIND_UPDATE
    assert "+b = 3" in preview.diff


def test_edit_missing_file_is_error(tmp_path: Path) -> None:
    preview = build_preview("edit_file", _edit_args("ghost.py", "a", "b"), tmp_path)
    assert preview.kind == KIND_ERROR


# ============================================================
# 其他
# ============================================================

def test_unknown_tool_is_error(tmp_path: Path) -> None:
    preview = build_preview("bash", {"command": "ls"}, tmp_path)
    assert preview.kind == KIND_ERROR


def test_preview_never_raises_on_bad_args(tmp_path: Path) -> None:
    preview = build_preview("write_file", {"file_path": ""}, tmp_path)
    assert preview.kind in (KIND_ERROR, KIND_CREATE)


def test_huge_diff_is_truncated(tmp_path: Path) -> None:
    content = "\n".join(f"line {i}" for i in range(5000)) + "\n"
    preview = build_preview("write_file", _write_args("big.py", content), tmp_path)
    assert preview.truncated
    assert len(preview.diff) <= MAX_PREVIEW_CHARS
