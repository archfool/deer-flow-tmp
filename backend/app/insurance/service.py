"""协调客户档案、Adapter 与工作流任务的应用服务。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.insurance.adapters import CustomerDataAdapter, MockCustomerDataAdapter
from app.insurance.coverage_review import (
    COVERAGE_REVIEW_LANGUAGE_STEP_IDS,
    COVERAGE_REVIEW_TASK_NAME,
    COVERAGE_REVIEW_TASK_VERSION,
    CoverageReviewTools,
    KnowledgeSkillRegistry,
    ReviewAction,
    ReviewDecision,
    RuleBundle,
    build_coverage_review_skill_registry,
    build_coverage_review_task_definition,
    load_default_knowledge_registry,
    load_default_rule_bundle,
)
from app.insurance.coverage_review.hashing import sha256_digest
from app.insurance.coverage_review.narrative import CoverageReviewNarrativeHarness, RuleBoundNarrativeHarness
from app.insurance.coverage_review.reports import CUSTOMER_REPORT_TEMPLATE_VERSION
from app.insurance.coverage_review.workflow import SKILL_VERSION
from app.insurance.intake import CoverageReviewIntake, build_customer_profile_from_intake
from app.insurance.mock_data import build_registered_mock_profile
from app.insurance.profile import CustomerProfileService
from deerflow.subject_memory import MemoryUpdateSource, SubjectMemoryRepository
from deerflow.workflows import (
    StepStatus,
    TaskInstance,
    TaskRepository,
    TaskStatus,
    WorkflowEngine,
    WorkflowStateError,
)

_ADVANCE_TIME_BUDGET_SECONDS = 25.0


def _retryable_generation_failure_steps(
    task: TaskInstance,
) -> set[str]:
    """识别只由语言节点失败造成的可恢复任务。

    Args:
        task: 当前持久化保障检视任务。

    Returns:
        可重试的失败语言步骤；包含其他失败节点时返回空集合。
    """

    failed = {step_id for step_id, state in task.steps.items() if state.status is StepStatus.FAILED}
    if not failed or not failed.issubset(COVERAGE_REVIEW_LANGUAGE_STEP_IDS):
        return set()
    return failed


def _can_regenerate_customer_report(task: TaskInstance) -> bool:
    """识别可通过重建对客文案恢复的报告校验失败。

    Args:
        task: 当前持久化保障检视任务。

    Returns:
        仅对客报告因可修复的语言边界或推导完整性失败时返回 ``True``。
    """

    failed = {step_id: state for step_id, state in task.steps.items() if state.status is StepStatus.FAILED}
    if set(failed) != {"customer-report"}:
        return False
    error = failed["customer-report"].error or ""
    return any(
        marker in error
        for marker in (
            "customer report contains forbidden pattern:",
            "customer calculation explanation misses derivation component:",
        )
    )


class InsuranceService:
    """供 HTTP Route 和模型可见 Tool 使用的窄接口。

    所有者解析保留在本类之外，使授权边界清晰且易于测试：每个方法都要求传入
    已认证的所有者 ID，并把它同时传递给客户档案与任务 Repository。
    """

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        memory_repository: SubjectMemoryRepository,
        data_adapter: CustomerDataAdapter | None = None,
        coverage_review_tools: CoverageReviewTools | None = None,
        coverage_review_rules: RuleBundle | None = None,
        coverage_review_knowledge: KnowledgeSkillRegistry | None = None,
        coverage_review_narrative: CoverageReviewNarrativeHarness | None = None,
        coverage_review_knowledge_root: Path | None = None,
    ) -> None:
        """初始化保险领域服务。

        Args:
            task_repository: DeerFlow 持久化任务 Repository。
            memory_repository: 客户档案主体记忆 Repository。
            data_adapter: 可选的权威客户数据 Adapter。
            coverage_review_tools: 可注入的保障检视 Tool 集。
            coverage_review_rules: 可注入的签名规则包。
            coverage_review_knowledge: 可注入的 Knowledge Skill 注册表。
            coverage_review_narrative: 可注入的受约束语言 Harness。
            coverage_review_knowledge_root: 书籍 Knowledge Skill 根目录。
        """

        self.profiles = CustomerProfileService(memory_repository)
        self._task_repository = task_repository
        self._data_adapter = data_adapter or MockCustomerDataAdapter()
        self._coverage_review_tools = coverage_review_tools
        self._coverage_review_rules = coverage_review_rules
        self._coverage_review_knowledge = coverage_review_knowledge
        self._coverage_review_narrative = coverage_review_narrative or RuleBoundNarrativeHarness()
        self._coverage_review_knowledge_root = coverage_review_knowledge_root
        self._task_locks: dict[str, asyncio.Lock] = {}

    def _runtime_context(self) -> dict[str, Any]:
        """构造保障检视任务必须固定的运行上下文。

        Returns:
            包含工作流、规则、知识、模型、提示词和模板版本的签名上下文。
        """

        bundle = self._coverage_review_rules or load_default_rule_bundle()
        knowledge = self._coverage_review_knowledge or load_default_knowledge_registry()
        narrative = dict(self._coverage_review_narrative.runtime_metadata)
        payload = {
            "schema_version": "coverage-review-runtime-context-v1",
            "workflow_version": COVERAGE_REVIEW_TASK_VERSION,
            "skill_version": SKILL_VERSION,
            "rule_bundle_version": bundle.manifest_version,
            "rule_bundle_hash": bundle.bundle_hash,
            "knowledge_registry_version": knowledge.version,
            "knowledge_registry_hash": sha256_digest(knowledge),
            "model_name": narrative["model_name"],
            "provider_model": narrative["provider_model"],
            "narrative_harness_version": narrative["harness_version"],
            "prompt_versions": narrative["prompt_versions"],
            "structured_output_mode": narrative["structured_output_mode"],
            "customer_template_version": CUSTOMER_REPORT_TEMPLATE_VERSION,
        }
        return {**payload, "context_hash": sha256_digest(payload)}

    def _assert_runtime_context(self, task: TaskInstance) -> None:
        """阻止任务在规则、模型或提示词被静默替换后继续运行。

        Args:
            task: 当前持久化保障检视任务。

        Raises:
            WorkflowStateError: 上下文签名损坏或与当前运行环境不兼容时抛出。
        """

        stored = task.input_data.get("runtime_context")
        if stored is None:
            return
        if not isinstance(stored, dict):
            raise WorkflowStateError("coverage review runtime context is invalid")
        unsigned = dict(stored)
        claimed_hash = str(unsigned.pop("context_hash", ""))
        actual_hash = sha256_digest(unsigned)
        if not claimed_hash or claimed_hash != actual_hash:
            raise WorkflowStateError("coverage review runtime context hash mismatch")
        if stored.get("legacy_unpinned"):
            return
        active = self._runtime_context()
        if stored != active:
            changed = sorted(key for key in set(stored).union(active) if stored.get(key) != active.get(key))
            raise WorkflowStateError("coverage review runtime context changed: " + ", ".join(changed))

    def _engine(self) -> WorkflowEngine:
        """构造绑定当前 Tool 与规则包的无状态 WorkflowEngine。"""

        return WorkflowEngine(
            definitions=[build_coverage_review_task_definition()],
            skills=build_coverage_review_skill_registry(
                tools=self._coverage_review_tools,
                bundle=self._coverage_review_rules,
                knowledge_registry=self._coverage_review_knowledge,
                narrative_harness=self._coverage_review_narrative,
                knowledge_skills_root=self._coverage_review_knowledge_root,
            ),
            repository=self._task_repository,
        )

    async def _advance_workflow(
        self,
        engine: WorkflowEngine,
        task_id: str,
        *,
        owner_id: str,
        input_patch: dict[str, Any] | None = None,
        confirmations: dict[str, bool] | None = None,
    ) -> TaskInstance:
        """串行、限时并可恢复地推进一段保障检视工作流。

        Args:
            engine: 已绑定当前规则、工具和 Repository 的工作流引擎。
            task_id: 待推进的保障检视任务 ID。
            owner_id: 已认证的任务所有者 ID。
            input_patch: 本轮新增或修订的任务输入。
            confirmations: 代理人对等待确认步骤的决定。

        Returns:
            当前时间预算内推进后的最新任务实例。

        Raises:
            KeyError: 任务不存在或不属于当前所有者时抛出。
        """

        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            current = await engine.get(task_id, owner_id=owner_id)
            if current is None:
                raise KeyError(f"task not found: {task_id}")
            return current
        async with lock:
            current = await engine.get(task_id, owner_id=owner_id)
            if current is None:
                raise KeyError(f"task not found: {task_id}")
            self._assert_runtime_context(current)
            return await engine.advance(
                task_id,
                owner_id=owner_id,
                input_patch=input_patch,
                confirmations=confirmations,
                time_budget_seconds=_ADVANCE_TIME_BUDGET_SECONDS,
                recover_interrupted=True,
            )

    async def start_coverage_review(
        self,
        *,
        owner_id: str,
        customer_id: str,
        thread_id: str | None = None,
        parent_task_id: str | None = None,
        trigger_ids: list[str] | None = None,
        answers: dict[str, Any] | None = None,
    ) -> TaskInstance:
        """启动一项新的企业级保障检视任务。

        Args:
            owner_id: 任务所有者 ID。
            customer_id: 客户档案 ID。
            thread_id: 可选的 DeerFlow 会话 ID。
            parent_task_id: 可选的父任务 ID。
            trigger_ids: 本次检视触发场景编号。
            answers: 已由代理人确认的精算字段补充值。

        Returns:
            推进到追问、复核或完成屏障的任务实例。
        """

        profile_and_version = await self.profiles.get(owner_id=owner_id, customer_id=customer_id)
        if profile_and_version is None:
            raise KeyError(f"customer profile not found: {customer_id}")
        profile, version = profile_and_version

        # 当前 Mock 不返回任何变更。真实 Adapter 应在生成不可变快照前，
        # 通过 CustomerProfileService 以 AUTHORITATIVE_API 来源提交转换后的 Patch。
        await self._data_adapter.fetch_profile_patch(owner_id=owner_id, customer_id=customer_id)
        engine = self._engine()
        task = await engine.start(
            task_name=COVERAGE_REVIEW_TASK_NAME,
            owner_id=owner_id,
            subject_id=customer_id,
            thread_id=thread_id,
            parent_task_id=parent_task_id,
            input_data={
                "profile": profile.model_dump(mode="json"),
                "profile_version": version,
                "trigger_ids": trigger_ids or [],
                "answers": answers or {},
                "coverage_review_revision": 1,
                "review_revision": 1,
                "runtime_context": self._runtime_context(),
            },
        )
        return await self._advance_workflow(engine, task.id, owner_id=owner_id)

    async def start_coverage_review_intake(
        self,
        *,
        owner_id: str,
        intake: CoverageReviewIntake,
    ) -> TaskInstance:
        """用自然语言入口的显式事实创建档案并启动检视。

        同名 Mock 客户已存在时只覆盖本轮明确提供的字段，未提及的历史档案
        保持不变。

        Args:
            owner_id: 已认证的代理人 ID。
            intake: 已通过身份闸门的结构化入口数据。

        Returns:
            推进到下一稳定屏障的保障检视任务。
        """

        incoming = build_customer_profile_from_intake(intake)
        registered_mock = build_registered_mock_profile(intake.customer_name)
        current = await self.profiles.get(
            owner_id=owner_id,
            customer_id=incoming.customer_id,
        )
        if registered_mock is not None and current is None:
            await self.profiles.create(
                owner_id=owner_id,
                profile=registered_mock,
                thread_id=intake.thread_id,
            )
        elif registered_mock is not None:
            _profile, version = current
            mock_data = registered_mock.model_dump(mode="json")
            await self.profiles.update(
                owner_id=owner_id,
                customer_id=incoming.customer_id,
                expected_version=version,
                patch={f"/{key}": value for key, value in mock_data.items() if key not in {"customer_id", "field_evidence"}},
                source=MemoryUpdateSource.AUTHORITATIVE_API,
                reason="registered local mock customer lookup",
                thread_id=intake.thread_id,
            )
        elif current is None:
            await self.profiles.create(
                owner_id=owner_id,
                profile=incoming,
                thread_id=intake.thread_id,
            )
        else:
            profile, version = current
            patch: dict[str, Any] = {
                "/household_name": incoming.household_name,
            }
            if intake.marital_status is not None:
                patch["/marital_status"] = intake.marital_status
            self_index = next(
                (index for index, member in enumerate(profile.members) if member.relationship.value == "self"),
                None,
            )
            if self_index is None:
                patch["/members"] = [item.model_dump(mode="json") for item in incoming.members]
            else:
                member = incoming.members[0]
                patch.update(
                    {
                        f"/members/{self_index}/name": member.name,
                        f"/members/{self_index}/age": member.age,
                        f"/members/{self_index}/gender": member.gender.value if member.gender else None,
                    }
                )
                if intake.occupation is not None:
                    patch[f"/members/{self_index}/occupation"] = intake.occupation
            if intake.annual_income_wan is not None:
                annual_income = intake.annual_income_wan * 10000
                patch["/financial/annual_income"] = str(annual_income)
                patch["/financial/primary_annual_income"] = str(annual_income)
            if intake.policies_complete:
                patch["/policies"] = [item.model_dump(mode="json") for item in incoming.policies or []]
            await self.profiles.update(
                owner_id=owner_id,
                customer_id=incoming.customer_id,
                expected_version=version,
                patch=patch,
                source=MemoryUpdateSource.USER_EXPLICIT,
                reason="coverage review structured intake",
                thread_id=intake.thread_id,
            )
        return await self.start_coverage_review(
            owner_id=owner_id,
            customer_id=incoming.customer_id,
            thread_id=intake.thread_id,
            trigger_ids=list(intake.trigger_ids),
            answers=intake.answers,
        )

    async def advance_task(
        self,
        task_id: str,
        *,
        owner_id: str,
        answers: dict[str, Any] | None = None,
        trigger_ids: list[str] | None = None,
    ) -> TaskInstance:
        """补充任务输入并推进至下一个稳定屏障。

        Args:
            task_id: 保障检视任务 ID。
            owner_id: 任务所有者 ID。
            answers: 步骤 1.4 的结构化回答。
            trigger_ids: 补充或更正后的触发编号。

        Returns:
            推进后的任务实例。
        """

        engine = self._engine()
        task = await engine.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        if task.status is TaskStatus.FAILED:
            retryable_steps = _retryable_generation_failure_steps(task)
            if retryable_steps:
                task = await engine.retry_failed_steps(
                    task_id,
                    owner_id=owner_id,
                    step_ids=retryable_steps,
                )
            elif _can_regenerate_customer_report(task):
                task = await engine.retry_failed_steps(
                    task_id,
                    owner_id=owner_id,
                    step_ids={"customer-report"},
                )
                task = await engine.invalidate_steps(
                    task_id,
                    owner_id=owner_id,
                    step_ids={"customer-copy"},
                )
            else:
                return task
        if task.subject_id is None:
            raise ValueError("coverage-review task has no bound customer")
        profile_and_version = await self.profiles.get(owner_id=owner_id, customer_id=task.subject_id)
        if profile_and_version is None:
            raise KeyError(f"customer profile not found: {task.subject_id}")
        profile, version = profile_and_version
        input_patch: dict[str, Any] = {
            "profile": profile.model_dump(mode="json"),
            "profile_version": version,
        }
        if answers:
            input_patch["answers"] = {
                **dict(task.input_data.get("answers", {})),
                **answers,
            }
        if trigger_ids is not None:
            input_patch["trigger_ids"] = trigger_ids
        return await self._advance_workflow(
            engine,
            task_id,
            owner_id=owner_id,
            input_patch=input_patch,
        )

    async def decide_coverage_review(
        self,
        task_id: str,
        *,
        owner_id: str,
        decision: ReviewDecision,
    ) -> TaskInstance:
        """处理代理人批准、修改或拒绝决定。

        Args:
            task_id: 当前保障检视任务 ID。
            owner_id: 任务所有者 ID。
            decision: 结构化复核决定。

        Returns:
            批准后完成的任务，或修改后重新推进到复核屏障的任务。

        Raises:
            ValueError: 复核的内核已过期，或修改缺少档案版本时抛出。
        """

        engine = self._engine()
        if decision.reviewer_id != owner_id:
            raise ValueError("reviewer identity must match the authenticated task owner")
        task = await engine.get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        current_review_revision = int(task.input_data.get("review_revision", 1))
        if decision.expected_review_revision != current_review_revision:
            raise ValueError("review decision is stale: expected review revision does not match current task")
        kernel = task.steps["freeze-kernel"].output.get("kernel", {})
        current_kernel_hash = kernel.get("kernel_hash")
        if decision.expected_kernel_hash != current_kernel_hash:
            raise ValueError("review decision is stale: expected kernel hash does not match current task")
        if decision.action is ReviewAction.APPROVE:
            knowledge_refs = task.steps["meeting-support"].output.get("meeting_plan", {}).get("knowledge_refs", [])
            approval_record = {
                "action": decision.action.value,
                "reviewer_id": decision.reviewer_id,
                "note": decision.note,
                "approved_at": datetime.now(UTC).isoformat(),
                "review_revision": current_review_revision,
                "kernel_revision": kernel.get("revision"),
                "kernel_hash": kernel.get("kernel_hash"),
                "evidence_hash": kernel.get("evidence_hash"),
                "knowledge_refs": knowledge_refs,
                "runtime_context_hash": task.input_data.get(
                    "runtime_context",
                    {},
                ).get("context_hash"),
            }
            return await self._advance_workflow(
                engine,
                task_id,
                owner_id=owner_id,
                input_patch={"approval_record": approval_record},
                confirmations={"agent-review": True},
            )
        if decision.action is ReviewAction.REJECT:
            return await self._advance_workflow(
                engine,
                task_id,
                owner_id=owner_id,
                input_patch={
                    "rejection_record": {
                        "action": decision.action.value,
                        "reviewer_id": decision.reviewer_id,
                        "note": decision.note,
                        "rejected_at": datetime.now(UTC).isoformat(),
                        "review_revision": current_review_revision,
                        "kernel_revision": kernel.get("revision"),
                        "kernel_hash": kernel.get("kernel_hash"),
                    }
                },
                confirmations={"agent-review": False},
            )

        next_kernel_revision = int(task.input_data.get("coverage_review_revision", 1)) + 1
        next_review_revision = current_review_revision + 1

        if decision.action is ReviewAction.REVISE_TRIGGERS:
            invalidated = await engine.invalidate_steps(
                task_id,
                owner_id=owner_id,
                step_ids={"resolve-triggers"},
                input_patch={
                    "trigger_ids": list(decision.trigger_ids),
                    "coverage_review_revision": next_kernel_revision,
                    "review_revision": next_review_revision,
                    "approval_record": None,
                },
            )
            return await self._advance_workflow(
                engine,
                invalidated.id,
                owner_id=owner_id,
            )

        if decision.action is ReviewAction.REVISE_NARRATIVE:
            invalidated = await engine.invalidate_steps(
                task_id,
                owner_id=owner_id,
                step_ids={"customer-analysis", "verbalize-5-2", "verbalize-5-3"},
                input_patch={
                    "narrative_preferences": decision.narrative_preferences,
                    "review_revision": next_review_revision,
                    "approval_record": None,
                },
            )
            return await self._advance_workflow(
                engine,
                invalidated.id,
                owner_id=owner_id,
            )

        profile_patches = {key: value for key, value in decision.fact_patches.items() if key.startswith("/")}
        answer_patches = {key: value for key, value in decision.fact_patches.items() if not key.startswith("/")}
        existing_answers = dict(task.input_data.get("answers", {}))
        input_patch: dict[str, Any] = {
            "answers": {**existing_answers, **answer_patches},
            "coverage_review_revision": next_kernel_revision,
            "review_revision": next_review_revision,
            "approval_record": None,
        }
        seeds = {"evidence-gate"}
        if decision.trigger_ids:
            input_patch["trigger_ids"] = list(decision.trigger_ids)
            seeds.add("resolve-triggers")
        if profile_patches:
            if decision.expected_profile_version is None:
                raise ValueError("expected_profile_version is required for profile fact patches")
            if task.subject_id is None:
                raise ValueError("coverage-review task has no bound customer")
            profile, version, candidate = await self.profiles.update(
                owner_id=owner_id,
                customer_id=task.subject_id,
                expected_version=decision.expected_profile_version,
                patch=profile_patches,
                source=MemoryUpdateSource.USER_EXPLICIT,
                reason=decision.note,
                task_id=task_id,
            )
            if candidate is not None or profile is None or version is None:
                raise ValueError("explicit fact revision must be committed immediately")
            input_patch.update(
                {
                    "profile": profile.model_dump(mode="json"),
                    "profile_version": version,
                }
            )
            seeds.update({"collect-customer-center", "collect-customer-profile"})
        invalidated = await engine.invalidate_steps(
            task_id,
            owner_id=owner_id,
            step_ids=seeds,
            input_patch=input_patch,
        )
        return await self._advance_workflow(
            engine,
            invalidated.id,
            owner_id=owner_id,
        )

    async def apply_coverage_review_feedback(
        self,
        task_id: str,
        *,
        owner_id: str,
        action: ReviewAction,
        note: str,
        expected_review_revision: int,
        expected_kernel_hash: str,
        expected_profile_version: int | None = None,
    ) -> TaskInstance:
        """将代理人复核框中的自然语言意见转为结构化决策。

        Args:
            task_id: 当前保障检视任务 ID。
            owner_id: 已认证的任务所有者 ID。
            action: 代理人选择的复核动作。
            note: 代理人的自然语言意见。
            expected_review_revision: 代理人当前看到的报告修订号。
            expected_kernel_hash: 代理人当前看到的内核哈希。
            expected_profile_version: 事实修订时的客户档案版本。

        Returns:
            批准后的完成任务，或修订后重新进入复核闸门的任务。

        Raises:
            ValueError: 修订意见无法安全映射到白名单字段时抛出。
        """

        task = await self._engine().get(task_id, owner_id=owner_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")

        fact_patches: dict[str, Any] = {}
        trigger_ids: tuple[str, ...] = ()
        narrative_preferences: dict[str, Any] = {}
        effective_action = action
        if action is ReviewAction.REVISE_FACTS:
            extraction = await self._coverage_review_narrative.extract_review_patch(note, dict(task.input_data))
            if extraction.unresolved or (not extraction.fact_patches and not extraction.trigger_ids):
                unresolved = "；".join(extraction.unresolved) or note
                raise ValueError(f"未能安全识别需修改的事实，请写明字段、数值和单位：{unresolved}")
            fact_patches = extraction.fact_patches
            trigger_ids = extraction.trigger_ids
            if trigger_ids and not fact_patches:
                effective_action = ReviewAction.REVISE_TRIGGERS
        elif action is ReviewAction.REVISE_NARRATIVE:
            if not note.strip():
                raise ValueError("请输入需要调整的称呼、解释密度或话术方向")
            extraction = await self._coverage_review_narrative.extract_review_patch(note, dict(task.input_data))
            narrative_preferences = {
                **extraction.narrative_preferences,
                "agent_feedback": note.strip(),
            }

        decision = ReviewDecision(
            action=effective_action,
            reviewer_id=owner_id,
            expected_review_revision=expected_review_revision,
            expected_kernel_hash=expected_kernel_hash,
            expected_profile_version=expected_profile_version,
            note=note,
            fact_patches=fact_patches,
            trigger_ids=trigger_ids,
            narrative_preferences=narrative_preferences,
        )
        return await self.decide_coverage_review(task_id, owner_id=owner_id, decision=decision)

    async def get_task(self, task_id: str, *, owner_id: str) -> TaskInstance | None:
        return await self._engine().get(task_id, owner_id=owner_id)

    async def list_tasks(
        self,
        *,
        owner_id: str,
        customer_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[TaskInstance]:
        return await self._engine().list(owner_id=owner_id, subject_id=customer_id, thread_id=thread_id)

    async def suspend_task(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        return await self._engine().suspend(task_id, owner_id=owner_id, reason=reason)

    async def resume_task(self, task_id: str, *, owner_id: str) -> TaskInstance:
        return await self._engine().resume(task_id, owner_id=owner_id)

    async def cancel_task(self, task_id: str, *, owner_id: str, reason: str = "") -> TaskInstance:
        return await self._engine().cancel(task_id, owner_id=owner_id, reason=reason)

    async def update_profile_and_advance(
        self,
        *,
        owner_id: str,
        task_id: str,
        customer_id: str,
        expected_version: int,
        patch: dict[str, Any],
        inferred: bool,
        reason: str = "",
        thread_id: str | None = None,
    ) -> tuple[TaskInstance | None, Any]:
        source = MemoryUpdateSource.MODEL_INFERENCE if inferred else MemoryUpdateSource.USER_EXPLICIT
        profile, version, candidate = await self.profiles.update(
            owner_id=owner_id,
            customer_id=customer_id,
            expected_version=expected_version,
            patch=patch,
            source=source,
            reason=reason,
            thread_id=thread_id,
            task_id=task_id,
        )
        if candidate is not None:
            return None, candidate
        return await self.advance_task(task_id, owner_id=owner_id), {"profile": profile, "version": version}
