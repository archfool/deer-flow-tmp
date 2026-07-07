"""主体记忆各 Repository 后端共用的可序列化模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class MemoryUpdateSource(StrEnum):
    """用于控制更新能否立即提交的数据来源类型。"""

    USER_EXPLICIT = "user_explicit"
    AUTHORITATIVE_API = "authoritative_api"
    MODEL_INFERENCE = "model_inference"
    SYSTEM_DERIVED = "system_derived"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SubjectMemoryDocument(BaseModel):
    owner_id: str
    subject_id: str
    memory_type: str
    version: int
    data: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SubjectMemoryRevision(BaseModel):
    id: str
    owner_id: str
    subject_id: str
    memory_type: str
    from_version: int
    to_version: int
    patch: dict[str, Any]
    source: MemoryUpdateSource
    reason: str = ""
    thread_id: str | None = None
    task_id: str | None = None
    created_at: datetime


class SubjectMemoryCandidate(BaseModel):
    id: str
    owner_id: str
    subject_id: str
    memory_type: str
    base_version: int
    patch: dict[str, Any]
    source: MemoryUpdateSource
    status: CandidateStatus
    reason: str = ""
    thread_id: str | None = None
    task_id: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
