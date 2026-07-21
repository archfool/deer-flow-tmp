"""客户档案和保障检视任务的认证 HTTP API。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from app.gateway.deps import get_current_user
from app.insurance.coverage_review import COVERAGE_REVIEW_TASK_NAME, ReviewAction, ReviewDecision
from app.insurance.intake import CoverageReviewIntake
from app.insurance.mock_data import hydrate_registered_mock_intake
from app.insurance.models import CustomerProfile
from app.insurance.runtime import get_insurance_service
from app.insurance.ui import CoverageReviewUIEnvelope, build_intake_ui, build_task_ui
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.subject_memory import MemoryConflictError, MemoryUpdateSource, SubjectMemoryCandidate
from deerflow.workflows import TaskInstance, TaskStatus, WorkflowStateError

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
    trigger_ids: list[str] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)


class AdvanceCoverageReviewRequest(BaseModel):
    """补充步骤 1 所需结构化输入。"""

    trigger_ids: list[str] | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


class TaskActionRequest(BaseModel):
    reason: str = ""


class CoverageReviewFeedbackRequest(BaseModel):
    """代理人在复核闸门提交的自然语言意见。"""

    action: ReviewAction
    note: str = ""
    expected_review_revision: int = Field(ge=1)
    expected_kernel_hash: str = Field(min_length=8)
    expected_profile_version: int | None = Field(default=None, ge=1)


_ACTIVE_COVERAGE_REVIEW_STATUSES = {
    TaskStatus.READY,
    TaskStatus.RUNNING,
    TaskStatus.BLOCKED,
    TaskStatus.WAITING_INPUT,
    TaskStatus.WAITING_CONFIRMATION,
    TaskStatus.SUSPENDED,
}


async def _thread_coverage_reviews(
    *,
    owner_id: str,
    thread_id: str,
) -> list[TaskInstance]:
    """读取当前会话内按更新时间倒序排列的保障检视任务。"""

    tasks = await get_insurance_service().list_tasks(
        owner_id=owner_id,
        thread_id=thread_id,
    )
    return [task for task in tasks if task.task_name == COVERAGE_REVIEW_TASK_NAME]


@router.post(
    "/coverage-reviews/intake",
    response_model=CoverageReviewUIEnvelope,
    status_code=201,
)
async def start_coverage_review_intake(
    body: CoverageReviewIntake,
    request: Request,
) -> CoverageReviewUIEnvelope:
    """通过结构化补录表单创建客户档案并启动保障检视。"""

    body = hydrate_registered_mock_intake(body)
    if body.missing_identity_fields:
        return build_intake_ui(body)
    try:
        owner_id = await _owner_id(request)
        if body.thread_id:
            tasks = await _thread_coverage_reviews(
                owner_id=owner_id,
                thread_id=body.thread_id,
            )
            active = next(
                (task for task in tasks if task.status in _ACTIVE_COVERAGE_REVIEW_STATUSES),
                None,
            )
            if active is not None:
                return build_task_ui(active)
        task = await get_insurance_service().start_coverage_review_intake(
            owner_id=owner_id,
            intake=body,
        )
        return build_task_ui(task)
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get(
    "/coverage-reviews/latest",
    response_model=CoverageReviewUIEnvelope,
)
async def get_latest_coverage_review(
    thread_id: str,
    request: Request,
) -> CoverageReviewUIEnvelope:
    """恢复当前会话最近一次保障检视任务卡状态。"""

    try:
        tasks = await _thread_coverage_reviews(
            owner_id=await _owner_id(request),
            thread_id=thread_id,
        )
        if not tasks:
            raise HTTPException(status_code=404, detail="coverage review task not found")
        return build_task_ui(tasks[0])
    except HTTPException:
        raise
    except Exception as exc:
        raise _domain_error(exc) from exc


async def _owner_id(request: Request) -> str:
    """从服务端认证信息解析所有权，绝不信任请求数据中的所有者。"""

    # 关闭认证的本地开发环境仍使用 DeerFlow 的有效默认 Bucket。启用认证时，
    # Middleware 提供的 ID 始终优先，调用方无法在请求数据中提交其他所有者。
    return await get_current_user(request) or get_effective_user_id()


def _domain_error(exc: Exception) -> HTTPException:
    """把领域错误映射为稳定的 HTTP 语义，同时避免泄露内部信息。"""

    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, MemoryConflictError | WorkflowStateError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError | ValidationError):
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
            trigger_ids=body.trigger_ids,
            answers=body.answers,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/tasks/{task_id}", response_model=TaskInstance)
async def get_insurance_task(task_id: str, request: Request) -> TaskInstance:
    task = await get_insurance_service().get_task(task_id, owner_id=await _owner_id(request))
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get(
    "/tasks/{task_id}/ui",
    response_model=CoverageReviewUIEnvelope,
)
async def get_coverage_review_ui(
    task_id: str,
    request: Request,
) -> CoverageReviewUIEnvelope:
    """读取可直接渲染的保障检视任务卡。"""

    task = await get_insurance_service().get_task(
        task_id,
        owner_id=await _owner_id(request),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        return build_task_ui(task)
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/tasks/{task_id}/reports/internal", response_class=PlainTextResponse)
async def get_internal_coverage_review_report(
    task_id: str,
    request: Request,
) -> PlainTextResponse:
    """返回代理人复核用的 Markdown 对内诊断报告。"""

    task = await get_insurance_service().get_task(
        task_id,
        owner_id=await _owner_id(request),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    report = task.steps.get("internal-report")
    markdown = report.output.get("internal_report", {}).get("markdown") if report else None
    if not isinstance(markdown, str) or not markdown:
        raise HTTPException(status_code=409, detail="internal report is not ready")
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="coverage-review-{task.id}.md"'},
    )


@router.get("/tasks/{task_id}/reports/customer", response_class=HTMLResponse)
async def get_customer_coverage_review_report(
    task_id: str,
    request: Request,
) -> HTMLResponse:
    """返回代理人批准后由固定模板生成的 HTML 对客报告。"""

    task = await get_insurance_service().get_task(
        task_id,
        owner_id=await _owner_id(request),
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status.value != "completed":
        raise HTTPException(
            status_code=409,
            detail="customer report requires agent approval",
        )
    report = task.steps.get("customer-report")
    html = report.output.get("customer_report", {}).get("html") if report else None
    if not isinstance(html, str) or not html:
        raise HTTPException(status_code=409, detail="customer report is not ready")
    return HTMLResponse(html)


@router.get("/customers/{customer_id}/tasks", response_model=list[TaskInstance])
async def list_customer_tasks(customer_id: str, request: Request) -> list[TaskInstance]:
    return await get_insurance_service().list_tasks(
        owner_id=await _owner_id(request),
        customer_id=customer_id,
    )


@router.post("/tasks/{task_id}/advance", response_model=TaskInstance)
async def advance_insurance_task(
    task_id: str,
    request: Request,
    body: AdvanceCoverageReviewRequest | None = None,
) -> TaskInstance:
    try:
        return await get_insurance_service().advance_task(
            task_id,
            owner_id=await _owner_id(request),
            answers=body.answers if body is not None else None,
            trigger_ids=body.trigger_ids if body is not None else None,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/tasks/{task_id}/coverage-review-decision", response_model=TaskInstance)
async def decide_coverage_review(
    task_id: str,
    body: ReviewDecision,
    request: Request,
) -> TaskInstance:
    """提交代理人对保障检视复核包的决定。"""

    try:
        owner_id = await _owner_id(request)
        decision = body.model_copy(update={"reviewer_id": owner_id})
        return await get_insurance_service().decide_coverage_review(
            task_id,
            owner_id=owner_id,
            decision=decision,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/tasks/{task_id}/coverage-review-feedback", response_model=TaskInstance)
async def apply_coverage_review_feedback(
    task_id: str,
    body: CoverageReviewFeedbackRequest,
    request: Request,
) -> TaskInstance:
    """提交批准、拒绝或由 Harness 提取的自然语言修订。

    Args:
        task_id: 当前保障检视任务 ID。
        body: 复核动作、意见和乐观锁字段。
        request: 用于解析已认证所有者的 HTTP 请求。

    Returns:
        处理后的任务实例。
    """

    try:
        owner_id = await _owner_id(request)
        return await get_insurance_service().apply_coverage_review_feedback(
            task_id,
            owner_id=owner_id,
            action=body.action,
            note=body.note,
            expected_review_revision=body.expected_review_revision,
            expected_kernel_hash=body.expected_kernel_hash,
            expected_profile_version=body.expected_profile_version,
        )
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
