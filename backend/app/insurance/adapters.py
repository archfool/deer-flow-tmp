"""外部保险数据服务端口与确定性 Mock Adapter。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CustomerDataAdapter(ABC):
    """把外部服务响应转换为通过校验的客户档案 Patch。"""

    @abstractmethod
    async def fetch_profile_patch(self, *, owner_id: str, customer_id: str) -> dict[str, Any]: ...


class MockCustomerDataAdapter(CustomerDataAdapter):
    """在真实 HTTP 契约可用前使用的无网络 Adapter。

    Adapter 默认返回空 Patch，而不是合成事实；Mock 客户记录必须通过
    `mock_data.py` 显式创建。这样可以避免调用方把 Adapter 的回退数据
    误认为权威来源。
    """

    async def fetch_profile_patch(self, *, owner_id: str, customer_id: str) -> dict[str, Any]:  # noqa: ARG002
        return {}
