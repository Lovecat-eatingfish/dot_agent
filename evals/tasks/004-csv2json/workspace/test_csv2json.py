# -*- coding: utf-8 -*-
import json
from pathlib import Path
from csv2json import convert


def test_convert(tmp_path):
    src = Path(__file__).parent / "data" / "people.csv"
    dst = tmp_path / "out.json"
    convert(str(src), str(dst))
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert data == [
        {"name": "张三", "age": "28", "city": "西安"},
        {"name": "李四", "age": "32", "city": "北京"},
        {"name": "王五", "age": "25", "city": "上海"},
    ]


def test_empty_csv(tmp_path):
    """只有表头的 CSV 应转换为空列表"""
    src = Path(__file__).parent / "data" / "empty.csv"
    dst = tmp_path / "out.json"
    convert(str(src), str(dst))
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert data == []


def test_special_chars(tmp_path):
    """含逗号、引号、中文的字段应正确解析"""
    src = Path(__file__).parent / "data" / "special.csv"
    dst = tmp_path / "out.json"
    convert(str(src), str(dst))
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert len(data) == 2
    assert data[0]["address"] == "No. 1, Main St."
    assert data[1]["address"] == ' Apt 5, "Central" Ave'


def test_missing_values(tmp_path):
    """空字段应转换为空字符串，保持字段存在"""
    src = Path(__file__).parent / "data" / "missing.csv"
    dst = tmp_path / "out.json"
    convert(str(src), str(dst))
    data = json.loads(dst.read_text(encoding="utf-8"))
    assert len(data) == 3
    assert data[0] == {"name": "张三", "age": "28", "city": "西安"}
    assert data[1] == {"name": "李四", "age": "", "city": "北京"}
    assert data[2] == {"name": "王五", "age": "25", "city": ""}
