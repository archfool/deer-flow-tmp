"""客户档案和保障检视任务的认证 HTTP API。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.gateway.deps import get_current_user
from app.insurance.models import CustomerProfile
from app.insurance.runtime import get_insurance_service
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.subject_memory import MemoryConflictError, MemoryUpdateSource, SubjectMemoryCandidate
from deerflow.workflows import TaskInstance, WorkflowStateError

router = APIRouter(prefix="/api/insurance", tags=["insurance"])


class ProfileEnvelope(BaseModel):
    """返回通过校验的客户档案及其乐观锁版本号。"""

    profile: CustomerProfile
    version: int


class ProfilePatchRequest(BaseModel):
    """以 JSON Pointer 替换表达、并经过 Schema 校验的更新。"""

    expected_version: int = Field(ge=1)
    patch: dict[str, Any] = Field(description="JSON Pointer 路径到替换值的映射")
    source: Literal["user_explicit", "model_inference"] = "user_explicit"
    reason: str = ""
    thread_id: str | None = None
    task_id: str | None = None


class ProfilePatchResponse(BaseModel):
    """已提交更新返回档案，推断更新返回待确认候选项。"""

    profile: CustomerProfile | None = None
    version: int | None = None
    candidate: SubjectMemoryCandidate | None = None


class StartCoverageReviewRequest(BaseModel):
    thread_id: str | None = None
    parent_task_id: str | None = None


class TaskActionRequest(BaseModel):
    reason: str = ""


async def _owner_id(request: Request) -> str:
    """从服务端认证信息解析所有权，绝不信任请求数据中的所有者。"""

    # 关闭认证的本地开发环境仍使用 DeerFlow 的有效默认 Bucket。启用认证时，
    # Middleware 提供的 ID 始终优先，调用方无法在请求数据中提交其他所有者。
    return await get_current_user(request) or get_effective_user_id()


def _domain_error(exc: Exception) -> HTTPException:
    """把领域错误映射为稳定的 HTTP 语义，同时避免泄露内部信息。"""

    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, (MemoryConflictError, WorkflowStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (ValueError, ValidationError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="insurance domain operation failed")


@router.post("/customers", response_model=ProfileEnvelope, status_code=201)
async def create_customer_profile(body: CustomerProfile, request: Request) -> ProfileEnvelope:
    service = get_insurance_service()
    try:
        profile, version = await service.profiles.create(owner_id=await _owner_id(request), profile=body)
        return ProfileEnvelope(profile=profile, version=version)
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/customers/{customer_id}", response_model=ProfileEnvelope)
async def get_customer_profile(customer_id: str, request: Request) -> ProfileEnvelope:
    result = await get_insurance_service().profiles.get(
        owner_id=await _owner_id(request),
        customer_id=customer_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="customer profile not found")
    profile, version = result
    return ProfileEnvelope(profile=profile, version=version)


@router.patch("/customers/{customer_id}", response_model=ProfilePatchResponse)
async def patch_customer_profile(
    customer_id: str,
    body: ProfilePatchRequest,
    request: Request,
) -> ProfilePatchResponse:
    source = MemoryUpdateSource.MODEL_INFERENCE if body.source == "model_inference" else MemoryUpdateSource.USER_EXPLICIT
    try:
        profile, version, candidate = await get_insurance_service().profiles.update(
            owner_id=await _owner_id(request),
            customer_id=customer_id,
            expected_version=body.expected_version,
            patch=body.patch,
            source=source,
            reason=body.reason,
            thread_id=body.thread_id,
            task_id=body.task_id,
        )
        return ProfilePatchResponse(profile=profile, version=version, candidate=candidate)
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/customers/{customer_id}/candidates", response_model=list[SubjectMemoryCandidate])
async def list_profile_candidates(customer_id: str, request: Request) -> list[SubjectMemoryCandidate]:
    return await get_insurance_service().profiles.list_candidates(
        owner_id=await _owner_id(request),
        customer_id=customer_id,
    )


@router.post("/candidates/{candidate_id}/accept", response_model=ProfileEnvelope)
async def accept_profile_candidate(candidate_id: str, request: Request) -> ProfileEnvelope:
    try:
        profile, version = await get_insurance_service().profiles.accept_candidate(
            candidate_id,
            owner_id=await _owner_id(request),
        )
        return ProfileEnvelope(profile=profile, version=version)
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/candidates/{candidate_id}/reject", response_model=SubjectMemoryCandidate)
async def reject_profile_candidate(candidate_id: str, request: Request) -> SubjectMemoryCandidate:
    try:
        return await get_insurance_service().profiles.reject_candidate(
            candidate_id,
            owner_id=await _owner_id(request),
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/customers/{customer_id}/coverage-reviews", response_model=TaskInstance, status_code=201)
async def start_coverage_review(
    customer_id: str,
    body: StartCoverageReviewRequest,
    request: Request,
) -> TaskInstance:
    try:
        return await get_insurance_service().start_coverage_review(
            owner_id=await _owner_id(request),
            customer_id=customer_id,
            thread_id=body.thread_id,
            parent_task_id=body.parent_task_id,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/tasks/{task_id}", response_model=TaskInstance)
async def get_insurance_task(task_id: str, request: Request) -> TaskInstance:
    task = await get_insurance_service().get_task(task_id, owner_id=await _owner_id(request))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/customers/{customer_id}/tasks", response_model=list[TaskInstance])
async def list_customer_tasks(customer_id: str, request: Request) -> list[TaskInstance]:
    return await get_insurance_service().list_tasks(
        owner_id=await _owner_id(request),
        customer_id=customer_id,
    )


@router.post("/tasks/{task_id}/advance", response_model=TaskInstance)
async def advance_insurance_task(task_id: str, request: Request) -> TaskInstance:
    try:
        return await get_insurance_service().advance_task(task_id, owner_id=await _owner_id(request))
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/tasks/{task_id}/suspend", response_model=TaskInstance)
async def suspend_insurance_task(
    task_id: str,
    body: TaskActionRequest,
    request: Request,
) -> TaskInstance:
    try:
        return await get_insurance_service().suspend_task(
            task_id,
            owner_id=await _owner_id(request),
            reason=body.reason,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/tasks/{task_id}/resume", response_model=TaskInstance)
async def resume_insurance_task(task_id: str, request: Request) -> TaskInstance:
    try:
        return await get_insurance_service().resume_task(task_id, owner_id=await _owner_id(request))
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/tasks/{task_id}/cancel", response_model=TaskInstance)
async def cancel_insurance_task(
    task_id: str,
    body: TaskActionRequest,
    request: Request,
) -> TaskInstance:
    try:
        return await get_insurance_service().cancel_task(
            task_id,
            owner_id=await _owner_id(request),
            reason=body.reason,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc
