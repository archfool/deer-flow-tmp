"""保险书籍 Knowledge Skill 的登记、最小路由与受控加载。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import model_validator

from app.insurance.coverage_review.hashing import sha256_text
from app.insurance.coverage_review.models import FrozenModel

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / "knowledge" / "skill_registry.yaml"
REQUIRED_PROHIBITED_USES = (
    "CUSTOMER_FACT",
    "CALCULATION_RULE",
    "POLICY_CLAUSE",
    "LEGAL_OR_COMPLIANCE_SOURCE",
)


class KnowledgeSkillRule(FrozenModel):
    """单个书籍 Skill 的权限、节点和主题契约。"""

    skill_id: str
    version: str
    allowed_nodes: tuple[str, ...]
    topics: tuple[str, ...]
    authority: str = "COMMUNICATION_KNOWLEDGE"
    prohibited_uses: tuple[str, ...] = REQUIRED_PROHIBITED_USES


class KnowledgeSkillRegistry(FrozenModel):
    """保障检视可用的 Knowledge Skill 白名单。"""

    version: str
    skills: tuple[KnowledgeSkillRule, ...]

    @model_validator(mode="after")
    def validate_authority_boundary(self) -> KnowledgeSkillRegistry:
        """校验 Skill 唯一性和沟通知识权限边界。

        Returns:
            已校验的注册表。

        Raises:
            ValueError: Skill 重复、权限过高或禁用项不完整时抛出。
        """

        ids = [item.skill_id for item in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge skill ids must be unique")
        for item in self.skills:
            if item.authority != "COMMUNICATION_KNOWLEDGE":
                raise ValueError(f"knowledge skill has invalid authority: {item.skill_id}")
            if set(item.prohibited_uses) != set(REQUIRED_PROHIBITED_USES):
                raise ValueError(f"knowledge skill has incomplete prohibited uses: {item.skill_id}")
        return self

    def get(self, skill_id: str) -> KnowledgeSkillRule:
        """按 Skill ID 读取登记契约。

        Args:
            skill_id: 书籍 Skill 目录名。

        Returns:
            对应的 Skill 登记项。

        Raises:
            KeyError: Skill 未登记时抛出。
        """

        try:
            return next(item for item in self.skills if item.skill_id == skill_id)
        except StopIteration as exc:
            raise KeyError(f"unknown knowledge skill: {skill_id}") from exc


class KnowledgeExcerpt(FrozenModel):
    """为受约束模型节点加载的最小知识片段。"""

    skill_id: str
    version: str
    authority: str
    source_hash: str
    content: str
    truncated: bool
    prohibited_uses: tuple[str, ...]


@lru_cache(maxsize=1)
def load_default_knowledge_registry() -> KnowledgeSkillRegistry:
    """加载项目内的默认 Knowledge Skill 注册表。

    Returns:
        经过权限闭环校验的注册表。
    """

    return load_knowledge_registry(DEFAULT_REGISTRY_PATH)


def load_knowledge_registry(path: Path) -> KnowledgeSkillRegistry:
    """从 YAML 文件加载 Knowledge Skill 注册表。

    Args:
        path: 注册表 YAML 路径。

    Returns:
        经 Pydantic 校验的注册表。

    Raises:
        ValueError: YAML 顶层不是对象时抛出。
    """

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("knowledge registry must be a YAML object")
    return KnowledgeSkillRegistry.model_validate(payload)


def select_knowledge_skills(
    registry: KnowledgeSkillRegistry,
    *,
    node: str,
    topics: set[str],
    max_skills: int = 5,
) -> tuple[KnowledgeSkillRule, ...]:
    """按节点权限和主题交集选择最小 Skill 集。

    Args:
        registry: 已校验的 Skill 注册表。
        node: 当前 Executable Skill 节点。
        topics: 当前客户路线和触发对应的主题。
        max_skills: 单节点允许进入上下文的最大 Skill 数。

    Returns:
        按主题匹配度和注册顺序排列的 Skill 元组。
    """

    candidates = [(index, item, len(topics.intersection(item.topics))) for index, item in enumerate(registry.skills) if node in item.allowed_nodes and topics.intersection(item.topics)]
    candidates.sort(key=lambda entry: (-entry[2], entry[0]))
    return tuple(item for _index, item, _score in candidates[:max_skills])


def load_knowledge_excerpt(
    registry: KnowledgeSkillRegistry,
    *,
    skill_id: str,
    skills_root: Path,
    max_chars: int = 6000,
) -> KnowledgeExcerpt:
    """从注入的 Skills Root 安全加载单个 SKILL.md 最小片段。

    Args:
        registry: 已校验的 Skill 注册表。
        skill_id: 待加载的 Skill ID。
        skills_root: 由部署层注入的书籍 Skills 根目录。
        max_chars: 最大加载字符数。

    Returns:
        带来源哈希、权限和截断标记的知识片段。

    Raises:
        ValueError: 长度越界或路径越出 Skills Root 时抛出。
    """

    if not 500 <= max_chars <= 20000:
        raise ValueError("knowledge excerpt max_chars must be between 500 and 20000")
    rule = registry.get(skill_id)
    root = skills_root.resolve()
    path = (root / skill_id / "SKILL.md").resolve()
    if root not in path.parents:
        raise ValueError("knowledge skill path escapes skills root")
    content = path.read_text(encoding="utf-8")
    return KnowledgeExcerpt(
        skill_id=rule.skill_id,
        version=rule.version,
        authority=rule.authority,
        source_hash=sha256_text(content),
        content=content[:max_chars],
        truncated=len(content) > max_chars,
        prohibited_uses=rule.prohibited_uses,
    )
