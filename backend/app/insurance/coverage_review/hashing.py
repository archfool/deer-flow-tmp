"""保障检视产物的规范序列化与哈希工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(value: Any) -> str:
    """把领域对象序列化为稳定的 JSON 文本。

    Args:
        value: Pydantic 模型或可 JSON 序列化对象。

    Returns:
        排序稳定、无多余空白的 JSON 字符串。
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_digest(value: Any) -> str:
    """计算领域对象的带算法前缀 SHA-256。

    Args:
        value: 需要形成审计指纹的对象。

    Returns:
        形如 sha256:xxxx 的摘要。
    """

    payload = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_text(text: str) -> str:
    """计算原始文本的带算法前缀 SHA-256。

    Args:
        text: 原始文本。

    Returns:
        形如 sha256:xxxx 的摘要。
    """

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
