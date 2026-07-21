"""保障检视领域入口与 DeerFlow Lead Agent 的应用层装配。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from app.insurance.tools import (
    insurance_coverage_review,
    insurance_coverage_review_intake,
    insurance_get_customer_profile,
    insurance_update_customer_profile,
)
from deerflow.agents.lead_agent.agent import _make_lead_agent
from deerflow.config.app_config import get_app_config

logger = logging.getLogger(__name__)

_INSURANCE_TOOL_NAMES = {
    "insurance_coverage_review_intake",
    "insurance_coverage_review",
}
_MARKER = "insurance_coverage_review_router"


def _message_text(content: Any) -> str:
    """把 LangChain 多段消息内容规范为文本。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(content or "")


def _is_coverage_review_intent(text: str) -> bool:
    """识别保障检视请求或足够明确的客户资料首轮输入。"""

    if re.search(r"保障检视|保障体检|保单检视|保障分析", text):
        return True
    facts = (
        bool(re.search(r"\d{1,3}\s*岁", text)),
        bool(re.search(r"(?:男性|女性|男|女)(?:[，,。；;\s]|$)", text)),
        bool(re.search(r"年收入|医疗险|重疾险|寿险|年金险|只有.*险", text)),
    )
    return all(facts)


class CoverageReviewRoutingMiddleware(AgentMiddleware):
    """把保障检视强制派发到持久化工作流并阻止模型代写报告。"""

    @staticmethod
    def _route(state: dict[str, Any]) -> dict[str, Any] | None:
        """依据末条消息生成确定性派发或终态回复。"""

        messages = state.get("messages", [])
        if not messages:
            return None
        latest = messages[-1]
        if isinstance(latest, HumanMessage):
            text = _message_text(latest.content)
            previous_insurance = any(isinstance(message, ToolMessage) and getattr(message, "name", None) in _INSURANCE_TOOL_NAMES for message in messages)
            if not (_is_coverage_review_intent(text) or previous_insurance):
                return None
            tool_call = {
                "name": "insurance_coverage_review_intake",
                "args": {"source_text": text},
                "id": f"insurance_{uuid4().hex[:12]}",
                "type": "tool_call",
            }
            return {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[tool_call],
                        additional_kwargs={_MARKER: "dispatch"},
                    )
                ],
                "jump_to": "tools",
            }
        if not isinstance(latest, ToolMessage):
            return None
        if getattr(latest, "name", None) not in _INSURANCE_TOOL_NAMES:
            return None
        try:
            json.loads(_message_text(latest.content))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("coverage review tool returned a non-JSON UI envelope")
        message = "保障检视流程已受理，请在任务卡中继续完成信息补录、代理人复核和报告生成。"
        return {
            "messages": [
                AIMessage(
                    content=message,
                    additional_kwargs={_MARKER: "status"},
                )
            ],
            "jump_to": "end",
        }

    @hook_config(can_jump_to=["tools", "end"])
    def before_model(self, state, runtime) -> dict[str, Any] | None:
        """同步执行保障检视路由。"""

        try:
            return self._route(state)
        except Exception:
            logger.exception("coverage review deterministic routing failed")
            raise

    @hook_config(can_jump_to=["tools", "end"])
    async def abefore_model(self, state, runtime) -> dict[str, Any] | None:
        """异步执行保障检视路由。"""

        return self.before_model(state, runtime)


def make_gateway_lead_agent(config: RunnableConfig):
    """构造包含保险领域 Tool 与 FSM 路由的 Gateway Agent。"""

    runtime_config = dict(config.get("context", {}) or {})
    app_config = runtime_config.get("app_config") or get_app_config()
    return _make_lead_agent(
        config,
        app_config=app_config,
        domain_tools=[
            insurance_coverage_review_intake,
            insurance_coverage_review,
            insurance_get_customer_profile,
            insurance_update_customer_profile,
        ],
        custom_middlewares=[CoverageReviewRoutingMiddleware()],
    )
