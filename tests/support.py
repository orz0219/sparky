"""测试共用夹具。

不引入 pytest —— 零第三方依赖是硬约束（ADR 0001），
单测用标准库 ``unittest`` 即可。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sparky.events import EventBus
from sparky.models import (
    ACTION_REBUT,
    ROLE_ADVERSARY,
    ROLE_SPECIALIST,
    STATUS_OPEN,
    Brief,
    CloseProposal,
    Dimension,
    Disagreement,
    InfoScope,
    Ledger,
    OpenProposal,
    Panel,
    Persona,
    Reply,
    SourcedText,
    TradeoffProposal,
    Turn,
)
from sparky.session import Session


class TempSession:
    """在临时目录里建一个真实会话，退出时自动清理。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.session = Session.create(
            self.root / "sessions",
            "测试输入",
            bus=EventBus(),
            config_snapshot={"test": True},
        )

    def cleanup(self) -> None:
        self._tmp.cleanup()


def make_dimension(index: int, *, inferred: bool = False) -> Dimension:
    return Dimension(
        id=f"d{index}",
        name=f"维度{index}",
        why=f"因为维度{index}重要",
        source="inferred" if inferred else "user",
    )


def make_brief(*, dimension_count: int = 3, inferred_items: int = 1) -> Brief:
    constraints = [SourcedText("离线运行", "user")]
    constraints += [SourcedText(f"推断前提{i}", "inferred") for i in range(inferred_items)]
    return Brief(
        raw_input="做一个测试工具",
        goal="测试目标",
        constraints=constraints,
        non_goals=[SourcedText("不做移动端", "user")],
        unknowns=["产品形态未定"],
        dimensions=[make_dimension(i, inferred=(i == 1)) for i in range(1, dimension_count + 1)],
        uncertainty="medium",
    )


def make_persona(
    index: int,
    *,
    role_type: str = ROLE_SPECIALIST,
    dimensions: list[str] | None = None,
    questions: list[str] | None = None,
) -> Persona:
    return Persona(
        id=f"p{index}",
        name=f"专家{index}",
        title=f"头衔{index}",
        role_type=role_type,
        stance=f"立场{index}",
        bias=f"偏见{index}",
        blind_spot=None if role_type == ROLE_ADVERSARY else f"盲点{index}",
        cares_about=[f"指标{index}"],
        dimensions=dimensions or [f"d{index}"],
        info_scope=InfoScope(
            questions=questions or [f"问题{index}？"],
            search_queries=[],
            budget={"searches": 0, "tokens": 1000},
        ),
    )


def make_panel() -> Panel:
    return Panel(
        personas=[make_persona(1), make_persona(2), make_persona(3, role_type=ROLE_ADVERSARY)],
        panel_size_reasoning="测试用固定队伍",
    )


def make_single_panel() -> Panel:
    """单人专家组。与 ``VALID_DOCUMENT`` 的附录 A（1 条）对齐。"""
    return Panel(personas=[make_persona(1)], panel_size_reasoning="单人测试队伍")


def persona_payload(index: int, **overrides: Any) -> dict[str, Any]:
    """一份「看起来像模型返回」的人设 dict，可用 overrides 精确破坏某个字段。"""
    payload: dict[str, Any] = {
        "id": f"p{index}",
        "name": f"专家{index}",
        "title": f"头衔{index}",
        "role_type": "specialist",
        "stance": f"立场{index}",
        "bias": f"偏见{index}",
        "blind_spot": f"盲点{index}",
        "cares_about": [f"指标{index}"],
        "dimensions": [f"d{index}"],
        "info_scope": {
            "questions": [f"问题{index}？"],
            "search_queries": [],
            "budget": {"searches": 0, "tokens": 1000},
        },
    }
    payload.update(overrides)
    return payload


def make_outcome(panel: Panel, *, failing: tuple[str, ...] = ()):
    """造一份 S2 调研结果，供 S3 的提示词组装使用。"""
    from sparky.models import ReconOutcome, StanceMemo

    outcome = ReconOutcome()
    for persona in panel.personas:
        if persona.id in failing:
            outcome.failures.append((persona.id, "测试用调研失败"))
            continue
        outcome.memos.append(
            StanceMemo(
                persona_id=persona.id,
                core_claim=f"{persona.name}的核心主张",
                pros=[f"{persona.name}的优点"],
                cons=[f"{persona.name}的代价一", f"{persona.name}的代价二"],
                reasoning="因为所以",
                evidence=[],
                would_concede_if=f"如果{persona.name}的条件成立就改口",
                searched=False,
                search_note="本次未使用外部检索",
            )
        )
    return outcome


# ───────────────────── S3 / S4 夹具 ─────────────────────


def make_disagreement(
    disagreement_id: str = "x1",
    *,
    parties: tuple[str, str] = ("p1", "p2"),
    status: str = STATUS_OPEN,
    topic: str = "某个议题",
    positions: dict[str, str] | None = None,
    history: list | None = None,
    tradeoff: dict | None = None,
    resolution: dict | None = None,
) -> Disagreement:
    return Disagreement(
        id=disagreement_id,
        topic=topic,
        parties=list(parties),
        status=status,
        positions=positions or {parties[0]: "甲的立场", parties[1]: "乙的立场"},
        stakes=topic,
        history=list(history or []),
        resolution=resolution,
        tradeoff=tradeoff,
    )


def make_ledger(
    items: list[Disagreement] | None = None,
    focus_counts: dict[str, int] | None = None,
) -> Ledger:
    ledger = Ledger(items=list(items or []), focus_counts=dict(focus_counts or {}))
    # 让 new_id() 接着已有编号往下发，避免测试里出现 x1 撞号
    ledger.opened_total = len(ledger.items)
    return ledger


def make_turn(
    persona_id: str = "p1",
    round_no: int = 1,
    *,
    say: str = "我这一轮要说的话。",
    responds_to: list[Reply] | None = None,
    opens: list[OpenProposal] | None = None,
    closes: list[CloseProposal] | None = None,
    tradeoffs: list[TradeoffProposal] | None = None,
    stance_snapshot: dict[str, str] | None = None,
    new_points: list[str] | None = None,
    abstain_reason: str | None = None,
) -> Turn:
    return Turn(
        round=round_no,
        persona_id=persona_id,
        say=say,
        responds_to=list(responds_to or []),
        new_points=list(new_points or []),
        opens=list(opens or []),
        closes=list(closes or []),
        tradeoff_proposals=list(tradeoffs or []),
        stance_snapshot=dict(stance_snapshot or {}),
        abstain_reason=abstain_reason,
    )


def make_reply(
    disagreement_id: str = "x1", *, action: str = ACTION_REBUT, reason: str = "理由"
) -> Reply:
    return Reply(disagreement_id=disagreement_id, action=action, reason=reason)


def make_close(
    disagreement_id: str = "x1", *, by: str = "convinced", because: str = "被说服了"
) -> CloseProposal:
    return CloseProposal(disagreement_id=disagreement_id, by=by, because=because)


def make_tradeoff(
    disagreement_id: str = "x1",
    *,
    choose: str = "选甲的方案",
    when_to_flip: str = "如果体积不是瓶颈就选乙的",
) -> TradeoffProposal:
    return TradeoffProposal(
        disagreement_id=disagreement_id, choose=choose, when_to_flip=when_to_flip
    )


def make_open(
    *,
    topic: str = "新的议题",
    with_persona: str = "p2",
    your_position: str = "我这样",
    their_position: str = "他那样",
) -> OpenProposal:
    return OpenProposal(
        topic=topic,
        with_persona=with_persona,
        your_position=your_position,
        their_position=their_position,
    )


#: 一份**能通过全部 9 项合规校验**的最小文档。各测试基于它做破坏性修改。
VALID_DOCUMENT = """# 测试目标 · 设计文档

> 由 Sparky 生成于 2026-10-12 ｜ 会话 `test`
> 参会 1 人 · 讨论 0 轮 · 未决 0 项

## 0. 摘要

这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。

## 1. 问题与目标

### 1.1 目标

做一个东西。

## 2. 约束与假设

### 2.1 硬约束

- 离线运行

### 2.2 假设

- 推断前提0 `(推断)`

### 2.3 未知项

- 产品形态未定

## 3. 候选方案对比

| 方案 | 核心思路 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| A | 方案A的思路 | 优点A | 缺点A | 采纳 |
| B | 方案B的思路 | 优点B | 缺点B | 否决 |

### 3.2 被否决的方案

**方案B**

- 否决理由：成本过高，且没有解决核心问题，现有数据不支持它的前提。
- 什么条件下重新考虑它：若单次成本降到千分之一以下，且延迟不再是瓶颈。

## 4. 最终方案

### 4.1 总体架构

本项目按阶段推进。

## 5. 关键取舍台账

本次会话没有登记任何取舍项。

## 6. 风险与失败模式

### 6.3 失败模式表

| 什么坏了 | 表现是什么 | 怎么发现 |
|---|---|---|
| 磁盘写满 | 写盘失败 | 监控磁盘水位 |
| 网络中断 | 调用超时 | 超时计数器 |
| 上游改格式 | 解析失败 | 解析失败率告警 |

## 7. 里程碑与验收

### 7.1 M1

一天内验证核心假设。

## 8. 未决问题

本次会话未运行讨论阶段。

## 附录 A：参会人设

### A.1 专家1（头衔1）

- 角色类型：specialist

参会人数：1 人。

## 附录 B：信息来源

## 附录 C：讨论轮次摘要

## 附录 D：成本统计

| 指标 | 数值 |
|---|---|
| LLM 调用次数 | 0 |
"""
