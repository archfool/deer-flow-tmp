"""保障检视规则资产的编译、校验与只读加载。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from app.insurance.coverage_review.hashing import canonical_json, sha256_digest
from app.insurance.coverage_review.models import (
    DIMENSION_ORDER,
    ActionType,
    DimensionCode,
    FrozenModel,
    MeasurementType,
    TemperatureType,
)

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_DIR = PACKAGE_DIR / "rule_assets" / "source"
DEFAULT_BUNDLE_PATH = PACKAGE_DIR / "rule_assets" / "bundles" / "coverage_review.v2.1.json"

LEGACY_REASON_CODES = {
    "ACCIDENT_GAP",
    "EMERGENCY_LIQUIDITY_GAP",
    "RES_MISALLOC",
}


class RuleCompilerError(ValueError):
    """规则源无法形成闭合运行包时抛出的错误。"""


class DimensionRule(FrozenModel):
    """单个规范维度的注册信息。"""

    code: DimensionCode
    name: str
    group: str
    measurement_type: MeasurementType
    default_reason_code: str
    risk_exposure: int = Field(ge=0)


class ReasonRule(FrozenModel):
    """理由码在内核、5.2 与 5.3 之间的闭合映射。"""

    code: str
    category: str
    dimensions: tuple[DimensionCode, ...]
    framework_code: str
    explanation: str
    actions: tuple[ActionType, ...] = ()
    special_operation: str | None = None
    phase: str


class TriggerRule(FrozenModel):
    """触发场景、温度、重心维度和护栏。"""

    id: str
    category: str
    scene: str
    temperature: TemperatureType
    focus_dimensions: tuple[DimensionCode, ...]
    auxiliary_focus: bool = False
    tone: str
    scenario: str
    guardrails: tuple[str, ...] = ()


class FieldRule(FrozenModel):
    """步骤 1.4 的字段优先级与追问规则。"""

    key: str
    priority: str
    blocking: bool
    sensitive: bool
    reason: str
    prompt: str


class RuleBundle(FrozenModel):
    """运行时只读的签名规则包。"""

    manifest_version: str
    compiler_version: str
    dimensions: tuple[DimensionRule, ...]
    reasons: tuple[ReasonRule, ...]
    triggers: tuple[TriggerRule, ...]
    fields: tuple[FieldRule, ...]
    approved_defaults: dict[str, dict[str, Any]]
    diagnosis_rules: dict[str, Any]
    style_contracts: dict[str, Any]
    projection_assets: dict[str, Any]
    source_versions: dict[str, str]
    source_hashes: dict[str, str]
    reason_code_set_hash: str
    decision_coverage_hash: str
    golden_case_results: dict[str, str]
    bundle_hash: str

    @model_validator(mode="after")
    def validate_closure(self) -> RuleBundle:
        """执行八维、理由码、触发和辅助域闭合校验。

        Returns:
            校验通过的规则包。

        Raises:
            ValueError: 任一核心业务不变量不满足时抛出。
        """

        dimension_codes = tuple(item.code for item in self.dimensions)
        if dimension_codes != DIMENSION_ORDER:
            raise ValueError("dimension registry must contain eight canonical dimensions in fixed order")
        disability = next(item for item in self.dimensions if item.code is DimensionCode.DISABILITY)
        if disability.name != "伤残保障" or disability.default_reason_code != "DISABILITY_GAP":
            raise ValueError("D3 must use the disability semantic")

        reason_codes = [item.code for item in self.reasons]
        if len(reason_codes) != 20 or len(set(reason_codes)) != 20:
            raise ValueError("reason registry must contain 20 unique canonical codes")
        leaked = LEGACY_REASON_CODES.intersection(reason_codes)
        if leaked:
            raise ValueError(f"legacy reason codes leaked into runtime bundle: {sorted(leaked)}")

        known_reasons = set(reason_codes)
        for dimension in self.dimensions:
            if dimension.default_reason_code not in known_reasons:
                raise ValueError(f"dimension {dimension.code} references unknown reason code")

        auxiliary = next(
            (item for item in self.reasons if item.code == "EMERGENCY_LIQUIDITY_ALERT"),
            None,
        )
        if auxiliary is None or auxiliary.dimensions or auxiliary.actions:
            raise ValueError("emergency liquidity must stay outside dimensions and actions")

        trigger_ids = [item.id for item in self.triggers]
        if len(trigger_ids) != 29 or len(set(trigger_ids)) != 29 or "C6" not in trigger_ids:
            raise ValueError("trigger registry must contain 29 unique scenarios including C6")
        if not self.golden_case_results or any(result != "PASS" for result in self.golden_case_results.values()):
            raise ValueError("rule compiler golden cases must all pass")
        return self

    def dimension(self, code: DimensionCode) -> DimensionRule:
        """按规范维度码查询规则。

        Args:
            code: 八维规范码。

        Returns:
            对应的维度规则。

        Raises:
            KeyError: 规则包中不存在该维度时抛出。
        """

        try:
            return next(item for item in self.dimensions if item.code is code)
        except StopIteration as exc:
            raise KeyError(f"unknown dimension: {code}") from exc

    def reason(self, code: str) -> ReasonRule:
        """按规范理由码查询闭合规则。

        Args:
            code: 规范理由码。

        Returns:
            对应的理由规则。

        Raises:
            KeyError: 规则包中不存在该理由码时抛出。
        """

        try:
            return next(item for item in self.reasons if item.code == code)
        except StopIteration as exc:
            raise KeyError(f"unknown reason code: {code}") from exc

    def trigger(self, trigger_id: str) -> TriggerRule:
        """按编号查询触发场景。

        Args:
            trigger_id: 触发场景编号。

        Returns:
            对应的触发规则。

        Raises:
            KeyError: 规则包中不存在该触发时抛出。
        """

        try:
            return next(item for item in self.triggers if item.id == trigger_id)
        except StopIteration as exc:
            raise KeyError(f"unknown trigger: {trigger_id}") from exc

    def field(self, field_key: str) -> FieldRule:
        """按规范字段名查询追问规则。

        Args:
            field_key: 规范字段名。

        Returns:
            对应的字段规则。

        Raises:
            KeyError: 规则包中不存在该字段时抛出。
        """

        try:
            return next(item for item in self.fields if item.key == field_key)
        except StopIteration as exc:
            raise KeyError(f"unknown field: {field_key}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    """读取并校验单个 JSON 规则源。

    Args:
        path: 规则源文件路径。

    Returns:
        解析后的 JSON 对象。

    Raises:
        RuleCompilerError: 文件缺失、JSON 非法或顶层不是对象时抛出。
    """

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleCompilerError(f"cannot load rule source {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuleCompilerError(f"rule source must be a JSON object: {path.name}")
    return value


def compile_rule_bundle(source_dir: Path = DEFAULT_SOURCE_DIR) -> RuleBundle:
    """把 canonical JSON 源编译成闭合、可签名的运行规则包。

    Args:
        source_dir: canonical 规则源目录。

    Returns:
        已完成静态校验并带 SHA-256 签名的规则包。

    Raises:
        RuleCompilerError: 源文件或跨表映射不符合设计约束时抛出。
    """

    filenames = (
        "dimension_registry.json",
        "reason_registry.json",
        "trigger_registry.json",
        "field_registry.json",
        "diagnosis_rules.json",
        "style_contracts.json",
        "projection_assets.json",
    )
    sources = {name: _read_json(source_dir / name) for name in filenames}
    source_hashes = {name: sha256_digest(value) for name, value in sources.items()}
    source_versions = {name: str(value.get("version", "unversioned")) for name, value in sources.items()}

    try:
        dimensions = tuple(DimensionRule.model_validate(value) for value in sources["dimension_registry.json"]["dimensions"])
        reasons = tuple(ReasonRule.model_validate(value) for value in sources["reason_registry.json"]["reasons"])
        triggers = tuple(TriggerRule.model_validate(value) for value in sources["trigger_registry.json"]["triggers"])
        fields = tuple(FieldRule.model_validate(value) for value in sources["field_registry.json"]["fields"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuleCompilerError(f"rule source schema error: {exc}") from exc

    payload = {
        "manifest_version": "coverage-review-rules-v2.1",
        "compiler_version": "coverage-rule-compiler-v2.1",
        "dimensions": [item.model_dump(mode="json") for item in dimensions],
        "reasons": [item.model_dump(mode="json") for item in reasons],
        "triggers": [item.model_dump(mode="json") for item in triggers],
        "fields": [item.model_dump(mode="json") for item in fields],
        "approved_defaults": sources["field_registry.json"].get("approved_defaults", {}),
        "diagnosis_rules": sources["diagnosis_rules.json"],
        "style_contracts": sources["style_contracts.json"],
        "projection_assets": sources["projection_assets.json"],
        "source_versions": source_versions,
        "source_hashes": source_hashes,
        "reason_code_set_hash": sha256_digest(sorted(item.code for item in reasons)),
        "decision_coverage_hash": sha256_digest(
            {
                "reasons": sources["reason_registry.json"],
                "diagnosis": sources["diagnosis_rules.json"],
                "projections": sources["projection_assets.json"],
            }
        ),
        "golden_case_results": {
            "canonical_dimension_order": "PASS" if tuple(item.code for item in dimensions) == DIMENSION_ORDER else "FAIL",
            "canonical_reason_count": "PASS" if len(reasons) == 20 else "FAIL",
            "canonical_trigger_count": "PASS" if len(triggers) == 29 else "FAIL",
            "d3_disability_registry": "PASS" if any(item.code is DimensionCode.DISABILITY and item.name == "伤残保障" and item.default_reason_code == "DISABILITY_GAP" for item in dimensions) else "FAIL",
            "emergency_auxiliary_only": "PASS" if any(item.code == "EMERGENCY_LIQUIDITY_ALERT" and not item.dimensions and not item.actions for item in reasons) else "FAIL",
            "trigger_c6_present": "PASS" if any(item.id == "C6" for item in triggers) else "FAIL",
        },
    }
    bundle_hash = sha256_digest(payload)
    try:
        return RuleBundle.model_validate({**payload, "bundle_hash": bundle_hash})
    except ValueError as exc:
        raise RuleCompilerError(f"rule bundle closure check failed: {exc}") from exc


def write_compiled_bundle(
    bundle: RuleBundle,
    destination: Path = DEFAULT_BUNDLE_PATH,
) -> Path:
    """把签名规则包写入可提交的运行文件。

    Args:
        bundle: 已校验的规则包。
        destination: 目标文件路径。

    Returns:
        实际写入的目标路径。
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        canonical_json(bundle.model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    return destination


@lru_cache(maxsize=1)
def load_default_rule_bundle() -> RuleBundle:
    """校验并缓存已发布的默认规则包。

    Returns:
        当前进程共享的不可变规则包。
    """

    return load_compiled_rule_bundle(DEFAULT_BUNDLE_PATH)


def load_compiled_rule_bundle(path: Path) -> RuleBundle:
    """加载已发布规则包并校验内容哈希。

    Args:
        path: 编译后的规则 Bundle JSON 路径。

    Returns:
        通过哈希和领域闭环校验的规则包。

    Raises:
        RuleCompilerError: Bundle 缺失哈希、内容被篡改或 Schema 无效时抛出。
    """

    payload = _read_json(path)
    claimed_hash = str(payload.get("bundle_hash", ""))
    unsigned_payload = dict(payload)
    unsigned_payload.pop("bundle_hash", None)
    actual_hash = sha256_digest(unsigned_payload)
    if not claimed_hash or claimed_hash != actual_hash:
        raise RuleCompilerError(f"compiled rule bundle hash mismatch: expected {claimed_hash or 'missing'}, actual {actual_hash}")
    try:
        return RuleBundle.model_validate(payload)
    except ValueError as exc:
        raise RuleCompilerError(f"compiled rule bundle validation failed: {exc}") from exc
