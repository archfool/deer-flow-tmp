"""版本化主体记忆文档的持久化与隔离测试。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deerflow.persistence.base import Base
from deerflow.subject_memory import (
    MemoryConflictError,
    MemoryUpdateSource,
    SqlSubjectMemoryRepository,
)


@pytest_asyncio.fixture
async def repository():
    """使用 SQLite 创建真实 SQL Repository，与本地部署方式保持一致。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SqlSubjectMemoryRepository(session_factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_is_scoped_by_owner_subject_and_type(repository) -> None:
    document = await repository.create(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
        data={"name": "测试家庭", "financial": {"annual_income": "300000"}},
        source=MemoryUpdateSource.USER_EXPLICIT,
    )

    assert document.version == 1
    assert (
        await repository.get(
            owner_id="agent-b",
            subject_id="customer-1",
            memory_type="insurance.customer-profile",
        )
        is None
    )


@pytest.mark.asyncio
async def test_memory_patch_uses_optimistic_version_and_keeps_revision(repository) -> None:
    created = await repository.create(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
        data={"financial": {"annual_income": "300000"}},
        source=MemoryUpdateSource.USER_EXPLICIT,
    )

    updated = await repository.apply_patch(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
        expected_version=created.version,
        patch={"/financial/annual_income": "350000"},
        source=MemoryUpdateSource.USER_EXPLICIT,
        reason="用户纠正收入",
    )
    assert updated.version == 2
    assert updated.data["financial"]["annual_income"] == "350000"

    with pytest.raises(MemoryConflictError):
        await repository.apply_patch(
            owner_id="agent-a",
            subject_id="customer-1",
            memory_type="insurance.customer-profile",
            expected_version=1,
            patch={"/financial/annual_income": "360000"},
            source=MemoryUpdateSource.USER_EXPLICIT,
        )

    revisions = await repository.list_revisions(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
    )
    assert [revision.to_version for revision in revisions] == [1, 2]


@pytest.mark.asyncio
async def test_inference_is_staged_until_candidate_is_accepted(repository) -> None:
    created = await repository.create(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
        data={"health_summary": None},
        source=MemoryUpdateSource.USER_EXPLICIT,
    )
    candidate = await repository.propose_candidate(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
        base_version=created.version,
        patch={"/health_summary": "模型推断：可能有慢性病"},
        source=MemoryUpdateSource.MODEL_INFERENCE,
        reason="从对话中推断，不可直接写入",
    )

    unchanged = await repository.get(
        owner_id="agent-a",
        subject_id="customer-1",
        memory_type="insurance.customer-profile",
    )
    assert unchanged is not None
    assert unchanged.data["health_summary"] is None

    accepted = await repository.accept_candidate(candidate.id, owner_id="agent-a")
    assert accepted.data["health_summary"] == "模型推断：可能有慢性病"
    assert accepted.version == 2
