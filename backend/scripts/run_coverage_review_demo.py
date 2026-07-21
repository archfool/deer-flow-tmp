"""运行一次无外部依赖的企业级保障检视演示。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from app.insurance.coverage_review import ReviewAction, ReviewDecision
from app.insurance.mock_data import build_complete_mock_profile
from app.insurance.service import InsuranceService
from deerflow.subject_memory import InMemorySubjectMemoryRepository
from deerflow.workflows import InMemoryTaskRepository, TaskInstance, TaskStatus

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """解析演示命令参数。

    Returns:
        包含触发场景和产物目录的参数命名空间。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trigger",
        action="append",
        default=None,
        help="触发场景编号，可重复传入；默认为 A2。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".deer-flow/coverage-review-demo"),
        help="对内 Markdown、对客 HTML 和审计清单的输出目录。",
    )
    return parser.parse_args()


def write_artifacts(task: TaskInstance, output_dir: Path) -> tuple[Path, ...]:
    """将已批准任务的报告和审计数据写入目录。

    Args:
        task: 已完成的保障检视任务。
        output_dir: 产物输出目录。

    Returns:
        已写入文件的路径元组。

    Raises:
        ValueError: 任务尚未完成或缺失报告产物时抛出。
    """

    if task.status is not TaskStatus.COMPLETED:
        raise ValueError(f"coverage review is not completed: {task.status.value}")
    final_output = task.steps["finalize"].output
    internal = final_output["internal_report"]
    customer = final_output["customer_report"]
    output_dir.mkdir(parents=True, exist_ok=True)
    files = (
        output_dir / "internal_report.md",
        output_dir / "customer_report.html",
        output_dir / "manifest.json",
        output_dir / "task_snapshot.json",
    )
    files[0].write_text(internal["markdown"], encoding="utf-8")
    files[1].write_text(customer["html"], encoding="utf-8")
    files[2].write_text(
        json.dumps(customer["manifest"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    files[3].write_text(
        json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return files


async def run_demo(trigger_ids: list[str], output_dir: Path) -> TaskInstance:
    """创建演示客户、执行任务、复核批准并输出产物。

    Args:
        trigger_ids: 本次检视的触发场景编号。
        output_dir: 产物输出目录。

    Returns:
        已完成的 DeerFlow 任务快照。

    Raises:
        RuntimeError: 工作流未在预期的复核闸门停止时抛出。
    """

    service = InsuranceService(
        task_repository=InMemoryTaskRepository(),
        memory_repository=InMemorySubjectMemoryRepository(),
    )
    owner_id = "coverage-review-demo-agent"
    profile, _version = await service.profiles.create(
        owner_id=owner_id,
        profile=build_complete_mock_profile(),
    )
    waiting = await service.start_coverage_review(
        owner_id=owner_id,
        customer_id=profile.customer_id,
        thread_id="coverage-review-demo-thread",
        trigger_ids=trigger_ids,
    )
    if waiting.status is not TaskStatus.WAITING_CONFIRMATION:
        raise RuntimeError(f"workflow did not reach agent review gate: {waiting.status.value}")
    completed = await service.decide_coverage_review(
        waiting.id,
        owner_id=owner_id,
        decision=ReviewDecision(
            action=ReviewAction.APPROVE,
            reviewer_id=owner_id,
            expected_review_revision=waiting.steps["agent-review"].output["review_packet"]["revision"],
            expected_kernel_hash=waiting.steps["freeze-kernel"].output["kernel"]["kernel_hash"],
            note="本地演示自动批准",
        ),
    )
    paths = write_artifacts(completed, output_dir)
    logger.info(
        "coverage review completed task_id=%s kernel_hash=%s output_dir=%s files=%s",
        completed.id,
        completed.steps["freeze-kernel"].output["kernel"]["kernel_hash"],
        output_dir.resolve(),
        [str(path) for path in paths],
    )
    return completed


def main() -> None:
    """配置日志并执行命令行演示。"""

    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run_demo(args.trigger or ["A2"], args.output_dir))


if __name__ == "__main__":
    main()
