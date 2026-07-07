"""持久化、可恢复工作流的类型契约。

定义属于冻结配置。实例是可序列化的运行状态，刻意不包含活动协程、锁、
数据库 Session 或模型客户端；正是这一特性使跨进程暂停和恢复成为可能。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """为所有工作流后端返回格式统一且包含时区的时间戳。"""

    return datetime.now(UTC)


class TaskStatus(StrEnum):
    """持久化任务实例对外可见的生命周期。"""

    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUSPENDED = "suspended"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {type(self).COMPLETED, type(self).CANCELLED, type(self).FAILED}


class StepStatus(StrEnum):
    """单个工作流步骤的生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SideEffectLevel(StrEnum):
    """供 Guardrail 和审查逻辑使用的机器可读声明。"""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    IRREVERSIBLE = "irreversible"


class InputRequest(BaseModel):
    """可被渲染成对话问题的结构化输入请求。"""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="用于标识缺失值的类 JSON Pointer 路径")
    prompt: str = Field(description="向用户展示的追问")
    reason: str = Field(description="工作流无法安全继续执行的原因")
    sensitive: bool = Field(default=False, description="客户端是否应遮蔽该答案")


class ConfirmationRequest(BaseModel):
    """在接受步骤暂存输出前产生的人工确认点。"""

    model_config = ConfigDict(frozen=True)

    prompt: str
    risk: str = ""


class SkillDefinition(BaseModel):
    """可执行 Skill 的机器可读契约。

    `input_schema` 和 `output_schema` 是 JSON Schema 文档。运行时将其保存为
    字典，因为应用模块可以使用 Pydantic、dataclass 或外部 Schema Registry
    执行实际校验。
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    side_effect: SideEffectLevel = SideEffectLevel.NONE
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_attempts: int = Field(default=1, ge=1)

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)


class WorkflowStep(BaseModel):
    """任务 DAG 中的一个节点。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    skill: str = Field(min_length=1)
    skill_version: str = Field(default="1", min_length=1)
    depends_on: tuple[str, ...] = ()
    description: str = ""


class TaskDefinition(BaseModel):
    """在任务启动前完成校验的不可变版本化任务图。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    steps: tuple[WorkflowStep, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self) -> TaskDefinition:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow step ids must be unique")

        known = set(ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"step {step.id!r} has unknown dependencies: {sorted(unknown)}")
            if step.id in step.depends_on:
                raise ValueError(f"workflow dependency cycle includes {step.id!r}")

        # 深度优先校验会在配置加载阶段给出确定性错误，避免任务在运行时
        # 永久停留于 BLOCKED 状态。
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {step.id: step.depends_on for step in self.steps}

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"workflow dependency cycle detected at {step_id!r}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependencies[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)


class StepExecution(BaseModel):
    """单个已配置步骤的可序列化执行状态。"""

    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    output: dict[str, Any] = Field(default_factory=dict)
    input_requests: tuple[InputRequest, ...] = ()
    confirmation_request: ConfirmationRequest | None = None
    error: str | None = None
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class TaskInstance(BaseModel):
    """一个所有者及可选业务主体对应的任务持久化状态。"""

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_name: str
    definition_version: str
    owner_id: str
    subject_id: str | None = None
    thread_id: str | None = None
    parent_task_id: str | None = None
    status: TaskStatus = TaskStatus.READY
    input_data: dict[str, Any] = Field(default_factory=dict)
    output_data: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, StepExecution] = Field(default_factory=dict)
    pending_inputs: tuple[InputRequest, ...] = ()
    suspension_reason: str | None = None
    error: str | None = None
    revision: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_definition(
        cls,
        definition: TaskDefinition,
        *,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
        parent_task_id: str | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> TaskInstance:
        return cls(
            task_name=definition.name,
            definition_version=definition.version,
            owner_id=owner_id,
            subject_id=subject_id,
            thread_id=thread_id,
            parent_task_id=parent_task_id,
            input_data=input_data or {},
            steps={step.id: StepExecution() for step in definition.steps},
        )


class SkillExecutionResult(BaseModel):
    """Skill Handler 单次尝试执行后返回的结果。"""

    output: dict[str, Any] = Field(default_factory=dict)
    input_requests: tuple[InputRequest, ...] = ()
    confirmation_request: ConfirmationRequest | None = None


class SkillExecutionContext(BaseModel):
    """提供给 Skill Handler 的只读快照。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task: TaskInstance
    step: WorkflowStep
    input_data: dict[str, Any]
    dependency_outputs: dict[str, dict[str, Any]]
