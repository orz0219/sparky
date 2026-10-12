"""S3 交锋：组织多轮讨论并登记分歧。

职责
    1. **第 1 轮**全员立场陈述，由各人自己提出与其他人的冲突（``opens``）
    2. **后续轮**按 ``consensus.decide`` 给出的 focus 定向加轮，只召集当事双方
    3. 每轮结束后由"主持人"**串行**登记台账（新增 / 关闭 / 降级取舍 / 检出僵局）
    4. 检出轮级"原地打转"，交给 S4 判定

输入
    ``Brief`` · ``Panel`` · ``ReconOutcome``（各人的调研备忘）

输出
    ``DebateResult``（台账 + 逐轮发言）；落盘 ``ledger.json``、``rounds/rN.json``

依赖
    ``llm``（N 并发）· ``consensus``（纯函数判定）· ``config`` · ``events`` · ``models``

失败模式
    某人发言失败 → 该人本轮记为**弃权**（生成一个显式的 abstain 发言），
    并把失败原因记进 ``abstain_reason``。**不伪造发言。**
    连续弃权 2 轮 → 移出后续轮次，并发出告警。
    发言引用了不存在的人设 / 分歧编号 → 记告警并**忽略该条**，
    不让一条坏引用废掉整场讨论。
    同一项分歧同时出现在 ``closes`` 与 ``tradeoff_proposals`` → 记告警，以 ``closes`` 为准。

    ★ 两段式是**正确性要求**而非性能优化：
      · 轮内只读上轮快照
      · 写台账必须等本轮全员结束
      否则先完成的人会读到后完成者刚写入的条目，导致同轮不同人看到不同世界 ——
      这类 bug 在并发下不可复现。

设计要点
    主持人（登记逻辑）是纯代码，**不是**一个 LLM 人设。台账是收敛判定的唯一依据，
    让它经一次 LLM 转手就多了一个"漏抄或篡改"的失败面。

对应设计
    docs/design.md §4.4 · docs/consensus.md §2 §3 §5
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import consensus, events
from .config import RuntimeConfig, build_prompt
from .errors import LlmError, SchemaError, SparkyError
from .llm import LlmClient
from .models import (
    ACTION_REBUT,
    DECISION_ANOTHER_ROUND,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_STALEMATE,
    STATUS_TRADEOFF,
    Brief,
    Budget,
    DebateResult,
    Decision,
    Disagreement,
    Ledger,
    OpenProposal,
    Panel,
    Persona,
    ReconOutcome,
    StanceMemo,
    Turn,
)
from .session import Session

#: 连续弃权这么多轮的人设会被移出后续讨论。
MAX_ABSTAIN_STREAK = 2

#: 分歧总数的上限：**人均几项**。
#:
#: 为什么必须有这个上限 —— 一次端到端实测（4 人、4 轮）的教训：
#: 第 1 轮开了 26 项，之后每一轮每一位参与者都会再开 ~1 项，
#: 4 轮下来总数到了 **50 项**，其中 12 项最终无解。
#: 后果是 `consensus.decide` 的 `done` 分支**永远不可达** ——
#: 只要还有一项没谈拢，就只能靠 `max_rounds` 强停，
#: "达成一致"这个结果从设计上就出不来了。
#:
#: 收敛是有吞吐上限的：每轮只能处理有限几项（每项最多被定向加轮
#: `MAX_FOCUS_PER_ITEM` 次），所以分歧数必须与轮数上限匹配。
#: 人均 3 项、下限 8 项，是让"4 轮内有可能全部收敛"的粗略配平。
#: 这个系数与人数公式一样是**拍脑袋定的**，需要真实案例校准 ——
#: 见 docs/design.md §11 未决问题。
MAX_DISAGREEMENTS_PER_PERSONA = 3
MIN_DISAGREEMENT_CAP = 8

#: while 循环的硬保险。正常路径永远走不到 —— decide() 已经卡住了轮数上限。
#: 它是为了防止"判定逻辑写错导致死循环"，而不是产品行为。
MAX_ROUND_SAFETY = 16


def disagreement_cap(panel: Panel) -> int:
    """本次讨论允许登记的分歧总数上限。"""
    return max(MIN_DISAGREEMENT_CAP, MAX_DISAGREEMENTS_PER_PERSONA * len(panel.personas))


def run(
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    brief: Brief,
    panel: Panel,
    outcome: ReconOutcome,
    *,
    should_stop: Callable[[], bool] | None = None,
    interjections: object | None = None,
) -> DebateResult:
    """执行 S3 + S4，返回讨论结果。

    ``should_stop`` 由网页的「停止」按钮驱动。它**只在轮与轮之间**被检查 ——
    中途打断一轮会让并发中的几位专家看到不同的世界，破坏"轮内只读上轮快照"
    这条不变量（见 docs/consensus.md §2.1）。
    所以停止的语义是"**当前轮跑完就收口**"，不是"立刻掐断"。

    ``interjections`` 是委托人的插话队列（``server.InterjectionQueue``）。
    它在**每轮开始前**被取空并注入该轮的提示词 —— 与停止同理，
    中途打断一轮会破坏并发不变量。
    """
    ledger = Ledger()
    result = DebateResult(ledger=ledger)
    brief_json = json.dumps(brief.to_dict(), ensure_ascii=False, indent=2)
    budget_limit = runtime.budget_tokens

    fingerprints: list[RoundFingerprint] = []
    abstain_streak: dict[str, int] = {p.id: 0 for p in panel.personas}
    decision: Decision | None = None
    stagnation = False
    round_no = 0

    while True:
        round_no += 1
        if round_no > MAX_ROUND_SAFETY:
            raise LlmError(
                f"讨论超过 {MAX_ROUND_SAFETY} 轮仍未结束，判定逻辑可能有问题",
                hint="这是内部保险，不是产品行为。请检查 consensus.decide 的分支顺序。",
            )

        # 委托人插话：**轮间送达**。
        #
        # 归属划分（曾经搞错过一次，见下面的注释）：
        #   · ``session.interjections`` 是**全部收到过的插话**，由 HTTP 回调
        #     在收到时就登记 —— 它要覆盖"直到会话结束都没送达"的那些，
        #     文档附录 C 才能如实列出未送达的插话。
        #   · 队列的 ``_pending`` 只是**还没送达**的那些。
        #   · 两边放的是**同一个 dict 对象**，所以这里填 ``delivered_round``
        #     会同步到会话记录，不需要（也绝不能）再 append 一次。
        if interjections is not None:
            for note in interjections.drain():
                note["delivered_round"] = round_no
                session.emit(
                    events.CLIENT_INTERJECTION_DELIVERED,
                    round=round_no,
                    text=note["text"],
                )

        if should_stop is not None and should_stop():
            session.warn(
                "stop_requested",
                f"收到停止请求：第 {round_no} 轮不再开始，直接进入定稿。",
            )
            break

        session.current_round = round_no
        focus_ids: tuple[str, ...] = () if round_no == 1 else (decision.focus if decision else ())
        participants = _pick_participants(
            panel=panel,
            round_no=round_no,
            focus_ids=focus_ids,
            ledger=ledger,
            abstain_streak=abstain_streak,
            session=session,
        )
        if not participants:
            # 当事双方全都弃权/缺席 —— 没有可开的会了
            session.warn(
                "no_participants",
                f"第 {round_no} 轮没有可召集的参与者（当事双方均已连续弃权或缺席），提前收口。",
            )
            break

        session.emit(
            events.ROUND_START,
            round=round_no,
            mode="opening" if round_no == 1 else "focus",
            focus=list(focus_ids),
            participants=[p.id for p in participants],
            topics={d.id: d.topic for d in ledger.items if d.id in focus_ids},
        )

        # ── 阶段一：并发生成（轮内只读上轮快照）──
        turns = _collect_turns(
            client=client,
            runtime=runtime,
            session=session,
            panel=panel,
            participants=participants,
            brief_json=brief_json,
            outcome=outcome,
            ledger=ledger,
            focus_ids=focus_ids,
            history=result.rounds,
            round_no=round_no,
            client_notes=_render_client_notes(session),
        )
        result.rounds.append(turns)
        session.rounds.append(turns)

        # ── 屏障之后：阶段二，主持人串行登记 ──
        opened = _register(ledger, turns, round_no, session, panel)

        for persona in participants:
            turn = next((t for t in turns if t.persona_id == persona.id), None)
            abstain_streak[persona.id] = (
                abstain_streak[persona.id] + 1 if turn is None or turn.abstained else 0
            )

        fingerprints.append(_fingerprint(round_no, turns, opened, ledger))
        stagnation = _is_stagnant(fingerprints)

        session.emit(
            events.ROUND_END,
            round=round_no,
            open_count=len(ledger.unresolved),
            digest=ledger.digest(),
        )
        session.write_json(
            f"rounds/r{round_no}.json",
            {"round": round_no, "turns": [t.to_dict() for t in turns]},
        )

        # ── S4 收敛判定（纯函数）──
        decision = consensus.decide(
            ledger=ledger,
            round_no=round_no,
            budget=Budget(total_tokens=session.usage.total_tokens, limit=budget_limit),
            max_rounds=runtime.max_rounds,
            stagnation=stagnation,
        )
        session.emit(events.CONVERGENCE_DECISION, **decision.to_dict())

        if decision.kind != DECISION_ANOTHER_ROUND:
            break

        for disagreement_id in decision.focus:
            ledger.focus_counts[disagreement_id] = (
                ledger.focus_counts.get(disagreement_id, 0) + 1
            )

    newly_stalemate = consensus.finalize(ledger, round_no=round_no)
    for item in newly_stalemate:
        session.emit(
            events.DISAGREEMENT_UPDATE,
            id=item.id,
            topic=item.topic,
            patch={"status": STATUS_STALEMATE},
            reason="讨论结束仍未达成一致，如实记为未决问题",
        )

    result.final_decision = decision
    session.ledger = ledger
    session.debate = result
    session.write_json("ledger.json", ledger.to_dict())
    session.write_json("debate.json", result.to_dict())
    return result


# ───────────────────── 参与者选择 ─────────────────────


def _pick_participants(
    *,
    panel: Panel,
    round_no: int,
    focus_ids: tuple[str, ...],
    ledger: Ledger,
    abstain_streak: dict[str, int],
    session: Session,
) -> list[Persona]:
    """决定本轮由谁发言。

    第 1 轮全员；后续轮只召集未决分歧的当事双方（见 docs/consensus.md §5）。
    连续弃权达上限的人被移出 —— 但会发告警，不静默。
    """
    if round_no == 1:
        candidates = list(panel.personas)
    else:
        parties: set[str] = set()
        for disagreement_id in focus_ids:
            item = ledger.by_id(disagreement_id)
            if item is not None:
                parties.update(item.parties)
        candidates = [p for p in panel.personas if p.id in parties]

    kept: list[Persona] = []
    for persona in candidates:
        if abstain_streak.get(persona.id, 0) >= MAX_ABSTAIN_STREAK:
            session.warn(
                "persona_removed",
                f"{persona.id} {persona.name} 已连续 {MAX_ABSTAIN_STREAK} 轮弃权，"
                f"移出后续讨论。",
            )
            continue
        kept.append(persona)
    return kept


# ───────────────────── 阶段一：并发生成发言 ─────────────────────


def _collect_turns(
    *,
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    panel: Panel,
    participants: list[Persona],
    brief_json: str,
    outcome: ReconOutcome,
    ledger: Ledger,
    focus_ids: tuple[str, ...],
    history: list[list[Turn]],
    round_no: int,
    client_notes: str,
) -> list[Turn]:
    """N 人并发生成发言，**按人设顺序回收**。

    并发是 IO 并行的需要；按序回收是为了让 transcript 与 rounds/ 的内容
    不随线程调度抖动 —— 一个以"可复现"为卖点的工具，不该有随机的日志顺序。
    """
    for persona in participants:
        session.emit(
            events.TURN_START,
            round=round_no,
            persona_id=persona.id,
            name=persona.name,
            title=persona.title,
        )

    builders: dict[str, Callable[[Persona], str]] = {}
    for persona in participants:
        if round_no == 1:
            builders[persona.id] = lambda p, bj=brief_json, cn=client_notes: build_prompt(
                "turn",
                "opening",
                brief_json=bj,
                client_notes=cn,
                stances_overview=_stances_overview(panel, outcome, exclude_id=p.id),
                your_memo=_own_memo(outcome.memo_for(p.id)),
            )
        else:
            builders[persona.id] = lambda p, bj=brief_json, fids=focus_ids, cn=client_notes: build_prompt(
                "turn",
                "focus",
                brief_json=bj,
                client_notes=cn,
                focus_items=_focus_items(ledger, panel, fids, persona_id=p.id),
                your_concession=_concession_of(outcome.memo_for(p.id)),
            )

    results: dict[str, Turn | str] = {}
    with ThreadPoolExecutor(max_workers=runtime.concurrency) as pool:
        futures = {
            persona.id: pool.submit(
                _one_turn,
                client=client,
                runtime=runtime,
                session=session,
                persona=persona,
                builder=builders[persona.id],
                round_no=round_no,
            )
            for persona in participants
        }
        for persona_id, future in futures.items():
            try:
                results[persona_id] = future.result()
            except SparkyError as exc:
                results[persona_id] = f"{type(exc).__name__}: {exc}"

    turns: list[Turn] = []
    for persona in participants:
        outcome_or_error = results[persona.id]
        if isinstance(outcome_or_error, str):
            session.warn(
                "turn_failed",
                f"{persona.id} {persona.name} 第 {round_no} 轮发言失败，按弃权处理："
                f"{outcome_or_error}",
            )
            turn = Turn(
                round=round_no,
                persona_id=persona.id,
                say="",
                responds_to=[],
                new_points=[],
                opens=[],
                closes=[],
                tradeoff_proposals=[],
                stance_snapshot={},
                abstain_reason=f"发言失败：{outcome_or_error}",
            )
        else:
            turn = outcome_or_error
        turns.append(turn)
        session.emit(
            events.TURN_END,
            turn={**turn.to_dict(), "summary": turn.summary()},
        )
    return turns


def _one_turn(
    *,
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    persona: Persona,
    builder: Callable[[Persona], str],
    round_no: int,
) -> Turn:
    """一个人的一次发言。**流式**调用，边收边把 ``say`` 广播给界面。

    界面靠 ``turn.token`` 做打字机效果。这些事件是**临时事件**、不落盘 ——
    一次发言上千条增量，写进 ``transcript.jsonl`` 会让审计日志彻底不可用
    （见 ``events.EPHEMERAL_EVENT_TYPES``）。

    并发下多个人的增量会交错到达，这是**符合预期**的：
    界面上就是几个人同时在说话。
    """

    def on_delta(text: str, reset: bool) -> None:
        session.emit(
            events.TURN_TOKEN,
            round=round_no,
            persona_id=persona.id,
            delta=text,
            reset=reset,
        )

    data, _ = client.stream_json(
        system=_system_for(persona),
        user=builder(persona),
        purpose=f"S3 交锋·第{round_no}轮·{persona.id} {persona.name}",
        model=runtime.model_fast,
        temperature=0.7,
        on_delta=on_delta,
    )
    return Turn.from_llm(persona.id, round_no, data)


def _system_for(persona: Persona) -> str:
    """反方用**独立的 system**，而不是在主 system 后面追加一段限制。

    反方的行为准则与普通成员冲突（普通成员被鼓励提方案，反方被禁止提方案）。
    用追加说明会留下互相矛盾的指令，模型会两边各沾一点。
    """
    if persona.is_adversary:
        return build_prompt(
            "turn",
            "system_adversary",
            name=persona.name,
            stance=persona.stance,
            bias=persona.bias,
        )
    return build_prompt(
        "turn",
        "system",
        name=persona.name,
        title=persona.title,
        stance=persona.stance,
        cares_about="、".join(persona.cares_about),
    )


# ───────────────────── 提示词素材 ─────────────────────


def _stances_overview(panel: Panel, outcome: ReconOutcome, *, exclude_id: str) -> str:
    """其他参会者的**立场**（不交换推理链 —— 见 ADR 0004 的边界）。"""
    lines: list[str] = []
    for persona in panel.personas:
        if persona.id == exclude_id:
            continue
        memo = outcome.memo_for(persona.id)
        lines.append(f"### {persona.id} {persona.name}（{persona.title}）")
        if memo is None:
            lines.append("- ⚠️ 本次调研失败缺席，没有可比的立场。")
            lines.append("")
            continue
        lines.append(f"- 核心主张：{memo.core_claim}")
        for con in memo.cons[:2]:
            lines.append(f"- 他自己承认的代价：{con}")
        lines.append("")
    return "\n".join(lines).strip() or "（本次没有其他参会者。）"


def _own_memo(memo: StanceMemo | None) -> str:
    if memo is None:
        return "（你的调研失败了。本轮请基于既有知识发言，并对不确定的数字明确说「未验证」。）"
    return "\n".join(
        [
            f"核心主张：{memo.core_claim}",
            "优点：" + "；".join(memo.pros),
            "缺点：" + "；".join(memo.cons),
            f"让步条件：{memo.would_concede_if}",
        ]
    )


def _render_client_notes(session: Session) -> str:
    """把委托人的插话渲染成提示词里的一段。

    措辞上做了两件事：① 明说它**优先于专家各自的偏好**（委托人是要东西的人）；
    ② 但要求专家**冲突时说出来，不要默默接受** —— 否则插话会变成
    "所有人立刻改口"，那比不插话更糟。
    """
    if not session.interjections:
        return "（委托人在本次讨论中还没有插话。）"
    lines = [
        "委托人在讨论进行中补充了以下意见。**它们优先于你们各自的偏好** ——",
        "但如果你认为它有问题，**明确说出来**，不要默默接受：",
        "",
    ]
    for item in session.interjections:
        round_no = item.get("delivered_round") or item.get("received_round")
        lines.append(f"- （第 {round_no} 轮）「{item['text']}」")
    return "\n".join(lines)


def _concession_of(memo: StanceMemo | None) -> str:
    if memo is None:
        return "（没有记录到你的让步条件。）"
    return memo.would_concede_if


def _render_turn(turn: Turn, panel: Panel) -> str:
    persona = panel.by_id(turn.persona_id)
    lines = [f"### {turn.persona_id} {persona.name}（{persona.title}）"]
    if turn.abstained:
        reason = f"（{turn.abstain_reason}）" if turn.abstain_reason else ""
        lines.append(f"- 弃权，本轮没有发言 {reason}")
        return "\n".join(lines)
    for reply in turn.responds_to:
        lines.append(f"- 对 **{reply.disagreement_id}** 表态「{reply.action}」：{reply.reason}")
    for point in turn.new_points:
        lines.append(f"- 补充：{point}")
    for proposal in turn.opens:
        lines.append(
            f"- 新分歧：{proposal.topic}（对手 {proposal.with_persona}）"
        )
    for close in turn.closes:
        lines.append(f"- 关闭 **{close.disagreement_id}**（{close.by}）：{close.because}")
    for proposal in turn.tradeoff_proposals:
        lines.append(
            f"- 提议把 **{proposal.disagreement_id}** 降级为取舍："
            f"选「{proposal.choose}」；反过来选的条件是「{proposal.when_to_flip}」"
        )
    return "\n".join(lines)


def _focus_items(
    ledger: Ledger, panel: Panel, focus_ids: tuple[str, ...], *, persona_id: str
) -> str:
    """把该人牵涉的分歧完整摊开 —— 只给他自己当事的那些。"""
    blocks: list[str] = []
    for disagreement_id in focus_ids:
        item = ledger.by_id(disagreement_id)
        if item is None or persona_id not in item.parties:
            continue
        lines = [f"### {item.id}：{item.topic}"]
        if item.status == STATUS_STALEMATE:
            lines.append("- ⚠️ 系统判定：这项已连续两轮双方论据完全一致，是僵局。")
        for party in item.parties:
            name = panel.by_id(party).name
            mark = "（你）" if party == persona_id else ""
            lines.append(f"- {party} {name}{mark} 的立场：{item.positions.get(party, '（未记录）')}")
        recent = item.history[-3:]
        if recent:
            lines.append("- 最近的交锋：")
            for entry in recent:
                who = entry.get("by") or "系统"
                what = entry.get("reason") or entry.get("summary") or entry.get("action", "")
                lines.append(f"  - 第 {entry.get('round', '?')} 轮 {who}：{what}")
        blocks.append("\n".join(lines))
    if not blocks:
        raise SchemaError(
            f"{persona_id} 被点名为 {focus_ids} 的当事人，但它不在任何一项的 parties 里。"
            f"这是内部错误：_pick_participants 与 _focus_items 的口径不一致。"
        )
    return "\n\n".join(blocks)


# ───────────────────── 阶段二：主持人登记台账 ─────────────────────


def _register(
    ledger: Ledger, turns: list[Turn], round_no: int, session: Session, panel: Panel
) -> list[str]:
    """主持人登记。**串行**执行，且必须等本轮全员结束（正确性要求）。"""
    opened: list[str] = []
    dropped_before = len(ledger.dropped_topics)

    # 1. 新分歧 —— 先登记，后面的引用才有编号可指
    for turn in turns:
        for proposal in turn.opens:
            new_id = _open_disagreement(ledger, turn.persona_id, proposal, round_no, session, panel)
            if new_id:
                opened.append(new_id)

    # 2. 回应 → 记进历史（僵局检测要从这里读）
    for turn in turns:
        for reply in turn.responds_to:
            item = ledger.by_id(reply.disagreement_id)
            if item is None:
                session.warn(
                    "unknown_disagreement_ref",
                    f"{turn.persona_id} 回应了不存在的分歧 {reply.disagreement_id!r}，已忽略该条。",
                )
                continue
            item.history.append(
                {
                    "round": round_no,
                    "by": turn.persona_id,
                    "action": reply.action,
                    "reason": reply.reason,
                    # 归一化后的论据，专供"原地打转"检测；不参与展示
                    "reason_key": "".join(reply.reason.split()),
                }
            )

    # 3. 关闭分歧
    for turn in turns:
        for close in turn.closes:
            item = ledger.by_id(close.disagreement_id)
            if item is None:
                session.warn(
                    "unknown_disagreement_ref",
                    f"{turn.persona_id} 试图关闭不存在的分歧 {close.disagreement_id!r}，已忽略。",
                )
                continue
            if item.is_settled:
                session.warn(
                    "close_already_settled",
                    f"{turn.persona_id} 试图关闭已经收敛的 {item.id}（当前 {item.status}），已忽略。",
                )
                continue
            if any(t.disagreement_id == item.id for t in turn.tradeoff_proposals):
                session.warn(
                    "close_and_tradeoff_conflict",
                    f"{turn.persona_id} 对 {item.id} 既说被说服又提议取舍，"
                    f"按更强的表态处理：记为已解决。",
                )
            item.status = STATUS_RESOLVED
            item.resolution = {
                "round": round_no,
                "by": turn.persona_id,
                "kind": close.by,
                "because": close.because,
            }
            item.history.append(
                {
                    "round": round_no,
                    "by": turn.persona_id,
                    "action": f"close:{close.by}",
                    "reason": close.because,
                }
            )
            session.emit(
                events.DISAGREEMENT_CLOSE,
                id=item.id,
                topic=item.topic,
                status=item.status,
                resolution=item.resolution,
            )

    # 4. 取舍提议（记录 + 判定接受）
    for turn in turns:
        for proposal in turn.tradeoff_proposals:
            item = ledger.by_id(proposal.disagreement_id)
            if item is None:
                session.warn(
                    "unknown_disagreement_ref",
                    f"{turn.persona_id} 对不存在的分歧 {proposal.disagreement_id!r} 提议取舍，已忽略。",
                )
                continue
            if item.is_settled:
                session.warn(
                    "tradeoff_already_settled",
                    f"{turn.persona_id} 对已收敛的 {item.id} 提议取舍，已忽略。",
                )
                continue
            item.tradeoff = {
                "proposed_by": turn.persona_id,
                "choose": proposal.choose,
                "when_to_flip": proposal.when_to_flip,
                "round": round_no,
            }
            item.history.append(
                {
                    "round": round_no,
                    "by": turn.persona_id,
                    "action": "propose_tradeoff",
                    "reason": f"选「{proposal.choose}」，反过来选的条件：「{proposal.when_to_flip}」",
                }
            )
    _accept_tradeoffs(ledger, turns, round_no, session)

    # 5. 僵局检测
    _detect_stalemates(ledger, round_no, session)

    # 6. 超出上限而未登记的议题：**如实列出**，不悄悄丢掉
    newly_dropped = ledger.dropped_topics[dropped_before:]
    if newly_dropped:
        session.warn(
            "disagreement_cap_reached",
            f"本轮有 {len(newly_dropped)} 项议题因超出分歧数上限"
            f"（{disagreement_cap(panel)} 项）而未登记为分歧：{'；'.join(newly_dropped)}。"
            f"它们会列在文档附录 C 里，不会被丢掉。",
        )

    return opened


def _open_disagreement(
    ledger: Ledger,
    proposer_id: str,
    proposal: OpenProposal,
    round_no: int,
    session: Session,
    panel: Panel,
) -> str | None:
    """登记一条新分歧。返回新编号；被忽略时返回 ``None``。"""
    target = proposal.with_persona
    if target == proposer_id:
        session.warn(
            "self_conflict",
            f"{proposer_id} 把自己列为分歧对手，已忽略：{proposal.topic}",
        )
        return None
    try:
        panel.by_id(target)
    except SchemaError:
        session.warn(
            "unknown_persona_ref",
            f"{proposer_id} 提出的分歧指向不存在的人设 {target!r}，已忽略：{proposal.topic}",
        )
        return None

    topic_key = "".join(proposal.topic.split())
    for item in ledger.items:
        if item.is_settled:
            continue
        if set(item.parties) == {proposer_id, target} and "".join(item.topic.split()) == topic_key:
            session.warn(
                "duplicate_disagreement",
                f"{item.id} 已经是同一对当事人之间的同一议题，不再重复登记：{proposal.topic}",
            )
            return None

    # 分歧总数上限。超出的**不丢弃** —— 记进 ledger.dropped_topics，
    # 由附录 C 如实列出（见 disagreement_cap 的说明）。
    cap = disagreement_cap(panel)
    if len(ledger.items) >= cap:
        ledger.dropped_topics.append(proposal.topic)
        return None

    new_id = ledger.new_id()
    item = Disagreement(
        id=new_id,
        topic=proposal.topic,
        parties=[proposer_id, target],
        status=STATUS_OPEN,
        positions={
            proposer_id: proposal.your_position,
            target: proposal.their_position,
        },
        stakes=proposal.topic,
        history=[
            {
                "round": round_no,
                "by": proposer_id,
                "action": "open",
                "reason": proposal.topic,
            }
        ],
    )
    ledger.items.append(item)
    session.emit(
        events.DISAGREEMENT_OPEN,
        id=item.id,
        topic=item.topic,
        parties=item.parties,
        round=round_no,
    )
    return new_id


def _accept_tradeoffs(
    ledger: Ledger, turns: list[Turn], round_no: int, session: Session
) -> None:
    """判定取舍提议是否被接受。

    接受条件（二者之一）：

    1. **双方都提了取舍** —— 说明都认可"这是个条件分歧"；
    2. **只有一方提，而另一方本轮没有明确反对** —— 默认接受。

    条件 2 的默认接受是刻意的：取舍不是"认输"，而是"把僵局变成条件判断"。
    如果要求双方都主动提，绝大多数有价值的取舍会卡在"对方没接话"上。
    只要另一方明确 `rebut`，就不接受 —— 那时它仍是未决分歧。
    """
    by_id: dict[str, list[tuple[str, str, str]]] = {}
    for turn in turns:
        for proposal in turn.tradeoff_proposals:
            by_id.setdefault(proposal.disagreement_id, []).append(
                (turn.persona_id, proposal.choose, proposal.when_to_flip)
            )

    for disagreement_id, proposals in by_id.items():
        item = ledger.by_id(disagreement_id)
        if item is None or item.is_settled:
            continue
        proposers = {p[0] for p in proposals}
        opponents = [p for p in item.parties if p not in proposers]
        rebutted = any(
            reply.disagreement_id == item.id and reply.action == ACTION_REBUT
            for turn in turns
            if turn.persona_id in opponents
            for reply in turn.responds_to
        )
        unanimous = len(proposers) >= 2
        if not unanimous and rebutted:
            continue

        proposer, choose, when_to_flip = proposals[-1]
        item.status = STATUS_TRADEOFF
        item.tradeoff = {
            "proposed_by": proposer,
            "choose": choose,
            "when_to_flip": when_to_flip,
            "round": round_no,
            "accepted_by": "双方都提了取舍" if unanimous else "另一方未明确反对",
        }
        item.history.append(
            {
                "round": round_no,
                "by": proposer,
                "action": "accept_tradeoff",
                "reason": f"降级为取舍（{item.tradeoff['accepted_by']}）",
            }
        )
        session.emit(
            events.DISAGREEMENT_CLOSE,
            id=item.id,
            topic=item.topic,
            status=item.status,
            tradeoff=item.tradeoff,
        )


def _detect_stalemates(ledger: Ledger, round_no: int, session: Session) -> None:
    """检出僵局：同一项分歧连续两轮的（谁、动作、归一化论据）完全一致。

    判据是刻意的**逐字归一化比较**，不做语义比较 —— 语义比较要额外调模型，
    会破坏"S4 纯规则"这条设计约束。代价是模型每次换措辞就检测不到，
    由轮级停滞检测与轮数上限兜底。见 docs/consensus.md §4.1。
    """
    if round_no < 2:
        return
    for item in ledger.unresolved:
        current = _reason_keys(item, round_no)
        previous = _reason_keys(item, round_no - 1)
        if not current or current != previous:
            continue
        if item.status == STATUS_STALEMATE:
            continue
        item.status = STATUS_STALEMATE
        item.history.append(
            {
                "round": round_no,
                "summary": f"连续两轮双方论据完全一致（{len(current)} 条），记为僵局",
            }
        )
        session.emit(
            events.DISAGREEMENT_UPDATE,
            id=item.id,
            topic=item.topic,
            patch={"status": STATUS_STALEMATE},
        )


def _reason_keys(item: Disagreement, round_no: int) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (h.get("by", ""), h.get("action", ""), h.get("reason_key", ""))
            for h in item.history
            if h.get("round") == round_no and "reason_key" in h
        )
    )


# ───────────────────── 停滞检测（轮级）─────────────────────


@dataclass(frozen=True)
class RoundFingerprint:
    """一轮的指纹：立场方向 + 关闭了哪些 + 新开了哪些。

    这是 docs/consensus.md §4.1 那个"拍脑袋定的三元组"，放在这里是为了
    让它显眼、好调 —— 见 docs/design.md §11 未决问题 2。
    """

    round: int
    stances: tuple[tuple[str, str], ...] = ()
    closes: tuple[str, ...] = ()
    opens: tuple[str, ...] = ()


def _fingerprint(
    round_no: int, turns: list[Turn], opened: list[str], ledger: Ledger
) -> RoundFingerprint:
    known = {item.id for item in ledger.items}
    stances = tuple(
        sorted(
            {
                (disagreement_id, direction)
                for turn in turns
                for disagreement_id, direction in turn.stance_snapshot.items()
                if disagreement_id in known
            }
        )
    )
    closes = tuple(sorted(f"{t.persona_id}:{c.disagreement_id}" for t in turns for c in t.closes))
    return RoundFingerprint(
        round=round_no, stances=stances, closes=closes, opens=tuple(sorted(opened))
    )


def _is_stagnant(fingerprints: list[RoundFingerprint]) -> bool:
    """本轮与上一轮的（立场、关闭、新开）完全一致 → 判定为原地打转。"""
    if len(fingerprints) < 2:
        return False
    current, previous = fingerprints[-1], fingerprints[-2]
    return (
        current.stances == previous.stances
        and current.closes == previous.closes
        and current.opens == previous.opens
    )
