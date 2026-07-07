"""保障检视结果与报告 Schema。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CoverageDimension(StrEnum):
    LIFE = "life"
    CRITICAL_ILLNESS = "critical_illness"
    MEDICAL = "medical"
    ACCIDENT = "accident"
    RETIREMENT = "retirement"
    EMERGENCY_RESERVE = "emergency_reserve"


class DimensionStatus(StrEnum):
    COMPLETE = "complete"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


class ScenarioAssessment(BaseModel):
    target: Decimal = Decimal("0")
    current: Decimal = Decimal("0")
    gap: Decimal = Decimal("0")
    surplus: Decimal = Decimal("0")
    target_responsibilities: tuple[str, ...] = ()
    current_responsibilities: tuple[str, ...] = ()
    missing_responsibilities: tuple[str, ...] = ()
    explanation: str
    assumptions: tuple[str, ...] = ()


class DimensionAssessment(BaseModel):
    dimension: CoverageDimension
    status: DimensionStatus
    scenarios: dict[str, ScenarioAssessment] = Field(default_factory=dict)
    missing_facts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class CoverageReviewResult(BaseModel):
    customer_id: str
    household_name: str
    as_of_date: date
    profile_version: int | None = None
    parameter_version: str
    parameter_status: str
    dimensions: dict[CoverageDimension, DimensionAssessment]
    confidence_notes: tuple[str, ...] = ()
    draft_parameter_notes: tuple[str, ...] = ()


class CustomerCoverageReport(BaseModel):
    """客户可见 Schema，刻意不包含任何内部诊断字段。"""

    customer_id: str
    report_type: str = "customer"
    markdown: str
    structured_dimensions: dict[str, dict[str, Any]]
    disclaimer: str


class InternalCoverageReport(BaseModel):
    """仅供代理人使用的 Schema；API 不得将其映射为客户输出。"""

    customer_id: str
    report_type: str = "internal"
    markdown: str
    confidence_notes: tuple[str, ...]
    missing_facts: tuple[str, ...]
    draft_parameters: tuple[str, ...]
