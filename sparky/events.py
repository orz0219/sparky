"""事件基础设施：事件模型、发布/订阅总线、扇出中心、插话队列。

职责
    定义全部事件类型，并提供三层与事件有关的基础设施：
      · ``EventBus``          —— 同步发布/订阅，编排层与表现层之间**唯一**的通信方式
      · ``EventHub``          —— 把一个会话的事件扇出给 N 个 SSE 客户端，并留历史快照
      · ``InterjectionQueue`` —— 委托人的插话队列（``client.interjection*`` 两个事件的载体）

输入
    ``Event`` 实例；``InterjectionQueue`` 额外收用户输入的文本

输出
    同步回调。订阅者可以有多个（终端渲染 + 网页）。

依赖
    仅标准库 ``dataclasses`` / ``json`` / ``queue`` / ``threading``。
    不依赖 ``llm`` / ``config`` / ``server`` 等其它模块。

失败模式
    未知事件类型 → **抛 ``ValueError``**，不静默丢弃。
    （静默丢弃会让前端永久卡在某个状态，是最难查的一类 bug。）

    订阅者回调抛异常 → 记录后向上传递，不吞掉。
    （表现层出错必须被看见，否则用户看到一个安静但错误的界面。）

设计要点
    SSE（M3）直接复用这套结构，不维护第二套协议 —— 见 docs/web-ui.md §2.1。
    事件**不得**携带密钥、base_url 或完整 prompt。

    ``EventHub`` 与 ``InterjectionQueue`` 原本长在 ``server.py`` 里，
    M3.2 挪到了这里。理由不是"整理文件"：网页工作台要能**同时**管理多个会话，
    每个会话一套扇出与插话队列，而工作台属于编排层、
    **不许 import ``server``**（docs/architecture.md §2 禁令 1）。
    两者本来就不含任何 HTTP 概念 —— 挪进 ``events`` 让那条禁令保持绝对。
"""

from __future__ import annotations

import json
import queue
import threading
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

#: 委托人（用户）在讨论进行中插话。**收到即发**，界面立刻可见。
CLIENT_INTERJECTION = "client.interjection"
#: 插话被送进某一轮的提示词。插话**在轮与轮之间送达** ——
#: 见 docs/web-ui.md「为什么不是立即打断」。
CLIENT_INTERJECTION_DELIVERED = "client.interjection.delivered"

WARNING = "warning"
#: 会话级失败。**刻意不叫 ``error``** ——
#: SSE 的 ``event:`` 名与 ``EventSource`` 上的 DOM 事件同名同姓，
#: 而 ``error`` 正是 EventSource 内建的连接失败事件。叫 ``error`` 时，
#: 前端注册的 ``addEventListener("error", …)`` 会同时收到**两种**东西：
#: 服务端发来的业务错误（带 data），和浏览器自己发的连接错误（不带 data）。
#: 后者会让 ``JSON.parse(undefined)`` 抛异常，把真正的错误盖掉。
ERROR = "session.error"

#: 全部合法事件类型。发布未登记的类型即视为程序错误。
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({
    SESSION_START, SESSION_END,
    BRIEF_READY, PANEL_READY, PERSONA_ASSIGNED,
    RECON_START, RECON_MEMO, RECON_FAILED,
    ROUND_START, TURN_START, TURN_TOKEN, TURN_END, ROUND_END,
    DISAGREEMENT_OPEN, DISAGREEMENT_UPDATE, DISAGREEMENT_CLOSE,
    CONVERGENCE_DECISION,
    SYNTHESIS_START, DOC_READY,
    CLIENT_INTERJECTION, CLIENT_INTERJECTION_DELIVERED,
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


class InterjectionQueue:
    """委托人的插话队列。

    **收到即记，轮间送达。** 用户随时可以打字，但插话只在**轮与轮之间**
    被送进提示词 —— 中途打断一轮会让并发中的几位专家看到不同的世界，
    破坏"轮内只读上轮快照"这条不变量（见 docs/consensus.md §2.1）。
    所以停止与插话的语义都是"**当前轮跑完再生效**"，不是"立刻掐断"。

    线程安全：HTTP 处理线程写、讨论线程读。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._all: list[dict] = []

    def add(self, text: str, *, received_round: int) -> dict:
        record = {"text": text, "received_round": received_round, "delivered_round": None}
        with self._lock:
            self._pending.append(record)
            self._all.append(record)
        return record

    def drain(self) -> list[dict]:
        """取出待送达的插话并清空待送队列。"""
        with self._lock:
            drained, self._pending = self._pending, []
        return drained

    @property
    def all(self) -> list[dict]:
        with self._lock:
            return list(self._all)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class EventHub:
    """线程安全的扇出：每个 SSE 客户端一个队列，并保留一份历史快照。

    它同时是 ``EventBus`` 的一个订阅者 —— 编排层只管发事件，
    谁在听（终端渲染、网页）不关它的事。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[queue.Queue[Event]] = []
        #: 已广播过的**非临时**事件，供新连上来的页面补发。
        self._history: list[Event] = []
        self._closed = False

    def publish(self, event: Event) -> None:
        with self._lock:
            if event.type not in EPHEMERAL_EVENT_TYPES:
                self._history.append(event)
            queues = list(self._queues)
        # 在锁外投递：客户端队列满时不该阻塞事件发布
        for target in queues:
            target.put(event)

    def subscribe(self) -> tuple[queue.Queue[Event], list[Event]]:
        """订阅，返回 ``(实时队列, 历史快照)``。

        两步在**同一个锁内**完成 —— 否则快照与实时流之间会漏掉事件。
        """
        with self._lock:
            target: queue.Queue[Event] = queue.Queue()
            self._queues.append(target)
            return target, list(self._history)

    def unsubscribe(self, target: queue.Queue[Event]) -> None:
        with self._lock:
            if target in self._queues:
                self._queues.remove(target)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._queues)


__all__ = [
    "Event",
    "EventBus",
    "EventHub",
    "InterjectionQueue",
    "KNOWN_EVENT_TYPES",
    "EPHEMERAL_EVENT_TYPES",
]
