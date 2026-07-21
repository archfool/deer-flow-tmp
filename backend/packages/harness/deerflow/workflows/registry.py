"""任务定义和可执行 Skill Handler 的注册表。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable

from deerflow.workflows.models import (
    SkillDefinition,
    SkillExecutionContext,
    SkillExecutionResult,
    TaskDefinition,
)

SkillHandler = Callable[[SkillExecutionContext], Awaitable[SkillExecutionResult] | SkillExecutionResult]


def _validate_output_contract(
    definition: SkillDefinition,
    output: dict,
) -> None:
    """执行工作流 Skill 顶层输出契约校验。

    领域 Handler 负责用 Pydantic 校验嵌套对象；工作流注册表在统一边界校验
    顶层字段白名单和必填键，避免声明的 JSON Schema 只停留在文档层。

    Args:
        definition: 当前版本化 Skill 契约。
        output: Handler 返回的结构化输出。

    Raises:
        ValueError: 输出包含未声明字段或缺少必填字段时抛出。
    """

    schema = definition.output_schema
    if schema.get("type") != "object":
        return
    properties = schema.get("properties")
    if isinstance(properties, dict) and schema.get("additionalProperties") is False:
        unknown = set(output) - set(properties)
        if unknown:
            raise ValueError(f"skill {definition.name}@{definition.version} returned undeclared output keys: {sorted(unknown)}")
    required = schema.get("required")
    if isinstance(required, list):
        missing = set(required) - set(output)
        if missing:
            raise ValueError(f"skill {definition.name}@{definition.version} missed required output keys: {sorted(missing)}")


class DefinitionRegistry:
    """支持版本的不可变任务定义查询表。"""

    def __init__(self, definitions: Iterable[TaskDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, str], TaskDefinition] = {}
        self._latest: dict[str, TaskDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: TaskDefinition) -> None:
        if definition.key in self._definitions:
            raise ValueError(f"task definition already registered: {definition.key}")
        self._definitions[definition.key] = definition
        # 版本号是不透明字符串，因此注册顺序决定默认版本；需要可复现性的
        # 调用方始终使用 TaskInstance 中保存的明确版本号。
        self._latest[definition.name] = definition

    def get(self, name: str, version: str | None = None) -> TaskDefinition:
        definition = self._latest.get(name) if version is None else self._definitions.get((name, version))
        if definition is None:
            suffix = "latest" if version is None else version
            raise KeyError(f"unknown task definition: {name}@{suffix}")
        return definition


class SkillRegistry:
    """把版本化 Skill 契约绑定到对应的可执行 Handler。"""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], tuple[SkillDefinition, SkillHandler]] = {}

    def register(self, definition: SkillDefinition, handler: SkillHandler) -> None:
        if definition.key in self._entries:
            raise ValueError(f"skill already registered: {definition.key}")
        self._entries[definition.key] = (definition, handler)

    def get(self, name: str, version: str = "1") -> tuple[SkillDefinition, SkillHandler]:
        try:
            return self._entries[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown executable skill: {name}@{version}") from exc

    async def invoke(self, context: SkillExecutionContext) -> SkillExecutionResult:
        definition, handler = self.get(context.step.skill, context.step.skill_version)
        value = handler(context)
        if inspect.isawaitable(value):
            async with asyncio.timeout(definition.timeout_seconds):
                value = await value
        if not isinstance(value, SkillExecutionResult):
            raise TypeError(f"skill {definition.name}@{definition.version} returned {type(value).__name__}, expected SkillExecutionResult")
        _validate_output_contract(definition, value.output)
        return value
