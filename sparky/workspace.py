"""提案工作台：把「一次头脑风暴」当成一件可管理的**提案**。

职责
    1. **读**：扫描 ``--out`` 目录，把盘上的会话汇总成提案清单与详情
       （状态、规模、分歧、产出文档）。
    2. **写**：在网页上发起一份新提案 —— 建会话、起后台线程、跑流水线。
    3. **控**：停止、插话、Ctrl-C 收口。

    一个「提案」在磁盘上就是**一个会话目录**（``session.Session``）。
    UI 里叫「提案」是因为用户发起它时提交的是一份想法；
    代码里仍叫 ``session``，因为 ``transcript.jsonl`` / ``meta.json``
    这些既有结构都按会话组织。**两者是同一个东西，不另造一层**。

输入
    ``RuntimeConfig``（含密钥真值）+ 网页发来的提案参数

输出
    提案清单 / 详情（纯 dict，可直接 JSON 序列化）+ 运行句柄

依赖
    ``pipeline`` / ``session`` / ``events`` / ``config`` / ``errors`` / ``render``。
    **不得 import ``server``** —— 网页只是它的一个调用方
    （docs/architecture.md §2 禁令 1）。

失败模式
    ``--out`` 目录不存在 → 清单为空（**这不是错误**：还没发起过任何提案）。
    单个会话的 ``meta.json`` 损坏 → 那一条如实标成 ``broken`` 并把原因写进
    ``status_detail``，**不影响其余提案**。这不是兜底：损坏是已经发生的事实，
    隐藏它比显示它更糟，而让一条坏记录废掉整份清单同样不可接受。
    发起提案时参数越界 → 抛 ``UsageError``，HTTP 层回 400。
    读 transcript 遇到解析不了的行 → 抛 ``PersistenceError`` 并指出行号
    （**不跳过**：跳过会让界面显示一场缺了一段的历史，而用户无从知道）。

设计要点
    **运行态在内存里，历史态在磁盘上。** 只有本进程发起、且还没结束的提案
    才在 ``_handles`` 里有句柄；其余一律靠读盘。所以「进程重启后，之前
    进行中的提案」会被如实标成 ``stale``（进程已退出），而不是假装它还在跑。

    **看历史提案不重新跑模型。** 详情视图靠重放 ``transcript.jsonl``
    重建整场讨论 —— 这也是当初把事件全部落盘（且 ``turn.token`` 除外）的收益。

对应设计
    docs/web-ui.md §8 提案工作台 · docs/adr/0005-proposal-workspace.md
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import pipeline
from .config import RuntimeConfig
from .errors import EXIT_OK, EXIT_RUNTIME, PersistenceError, SparkyError, UsageError
from .events import (
    CLIENT_INTERJECTION,
    SESSION_END,
    Event,
    EventBus,
    EventHub,
    InterjectionQueue,
)
from .render import TerminalRenderer
from .session import Session

#: 网页上允许提交的轮数上限。与 ``debate.MAX_ROUND_SAFETY`` 无关 ——
#: 那是防死循环的硬闸门，这里是"别让人在表单里填 200"。
MAX_ROUNDS_FROM_WEB = 12

#: 网页上允许提交的 panel_size 范围。与 ``personas.PANEL_MIN/MAX`` 同源。
PANEL_MIN = 3
PANEL_MAX = 7


@dataclass
class RunHandle:
    """一次**正在进行**的运行的句柄。

    只有本进程发起的运行才有句柄。它的生命周期与进程绑定，
    **不落盘** —— 落盘的话，进程被杀之后盘上就会留下一个
    "看起来还在跑"的句柄，而实际上没有任何东西在跑。
    """

    session: Session
    hub: EventHub
    interjections: InterjectionQueue
    stop_event: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    #: 失败时的人话错误信息（已经渲染并打过终端，这里留一份给网页）。
    error: str | None = None
    #: 与 ``error`` 配套的进程退出码语义。0 表示正常结束。
    exit_code: int = EXIT_OK
    #: 已经请求中断（Ctrl-C）。线程可能还在收口，但界面上不该再显示"进行中"。
    stopping: bool = False

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def alive(self) -> bool:
        return not self.finished.is_set()


#: 流水线入口的签名。工作台只依赖这个形状，不依赖它的实现 ——
#: 测试可以塞一个不调模型的假流水线进来，验证线程生命周期与失败收口。
PipelineRunner = Callable[..., Path]


class Workspace:
    """提案清单 + 运行管理。

    线程安全：HTTP 处理线程读清单、发起运行；运行线程写会话状态。
    ``_handles`` 的增删与 ``list_proposals`` 的遍历都持 ``_lock``。
    """

    def __init__(
        self,
        *,
        runtime: RuntimeConfig,
        renderer: TerminalRenderer,
        run_pipeline: PipelineRunner = pipeline.run,
    ) -> None:
        self._runtime = runtime
        self._renderer = renderer
        #: 流水线入口。默认就是真的那条；测试传假的进来即可，
        #: 不必去 mock 模块属性 —— 显式注入比打补丁更好读，也更难写错。
        self._run_pipeline = run_pipeline
        self._out_root = Path(runtime.out_root)
        self._lock = threading.RLock()
        self._handles: dict[str, RunHandle] = {}

    # ───────────────────── 读：清单与详情 ─────────────────────

    @property
    def out_root(self) -> Path:
        return self._out_root

    def list_proposals(self) -> list[dict[str, Any]]:
        """全部提案，按开始时间**倒序**（最近的排最前）。

        ``--out`` 不存在时返回空清单：还没发起过任何提案，这不是错误。
        """
        if not self._out_root.is_dir():
            return []
        entries = [
            self._describe(child)
            for child in sorted(self._out_root.iterdir())
            if child.is_dir()
        ]
        # 会话 id 形如 20261012-103000-slug，字典序即时间序 —— 倒序即可。
        entries.sort(key=lambda item: item["session_id"], reverse=True)
        return entries

    def get_proposal(self, session_id: str) -> dict[str, Any] | None:
        """单个提案的详情。目录不存在 → ``None``（HTTP 层回 404）。"""
        root = self._proposal_root(session_id)
        if root is None:
            return None
        return self._describe(root)

    def read_transcript(self, session_id: str) -> list[Event]:
        """重放 ``transcript.jsonl``，返回按 ``seq`` 递增的事件列表。

        这是**看历史提案不重新跑模型**的实现方式。临时事件（``turn.token``）
        本来就没落盘，所以重放出来的是一场没有打字机动画的讨论 ——
        完整正文在 ``turn.end`` 里，一个字都不少。
        """
        root = self._proposal_root(session_id)
        if root is None:
            raise PersistenceError(f"没有这个提案：{session_id}")
        path = root / "transcript.jsonl"
        if not path.is_file():
            raise PersistenceError(
                f"{path} 不存在，无法重放这场讨论。",
                hint="会话目录可能在写入 meta.json 之后、开跑之前就被中断了。",
            )
        events: list[Event] = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PersistenceError(
                    f"{path}:{lineno} 不是合法 JSON：{exc}",
                    hint=(
                        "进程被强杀时可能留下半行。这一行之后的事件无法恢复，"
                        "所以这里**不跳过** —— 跳过会让你看到一场缺了一段的历史。"
                    ),
                ) from exc
            missing = {"type", "session_id", "seq", "data", "ts"} - set(
                payload if isinstance(payload, dict) else ()
            )
            if missing:
                raise PersistenceError(
                    f"{path}:{lineno} 的事件缺少字段 {sorted(missing)}：{raw[:120]}",
                    hint="transcript.jsonl 只应由 session.emit() 写入；这一行不是它写的。",
                )
            events.append(
                Event(
                    type=payload["type"],
                    session_id=payload["session_id"],
                    seq=payload["seq"],
                    data=payload["data"],
                    ts=payload["ts"],
                )
            )
        return events

    def read_design_doc(self, session_id: str) -> str | None:
        """读最终产出的 ``design.md``。还没定稿 → ``None``。"""
        root = self._proposal_root(session_id)
        if root is None:
            raise PersistenceError(f"没有这个提案：{session_id}")
        path = root / "design.md"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def subscribe(
        self, session_id: str
    ) -> tuple[queue.Queue[Event], list[Event]] | None:
        """订阅一个提案的事件流：``(实时队列, 历史快照)``。

        正在跑的提案 → 走它的 ``EventHub``（历史 + 实时）。
        已经结束的提案 → 用重放出来的事件充当快照，实时队列永远不会来东西
        （SSE 层看到快照里有 ``session.end`` 就收口）。

        没有这个提案 → ``None``。
        """
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is not None:
            return handle.hub.subscribe()
        if self._proposal_root(session_id) is None:
            return None
        return queue.Queue(), self.read_transcript(session_id)

    def is_live(self, session_id: str) -> bool:
        """这个提案是否**正在本进程里跑**。

        SSE 层靠它决定"补完历史要不要继续挂着连接"：已结束的提案
        再挂下去只会让页面显示一个永远不动的"连接中"。
        """
        with self._lock:
            handle = self._handles.get(session_id)
        return handle is not None and handle.alive

    def unsubscribe(self, session_id: str, target: queue.Queue[Event]) -> None:
        """退订。只有走 ``EventHub`` 的实时订阅需要退；
        重放历史的订阅本来就没在任何 hub 里登记过，无事可做。"""
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is not None:
            handle.hub.unsubscribe(target)

    # ───────────────────── 写：发起一份提案 ─────────────────────

    def create_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按网页表单发起一份新提案，立刻返回，运行在后台线程里。

        参数越界 → ``UsageError``（HTTP 层回 400），**不静默取默认值**。
        表单留空的项回落到启动工作台时那份配置（例如 ``--panel-size``），
        而不是回落到内置默认 —— 否则命令行显式给的值会被表单悄悄盖掉。
        """
        idea = str(payload.get("idea", "")).strip()
        if not idea:
            raise UsageError("想法不能为空。")

        panel_size = _optional_int(payload, "panel_size", PANEL_MIN, PANEL_MAX)
        max_rounds = _optional_int(payload, "max_rounds", 1, MAX_ROUNDS_FROM_WEB)
        budget_tokens = _optional_int(payload, "budget_tokens", 1000, 10**9)

        runtime = replace(
            self._runtime,
            raw_input=idea,
            panel_size=panel_size if panel_size is not None else self._runtime.panel_size,
            max_rounds=max_rounds if max_rounds is not None else self._runtime.max_rounds,
            budget_tokens=(
                budget_tokens if budget_tokens is not None else self._runtime.budget_tokens
            ),
        )
        handle = self.start(runtime)
        return self._describe(handle.session.root)

    def start(self, runtime: RuntimeConfig) -> RunHandle:
        """建会话并起后台线程。返回句柄；**不等待**它跑完。

        会话目录与 ``meta.json`` 在 ``Session.create`` 里**先落盘**，
        所以即使进程在第 3 秒崩掉，清单里也能看到"这次想干什么"。
        """
        bus = EventBus()
        bus.subscribe(self._renderer)
        hub = EventHub()
        bus.subscribe(hub.publish)

        session = Session.create(
            runtime.out_root,
            runtime.raw_input,
            bus=bus,
            config_snapshot=pipeline.config_snapshot(runtime),
        )
        handle = RunHandle(
            session=session,
            hub=hub,
            interjections=InterjectionQueue(),
        )
        with self._lock:
            self._handles[session.session_id] = handle

        thread = threading.Thread(
            target=self._run,
            args=(handle, runtime),
            name=f"sparky-run-{session.session_id}",
            daemon=True,
        )
        handle.thread = thread
        thread.start()
        return handle

    def stop_proposal(self, session_id: str) -> bool:
        """请求优雅停止：**当前轮跑完**才进入定稿（见 docs/web-ui.md §4.2）。

        返回 ``False`` 表示这个提案不在本进程里跑 —— 可能已经结束，
        也可能是上一个进程留下的。两种情况都无法停止，如实返回。
        """
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is None or not handle.alive:
            return False
        handle.stop_event.set()
        return True

    def interject(self, session_id: str, text: str) -> bool:
        """委托人插话：收到即记、即广播；**送达要等本轮结束**。

        返回 ``False`` 表示当前不接受（提案没在跑，或已经定稿），
        HTTP 层回 409 —— 不静默丢弃用户写的东西。
        """
        with self._lock:
            handle = self._handles.get(session_id)
        if handle is None or not handle.alive or handle.session.doc_path is not None:
            return False
        # 队列与会话记录共享**同一个 dict 对象** —— debate 填 delivered_round 时
        # 两处同步更新，不会分叉成两份真相。
        record = handle.interjections.add(
            text, received_round=handle.session.current_round
        )
        handle.session.interjections.append(record)
        handle.session.emit(
            CLIENT_INTERJECTION,
            text=text,
            received_round=handle.session.current_round,
        )
        return True

    def interrupt_all(self) -> list[str]:
        """Ctrl-C：把仍在跑的提案如实标成 ``interrupted``，返回它们的 id。

        **不等待**正在进行的 LLM 调用返回 —— 用户按了 Ctrl-C 就是要立刻退出，
        让他再等一轮讨论是错的。运行线程是 daemon，进程退出时一并结束。
        """
        interrupted: list[str] = []
        with self._lock:
            handles = list(self._handles.values())
        for handle in handles:
            if not handle.alive:
                continue
            handle.stop_event.set()
            handle.stopping = True
            try:
                handle.session.set_status("interrupted")
                handle.session.emit(
                    SESSION_END,
                    status="interrupted",
                    usage=handle.session.usage.to_dict(),
                )
                interrupted.append(handle.session_id)
            except SparkyError as exc:
                # 状态写不进去也要说出来 —— 用户需要知道哪一份提案不完整。
                self._renderer.fail(
                    f"{handle.session_id} 的中断状态没能写盘：{exc.message}"
                )
        return interrupted

    # ───────────────────── 内部 ─────────────────────

    def _run(self, handle: RunHandle, runtime: RuntimeConfig) -> None:
        """后台线程主体。**所有错误都在这里收口**，不向外抛。

        这样命令行与网页两种调用方都拿到同一份行为：
        失败会写进 ``meta.json`` + transcript、打到终端、并记进句柄。
        """
        try:
            self._run_pipeline(
                runtime=runtime,
                session=handle.session,
                renderer=self._renderer,
                interjections=handle.interjections,
                should_stop=handle.stop_event.is_set,
            )
        except SparkyError as exc:
            handle.error = exc.render()
            handle.exit_code = exc.exit_code
            pipeline.mark_failed(handle.session, exc)
            self._renderer.fail("")
            self._renderer.fail(exc.render())
        except Exception:  # noqa: BLE001 —— 未预期的异常是 bug，必须带回溯暴露
            handle.error = "遇到未预期的内部异常（这是 bug，不是你的用法问题）"
            handle.exit_code = EXIT_RUNTIME
            pipeline.mark_failed(handle.session, None)
            self._renderer.fail("")
            self._renderer.fail(f"错误：{handle.error}")
            traceback.print_exc()
        finally:
            handle.finished.set()

    def _proposal_root(self, session_id: str) -> Path | None:
        """把 session_id 解析成目录。**必须防目录穿越**：id 来自 URL。"""
        if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
            return None
        root = self._out_root / session_id
        return root if root.is_dir() else None

    def _describe(self, root: Path) -> dict[str, Any]:
        """把一个会话目录汇总成清单条目。**只读小文件**，不碰 transcript。"""
        session_id = root.name
        meta = _load_json(root / "meta.json")
        if meta is None:
            return {
                "session_id": session_id,
                "raw_input": "",
                "goal": "",
                "started_at": None,
                "finished_at": None,
                "status": "broken",
                "status_detail": "meta.json 缺失或不是合法 JSON",
                "live": False,
                "panel_size": None,
                "round_count": 0,
                "doc_chars": 0,
                "has_doc": False,
                "disagreements": None,
                "model": None,
                "max_rounds": None,
            }

        config = meta.get("config") or {}
        personas = _load_json(root / "personas.json")
        brief = _load_json(root / "brief.json")
        ledger = _load_json(root / "ledger.json")
        doc_path = root / "design.md"

        with self._lock:
            handle = self._handles.get(session_id)
        live = handle is not None and handle.alive
        stopping = handle is not None and handle.stopping
        status, detail = _derive_status(meta.get("status"), live=live, stopping=stopping)

        return {
            "session_id": session_id,
            "raw_input": meta.get("raw_input") or "",
            "goal": (brief or {}).get("goal") or meta.get("raw_input") or "",
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "status": status,
            "status_detail": detail,
            "live": live,
            "panel_size": (personas or {}).get("panel_size"),
            "round_count": _count_rounds(root),
            "doc_chars": len(doc_path.read_text(encoding="utf-8")) if doc_path.is_file() else 0,
            "has_doc": doc_path.is_file(),
            "disagreements": (ledger or {}).get("digest"),
            "model": config.get("model_strong"),
            "max_rounds": config.get("max_rounds"),
        }


def _derive_status(raw: Any, *, live: bool, stopping: bool) -> tuple[str, str]:
    """把 ``meta.json`` 里的状态归一成清单用的六种，并附一句人话。

    ``running`` 但本进程没有句柄 → ``stale``：**上一个进程已经不在了**。
    假装它还在跑（显示"进行中"）会让用户一直等一场永远不会结束的讨论。

    ``stopping`` 优先于 ``live``：已经按了 Ctrl-C / 停止，
    界面不该再说"进行中" —— 它只是在等当前轮跑完。
    """
    if stopping:
        return "interrupted", "已请求中断，正在收口"
    if live:
        return "running", "本进程正在跑"
    text = str(raw or "")
    if text == "running":
        return "stale", "发起它的进程已退出，讨论停在这里"
    if text == "completed":
        return "completed", "已完成"
    if text == "interrupted":
        return "interrupted", "被中断"
    if text.startswith("failed"):
        return "failed", text[len("failed: ") :] if ": " in text else "失败"
    return "unknown", text


def _count_rounds(root: Path) -> int:
    """数 ``rounds/`` 下有几个 ``r*.json``。

    刻意**不读** ``debate.json``：它内嵌了全部发言与台账，
    为了一个轮数去解析它是浪费 —— 清单每 3 秒被轮询一次。
    """
    rounds_dir = root / "rounds"
    if not rounds_dir.is_dir():
        return 0
    return sum(1 for item in rounds_dir.iterdir() if item.suffix == ".json")


def _load_json(path: Path) -> dict[str, Any] | None:
    """读一个小 JSON 文件。不存在 → ``None``；损坏 → ``None``（由调用方如实标注）。"""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _optional_int(payload: dict[str, Any], key: str, low: int, high: int) -> int | None:
    """从表单里取一个可选的整数参数，越界即报错。

    空串与 ``None`` 都表示"没填"，返回 ``None`` 让调用方回落到默认值 ——
    这是**显式的可选参数语义**，不是用默认值掩盖缺失的前置条件。
    """
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise UsageError(f"{key} 必须是整数，实际是 {raw!r}") from exc
    if not (low <= value <= high):
        raise UsageError(f"{key} 必须在 {low}–{high} 之间，实际是 {value}")
    return value


__all__ = [
    "Workspace",
    "RunHandle",
    "PipelineRunner",
    "PANEL_MIN",
    "PANEL_MAX",
    "MAX_ROUNDS_FROM_WEB",
]
