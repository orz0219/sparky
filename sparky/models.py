"""共享数据模型。

职责
    定义贯穿全流程的结构化数据，以及**严格的**从 dict 构造逻辑。
    所有跨阶段传递的对象都在这里，避免各模块各造一套。

输入
    来自 LLM 的 dict（由各阶段模块校验后构造）

输出
    dataclass 实例；``to_dict()`` 可直接 JSON 序列化。

依赖
    仅标准库 ``dataclasses`` / ``datetime``。**不依赖任何其他 sparky 模块** ——
    它是所有模块的公共底座，反向依赖会造成循环导入。

失败模式
    字段缺失 / 类型不符 → 抛 ``SchemaError``，消息里带**字段路径与原始值**。
    **绝不填充默认值。**

    这一条是本模块最重要的纪律。举例：``Persona.bias`` 缺失时若默认填成
    "无明显偏见"，讨论就会退化成轮流念稿，而用户完全不知道产出质量被悄悄
    降低了。宁可报错让用户看到，也不要产出看似正常的废品。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import SchemaError

# ─────────────────────────── 校验工具 ───────────────────────────


def _require(d: Any, key: str, path: str) -> Any:
    """取出必填字段。缺失即报错，不返回 None。"""
    if not isinstance(d, dict):
        raise SchemaError(f"{path or '<根>'} 期望是对象，实际是 {type(d).__name__}：{d!r}")
    if key not in d:
        raise SchemaError(
            f"缺少字段 {path}{key}；实际拿到的键：{sorted(d)}"
        )
    return d[key]


def _str(d: Any, key: str, path: str) -> str:
    v = _require(d, key, path)
    if not isinstance(v, str):
        raise SchemaError(f"{path}{key} 期望是字符串，实际是 {type(v).__name__}：{v!r}")
    if not v.strip():
        raise SchemaError(f"{path}{key} 是空白字符串（这通常意味着模型敷衍了事）")
    return v


def _opt_str(d: Any, key: str, path: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise SchemaError(f"{path}{key} 期望是字符串或 null，实际是 {type(v).__name__}：{v!r}")
    return v


def _str_list(d: Any, key: str, path: str, *, min_len: int = 0) -> list[str]:
    v = _require(d, key, path)
    if not isinstance(v, list):
        raise SchemaError(f"{path}{key} 期望是数组，实际是 {type(v).__name__}：{v!r}")
    out: list[str] = []
    for i, item in enumerate(v):
        if not isinstance(item, str):
            raise SchemaError(f"{path}{key}[{i}] 期望是字符串，实际是 {type(item).__name__}")
        if item.strip():
            out.append(item.strip())
    if len(out) < min_len:
        raise SchemaError(
            f"{path}{key} 至少需要 {min_len} 条非空内容，实际只有 {len(out)} 条"
        )
    return out


def _opt_str_list(d: Any, key: str, path: str) -> list[str]:
    """可选字符串数组。字段缺失或为 null 时返回空列表。

    只对**明确可选**的字段使用 —— 例如 ``InfoScope.assigned_files``：
    它由代码按维度相关性分配，模型本来就不该产出它。
    """
    if key not in d or d[key] is None:
        return []
    return _str_list(d, key, path)


def _enum(d: Any, key: str, path: str, allowed: tuple[str, ...]) -> str:
    v = _str(d, key, path)
    if v not in allowed:
        raise SchemaError(f"{path}{key} 取值必须是 {allowed} 之一，实际是 {v!r}")
    return v


def now_iso() -> str:
    """当前本地时间，ISO 8601，精确到秒。全项目统一用它打时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ─────────────────────────── S0 立案 ───────────────────────────

SOURCE_USER = "user"
SOURCE_INFERRED = "inferred"


@dataclass
class SourcedText:
    """一条带来源标注的文本。

    ``source`` 区分「用户原话」与「讨论推断」，最终文档里后者会带 ``(推断)``
    标记 —— 让用户一眼看出哪些不是自己说的。
    """

    text: str
    source: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "SourcedText":
        return cls(
            text=_str(d, "text", path),
            source=_enum(d, "source", path, (SOURCE_USER, SOURCE_INFERRED)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source": self.source}


@dataclass
class Dimension:
    """设计维度：这个设计需要考虑的一个方面。"""

    id: str
    name: str
    why: str
    source: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "Dimension":
        return cls(
            id=_str(d, "id", path),
            name=_str(d, "name", path),
            why=_str(d, "why", path),
            source=_enum(d, "source", path, (SOURCE_USER, SOURCE_INFERRED)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "why": self.why, "source": self.source}


@dataclass
class Brief:
    """S0 产物：结构化后的问题陈述。"""

    raw_input: str
    goal: str
    constraints: list[SourcedText]
    non_goals: list[SourcedText]
    unknowns: list[str]
    dimensions: list[Dimension]
    uncertainty: str

    @classmethod
    def from_llm(cls, raw_input: str, d: Any) -> "Brief":
        return cls(
            raw_input=raw_input,
            goal=_str(d, "goal", ""),
            constraints=[SourcedText.from_dict(x, f"constraints[{i}].")
                         for i, x in enumerate(_require(d, "constraints", ""))],
            non_goals=[SourcedText.from_dict(x, f"non_goals[{i}].")
                       for i, x in enumerate(_require(d, "non_goals", ""))],
            unknowns=_str_list(d, "unknowns", "", min_len=1),
            dimensions=[Dimension.from_dict(x, f"dimensions[{i}].")
                        for i, x in enumerate(_require(d, "dimensions", ""))],
            uncertainty=_enum(d, "uncertainty", "", ("low", "medium", "high")),
        )

    @property
    def inferred_items(self) -> list[str]:
        """所有标为 inferred 的条目文本。合规校验第 9 项要用它。"""
        return [x.text for x in [*self.constraints, *self.non_goals] if x.source == SOURCE_INFERRED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_input": self.raw_input,
            "goal": self.goal,
            "constraints": [x.to_dict() for x in self.constraints],
            "non_goals": [x.to_dict() for x in self.non_goals],
            "unknowns": self.unknowns,
            "dimensions": [x.to_dict() for x in self.dimensions],
            "uncertainty": self.uncertainty,
        }


# ─────────────────────────── S1 组阁 ───────────────────────────

ROLE_SPECIALIST = "specialist"
ROLE_GENERALIST = "generalist"
ROLE_ADVERSARY = "adversary"


@dataclass
class InfoScope:
    """分配给某个人设的**独立**信息面。

    这是信息面隔离（ADR 0004）的载体：两个人设的 ``questions`` 不得重复，
    否则讨论会退化成对同一份输入的重复采样。
    """

    questions: list[str]
    search_queries: list[str]
    #: 由 recon.py 按维度关键词从 --context-dir 分配而来，**不由模型产出**，
    #: 因此解析时按可选字段处理。
    assigned_files: list[str] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "InfoScope":
        budget_raw = d.get("budget") or {}
        if not isinstance(budget_raw, dict):
            raise SchemaError(f"{path}budget 期望是对象，实际是 {type(budget_raw).__name__}")
        budget: dict[str, int] = {}
        for k, v in budget_raw.items():
            if not isinstance(v, int):
                raise SchemaError(f"{path}budget.{k} 期望是整数，实际是 {type(v).__name__}")
            budget[str(k)] = v
        return cls(
            questions=_str_list(d, "questions", path, min_len=1),
            search_queries=_str_list(d, "search_queries", path),
            assigned_files=_opt_str_list(d, "assigned_files", path),
            budget=budget,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "questions": self.questions,
            "search_queries": self.search_queries,
            "assigned_files": self.assigned_files,
            "budget": self.budget,
        }


@dataclass
class Persona:
    """一个有名字、有立场、有偏见、有盲点的虚拟专家。

    ``bias`` 与 ``blind_spot`` 是**必填**的：它们是讨论张力的燃料。
    缺了它们，讨论会退化成轮流念稿（见 docs/personas.md §4）。
    """

    id: str
    name: str
    title: str
    role_type: str
    stance: str
    bias: str
    blind_spot: str | None
    cares_about: list[str]
    dimensions: list[str]
    info_scope: InfoScope

    @property
    def is_adversary(self) -> bool:
        return self.role_type == ROLE_ADVERSARY

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "Persona":
        blind = _opt_str(d, "blind_spot", path)
        role = _enum(d, "role_type", path, (ROLE_SPECIALIST, ROLE_GENERALIST, ROLE_ADVERSARY))
        if role != ROLE_ADVERSARY and not blind:
            # 非反方必须有盲点：没有盲点的人设不会被攻击，讨论就没有张力
            raise SchemaError(
                f"{path}blind_spot 缺失。非 adversary 人设必须有盲点 —— "
                f"没有盲点的人设不会成为被攻击的对象，讨论会退化成轮流念稿"
            )
        return cls(
            id=_str(d, "id", path),
            name=_str(d, "name", path),
            title=_str(d, "title", path),
            role_type=role,
            stance=_str(d, "stance", path),
            bias=_str(d, "bias", path),
            blind_spot=blind,
            cares_about=_str_list(d, "cares_about", path, min_len=1),
            dimensions=_str_list(d, "dimensions", path, min_len=1),
            info_scope=InfoScope.from_dict(_require(d, "info_scope", path), f"{path}info_scope."),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "role_type": self.role_type,
            "stance": self.stance,
            "bias": self.bias,
            "blind_spot": self.blind_spot,
            "cares_about": self.cares_about,
            "dimensions": self.dimensions,
            "info_scope": self.info_scope.to_dict(),
        }


@dataclass
class Panel:
    """S1 产物：本次会议的全部人设。"""

    personas: list[Persona]
    panel_size_reasoning: str
    auto_added_adversary: bool = False

    def by_id(self, persona_id: str) -> Persona:
        for p in self.personas:
            if p.id == persona_id:
                return p
        raise SchemaError(f"人设 id 不存在：{persona_id}；已知：{[p.id for p in self.personas]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "panel_size": len(self.personas),
            "panel_size_reasoning": self.panel_size_reasoning,
            "auto_added_adversary": self.auto_added_adversary,
            "personas": [p.to_dict() for p in self.personas],
        }


# ─────────────────────────── S2 调研 ───────────────────────────


@dataclass
class Evidence:
    """一条论据。``unverified`` 为真时最终文档里会带 ⚠️ 未验证 标记。"""

    claim: str
    source_url: str | None
    confidence: str
    unverified: bool

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "Evidence":
        url = _opt_str(d, "source_url", path)
        raw_unverified = _require(d, "unverified", path)
        if not isinstance(raw_unverified, bool):
            raise SchemaError(
                f"{path}unverified 期望是布尔值，实际是 {type(raw_unverified).__name__}"
            )
        return cls(
            claim=_str(d, "claim", path),
            source_url=url,
            confidence=_enum(d, "confidence", path, ("high", "medium", "low")),
            unverified=raw_unverified,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "unverified": self.unverified,
        }


@dataclass
class StanceMemo:
    """S2 产物：一个人的立场备忘。"""

    persona_id: str
    core_claim: str
    pros: list[str]
    cons: list[str]
    reasoning: str
    evidence: list[Evidence]
    would_concede_if: str
    searched: bool
    search_note: str

    @classmethod
    def from_llm(cls, persona_id: str, d: Any, *, searched: bool, search_note: str) -> "StanceMemo":
        return cls(
            persona_id=persona_id,
            core_claim=_str(d, "core_claim", ""),
            pros=_str_list(d, "pros", "", min_len=1),
            # cons 至少 2 条：找不到缺点的方案不存在，只存在没想透的分析
            cons=_str_list(d, "cons", "", min_len=2),
            reasoning=_str(d, "reasoning", ""),
            evidence=[Evidence.from_dict(x, f"evidence[{i}].")
                      for i, x in enumerate(_require(d, "evidence", ""))],
            would_concede_if=_str(d, "would_concede_if", ""),
            searched=searched,
            search_note=search_note,
        )

    @property
    def unverified_claims(self) -> list[str]:
        return [e.claim for e in self.evidence if e.unverified]

    def to_dict(self) -> dict[str, Any]:
        return {
            "persona_id": self.persona_id,
            "core_claim": self.core_claim,
            "pros": self.pros,
            "cons": self.cons,
            "reasoning": self.reasoning,
            "evidence": [e.to_dict() for e in self.evidence],
            "would_concede_if": self.would_concede_if,
            "searched": self.searched,
            "search_note": self.search_note,
        }


@dataclass
class ReconOutcome:
    """S2 的完整结果，含失败者。

    失败者**不隐藏** —— 最终文档的附录 A 要标注谁缺席了。
    """

    memos: list[StanceMemo] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)  # (persona_id, 原因)

    def memo_for(self, persona_id: str) -> StanceMemo | None:
        for m in self.memos:
            if m.persona_id == persona_id:
                return m
        return None


# ─────────────────────────── 成本统计 ───────────────────────────


@dataclass
class Usage:
    """累计用量。各阶段把自己的调用用量加进来，供附录 D 展示。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


__all__ = [
    "SOURCE_USER",
    "SOURCE_INFERRED",
    "ROLE_SPECIALIST",
    "ROLE_GENERALIST",
    "ROLE_ADVERSARY",
    "SourcedText",
    "Dimension",
    "Brief",
    "InfoScope",
    "Persona",
    "Panel",
    "Evidence",
    "StanceMemo",
    "ReconOutcome",
    "Usage",
    "now_iso",
]
