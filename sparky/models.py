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


# ─────────────────────────── S3 交锋 ───────────────────────────

#: 分歧的生命周期状态。见 docs/consensus.md §3.2
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_TRADEOFF = "tradeoff"
STATUS_STALEMATE = "stalemate"

#: 一次回应的三种动作。**"不表态"不在其中** —— 不表态是弃权，由空 responds_to 表示。
ACTION_SUPPORT = "support"
ACTION_REBUT = "rebut"
ACTION_AMEND = "amend"

#: 关闭一项分歧的两种方式。
CONVINCED = "convinced"  # 我被对方说服了
CONCEDED = "conceded"  # 我放弃我的立场（未必被说服）


def _str_map(d: Any, key: str, path: str) -> dict[str, str]:
    """可选字符串→字符串映射。缺失或为 null 时返回空字典。"""
    raw = d.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchemaError(f"{path}{key} 期望是对象，实际是 {type(raw).__name__}")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            raise SchemaError(f"{path}{key}.{k} 期望是字符串，实际是 {type(v).__name__}")
        out[str(k)] = v.strip()
    return out


def _list_of(d: Any, key: str, path: str) -> list[Any]:
    """可选数组。缺失或为 null 时返回空列表。"""
    raw = d.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SchemaError(f"{path}{key} 期望是数组，实际是 {type(raw).__name__}")
    return raw


@dataclass
class Reply:
    """对某条分歧的一次明确回应。"""

    disagreement_id: str
    action: str
    reason: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "Reply":
        return cls(
            disagreement_id=_str(d, "disagreement_id", path),
            action=_enum(d, "action", path, (ACTION_SUPPORT, ACTION_REBUT, ACTION_AMEND)),
            reason=_str(d, "reason", path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disagreement_id": self.disagreement_id,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class CloseProposal:
    """关闭一项分歧。``by`` 区分"被说服"与"放弃"。"""

    disagreement_id: str
    by: str
    because: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "CloseProposal":
        return cls(
            disagreement_id=_str(d, "disagreement_id", path),
            by=_enum(d, "by", path, (CONVINCED, CONCEDED)),
            because=_str(d, "because", path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"disagreement_id": self.disagreement_id, "by": self.by, "because": self.because}


@dataclass
class OpenProposal:
    """提出一项新分歧。"""

    topic: str
    with_persona: str
    your_position: str
    their_position: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "OpenProposal":
        return cls(
            topic=_str(d, "topic", path),
            with_persona=_str(d, "with", path),
            your_position=_str(d, "your_position", path),
            their_position=_str(d, "their_position", path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "with": self.with_persona,
            "your_position": self.your_position,
            "their_position": self.their_position,
        }


@dataclass
class TradeoffProposal:
    """把一项分歧**降级为取舍**：不解决它，而是写明"什么条件下该反过来选"。

    这是本项目最看重的产出之一 —— 见 docs/consensus.md §7。
    """

    disagreement_id: str
    choose: str
    when_to_flip: str

    @classmethod
    def from_dict(cls, d: Any, path: str) -> "TradeoffProposal":
        return cls(
            disagreement_id=_str(d, "disagreement_id", path),
            choose=_str(d, "choose", path),
            when_to_flip=_str(d, "when_to_flip", path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disagreement_id": self.disagreement_id,
            "choose": self.choose,
            "when_to_flip": self.when_to_flip,
        }


@dataclass
class Turn:
    """讨论中的一次发言。

    **回应义务**：第 1 轮的 ``responds_to`` 允许为空（那时还没有分歧编号）；
    其余轮次里它为空即视为弃权，如实记账，**不伪造发言**。
    弃权的完整判据见 ``abstained``。
    """

    round: int
    persona_id: str
    #: 这个人这一轮**说出口的话**（模型自己写的自然语言，不是从结构化字段拼的）。
    #:
    #: 它必须放在 JSON schema 的**第一位**，原因有两个：
    #:   1. 界面靠它做打字机效果 —— 流式前缀里最先出现的就是它，
    #:      前端（其实是后端）能边收边抽，用户看到的是"人在说话"，
    #:      而不是一段裸 JSON 被逐字敲出来；
    #:   2. 它是发言的**人话版本**，终端与附录 C 直接用它，比拼
    #:      "amend x1、新开 1 项" 这种机器摘要可读得多。
    say: str
    responds_to: list[Reply]
    new_points: list[str]
    opens: list[OpenProposal]
    closes: list[CloseProposal]
    tradeoff_proposals: list[TradeoffProposal]
    stance_snapshot: dict[str, str]
    #: 弃权的原因（仅当本轮是"发言失败按弃权处理"时非空）。
    #: 它让附录 C 能如实写出"谁因为什么没发言"，而不是一个无来由的空白。
    abstain_reason: str | None = None

    @property
    def abstained(self) -> bool:
        """弃权：**什么都没说**。

        判据必须包含 ``new_points``，不能只看 ``responds_to`` 与 ``opens`` ——
        第 1 轮的 ``responds_to`` 本来就是空的（那时还没有分歧编号），
        而提示词又明确鼓励"确实没有实质冲突就别硬造"。
        只看前两者会把**一个正常陈述了立场、只是没发现冲突的人**判成弃权，
        进而计入连续弃权、被移出后续讨论 —— 那是在冤枉诚实行为。
        """
        return not self.responds_to and not self.opens and not self.new_points

    @classmethod
    def from_llm(cls, persona_id: str, round_no: int, d: Any) -> "Turn":
        return cls(
            round=round_no,
            persona_id=persona_id,
            say=_str(d, "say", ""),
            responds_to=[
                Reply.from_dict(x, f"responds_to[{i}].")
                for i, x in enumerate(_list_of(d, "responds_to", ""))
            ],
            new_points=_opt_str_list(d, "new_points", ""),
            opens=[
                OpenProposal.from_dict(x, f"opens[{i}].")
                for i, x in enumerate(_list_of(d, "opens", ""))
            ],
            closes=[
                CloseProposal.from_dict(x, f"closes[{i}].")
                for i, x in enumerate(_list_of(d, "closes", ""))
            ],
            tradeoff_proposals=[
                TradeoffProposal.from_dict(x, f"tradeoff_proposals[{i}].")
                for i, x in enumerate(_list_of(d, "tradeoff_proposals", ""))
            ],
            stance_snapshot=_str_map(d, "stance_snapshot", ""),
        )

    def stance_key(self) -> tuple[tuple[str, str], ...]:
        """立场快照的规范化形式，供停滞检测做比较。"""
        return tuple(sorted(self.stance_snapshot.items()))

    def reply_key(self) -> tuple[tuple[str, str, str], ...]:
        """本轮的（分歧 id、动作、理由）三元组，规范化后用于检出"原地打转"。

        理由只做空白归一化 —— **不**做语义比较。逐字比较会漏（模型每次措辞都变），
        语义比较又要额外调模型（违背"S4 不调 LLM"这条设计约束）。
        因此停滞检测另有轮级三元组兜底，见 docs/consensus.md §4.1。
        """
        return tuple(
            sorted(
                (r.disagreement_id, r.action, "".join(r.reason.split()))
                for r in self.responds_to
            )
        )

    def summary(self) -> str:
        """给人看的一句话 —— 就是这个人自己写的 ``say``。"""
        if self.abstained:
            reason = f"（{self.abstain_reason}）" if self.abstain_reason else ""
            return f"弃权，本轮没有发言 {reason}"
        return self.say

    def actions(self) -> str:
        """给机器看的结构化动作摘要，如 ``amend x1、新开 1 项、关闭 1 项``。

        与 ``summary()`` 分工明确：``summary`` 是**人话**（给讨论流与附录 C），
        ``actions`` 是**台账动作**（给人核对"这一轮到底动了哪些条目"）。
        """
        if self.abstained:
            return "弃权"
        parts = [f"{r.action} {r.disagreement_id}" for r in self.responds_to]
        if self.opens:
            parts.append(f"新开 {len(self.opens)} 项")
        if self.closes:
            parts.append(f"关闭 {len(self.closes)} 项")
        if self.tradeoff_proposals:
            parts.append(f"提议取舍 {len(self.tradeoff_proposals)} 项")
        return "、".join(parts) or "无动作"

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "persona_id": self.persona_id,
            "say": self.say,
            "actions": self.actions(),
            "responds_to": [r.to_dict() for r in self.responds_to],
            "new_points": self.new_points,
            "opens": [o.to_dict() for o in self.opens],
            "closes": [c.to_dict() for c in self.closes],
            "tradeoff_proposals": [t.to_dict() for t in self.tradeoff_proposals],
            "stance_snapshot": self.stance_snapshot,
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
        }


@dataclass
class Disagreement:
    """台账里的一条分歧。

    **只增不删**：状态可以变，条目永不删除 —— 最终文档的「候选方案对比」
    要靠已解决条目里的信息来写。见 docs/consensus.md §3.3。
    """

    id: str
    topic: str
    parties: list[str]
    status: str
    positions: dict[str, str]
    stakes: str
    history: list[dict[str, Any]] = field(default_factory=list)
    resolution: dict[str, Any] | None = None
    tradeoff: dict[str, Any] | None = None

    @property
    def is_settled(self) -> bool:
        return self.status in (STATUS_RESOLVED, STATUS_TRADEOFF)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "parties": self.parties,
            "status": self.status,
            "positions": self.positions,
            "stakes": self.stakes,
            "history": self.history,
            "resolution": self.resolution,
            "tradeoff": self.tradeoff,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Disagreement":
        """仅用于读回自己写出的台账快照（不解析 LLM 输出），因此校验从简。"""
        return cls(
            id=d["id"],
            topic=d["topic"],
            parties=list(d["parties"]),
            status=d["status"],
            positions=dict(d["positions"]),
            stakes=d["stakes"],
            history=list(d.get("history", [])),
            resolution=d.get("resolution"),
            tradeoff=d.get("tradeoff"),
        )


@dataclass
class Ledger:
    """分歧台账 —— 收敛判定的**唯一依据**。"""

    items: list[Disagreement] = field(default_factory=list)
    #: 已发出的分歧编号计数。单调递增，永不回收（回收会让历史记录指向错误的条目）。
    opened_total: int = 0
    #: 每项分歧被"定向加轮"针对过几次
    focus_counts: dict[str, int] = field(default_factory=dict)
    #: 模型提出、但超出分歧数上限因而**未被登记**的议题。
    #: 保留它们是为了**不丢信息** —— 它们会列在附录 C 里，
    #: 而不是被悄悄丢掉。见 debate.MAX_DISAGREEMENTS_PER_PERSONA。
    dropped_topics: list[str] = field(default_factory=list)

    @property
    def open_items(self) -> list[Disagreement]:
        return [d for d in self.items if d.status == STATUS_OPEN]

    @property
    def stalemates(self) -> list[Disagreement]:
        return [d for d in self.items if d.status == STATUS_STALEMATE]

    @property
    def tradeoffs(self) -> list[Disagreement]:
        return [d for d in self.items if d.status == STATUS_TRADEOFF]

    @property
    def resolved(self) -> list[Disagreement]:
        return [d for d in self.items if d.status == STATUS_RESOLVED]

    @property
    def unresolved(self) -> list[Disagreement]:
        """还没谈拢的（含 open 与 stalemate）。"""
        return [d for d in self.items if d.status in (STATUS_OPEN, STATUS_STALEMATE)]

    def by_id(self, disagreement_id: str) -> Disagreement | None:
        for item in self.items:
            if item.id == disagreement_id:
                return item
        return None

    def require(self, disagreement_id: str, *, context: str) -> Disagreement:
        item = self.by_id(disagreement_id)
        if item is None:
            raise SchemaError(
                f"{context}：引用了不存在的分歧 {disagreement_id!r}；"
                f"台账里现有：{[d.id for d in self.items]}"
            )
        return item

    def new_id(self) -> str:
        self.opened_total += 1
        return f"x{self.opened_total}"

    def digest(self) -> dict[str, int]:
        """各状态的条目数，供事件与附录使用。"""
        return {
            "total": len(self.items),
            "open": len(self.open_items),
            "resolved": len(self.resolved),
            "tradeoff": len(self.tradeoffs),
            "stalemate": len(self.stalemates),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "opened_total": self.opened_total,
            "focus_counts": self.focus_counts,
            "digest": self.digest(),
            "dropped_topics": self.dropped_topics,
            "items": [d.to_dict() for d in self.items],
        }


#: 讨论结果：台账 + 逐轮发言。
@dataclass
class DebateResult:
    ledger: Ledger
    rounds: list[list[Turn]] = field(default_factory=list)
    #: S4 对最后一轮给出的判定（done 或 force_stop）
    final_decision: Decision | None = None

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_count": self.round_count,
            "final_decision": self.final_decision.to_dict() if self.final_decision else None,
            "ledger": self.ledger.to_dict(),
            "rounds": [[t.to_dict() for t in turns] for turns in self.rounds],
        }


# ─────────────────────────── S4 收敛 ───────────────────────────

DECISION_DONE = "done"
DECISION_ANOTHER_ROUND = "another_round"
DECISION_FORCE_STOP = "force_stop"


@dataclass(frozen=True)
class Budget:
    """预算快照。**冻结**，因为它是纯函数的输入，不许在判定过程中变化。"""

    total_tokens: int
    limit: int | None

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.total_tokens >= self.limit


@dataclass(frozen=True)
class Decision:
    """收敛判定的结果。

    ``another_round`` 一定带 ``focus`` —— Sparky **从不**让全员再发一轮言，
    只召集分歧的当事双方。见 docs/consensus.md §5。
    """

    kind: str
    reason: str
    focus: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_done(self) -> bool:
        return self.kind == DECISION_DONE

    @property
    def is_force_stop(self) -> bool:
        return self.kind == DECISION_FORCE_STOP

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.kind,
            "reason": self.reason,
            "focus": list(self.focus),
            "detail": self.detail,
        }


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
    # S3 交锋
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "STATUS_TRADEOFF",
    "STATUS_STALEMATE",
    "ACTION_SUPPORT",
    "ACTION_REBUT",
    "ACTION_AMEND",
    "CONVINCED",
    "CONCEDED",
    "Reply",
    "CloseProposal",
    "OpenProposal",
    "TradeoffProposal",
    "Turn",
    "Disagreement",
    "Ledger",
    "DebateResult",
    # S4 收敛
    "DECISION_DONE",
    "DECISION_ANOTHER_ROUND",
    "DECISION_FORCE_STOP",
    "Budget",
    "Decision",
]
