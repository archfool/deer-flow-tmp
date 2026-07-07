"""供 Gateway Route 与 Agent Tool 共用的进程级 Repository 装配。"""

from __future__ import annotations

import threading

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insurance.service import InsuranceService
from deerflow.subject_memory import InMemorySubjectMemoryRepository, SqlSubjectMemoryRepository
from deerflow.workflows import InMemoryTaskRepository, SqlTaskRepository

_service: InsuranceService | None = None
_lock = threading.Lock()


def initialize_insurance_service(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> InsuranceService:
    """在 Gateway 完成数据库引擎初始化后构建 Repository。"""

    global _service
    with _lock:
        if session_factory is None:
            task_repository = InMemoryTaskRepository()
            memory_repository = InMemorySubjectMemoryRepository()
        else:
            task_repository = SqlTaskRepository(session_factory)
            memory_repository = SqlSubjectMemoryRepository(session_factory)
        _service = InsuranceService(
            task_repository=task_repository,
            memory_repository=memory_repository,
        )
        return _service


def get_insurance_service() -> InsuranceService:
    if _service is None:
        raise RuntimeError("insurance service is unavailable before Gateway runtime initialization")
    return _service


def reset_insurance_service() -> None:
    global _service
    with _lock:
        _service = None
