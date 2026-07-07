"""在主体记忆之上提供 Schema 校验的客户档案服务。"""

from __future__ import annotations

from typing import Any

from app.insurance.models import CustomerProfile, FactSource, FieldEvidence
from deerflow.subject_memory import (
    MemoryUpdateSource,
    SubjectMemoryCandidate,
    SubjectMemoryDocument,
    SubjectMemoryRepository,
    apply_json_pointer_patch,
)

CUSTOMER_PROFILE_MEMORY_TYPE = "insurance.customer-profile"


class CustomerProfileService:
    """负责档案校验、来源策略和 customer_id 不变量。"""

    def __init__(self, repository: SubjectMemoryRepository) -> None:
        self._repository = repository

    @staticmethod
    def _patch_with_evidence(
        patch: dict[str, Any],
        *,
        source: MemoryUpdateSource,
        reason: str,
    ) -> dict[str, Any]:
        """为每一次领域数据替换附加字段级来源信息。

        修订记录回答“哪个操作修改了文档”；Evidence Map 则无需重放完整事件日志，
        即可回答报告生成时更重要的问题：“当前字段值来自哪里”。推断来源与候选
        Patch 一同暂存，因此只会在候选项被接受后生效。
        """

        if any(path == "/field_evidence" or path.startswith("/field_evidence/") for path in patch):
            raise ValueError("field_evidence is maintained by the profile service")
        fact_source = {
            MemoryUpdateSource.USER_EXPLICIT: FactSource.USER_EXPLICIT,
            MemoryUpdateSource.AUTHORITATIVE_API: FactSource.AUTHORITATIVE_API,
            MemoryUpdateSource.MODEL_INFERENCE: FactSource.MODEL_INFERENCE_CONFIRMED,
        }.get(source)
        if fact_source is None:
            return dict(patch)

        effective_patch = dict(patch)
        for path in patch:
            # JSON Pointer 本身也可以作为 field_evidence 的字典键。这里再转义一次，
            # 确保通用 Patcher 把 `/foo/bar` 保存为一个键，而不是尝试创建嵌套对象。
            evidence_key = path.replace("~", "~0").replace("/", "~1")
            effective_patch[f"/field_evidence/{evidence_key}"] = FieldEvidence(
                source=fact_source,
                source_reference=reason or None,
                confirmed=True,
            ).model_dump(mode="json")
        return effective_patch

    @staticmethod
    def _profile(document: SubjectMemoryDocument) -> CustomerProfile:
        return CustomerProfile.model_validate(document.data)

    async def create(
        self,
        *,
        owner_id: str,
        profile: CustomerProfile,
        thread_id: str | None = None,
    ) -> tuple[CustomerProfile, int]:
        document = await self._repository.create(
            owner_id=owner_id,
            subject_id=profile.customer_id,
            memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
            data=profile.model_dump(mode="json"),
            source=MemoryUpdateSource.USER_EXPLICIT,
            reason="create insurance customer profile",
            thread_id=thread_id,
        )
        return self._profile(document), document.version

    async def get(self, *, owner_id: str, customer_id: str) -> tuple[CustomerProfile, int] | None:
        document = await self._repository.get(
            owner_id=owner_id,
            subject_id=customer_id,
            memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
        )
        return (self._profile(document), document.version) if document else None

    async def update(
        self,
        *,
        owner_id: str,
        customer_id: str,
        expected_version: int,
        patch: dict[str, Any],
        source: MemoryUpdateSource,
        reason: str = "",
        thread_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[CustomerProfile | None, int | None, SubjectMemoryCandidate | None]:
        current = await self._repository.get(
            owner_id=owner_id,
            subject_id=customer_id,
            memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
        )
        if current is None:
            raise KeyError(f"customer profile not found: {customer_id}")

        effective_patch = self._patch_with_evidence(patch, source=source, reason=reason)
        candidate_data = apply_json_pointer_patch(current.data, effective_patch)
        validated = CustomerProfile.model_validate(candidate_data)
        if validated.customer_id != customer_id:
            raise ValueError("customer_id is immutable")

        if source is MemoryUpdateSource.SYSTEM_DERIVED:
            raise ValueError("derived analysis belongs in task output, not customer facts")
        if source is MemoryUpdateSource.MODEL_INFERENCE:
            candidate = await self._repository.propose_candidate(
                owner_id=owner_id,
                subject_id=customer_id,
                memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
                base_version=expected_version,
                patch=effective_patch,
                source=source,
                reason=reason,
                thread_id=thread_id,
                task_id=task_id,
            )
            return None, None, candidate

        document = await self._repository.apply_patch(
            owner_id=owner_id,
            subject_id=customer_id,
            memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
            expected_version=expected_version,
            patch=effective_patch,
            source=source,
            reason=reason,
            thread_id=thread_id,
            task_id=task_id,
        )
        return self._profile(document), document.version, None

    async def list_candidates(self, *, owner_id: str, customer_id: str) -> list[SubjectMemoryCandidate]:
        return await self._repository.list_candidates(
            owner_id=owner_id,
            subject_id=customer_id,
            memory_type=CUSTOMER_PROFILE_MEMORY_TYPE,
        )

    async def accept_candidate(self, candidate_id: str, *, owner_id: str) -> tuple[CustomerProfile, int]:
        document = await self._repository.accept_candidate(candidate_id, owner_id=owner_id)
        return self._profile(document), document.version

    async def reject_candidate(self, candidate_id: str, *, owner_id: str) -> SubjectMemoryCandidate:
        return await self._repository.reject_candidate(candidate_id, owner_id=owner_id)
