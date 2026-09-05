"""
dot.core.wire — WireModel 序列化基类

所有层的事件/消息模型的公共基类：
Python 字段名 + camelCase JSON 别名，严格模式（extra=forbid）。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


class WireModel(BaseModel):
    """严格模型：Python 字段名 + camelCase JSON 别名"""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        alias_generator=_to_camel,
    )
