"""新增持久化工作流与版本化主体记忆数据表。

Revision ID: 0003_workflows_subject_memory
Revises: 0002_runs_token_usage
Create Date: 2026-07-07

这些表保持领域无关。保险客户档案由 ``app.insurance`` 序列化为一种主体记忆类型；
可发布的 Harness 不会反向导入任何保险模块。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_workflows_subject_memory"
down_revision: str | Sequence[str] | None = "0002_runs_token_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subject_memories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("memory_type", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "subject_id", "memory_type", name="uq_subject_memory_scope"),
    )
    op.create_index("ix_subject_memories_owner_id", "subject_memories", ["owner_id"], unique=False)
    op.create_index("ix_subject_memory_owner_subject", "subject_memories", ["owner_id", "subject_id"], unique=False)

    op.create_table(
        "subject_memory_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("memory_type", sa.String(length=128), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("patch_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subject_memory_revisions_owner_id", "subject_memory_revisions", ["owner_id"], unique=False)
    op.create_index(
        "ix_subject_memory_revision_scope",
        "subject_memory_revisions",
        ["owner_id", "subject_id", "memory_type", "to_version"],
        unique=False,
    )

    op.create_table(
        "subject_memory_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("memory_type", sa.String(length=128), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("patch_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subject_memory_candidates_owner_id", "subject_memory_candidates", ["owner_id"], unique=False)
    op.create_index(
        "ix_subject_memory_candidate_scope",
        "subject_memory_candidates",
        ["owner_id", "subject_id", "memory_type", "status"],
        unique=False,
    )

    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_name", sa.String(length=128), nullable=False),
        sa.Column("definition_version", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("parent_task_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("task_name", "owner_id", "subject_id", "thread_id", "parent_task_id", "status"):
        op.create_index(f"ix_workflow_tasks_{column}", "workflow_tasks", [column], unique=False)
    op.create_index("ix_workflow_task_owner_subject", "workflow_tasks", ["owner_id", "subject_id", "updated_at"], unique=False)
    op.create_index("ix_workflow_task_owner_thread", "workflow_tasks", ["owner_id", "thread_id", "updated_at"], unique=False)

    op.create_table(
        "workflow_task_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_revision", sa.Integer(), nullable=False),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_task_events_task_id", "workflow_task_events", ["task_id"], unique=False)
    op.create_index("ix_workflow_task_events_owner_id", "workflow_task_events", ["owner_id"], unique=False)
    op.create_index(
        "ix_workflow_task_event_task_revision",
        "workflow_task_events",
        ["task_id", "to_revision"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("workflow_task_events")
    op.drop_table("workflow_tasks")
    op.drop_table("subject_memory_candidates")
    op.drop_table("subject_memory_revisions")
    op.drop_table("subject_memories")
