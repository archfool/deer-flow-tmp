"""任务 Repository 契约及进程内实现。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from deerflow.workflows.models import TaskInstance


class TaskConflictError(RuntimeError):
    """其他写入方已推进任务乐观锁版本时抛出。"""


class TaskRepository(ABC):
    """工作流引擎使用的持久化契约。"""

    @abstractmethod
    async def create(self, task: TaskInstance) -> TaskInstance: ...

    @abstractmethod
    async def get(self, task_id: str, *, owner_id: str) -> TaskInstance | None: ...

    @abstractmethod
    async def save(self, task: TaskInstance, *, expected_revision: int) -> TaskInstance: ...

    @abstractmethod
    async def list(
        self,
        *,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]: ...


class InMemoryTaskRepository(TaskRepository):
    """用于测试和纯内存部署的并发安全 Repository。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInstance] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: TaskInstance) -> TaskInstance:
        async with self._lock:
            if task.id in self._tasks:
                raise TaskConflictError(f"task already exists: {task.id}")
            stored = task.model_copy(deep=True)
            self._tasks[stored.id] = stored
            return stored.model_copy(deep=True)

    async def get(self, task_id: str, *, owner_id: str) -> TaskInstance | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.owner_id != owner_id:
                return None
            return task.model_copy(deep=True)

    async def save(self, task: TaskInstance, *, expected_revision: int) -> TaskInstance:
        async with self._lock:
            current = self._tasks.get(task.id)
            if current is None or current.owner_id != task.owner_id:
                raise TaskConflictError(f"task does not exist for owner: {task.id}")
            if current.revision != expected_revision:
                raise TaskConflictError(f"task {task.id} revision changed: expected {expected_revision}, current {current.revision}")
            stored = task.model_copy(deep=True, update={"revision": expected_revision + 1})
            self._tasks[stored.id] = stored
            return stored.model_copy(deep=True)

    async def list(
        self,
        *,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]:
        async with self._lock:
            tasks = [task.model_copy(deep=True) for task in self._tasks.values() if task.owner_id == owner_id and (subject_id is None or task.subject_id == subject_id) and (thread_id is None or task.thread_id == thread_id)]
        return sorted(tasks, key=lambda item: (item.updated_at, item.id), reverse=True)
