"""持久化工作流任务实例和审计事件的 SQL 实现。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base
from deerflow.workflows.models import TaskInstance
from deerflow.workflows.repository import TaskConflictError, TaskRepository


class WorkflowTaskRow(Base):
    """用于按所有者、主体和线程快速查询的任务实例物化记录。"""

    __tablename__ = "workflow_tasks"
    __table_args__ = (
        Index("ix_workflow_task_owner_subject", "owner_id", "subject_id", "updated_at"),
        Index("ix_workflow_task_owner_thread", "owner_id", "thread_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowTaskEventRow(Base):
    """用于运维审计和调试的追加式状态迁移记录。"""

    __tablename__ = "workflow_task_events"
    __table_args__ = (Index("ix_workflow_task_event_task_revision", "task_id", "to_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    to_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlTaskRepository(TaskRepository):
    """SQLite 与 PostgreSQL 共用的乐观锁 SQL 任务 Repository。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _payload(task: TaskInstance) -> dict[str, Any]:
        # JSON 模式会转换 datetime 和枚举，使同一份数据同时兼容 SQLite JSON
        # 与 PostgreSQL JSONB 驱动。
        return task.model_dump(mode="json")

    @staticmethod
    def _instance(row: WorkflowTaskRow) -> TaskInstance:
        payload = dict(row.payload_json)
        # 查询列是数据库中的权威数据源。用它们覆盖 JSON 字段，可以兼容被中断的
        # 旧写入场景：此时 JSON 可能比物化列落后一个版本。
        payload.update(
            {
                "id": row.id,
                "owner_id": row.owner_id,
                "subject_id": row.subject_id,
                "thread_id": row.thread_id,
                "parent_task_id": row.parent_task_id,
                "status": row.status,
                "revision": row.revision,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
        )
        return TaskInstance.model_validate(payload)

    @staticmethod
    def _event(task: TaskInstance, *, event_type: str, from_revision: int, to_revision: int) -> WorkflowTaskEventRow:
        return WorkflowTaskEventRow(
            id=str(uuid4()),
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=event_type,
            from_revision=from_revision,
            to_revision=to_revision,
            status=task.status.value,
            payload_json=SqlTaskRepository._payload(task),
            summary=task.error or task.suspension_reason or "",
            created_at=task.updated_at,
        )

    async def create(self, task: TaskInstance) -> TaskInstance:
        row = WorkflowTaskRow(
            id=task.id,
            task_name=task.task_name,
            definition_version=task.definition_version,
            owner_id=task.owner_id,
            subject_id=task.subject_id,
            thread_id=task.thread_id,
            parent_task_id=task.parent_task_id,
            status=task.status.value,
            payload_json=self._payload(task),
            revision=task.revision,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        async with self._sf() as session:
            session.add_all([row, self._event(task, event_type="created", from_revision=-1, to_revision=task.revision)])
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise TaskConflictError(f"task already exists: {task.id}") from exc
            return self._instance(row)

    async def get(self, task_id: str, *, owner_id: str) -> TaskInstance | None:
        async with self._sf() as session:
            row = await session.scalar(select(WorkflowTaskRow).where(WorkflowTaskRow.id == task_id, WorkflowTaskRow.owner_id == owner_id))
            return self._instance(row) if row is not None else None

    async def save(self, task: TaskInstance, *, expected_revision: int) -> TaskInstance:
        next_revision = expected_revision + 1
        stored = task.model_copy(deep=True, update={"revision": next_revision})
        async with self._sf() as session:
            result = await session.execute(
                update(WorkflowTaskRow)
                .where(
                    WorkflowTaskRow.id == task.id,
                    WorkflowTaskRow.owner_id == task.owner_id,
                    WorkflowTaskRow.revision == expected_revision,
                )
                .values(
                    status=stored.status.value,
                    payload_json=self._payload(stored),
                    revision=next_revision,
                    subject_id=stored.subject_id,
                    thread_id=stored.thread_id,
                    parent_task_id=stored.parent_task_id,
                    updated_at=stored.updated_at,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise TaskConflictError(f"task {task.id} revision changed or is unavailable for owner {task.owner_id}")
            session.add(
                self._event(
                    stored,
                    event_type="state_changed",
                    from_revision=expected_revision,
                    to_revision=next_revision,
                )
            )
            await session.commit()
            return stored

    async def list(
        self,
        *,
        owner_id: str,
        subject_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]:
        stmt = select(WorkflowTaskRow).where(WorkflowTaskRow.owner_id == owner_id)
        if subject_id is not None:
            stmt = stmt.where(WorkflowTaskRow.subject_id == subject_id)
        if thread_id is not None:
            stmt = stmt.where(WorkflowTaskRow.thread_id == thread_id)
        stmt = stmt.order_by(WorkflowTaskRow.updated_at.desc(), WorkflowTaskRow.id.desc())
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [self._instance(row) for row in rows]
