"""保险客户档案更新策略的契约测试。

这些测试使用内存主体记忆 Repository，以便聚焦领域规则；SQL Repository 的
乐观锁和所有者隔离由 ``test_subject_memory_repository.py`` 单独覆盖。
"""

from __future__ import annotations

import pytest

from app.insurance.mock_data import build_complete_mock_profile
from app.insurance.profile import CustomerProfileService
from deerflow.subject_memory import InMemorySubjectMemoryRepository, MemoryUpdateSource


@pytest.mark.asyncio
async def test_explicit_fact_commits_but_model_inference_is_staged() -> None:
    service = CustomerProfileService(InMemorySubjectMemoryRepository())
    profile, version = await service.create(
        owner_id="agent-a",
        profile=build_complete_mock_profile(),
    )

    committed, version, candidate = await service.update(
        owner_id="agent-a",
        customer_id=profile.customer_id,
        expected_version=version,
        patch={"/financial/annual_income": "650000"},
        source=MemoryUpdateSource.USER_EXPLICIT,
        reason="代理人明确更正家庭年收入",
    )
    assert committed is not None
    assert committed.financial.annual_income == 650000
    assert committed.field_evidence["/financial/annual_income"].source.value == "user_explicit"
    assert candidate is None

    inferred_profile, inferred_version, candidate = await service.update(
        owner_id="agent-a",
        customer_id=profile.customer_id,
        expected_version=version,
        patch={"/members/0/health_summary": "模型推断：可能存在慢性病"},
        source=MemoryUpdateSource.MODEL_INFERENCE,
        reason="对话措辞产生的推断",
    )
    assert inferred_profile is None
    assert inferred_version is None
    assert candidate is not None

    unchanged = await service.get(owner_id="agent-a", customer_id=profile.customer_id)
    assert unchanged is not None
    assert unchanged[0].members[0].health_summary is None

    accepted, accepted_version = await service.accept_candidate(candidate.id, owner_id="agent-a")
    assert accepted.members[0].health_summary == "模型推断：可能存在慢性病"
    assert accepted.field_evidence["/members/0/health_summary"].source.value == "model_inference_confirmed"
    assert accepted_version == version + 1


@pytest.mark.asyncio
async def test_profile_service_rejects_derived_results_and_cross_owner_reads() -> None:
    service = CustomerProfileService(InMemorySubjectMemoryRepository())
    profile, version = await service.create(
        owner_id="agent-a",
        profile=build_complete_mock_profile(),
    )

    assert await service.get(owner_id="agent-b", customer_id=profile.customer_id) is None
    with pytest.raises(ValueError, match="derived analysis"):
        await service.update(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            expected_version=version,
            patch={"/household_name": "不应写入的派生结果"},
            source=MemoryUpdateSource.SYSTEM_DERIVED,
        )


@pytest.mark.asyncio
async def test_customer_id_is_immutable_even_through_root_patch() -> None:
    service = CustomerProfileService(InMemorySubjectMemoryRepository())
    profile, version = await service.create(
        owner_id="agent-a",
        profile=build_complete_mock_profile(),
    )

    with pytest.raises(ValueError, match="customer_id is immutable"):
        await service.update(
            owner_id="agent-a",
            customer_id=profile.customer_id,
            expected_version=version,
            patch={"/customer_id": "another-customer"},
            source=MemoryUpdateSource.USER_EXPLICIT,
        )
