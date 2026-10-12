"""S4 收敛：判定讨论是否可以结束。

职责
    ``decide(...) -> Decision``：按固定顺序判断"继续辩 / 可以收口 / 必须停"。
    ``finalize(ledger)``：收口时把仍然 ``open`` 的分歧如实标为 ``stalemate``。
    ``is_harmonious(...)``：检出"零分歧"这一**失败信号**。

输入
    分歧台账 · 已进行轮数 · 预算快照 · 轮数上限 · 停滞标记

输出
    ``Decision``（done / another_round / force_stop）

依赖
    **无。** 不许 import ``llm``，不许做任何 IO，不许用随机数。
    只用 ``models`` 里的数据类型。

失败模式
    无 —— 纯函数无 IO，因此没有失败模式。**这正是选它的理由之一**：
    终止条件必须是控制流，不能是创作。

    ★ 三条禁令（实现时不得违反）：
      1. 不得 import ``llm``。一旦调了模型，终止条件就不可复现了。
      2. 不得做任何 IO。判定结果不许依赖磁盘或网络的状态。
      3. 判断顺序不许改：done → budget → max_rounds → stagnation → deadlock
         → another_round。**``done`` 必须排在预算/轮数之前** ——
         若刚好在最后一轮达成一致，不应被误判为未收敛。

设计要点
    ``another_round`` **一定带 focus**。Sparky 从不"让全员再发一轮言"，
    只召集分歧的当事双方 —— 这既省钱，也避免无关人等把话题带偏。
    见 docs/consensus.md §5。

对应设计
    docs/design.md §4.5 · docs/consensus.md §4 §7
"""

from __future__ import annotations

from .models import (
    DECISION_ANOTHER_ROUND,
    DECISION_DONE,
    DECISION_FORCE_STOP,
    STATUS_OPEN,
    STATUS_STALEMATE,
    Budget,
    DebateResult,
    Decision,
    Ledger,
)

#: 同一项分歧最多被"定向加轮"针对几次。
#:
#: 超过就不再给它排轮次 —— 否则会出现"解决了 A 冒出 B，解决了 B 又冒出 A"的无限循环。
#: 见 docs/consensus.md §5.1。
MAX_FOCUS_PER_ITEM = 2

#: ``force_stop`` 的原因码。写进文档时会翻译成中文说明。
REASON_BUDGET = "budget_exhausted"
REASON_MAX_ROUNDS = "max_rounds"
REASON_STAGNATION = "stagnation"
REASON_DEADLOCK = "deadlock"

_REASON_TEXT = {
    REASON_BUDGET: "token 预算用尽",
    REASON_MAX_ROUNDS: "达到轮数上限",
    REASON_STAGNATION: "本轮与上一轮的立场、开合分歧完全一致（原地打转）",
    REASON_DEADLOCK: "剩余分歧都已被定向加轮到达上限，再辩也不会有新东西",
}


def decide(
    *,
    ledger: Ledger,
    round_no: int,
    budget: Budget,
    max_rounds: int,
    stagnation: bool = False,
) -> Decision:
    """收敛判定。**纯函数**：同样的输入必然给出同样的输出。

    判断顺序是规格的一部分，不许调整（见模块文档的禁令 3）。
    """
    pending = ledger.unresolved

    # 1. 真达成 —— 没有 open，也没有 stalemate。
    #    必须排在预算与轮数之前：最后一轮刚好谈拢，不该被判成"没收敛"。
    if not pending:
        return Decision(
            kind=DECISION_DONE,
            reason="all_settled",
            detail=(
                f"本轮结束时已无未决分歧（台账共 {len(ledger.items)} 项："
                f"已解决 {len(ledger.resolved)}、取舍 {len(ledger.tradeoffs)}）"
            ),
        )

    # 2. 预算硬闸门
    if budget.exhausted:
        return _force_stop(REASON_BUDGET, pending, budget=budget)

    # 3. 轮数硬闸门
    if round_no >= max_rounds:
        return _force_stop(REASON_MAX_ROUNDS, pending, round_no=round_no, max_rounds=max_rounds)

    # 4. 原地打转：本轮与上轮的（立场、关闭、新开）完全一致
    if stagnation:
        return _force_stop(REASON_STAGNATION, pending)

    # 5. 剩下的分歧都已经用满定向加轮额度 —— 再辩也不会出新东西
    focusable = [
        item
        for item in pending
        if ledger.focus_counts.get(item.id, 0) < MAX_FOCUS_PER_ITEM
    ]
    if not focusable:
        return _force_stop(REASON_DEADLOCK, pending)

    # 6. 定向加轮：只召集这些分歧的当事双方
    subjects = "、".join(f"{item.id}（{item.topic}）" for item in focusable)
    return Decision(
        kind=DECISION_ANOTHER_ROUND,
        reason="pending_disagreements",
        focus=tuple(item.id for item in focusable),
        detail=f"还有 {len(focusable)} 项未决，定向加轮：{subjects}",
    )


def _force_stop(reason: str, pending: list, **extra: object) -> Decision:
    """构造一次"没聊完但必须停"。**这不是失败** —— 见 docs/consensus.md §4.2。"""
    subjects = "、".join(f"{item.id}（{item.topic}）" for item in pending)
    return Decision(
        kind=DECISION_FORCE_STOP,
        reason=reason,
        focus=tuple(item.id for item in pending),
        detail=(
            f"{_REASON_TEXT[reason]}，仍有 {len(pending)} 项未决：{subjects}。"
            f"这些项会原样进入文档的「未决问题」一节，交给人类决策。"
        ),
    )


def finalize(ledger: Ledger, *, round_no: int) -> list:
    """收口。返回被标为僵局的条目，供调用方发事件。"""
    """收口：把仍然 ``open`` 的分歧如实标为 ``stalemate``。

    这是本模块**唯一**会改状态的地方，刻意与 ``decide`` 分开：
    ``decide`` 必须保持纯函数，而收口是协议动作。

    为什么是标成 ``stalemate`` 而不是"强制降级为 tradeoff"：

    ``tradeoff`` 要求写出「什么条件下该反过来选」。这个条件必须由**模型**
    在讨论中给出来（定向加轮的第三条选项）。如果到收口时还没有任何一方提出过，
    那就意味着**这个条件不存在或没被想出来** —— 此时由代码硬填一句
    "视情况而定" 是在编造，比诚实地标为未决有害得多。

    所以：有模型提出的取舍方案 → ``tradeoff``（由 debate.py 在做决定时登记）；
    没有 → ``stalemate``，进文档的「未决问题」交给人类。
    """
    changed = []
    for item in ledger.items:
        if item.status == STATUS_OPEN:
            item.status = STATUS_STALEMATE
            item.history.append(
                {
                    "round": round_no,
                    "summary": "讨论结束仍未达成一致，如实记为未决问题",
                }
            )
            changed.append(item)
    return changed


def is_harmonious(ledger: Ledger, result: DebateResult) -> bool:
    """判断本次讨论是否**根本没吵起来**。

    零分歧是**失败信号**而不是顺利信号：说明人设同质化，或者问题约束强到
    只有一条路。它必须被显式暴露，而不是让用户以为"大家一致同意"。
    见 docs/consensus.md §6。
    """
    del result  # 目前只需看台账；保留参数是为了将来把"发言是否雷同"也纳入判定
    return len(ledger.items) == 0


def describe(ledger: Ledger) -> str:
    """给终端与附录 C 用的一行台账摘要。

    「未决」= ``open`` + ``stalemate`` —— 两者都是**没谈拢**，
    区别只在"是否已被判定为僵局"。终端渲染与文档章节必须用同一个口径，
    否则读者会看到两个对不上的总数。
    """
    digest = ledger.digest()
    unresolved = digest["open"] + digest["stalemate"]
    text = (
        f"分歧 {digest['total']} 项："
        f"已解决 {digest['resolved']}、取舍 {digest['tradeoff']}、未决 {unresolved}"
    )
    if digest["stalemate"]:
        text += f"（其中僵局 {digest['stalemate']}）"
    return text


__all__ = [
    "MAX_FOCUS_PER_ITEM",
    "REASON_BUDGET",
    "REASON_MAX_ROUNDS",
    "REASON_STAGNATION",
    "REASON_DEADLOCK",
    "decide",
    "finalize",
    "is_harmonious",
    "describe",
    "STATUS_OPEN",
    "STATUS_STALEMATE",
]
