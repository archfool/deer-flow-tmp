"""隔离且版本化的主体记忆 Repository 实现。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base
from deerflow.subject_memory.models import (
    CandidateStatus,
    MemoryUpdateSource,
    SubjectMemoryCandidate,
    SubjectMemoryDocument,
    SubjectMemoryRevision,
)


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryConflictError(RuntimeError):
    """文档被并发修改，导致比较并交换失败。"""


class InvalidMemoryPatch(ValueError):
    """JSON Pointer Patch 无法应用到当前文档结构。"""


def _decode_pointer_token(token: str) -> str:
    # RFC 6901 要求先解码 ~1 再解码 ~0。反转顺序会把 `~01` 错误转换为 `/`，
    # 从而改变实际寻址的键。
    return token.replace("~1", "/").replace("~0", "~")


def apply_json_pointer_patch(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """返回应用路径/值替换后的文档深拷贝。"""

    result: Any = deepcopy(data)
    for pointer, value in patch.items():
        if not pointer.startswith("/"):
            raise InvalidMemoryPatch(f"patch path must be an absolute JSON Pointer: {pointer!r}")
        tokens = [_decode_pointer_token(token) for token in pointer.split("/")[1:]]
        if not tokens:
            if not isinstance(value, dict):
                raise InvalidMemoryPatch("root replacement must remain a JSON object")
            result = deepcopy(value)
            continue

        current: Any = result
        for token in tokens[:-1]:
            if isinstance(current, dict):
                if token not in current:
                    raise InvalidMemoryPatch(f"missing patch parent {token!r} in {pointer!r}")
                current = current[token]
            elif isinstance(current, list):
                try:
                    current = current[int(token)]
                except (ValueError, IndexError) as exc:
                    raise InvalidMemoryPatch(f"invalid list index {token!r} in {pointer!r}") from exc
            else:
                raise InvalidMemoryPatch(f"cannot traverse scalar value in {pointer!r}")

        leaf = tokens[-1]
        if isinstance(current, dict):
            current[leaf] = deepcopy(value)
        elif isinstance(current, list):
            try:
                index = int(leaf)
                current[index] = deepcopy(value)
            except (ValueError, IndexError) as exc:
                raise InvalidMemoryPatch(f"invalid list index {leaf!r} in {pointer!r}") from exc
        else:
            raise InvalidMemoryPatch(f"cannot replace a child of scalar value in {pointer!r}")

    if not isinstance(result, dict):
        raise InvalidMemoryPatch("subject memory root must be a JSON object")
    return result


class SubjectMemoryRow(Base):
    """当前文档的物化快照。"""

    __tablename__ = "subject_memories"
    __table_args__ = (
        UniqueConstraint("owner_id", "subject_id", "memory_type", name="uq_subject_memory_scope"),
        Index("ix_subject_memory_owner_subject", "owner_id", "subject_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectMemoryRevisionRow(Base):
    """单次已提交 Patch 的追加式审计记录。"""

    __tablename__ = "subject_memory_revisions"
    __table_args__ = (Index("ix_subject_memory_revision_scope", "owner_id", "subject_id", "memory_type", "to_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(128), nullable=False)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thread_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubjectMemoryCandidateRow(Base):
    """等待所有者决策、尚未提交的推断 Patch。"""

    __tablename__ = "subject_memory_candidates"
    __table_args__ = (Index("ix_subject_memory_candidate_scope", "owner_id", "subject_id", "memory_type", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(128), nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thread_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubjectMemoryRepository(ABC):
    """SQL 与纯内存部署共用的存储契约。"""

    @abstractmethod
    async def create(self, **kwargs) -> SubjectMemoryDocument: ...

    @abstractmethod
    async def get(self, *, owner_id: str, subject_id: str, memory_type: str) -> SubjectMemoryDocument | None: ...

    @abstractmethod
    async def apply_patch(self, **kwargs) -> SubjectMemoryDocument: ...

    @abstractmethod
    async def list_revisions(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryRevision]: ...

    @abstractmethod
    async def propose_candidate(self, **kwargs) -> SubjectMemoryCandidate: ...

    @abstractmethod
    async def list_candidates(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryCandidate]: ...

    @abstractmethod
    async def accept_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryDocument: ...

    @abstractmethod
    async def reject_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryCandidate: ...


class SqlSubjectMemoryRepository(SubjectMemoryRepository):
    """同时兼容 SQLite 和 PostgreSQL 的 SQLAlchemy Repository。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _document(row: SubjectMemoryRow) -> SubjectMemoryDocument:
        return SubjectMemoryDocument(
            owner_id=row.owner_id,
            subject_id=row.subject_id,
            memory_type=row.memory_type,
            version=row.version,
            data=deepcopy(row.data_json),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _revision(row: SubjectMemoryRevisionRow) -> SubjectMemoryRevision:
        return SubjectMemoryRevision(
            id=row.id,
            owner_id=row.owner_id,
            subject_id=row.subject_id,
            memory_type=row.memory_type,
            from_version=row.from_version,
            to_version=row.to_version,
            patch=deepcopy(row.patch_json),
            source=MemoryUpdateSource(row.source),
            reason=row.reason,
            thread_id=row.thread_id,
            task_id=row.task_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _candidate(row: SubjectMemoryCandidateRow) -> SubjectMemoryCandidate:
        return SubjectMemoryCandidate(
            id=row.id,
            owner_id=row.owner_id,
            subject_id=row.subject_id,
            memory_type=row.memory_type,
            base_version=row.base_version,
            patch=deepcopy(row.patch_json),
            source=MemoryUpdateSource(row.source),
            status=CandidateStatus(row.status),
            reason=row.reason,
            thread_id=row.thread_id,
            task_id=row.task_id,
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )

    @staticmethod
    def _scope(owner_id: str, subject_id: str, memory_type: str):
        return (
            SubjectMemoryRow.owner_id == owner_id,
            SubjectMemoryRow.subject_id == subject_id,
            SubjectMemoryRow.memory_type == memory_type,
        )

    async def create(
        self,
        *,
        owner_id: str,
        subject_id: str,
        memory_type: str,
        data: dict[str, Any],
        source: MemoryUpdateSource,
        reason: str = "initial document",
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> SubjectMemoryDocument:
        now = _now()
        row = SubjectMemoryRow(
            id=str(uuid4()),
            owner_id=owner_id,
            subject_id=subject_id,
            memory_type=memory_type,
            version=1,
            data_json=deepcopy(data),
            created_at=now,
            updated_at=now,
        )
        revision = SubjectMemoryRevisionRow(
            id=str(uuid4()),
            owner_id=owner_id,
            subject_id=subject_id,
            memory_type=memory_type,
            from_version=0,
            to_version=1,
            patch_json={"": deepcopy(data)},
            source=source.value,
            reason=reason,
            thread_id=thread_id,
            task_id=task_id,
            created_at=now,
        )
        async with self._sf() as session:
            existing = await session.scalar(select(SubjectMemoryRow).where(*self._scope(owner_id, subject_id, memory_type)))
            if existing is not None:
                raise MemoryConflictError("subject memory already exists")
            session.add_all([row, revision])
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                raise MemoryConflictError("subject memory was created concurrently") from exc
            return self._document(row)

    async def get(self, *, owner_id: str, subject_id: str, memory_type: str) -> SubjectMemoryDocument | None:
        async with self._sf() as session:
            row = await session.scalar(select(SubjectMemoryRow).where(*self._scope(owner_id, subject_id, memory_type)))
            return self._document(row) if row is not None else None

    async def apply_patch(
        self,
        *,
        owner_id: str,
        subject_id: str,
        memory_type: str,
        expected_version: int,
        patch: dict[str, Any],
        source: MemoryUpdateSource,
        reason: str = "",
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> SubjectMemoryDocument:
        async with self._sf() as session:
            row = await session.scalar(select(SubjectMemoryRow).where(*self._scope(owner_id, subject_id, memory_type)))
            if row is None or row.version != expected_version:
                raise MemoryConflictError("subject memory version changed or document is unavailable")
            new_data = apply_json_pointer_patch(row.data_json, patch)
            now = _now()
            result = await session.execute(update(SubjectMemoryRow).where(SubjectMemoryRow.id == row.id, SubjectMemoryRow.version == expected_version).values(data_json=new_data, version=expected_version + 1, updated_at=now))
            if result.rowcount != 1:
                await session.rollback()
                raise MemoryConflictError("subject memory version changed during update")
            session.add(
                SubjectMemoryRevisionRow(
                    id=str(uuid4()),
                    owner_id=owner_id,
                    subject_id=subject_id,
                    memory_type=memory_type,
                    from_version=expected_version,
                    to_version=expected_version + 1,
                    patch_json=deepcopy(patch),
                    source=source.value,
                    reason=reason,
                    thread_id=thread_id,
                    task_id=task_id,
                    created_at=now,
                )
            )
            await session.commit()
            return SubjectMemoryDocument(
                owner_id=owner_id,
                subject_id=subject_id,
                memory_type=memory_type,
                version=expected_version + 1,
                data=new_data,
                created_at=row.created_at,
                updated_at=now,
            )

    async def list_revisions(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryRevision]:
        stmt = (
            select(SubjectMemoryRevisionRow)
            .where(
                SubjectMemoryRevisionRow.owner_id == owner_id,
                SubjectMemoryRevisionRow.subject_id == subject_id,
                SubjectMemoryRevisionRow.memory_type == memory_type,
            )
            .order_by(SubjectMemoryRevisionRow.to_version.asc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [self._revision(row) for row in rows]

    async def propose_candidate(
        self,
        *,
        owner_id: str,
        subject_id: str,
        memory_type: str,
        base_version: int,
        patch: dict[str, Any],
        source: MemoryUpdateSource,
        reason: str = "",
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> SubjectMemoryCandidate:
        if source is not MemoryUpdateSource.MODEL_INFERENCE:
            raise ValueError("candidate staging is reserved for model inference")
        document = await self.get(owner_id=owner_id, subject_id=subject_id, memory_type=memory_type)
        if document is None or document.version != base_version:
            raise MemoryConflictError("candidate base version is stale")
        # 在创建候选项时即校验路径，避免把不可应用的候选项展示给用户，
        # 并在用户批准后才失败。
        apply_json_pointer_patch(document.data, patch)
        row = SubjectMemoryCandidateRow(
            id=str(uuid4()),
            owner_id=owner_id,
            subject_id=subject_id,
            memory_type=memory_type,
            base_version=base_version,
            patch_json=deepcopy(patch),
            source=source.value,
            status=CandidateStatus.PENDING.value,
            reason=reason,
            thread_id=thread_id,
            task_id=task_id,
            created_at=_now(),
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            return self._candidate(row)

    async def list_candidates(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryCandidate]:
        stmt = (
            select(SubjectMemoryCandidateRow)
            .where(
                SubjectMemoryCandidateRow.owner_id == owner_id,
                SubjectMemoryCandidateRow.subject_id == subject_id,
                SubjectMemoryCandidateRow.memory_type == memory_type,
            )
            .order_by(SubjectMemoryCandidateRow.created_at.asc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [self._candidate(row) for row in rows]

    async def accept_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryDocument:
        async with self._sf() as session:
            candidate = await session.scalar(
                select(SubjectMemoryCandidateRow).where(
                    SubjectMemoryCandidateRow.id == candidate_id,
                    SubjectMemoryCandidateRow.owner_id == owner_id,
                )
            )
            if candidate is None or candidate.status != CandidateStatus.PENDING.value:
                raise KeyError("pending candidate not found")
            document = await session.scalar(select(SubjectMemoryRow).where(*self._scope(owner_id, candidate.subject_id, candidate.memory_type)))
            if document is None or document.version != candidate.base_version:
                raise MemoryConflictError("candidate base version is stale")
            new_data = apply_json_pointer_patch(document.data_json, candidate.patch_json)
            now = _now()
            result = await session.execute(
                update(SubjectMemoryRow).where(SubjectMemoryRow.id == document.id, SubjectMemoryRow.version == candidate.base_version).values(data_json=new_data, version=candidate.base_version + 1, updated_at=now)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise MemoryConflictError("subject memory changed while accepting candidate")
            candidate.status = CandidateStatus.ACCEPTED.value
            candidate.resolved_at = now
            session.add(
                SubjectMemoryRevisionRow(
                    id=str(uuid4()),
                    owner_id=owner_id,
                    subject_id=candidate.subject_id,
                    memory_type=candidate.memory_type,
                    from_version=candidate.base_version,
                    to_version=candidate.base_version + 1,
                    patch_json=deepcopy(candidate.patch_json),
                    source=candidate.source,
                    reason=candidate.reason,
                    thread_id=candidate.thread_id,
                    task_id=candidate.task_id,
                    created_at=now,
                )
            )
            await session.commit()
            return SubjectMemoryDocument(
                owner_id=owner_id,
                subject_id=candidate.subject_id,
                memory_type=candidate.memory_type,
                version=candidate.base_version + 1,
                data=new_data,
                created_at=document.created_at,
                updated_at=now,
            )

    async def reject_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryCandidate:
        async with self._sf() as session:
            row = await session.scalar(
                select(SubjectMemoryCandidateRow).where(
                    SubjectMemoryCandidateRow.id == candidate_id,
                    SubjectMemoryCandidateRow.owner_id == owner_id,
                )
            )
            if row is None or row.status != CandidateStatus.PENDING.value:
                raise KeyError("pending candidate not found")
            row.status = CandidateStatus.REJECTED.value
            row.resolved_at = _now()
            await session.commit()
            return self._candidate(row)


class InMemorySubjectMemoryRepository(SubjectMemoryRepository):
    """关闭 ORM 持久化时使用的功能等价进程内后端。"""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str, str], SubjectMemoryDocument] = {}
        self._revisions: list[SubjectMemoryRevision] = []
        self._candidates: dict[str, SubjectMemoryCandidate] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(owner_id: str, subject_id: str, memory_type: str) -> tuple[str, str, str]:
        return owner_id, subject_id, memory_type

    async def create(self, **kwargs) -> SubjectMemoryDocument:
        key = self._key(kwargs["owner_id"], kwargs["subject_id"], kwargs["memory_type"])
        async with self._lock:
            if key in self._documents:
                raise MemoryConflictError("subject memory already exists")
            now = _now()
            document = SubjectMemoryDocument(
                owner_id=key[0],
                subject_id=key[1],
                memory_type=key[2],
                version=1,
                data=deepcopy(kwargs["data"]),
                created_at=now,
                updated_at=now,
            )
            self._documents[key] = document
            self._revisions.append(
                SubjectMemoryRevision(
                    id=str(uuid4()),
                    owner_id=key[0],
                    subject_id=key[1],
                    memory_type=key[2],
                    from_version=0,
                    to_version=1,
                    patch={"": deepcopy(kwargs["data"])},
                    source=kwargs["source"],
                    reason=kwargs.get("reason", "initial document"),
                    thread_id=kwargs.get("thread_id"),
                    task_id=kwargs.get("task_id"),
                    created_at=now,
                )
            )
            return document.model_copy(deep=True)

    async def get(self, *, owner_id: str, subject_id: str, memory_type: str) -> SubjectMemoryDocument | None:
        async with self._lock:
            document = self._documents.get(self._key(owner_id, subject_id, memory_type))
            return document.model_copy(deep=True) if document else None

    async def apply_patch(self, **kwargs) -> SubjectMemoryDocument:
        key = self._key(kwargs["owner_id"], kwargs["subject_id"], kwargs["memory_type"])
        async with self._lock:
            current = self._documents.get(key)
            if current is None or current.version != kwargs["expected_version"]:
                raise MemoryConflictError("subject memory version changed or document is unavailable")
            now = _now()
            updated = current.model_copy(
                deep=True,
                update={
                    "version": current.version + 1,
                    "data": apply_json_pointer_patch(current.data, kwargs["patch"]),
                    "updated_at": now,
                },
            )
            self._documents[key] = updated
            self._revisions.append(
                SubjectMemoryRevision(
                    id=str(uuid4()),
                    owner_id=key[0],
                    subject_id=key[1],
                    memory_type=key[2],
                    from_version=current.version,
                    to_version=updated.version,
                    patch=deepcopy(kwargs["patch"]),
                    source=kwargs["source"],
                    reason=kwargs.get("reason", ""),
                    thread_id=kwargs.get("thread_id"),
                    task_id=kwargs.get("task_id"),
                    created_at=now,
                )
            )
            return updated.model_copy(deep=True)

    async def list_revisions(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryRevision]:
        async with self._lock:
            return [item.model_copy(deep=True) for item in self._revisions if (item.owner_id, item.subject_id, item.memory_type) == (owner_id, subject_id, memory_type)]

    async def propose_candidate(self, **kwargs) -> SubjectMemoryCandidate:
        if kwargs["source"] is not MemoryUpdateSource.MODEL_INFERENCE:
            raise ValueError("candidate staging is reserved for model inference")
        document = await self.get(owner_id=kwargs["owner_id"], subject_id=kwargs["subject_id"], memory_type=kwargs["memory_type"])
        if document is None or document.version != kwargs["base_version"]:
            raise MemoryConflictError("candidate base version is stale")
        apply_json_pointer_patch(document.data, kwargs["patch"])
        candidate = SubjectMemoryCandidate(
            id=str(uuid4()),
            owner_id=kwargs["owner_id"],
            subject_id=kwargs["subject_id"],
            memory_type=kwargs["memory_type"],
            base_version=kwargs["base_version"],
            patch=deepcopy(kwargs["patch"]),
            source=kwargs["source"],
            status=CandidateStatus.PENDING,
            reason=kwargs.get("reason", ""),
            thread_id=kwargs.get("thread_id"),
            task_id=kwargs.get("task_id"),
            created_at=_now(),
        )
        async with self._lock:
            self._candidates[candidate.id] = candidate
        return candidate.model_copy(deep=True)

    async def list_candidates(self, *, owner_id: str, subject_id: str, memory_type: str) -> list[SubjectMemoryCandidate]:
        async with self._lock:
            return [item.model_copy(deep=True) for item in self._candidates.values() if (item.owner_id, item.subject_id, item.memory_type) == (owner_id, subject_id, memory_type)]

    async def accept_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryDocument:
        async with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.owner_id != owner_id or candidate.status is not CandidateStatus.PENDING:
                raise KeyError("pending candidate not found")
            candidate_copy = candidate.model_copy(deep=True)
        document = await self.apply_patch(
            owner_id=owner_id,
            subject_id=candidate_copy.subject_id,
            memory_type=candidate_copy.memory_type,
            expected_version=candidate_copy.base_version,
            patch=candidate_copy.patch,
            source=candidate_copy.source,
            reason=candidate_copy.reason,
            thread_id=candidate_copy.thread_id,
            task_id=candidate_copy.task_id,
        )
        async with self._lock:
            self._candidates[candidate_id] = candidate_copy.model_copy(update={"status": CandidateStatus.ACCEPTED, "resolved_at": _now()})
        return document

    async def reject_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryCandidate:
        async with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None or candidate.owner_id != owner_id or candidate.status is not CandidateStatus.PENDING:
                raise KeyError("pending candidate not found")
            rejected = candidate.model_copy(update={"status": CandidateStatus.REJECTED, "resolved_at": _now()})
            self._candidates[candidate_id] = rejected
            return rejected.model_copy(deep=True)
