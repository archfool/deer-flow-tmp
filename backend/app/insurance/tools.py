"""具备服务端所有者隔离的模型可见保险 Tool。

这些 Tool 刻意不暴露 ``owner_id`` 参数。所有权从 ``ToolRuntime`` 解析，
因此 LLM 无法通过伪造标识符读取或修改其他保险代理人的客户数据。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import tool

from app.insurance.runtime import get_insurance_service
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.subject_memory import MemoryUpdateSource
from deerflow.tools.types import Runtime


def _json(value: Any) -> str:
    """以统一格式序列化 Tool 结果，供模型和审计逻辑使用。"""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


@tool(parse_docstring=True)
async def insurance_get_customer_profile(customer_id: str, runtime: Runtime) -> str:
    """读取已认证保险代理人名下的客户档案。

    Args:
        customer_id: 用户选择的客户或家庭标识符。
    """

    owner_id = resolve_runtime_user_id(runtime)
    result = await get_insurance_service().profiles.get(owner_id=owner_id, customer_id=customer_id)
    if result is None:
        return _json({"error": "customer profile not found", "customer_id": customer_id})
    profile, version = result
    return _json({"profile": profile, "version": version})


@tool(parse_docstring=True)
async def insurance_update_customer_profile(
    customer_id: str,
    expected_version: int,
    patch: dict[str, Any],
    inferred: bool,
    reason: str,
    runtime: Runtime,
) -> str:
    """更新明确事实，或暂存模型推断的客户事实。

    使用类似 ``/financial/annual_income`` 的 JSON Pointer 路径。如果值是模型
    推断而非用户明确陈述，必须设置 ``inferred``；推断变更会等待人工接受。

    Args:
        customer_id: 已绑定的客户或家庭 ID。
        expected_version: Agent 先前读取的客户档案版本。
        patch: JSON Pointer 路径到替换值的映射。
        inferred: 该值是否由模型推断，而不是来自明确事实。
        reason: 描述来源话语或推断依据的简短来源说明。
    """

    owner_id = resolve_runtime_user_id(runtime)
    source = MemoryUpdateSource.MODEL_INFERENCE if inferred else MemoryUpdateSource.USER_EXPLICIT
    profile, version, candidate = await get_insurance_service().profiles.update(
        owner_id=owner_id,
        customer_id=customer_id,
        expected_version=expected_version,
        patch=patch,
        source=source,
        reason=reason,
        thread_id=runtime.context.get("thread_id") if runtime.context else None,
        task_id=None,
    )
    if candidate is not None:
        return _json({"status": "pending_confirmation", "candidate": candidate})
    return _json({"status": "updated", "profile": profile, "version": version})


@tool(parse_docstring=True)
async def insurance_coverage_review(
    action: Literal["start", "status", "advance", "suspend", "resume", "cancel"],
    runtime: Runtime,
    customer_id: str | None = None,
    task_id: str | None = None,
    reason: str = "",
) -> str:
    """控制可恢复的保险保障检视任务。

    任务不会接管对话。用户临时切换工作时使用 suspend/resume，使用 status
    查看仍待补充的客户事实。

    Args:
        action: 要执行的生命周期操作。
        customer_id: 仅启动新保障检视时必需。
        task_id: status、advance、suspend、resume 和 cancel 操作必需。
        reason: 暂停或取消时可选的用户可见原因。
    """

    service = get_insurance_service()
    owner_id = resolve_runtime_user_id(runtime)
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if action == "start":
        if not customer_id:
            return _json({"error": "customer_id is required for start"})
        task = await service.start_coverage_review(
            owner_id=owner_id,
            customer_id=customer_id,
            thread_id=thread_id,
        )
    else:
        if not task_id:
            return _json({"error": f"task_id is required for {action}"})
        if action == "status":
            task = await service.get_task(task_id, owner_id=owner_id)
            if task is None:
                return _json({"error": "task not found"})
        elif action == "advance":
            task = await service.advance_task(task_id, owner_id=owner_id)
        elif action == "suspend":
            task = await service.suspend_task(task_id, owner_id=owner_id, reason=reason)
        elif action == "resume":
            task = await service.resume_task(task_id, owner_id=owner_id)
        else:
            task = await service.cancel_task(task_id, owner_id=owner_id, reason=reason)
    return _json(task)
