"""具备服务端所有者隔离的模型可见保险 Tool。

这些 Tool 刻意不暴露 ``owner_id`` 参数。所有权从 ``ToolRuntime`` 解析，
因此 LLM 无法通过伪造标识符读取或修改其他保险代理人的客户数据。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.tools import tool

from app.insurance.coverage_review import ReviewAction, ReviewDecision
from app.insurance.intake import parse_coverage_review_intake
from app.insurance.mock_data import hydrate_registered_mock_intake
from app.insurance.runtime import get_insurance_service
from app.insurance.ui import build_intake_ui, build_task_ui
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.subject_memory import MemoryUpdateSource
from deerflow.tools.types import Runtime
from deerflow.workflows import TaskStatus


def _json(value: Any) -> str:
    """以统一格式序列化 Tool 结果，供模型和审计逻辑使用。"""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _thread_id(runtime: Runtime) -> str | None:
    """从服务端运行配置读取当前会话 ID。"""

    if runtime.context and runtime.context.get("thread_id"):
        return str(runtime.context["thread_id"])
    configurable = runtime.config.get("configurable", {})
    if isinstance(configurable, dict) and configurable.get("thread_id"):
        return str(configurable["thread_id"])
    return None


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
        thread_id=_thread_id(runtime),
        task_id=None,
    )
    if candidate is not None:
        return _json({"status": "pending_confirmation", "candidate": candidate})
    return _json({"status": "updated", "profile": profile, "version": version})


@tool(parse_docstring=True)
async def insurance_coverage_review_intake(source_text: str, runtime: Runtime) -> str:
    """从代理人原话启动或继续企业级保障检视工作流。

    该 Tool 只机械提取明确事实。信息不足时返回结构化表格，不允许模型补写
    客户事实；对外 HTML 只会在代理人批准对内报告后生成。

    Args:
        source_text: 代理人本轮输入的客户信息或缺口补充事实。
    """

    service = get_insurance_service()
    owner_id = resolve_runtime_user_id(runtime)
    thread_id = _thread_id(runtime)
    tasks = await service.list_tasks(owner_id=owner_id, thread_id=thread_id)
    active = next(
        (
            task
            for task in sorted(tasks, key=lambda item: item.updated_at, reverse=True)
            if task.status
            in {
                TaskStatus.READY,
                TaskStatus.RUNNING,
                TaskStatus.BLOCKED,
                TaskStatus.WAITING_INPUT,
                TaskStatus.WAITING_CONFIRMATION,
                TaskStatus.SUSPENDED,
            }
        ),
        None,
    )
    intake = hydrate_registered_mock_intake(parse_coverage_review_intake(source_text, thread_id=thread_id))
    if active is not None:
        if active.status in {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.BLOCKED}:
            active = await service.advance_task(
                active.id,
                owner_id=owner_id,
            )
        elif active.status is TaskStatus.WAITING_INPUT:
            pending_keys = {request.path.rsplit("/", maxsplit=1)[-1] for request in active.pending_inputs}
            answers = {key: value for key, value in intake.answers.items() if key in pending_keys}
            trigger_ids = list(intake.trigger_ids) if "trigger_ids" in pending_keys else None
            if answers or trigger_ids is not None:
                active = await service.advance_task(
                    active.id,
                    owner_id=owner_id,
                    answers=answers,
                    trigger_ids=trigger_ids,
                )
        return _json(build_task_ui(active))
    if intake.missing_identity_fields:
        return _json(build_intake_ui(intake))
    task = await service.start_coverage_review_intake(
        owner_id=owner_id,
        intake=intake,
    )
    return _json(build_task_ui(task))


@tool(parse_docstring=True)
async def insurance_coverage_review(
    action: Literal["start", "status", "advance", "review", "suspend", "resume", "cancel"],
    runtime: Runtime,
    customer_id: str | None = None,
    task_id: str | None = None,
    trigger_ids: list[str] | None = None,
    answers: dict[str, Any] | None = None,
    review_action: Literal[
        "approve",
        "reject",
        "revise_facts",
        "revise_triggers",
        "revise_narrative",
    ]
    | None = None,
    expected_review_revision: int | None = None,
    expected_kernel_hash: str | None = None,
    expected_profile_version: int | None = None,
    fact_patches: dict[str, Any] | None = None,
    narrative_preferences: dict[str, Any] | None = None,
    note: str = "",
    reason: str = "",
) -> str:
    """控制可恢复的保险保障检视任务。

    任务不会接管对话。用户临时切换工作时使用 suspend/resume，使用 status
    查看仍待补充的客户事实。

    Args:
        action: 要执行的生命周期操作；review 用于代理人复核。
        customer_id: 仅启动新保障检视时必需。
        task_id: start 之外的操作必需。
        trigger_ids: 本次检视的触发场景编号，用于 start、advance 或修订。
        answers: 代理人已确认的结构化追问答案。
        review_action: 批准、拒绝或三类修订决定。
        expected_review_revision: 代理人当前复核包中的报告修订号。
        expected_kernel_hash: 代理人当前复核包中的诊断内核哈希。
        expected_profile_version: 修改客户档案事实时的乐观锁版本。
        fact_patches: 修改事实时的字段映射；JSON Pointer 路径写回档案。
        narrative_preferences: 只影响报告的叙事偏好。
        note: 复核决定的审计备注。
        reason: 暂停或取消时可选的用户可见原因。
    """

    service = get_insurance_service()
    owner_id = resolve_runtime_user_id(runtime)
    thread_id = _thread_id(runtime)
    if action == "start":
        if not customer_id:
            return _json({"error": "customer_id is required for start"})
        task = await service.start_coverage_review(
            owner_id=owner_id,
            customer_id=customer_id,
            thread_id=thread_id,
            trigger_ids=trigger_ids,
            answers=answers,
        )
    else:
        if not task_id:
            return _json({"error": f"task_id is required for {action}"})
        if action == "status":
            task = await service.get_task(task_id, owner_id=owner_id)
            if task is None:
                return _json({"error": "task not found"})
        elif action == "advance":
            task = await service.advance_task(
                task_id,
                owner_id=owner_id,
                answers=answers,
                trigger_ids=trigger_ids,
            )
        elif action == "review":
            if review_action is None:
                return _json({"error": "review_action is required for review"})
            if expected_kernel_hash is None:
                return _json({"error": "expected_kernel_hash is required for review"})
            if expected_review_revision is None:
                return _json({"error": "expected_review_revision is required for review"})
            task = await service.decide_coverage_review(
                task_id,
                owner_id=owner_id,
                decision=ReviewDecision(
                    action=ReviewAction(review_action),
                    reviewer_id=owner_id,
                    expected_review_revision=expected_review_revision,
                    expected_kernel_hash=expected_kernel_hash,
                    note=note,
                    expected_profile_version=expected_profile_version,
                    fact_patches=fact_patches or {},
                    trigger_ids=tuple(trigger_ids or ()),
                    narrative_preferences=narrative_preferences or {},
                ),
            )
        elif action == "suspend":
            task = await service.suspend_task(task_id, owner_id=owner_id, reason=reason)
        elif action == "resume":
            task = await service.resume_task(task_id, owner_id=owner_id)
        else:
            task = await service.cancel_task(task_id, owner_id=owner_id, reason=reason)
    return _json(build_task_ui(task))
