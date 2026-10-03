# -*- coding: utf-8 -*-
import json
from json_utils import save_json, load_json


def test_roundtrip(tmp_path):
    data = {"name": "张三", "scores": [90, 85], "ok": True}
    p = tmp_path / "d.json"
    save_json(data, str(p))
    assert load_json(str(p)) == data


def test_chinese_not_escaped(tmp_path):
    p = tmp_path / "c.json"
    save_json({"k": "中文值"}, str(p))
    assert "中文值" in p.read_text(encoding="utf-8")


def test_load_missing_file():
    """load_json 对不存在的文件应抛 FileNotFoundError"""
    import pytest
    with pytest.raises(FileNotFoundError):
        load_json("/nonexistent/path/that/does/not/exist.json")


def test_save_creates_parent_dirs(tmp_path):
    """save_json 应自动创建不存在的父目录"""
    p = tmp_path / "nested" / "deep" / "dir" / "out.json"
    save_json({"a": 1}, str(p))
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}


def test_non_serializable_raises():
    """不可序列化的对象应抛 TypeError"""
    import pytest
    import tempfile
    import os
    # Windows 下 NamedTemporaryFile(delete=True) 会锁定文件，需先关闭
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        tmppath = f.name
    try:
        with pytest.raises(TypeError):
            save_json({"obj": object()}, tmppath)
    finally:
        if os.path.exists(tmppath):
            os.unlink(tmppath)
