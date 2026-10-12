"""事件模型与内存总线。

职责
    定义全部事件类型，并提供发布/订阅总线。
    这是编排层与表现层之间**唯一**的通信方式。

输入
    ``Event`` 实例

输出
    同步回调。订阅者可以有多个（终端渲染 + M3 的 SSE 服务）。

依赖
    仅标准库 ``dataclasses`` / ``json``。不依赖 ``llm`` / ``config`` 等基础层模块。

失败模式
    未知事件类型 → **抛 ``ValueError``**，不静默丢弃。
    （静默丢弃会让前端永久卡在某个状态，是最难查的一类 bug。）

    订阅者回调抛异常 → 记录后向上传递，不吞掉。
    （表现层出错必须被看见，否则用户看到一个安静但错误的界面。）

设计要点
    SSE（M3）直接复用这套结构，不维护第二套协议 —— 见 docs/web-ui.md §2.1。
    事件**不得**携带密钥、base_url 或完整 prompt。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import now_iso

# ─────────────────────────── 事件类型 ───────────────────────────
# 完整清单见 docs/architecture.md §5。M1 只发出其中一部分，
# 但类型在这里一次定义齐全 —— 它是协议，不是 M1 的私有约定。

SESSION_START = "session.start"
SESSION_END = "session.end"

BRIEF_READY = "brief.ready"
PANEL_READY = "panel.ready"
PERSONA_ASSIGNED = "persona.assigned"

RECON_START = "recon.start"
RECON_MEMO = "recon.memo"
RECON_FAILED = "recon.failed"

ROUND_START = "round.start"
TURN_START = "turn.start"
TURN_TOKEN = "turn.token"
TURN_END = "turn.end"
ROUND_END = "round.end"

DISAGREEMENT_OPEN = "disagreement.open"
DISAGREEMENT_UPDATE = "disagreement.update"
DISAGREEMENT_CLOSE = "disagreement.close"

CONVERGENCE_DECISION = "convergence.decision"

SYNTHESIS_START = "synthesis.start"
DOC_READY = "doc.ready"

WARNING = "warning"
ERROR = "error"

#: 全部合法事件类型。发布未登记的类型即视为程序错误。
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    SESSION_START, SESSION_END,
    BRIEF_READY, PANEL_READY, PERSONA_ASSIGNED,
    RECON_START, RECON_MEMO, RECON_FAILED,
    ROUND_START, TURN_START, TURN_TOKEN, TURN_END, ROUND_END,
    DISAGREEMENT_OPEN, DISAGREEMENT_UPDATE, DISAGREEMENT_CLOSE,
    CONVERGENCE_DECISION,
    SYNTHESIS_START, DOC_READY,
    WARNING, ERROR,
})

#: **不落盘**的事件类型：高频、且对审计与回放没有价值。
#:
#: ``turn.token`` 一次发言就能产生上千条（实测一次两句话的回答有 266 个分片，
#: 而一整个发言的 JSON 更长）。写进 ``transcript.jsonl`` 会让这份
#: 「事件溯源日志」彻底不可用 —— 它的用途是**审计与回放**，
#: 而逐 token 的增量对这两者都没有价值：完整正文已经在 ``turn.end`` 里了。
#:
#: 这些事件**照常广播**给订阅者（网页靠它们做打字机效果），只是不写盘。
EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset({TURN_TOKEN})


@dataclass
class Event:
    """总线上的一条事件。

    ``seq`` 由会话分配，单调递增，是 transcript 里的行号基准。
    """

    type: str
    session_id: str
    seq: int
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "session_id": self.session_id,
            "data": self.data,
        }

    def to_json_line(self) -> str:
        """渲染成 transcript.jsonl 的一行。

        ``ensure_ascii=False`` 是刻意的：中文直接落盘，日志要人能读。
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)


Subscriber = Callable[[Event], None]


class EventBus:
    """同步发布/订阅总线。

    刻意不做异步：编排流程需要"事件已送达"的确定性顺序，
    异步总线会让 transcript 的行序与实际发生顺序脱钩。
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, event: Event) -> None:
        if event.type not in KNOWN_EVENT_TYPES:
            raise ValueError(
                f"未登记的事件类型：{event.type!r}。"
                f"新增事件类型时必须同时加入 KNOWN_EVENT_TYPES 与 "
                f"docs/architecture.md §5 的事件表"
            )
        for subscriber in self._subscribers:
            # 不捕获订阅者的异常：表现层出错必须向上暴露。
            subscriber(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
