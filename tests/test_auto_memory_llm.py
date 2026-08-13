"""auto_memory LLM 提取器与回退测试

对齐 Claude Code 后台记忆提取子 Agent：
- LLM 提取成功 → 写入 TopicStore + 更新 MEMORY.md 索引 + 去重
- LLM 不可用 / 返回非 JSON → 回退到正则提取
- 提取器不 bind 任何工具（禁止 bash / MCP / 再派子 Agent 沙箱约束）
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import mokioclaw.memory.auto_memory as auto_memory
import mokioclaw.providers.openai_provider as openai_provider
from mokioclaw.memory.auto_memory import extract_with_model
from mokioclaw.memory.topic_store import TopicStore


class _FakeModel:
    """模拟一个不 bind 工具的纯 invoke 模型"""

    bind_tools_called = False

    def bind_tools(self, *_args, **_kwargs):  # noqa: D401 - mimic langchain API
        _FakeModel.bind_tools_called = True
        return self

    def invoke(self, prompt):  # noqa: ARG002
        return self._response

    def set_response(self, content: str) -> None:
        self._response = content


def _make_fake_model(content: str) -> _FakeModel:
    m = _FakeModel()
    m.set_response(content)
    return m


def test_extract_with_model_success(tmp_path: Path):
    """LLM 返回结构化 JSON → 写入主题 + 更新索引 + 去重"""
    response_json = (
        '{"topics": ['
        '{"name":"user_pref_tabs","content":"user prefers tabs over spaces","type":"user"},'
        '{"name":"project_uses_pytest","content":"project tests run with pytest","type":"project"}'
        "]}"
    )
    fake = _make_fake_model(response_json)

    with patch.object(openai_provider, "create_model", return_value=fake) as mock_create:
        # create_model 在 extract_with_model 内延迟 import，patch 源模块属性即可
        ok = extract_with_model(tmp_path, new_messages=["user: please use tabs"], session_id="s1")

    assert ok is True
    mock_create.assert_called_once()

    store = TopicStore(tmp_path)
    topics = {t.name: t for t in store.list_topics()}
    assert "user_pref_tabs" in topics
    assert topics["user_pref_tabs"].topic_type == "user"
    assert "project_uses_pytest" in topics

    # MEMORY.md 索引应包含新条目
    index = store.load_index()
    assert "user_pref_tabs" in index
    assert "project_uses_pytest" in index

    # 第二次提取相同内容 → 去重，不重复写
    fake2 = _make_fake_model(response_json)
    with patch.object(openai_provider, "create_model", return_value=fake2):
        ok2 = extract_with_model(tmp_path, new_messages=["user: please use tabs"], session_id="s2")
    assert ok2 is True
    topics2 = {t.name for t in store.list_topics()}
    assert topics2 == {"user_pref_tabs", "project_uses_pytest"}


def test_extract_with_model_falls_back_when_model_raises(tmp_path: Path):
    """create_model 抛异常 → extract_with_model 返回 False → 调用方回退到正则"""
    def _boom(*_args, **_kwargs):
        raise RuntimeError("no api key")

    with patch.object(openai_provider, "create_model", side_effect=_boom):
        ok = extract_with_model(tmp_path, new_messages=["user: prefer tabs always"], session_id="s1")

    assert ok is False
    # LLM 失败，不应写任何主题
    assert TopicStore(tmp_path).list_topics() == []

    # 调用方回退到正则路径
    auto_memory._extract_memories_regex(
        tmp_path, new_messages=["prefer tabs always use tabs"], session_id="s1"
    )
    topics = {t.name for t in TopicStore(tmp_path).list_topics()}
    assert topics, "regex fallback should have written a topic"


def test_extract_with_model_invalid_json_returns_false(tmp_path: Path):
    """LLM 返回非 JSON → 返回 False，不写主题"""
    fake = _make_fake_model("sorry, I cannot extract anything here")

    with patch.object(openai_provider, "create_model", return_value=fake):
        ok = extract_with_model(tmp_path, new_messages=["user: hello"], session_id="s1")

    assert ok is False
    assert TopicStore(tmp_path).list_topics() == []


def test_extract_with_model_no_tools_bound(tmp_path: Path):
    """提取路径不调用 model.bind_tools —— 满足 Claude Code 沙箱约束"""
    _FakeModel.bind_tools_called = False
    response_json = '{"topics": [{"name":"x","content":"y","type":"project"}]}'
    fake = _make_fake_model(response_json)

    with patch.object(openai_provider, "create_model", return_value=fake):
        extract_with_model(tmp_path, new_messages=["user: foo"], session_id="s1")

    assert _FakeModel.bind_tools_called is False, "extractor must not bind tools (sandbox constraint)"


def test_extract_with_model_empty_topics_list(tmp_path: Path):
    """LLM 返回空 topics 列表 → 视为成功但无写入，会话计数仍累加"""
    fake = _make_fake_model('{"topics": []}')

    with patch.object(openai_provider, "create_model", return_value=fake):
        ok = extract_with_model(tmp_path, new_messages=["user: nothing notable"], session_id="s1")

    assert ok is True
    assert TopicStore(tmp_path).list_topics() == []


def test_extract_with_model_caps_topic_count(tmp_path: Path):
    """LLM 返回超过上限的主题 → 只写前 _MAX_EXTRACTED_TOPICS 条"""
    topics_payload = {
        "topics": [
            {"name": f"topic_{i}", "content": f"fact number {i}", "type": "project"}
            for i in range(20)
        ]
    }
    import json
    fake = _make_fake_model(json.dumps(topics_payload))

    cap = auto_memory._MAX_EXTRACTED_TOPICS
    with patch.object(openai_provider, "create_model", return_value=fake):
        ok = extract_with_model(tmp_path, new_messages=["user: many facts"], session_id="s1")

    assert ok is True
    written = TopicStore(tmp_path).list_topics()
    assert len(written) == cap


def test_trigger_background_extraction_uses_llm_then_falls_back(tmp_path: Path):
    """trigger_background_extraction: LLM 成功时不走正则；失败时走正则"""
    # 场景 1: LLM 成功
    fake_ok = _make_fake_model(
        '{"topics":[{"name":"ok_topic","content":"a real fact","type":"project"}]}'
    )
    with patch.object(openai_provider, "create_model", return_value=fake_ok):
        auto_memory.trigger_background_extraction(
            tmp_path, new_messages=["user: note this"], session_id="s1"
        )
    # 后台线程跑在另一线程，等待一下
    import time
    time.sleep(0.3)
    names_ok = {t.name for t in TopicStore(tmp_path).list_topics()}
    assert "ok_topic" in names_ok

    # 场景 2: LLM 抛异常 → 回退正则
    fresh_ws = tmp_path / "ws2"
    fresh_ws.mkdir()
    with patch.object(openai_provider, "create_model", side_effect=RuntimeError("boom")):
        auto_memory.trigger_background_extraction(
            fresh_ws, new_messages=["prefer tabs always use tabs"], session_id="s2"
        )
    time.sleep(0.3)
    names_fallback = {t.name for t in TopicStore(fresh_ws).list_topics()}
    assert names_fallback, "regex fallback should have written topics when LLM fails"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
