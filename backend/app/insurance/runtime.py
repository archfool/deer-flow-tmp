"""供 Gateway Route 与 Agent Tool 共用的进程级 Repository 装配。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insurance.coverage_review.narrative import ModelNarrativeHarness
from app.insurance.service import InsuranceService
from deerflow.config.app_config import get_app_config
from deerflow.subject_memory import InMemorySubjectMemoryRepository, SqlSubjectMemoryRepository
from deerflow.workflows import InMemoryTaskRepository, SqlTaskRepository

_service: InsuranceService | None = None
_lock = threading.Lock()
DEFAULT_COVERAGE_REVIEW_MODEL = "minimax-m2.5-main"


def initialize_insurance_service(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> InsuranceService:
    """在 Gateway 完成数据库引擎初始化后构建 Repository。"""

    global _service
    with _lock:
        model_name = os.getenv("COVERAGE_REVIEW_MODEL") or DEFAULT_COVERAGE_REVIEW_MODEL
        model_config = get_app_config().get_model_config(model_name)
        if model_config is None:
            raise RuntimeError(f"coverage review model is not configured: {model_name}")
        if session_factory is None:
            task_repository = InMemoryTaskRepository()
            memory_repository = InMemorySubjectMemoryRepository()
        else:
            task_repository = SqlTaskRepository(session_factory)
            memory_repository = SqlSubjectMemoryRepository(session_factory)
        _service = InsuranceService(
            task_repository=task_repository,
            memory_repository=memory_repository,
            coverage_review_narrative=ModelNarrativeHarness(
                model_name=model_name,
                provider_model=model_config.model,
                timeout_seconds=_model_timeout_seconds(),
            ),
            coverage_review_knowledge_root=_knowledge_skills_root(),
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


def _knowledge_skills_root() -> Path | None:
    """解析保险书籍 Knowledge Skill 根目录。

    生产环境优先使用显式环境变量；当前本地工作区则可从
    项目父目录发现已经提取好的书籍 Skills。

    Returns:
        存在的 Skills 根目录；未配置时返回 None。
    """

    configured = os.getenv("COVERAGE_REVIEW_KNOWLEDGE_ROOT")
    candidate = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[4] / "保险书籍知识提取" / "skills"
    return candidate.resolve() if candidate.is_dir() else None


def _model_timeout_seconds() -> float:
    """读取保障检视单个语言节点的模型超时。

    Returns:
        大于零的超时秒数；配置无效时使用 90 秒。
    """

    configured = os.getenv("COVERAGE_REVIEW_MODEL_TIMEOUT_SECONDS", "90")
    try:
        value = float(configured)
    except ValueError:
        return 90.0
    return value if value > 0 else 90.0
