"""任务定义和可执行 Skill Handler 的注册表。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable

from deerflow.workflows.models import (
    SkillDefinition,
    SkillExecutionContext,
    SkillExecutionResult,
    TaskDefinition,
)

SkillHandler = Callable[[SkillExecutionContext], Awaitable[SkillExecutionResult] | SkillExecutionResult]


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
            value = await value
        if not isinstance(value, SkillExecutionResult):
            raise TypeError(f"skill {definition.name}@{definition.version} returned {type(value).__name__}, expected SkillExecutionResult")
        return value
