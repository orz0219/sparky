"""会话状态机与事件溯源持久化。

职责
    · **在流水线开跑之前**就建好会话目录并写入 ``meta.json``
    · 把每个事件以 JSONL 追加写入 ``transcript.jsonl``（**先落盘，再广播**）
    · 维护本次会话的内存态（Brief / Panel / ReconOutcome / Usage）
    · 为每次 LLM 调用保存上下文快照（``llm-calls/``）

输入
    事件 + 会话根目录 + LLM 请求体

输出
    磁盘：``meta.json`` · ``transcript.jsonl`` · ``brief.json`` · ``personas.json``
          · ``stances/`` · ``llm-calls/`` · ``design.md``
    内存：``Session`` 实例本身

依赖
    仅标准库 ``json`` / ``pathlib`` / ``re``，以及本包的 ``events`` / ``models``。

失败模式
    目录不可写 / JSON 序列化失败 → 抛 ``PersistenceError``，退出码 4。
    transcript 追加失败 → 抛 ``PersistenceError``（不继续跑：
    磁盘上记不下来的会话，跑完也无法审计）。

设计要点
    **调用快照必须在调用 LLM 之前落盘。** 若写在调用之后，一旦这次调用失败，
    你手上恰恰没有"这次喂了什么"的记录 —— 而失败时最需要的就是这个。
    代价是失败也会留下一条 ``-request.json``，这个代价值得付。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import PersistenceError
from .events import EPHEMERAL_EVENT_TYPES, Event, EventBus
from .models import Brief, Panel, ReconOutcome, Usage, now_iso

#: 会话目录名里 slug 的最大长度。太长会让路径难以阅读与补全。
SLUG_MAX_LEN = 24

#: 看起来像机密的键名。落盘前一律替换掉 —— 这是防止密钥泄漏的最后一道闸门。
#: 见 docs/configuration.md §6 已知坑 8。
#:
#: 刻意**不**包含裸的 ``token``：用量统计里的 prompt_tokens / completion_tokens
#: / reasoning_tokens / total_tokens 全都会命中，把有用的计量数据擦成
#: ``<已擦除>``，附录 D 就废了。只认那些确实承载凭证的名字。
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|authorization|bearer|secret|password|passwd|credential"
    r"|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token)",
    re.I,
)

#: 这些名字看起来像机密但其实不是。与上面配合使用。
_NOT_SECRET_KEY_PATTERN = re.compile(
    r"^(prompt|completion|reasoning|cached|total)?_?tokens?$", re.I
)

_SLUG_STRIP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def slugify(text: str, *, max_len: int = SLUG_MAX_LEN) -> str:
    """把用户输入压成一个可用作目录名的短 slug。

    保留 ASCII 字母数字与 CJK 汉字，其余字符折叠成 ``-``。
    全是标点等无法成 slug 的输入 → 返回 ``"session"``。
    """
    slug = _SLUG_STRIP.sub("-", text).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return "session"
    return slug[:max_len].strip("-") or "session"


def redact(payload: Any) -> Any:
    """递归替换掉 payload 中疑似机密的字段值。

    这是一道**安全闸门**，不是兜底逻辑：它不掩盖任何缺失的前置条件，
    只确保密钥不会被写进可以被分享的会话目录。

    倾向是"宁可多擦"：``api_key_env`` 这种只是变量名、并不含真值的字段
    也会被擦掉。少一条诊断信息可以接受，漏一次密钥泄漏不可以。
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if (
                isinstance(key, str)
                and _SECRET_KEY_PATTERN.search(key)
                and not _NOT_SECRET_KEY_PATTERN.match(key)
            ):
                out[key] = "<已擦除>"
            else:
                out[key] = redact(value)
        return out
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


@dataclass
class Session:
    """一次头脑风暴会议。

    实例同时是持久化句柄与内存态容器。
    """

    session_id: str
    root: Path
    bus: EventBus
    started_at: str = field(default_factory=now_iso)

    # 内存态：随流水线推进逐步填充
    brief: Brief | None = None
    panel: Panel | None = None
    recon: ReconOutcome | None = None
    #: 讨论轮次（每轮是一个 Turn 列表）。定稿阶段据此判断是否要插入
    #: 「本次未经交锋检验」的告警横幅。
    rounds: list[Any] = field(default_factory=list)
    #: S3 产出的分歧台账 —— 定稿阶段的 §5 取舍台账与 §8 未决问题的唯一来源。
    ledger: Any | None = None
    #: S3 的完整结果（台账 + 逐轮发言 + 最终判定）
    debate: Any | None = None
    usage: Usage = field(default_factory=Usage)
    doc_path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    # 内部计数
    _seq: int = 0
    _call_index: int = 0

    #: S2 阶段 N 人并发会同时改 _seq / _call_index / usage / transcript，
    #: 且 `emit` 必须保证"一行 JSONL 完整写入"。这把锁是**正确性要求**，
    #: 不是性能优化。可重入，因为 emit → warn 会嵌套。
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ───────────────────── 构造 ─────────────────────

    @classmethod
    def create(
        cls,
        out_root: Path,
        raw_input: str,
        *,
        bus: EventBus,
        config_snapshot: dict[str, Any],
    ) -> "Session":
        """建目录、写 ``meta.json``，然后才返回。

        **顺序很重要**：目录与 meta 必须在流水线开跑前落地，
        这样即使进程在第 3 秒崩掉，磁盘上也留着"这次想干什么"。
        """
        started = now_iso()
        # 会话 id 形如 20261012-103000-podcast，可排序、可读、可补全。
        # 用 strftime 而不是对 ISO 串做字符替换 —— 后者拼出来的格式不可控。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_id = f"{stamp}-{slugify(raw_input)}"
        root = out_root / session_id

        try:
            if root.exists():
                raise PersistenceError(
                    f"会话目录已存在：{root}",
                    hint="同一秒内重复发起同一个输入时会撞名。请稍后重试。",
                )
            (root / "stances").mkdir(parents=True)
            (root / "llm-calls").mkdir(parents=True)
            (root / "transcript.jsonl").touch()
        except OSError as exc:
            raise PersistenceError(
                f"无法创建会话目录 {root}：{exc}",
                hint="检查目录是否可写、磁盘是否已满。",
            ) from exc

        session = cls(session_id=session_id, root=root, bus=bus, started_at=started)
        session.write_json(
            "meta.json",
            {
                "session_id": session_id,
                "raw_input": raw_input,
                "started_at": started,
                "sparky_version": _version(),
                "config": config_snapshot,
                "status": "running",
            },
        )
        return session

    # ───────────────────── 事件 ─────────────────────

    def emit(self, event_type: str, **data: Any) -> Event:
        """落盘 + 广播。**先落盘**：磁盘上的记录是权威副本。

        整个过程持锁：seq 分配与行写入必须是原子的，
        否则并发下 transcript 的行序会与实际发生顺序脱钩。

        **高频事件不落盘**（见 ``events.EPHEMERAL_EVENT_TYPES``）：
        ``turn.token`` 一次发言上千条，写进日志会让它彻底不可用。
        它们照常广播，只是跳过写盘 —— 这不是「静默丢弃」，
        而是**按用途分级**：审计日志收事件，不收逐 token 的增量。
        """
        with self._lock:
            self._seq += 1
            event = Event(
                type=event_type,
                session_id=self.session_id,
                seq=self._seq,
                ts=now_iso(),
                data=data,
            )
            if event_type not in EPHEMERAL_EVENT_TYPES:
                try:
                    with (self.root / "transcript.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(event.to_json_line() + "\n")
                except OSError as exc:
                    raise PersistenceError(
                        f"写入 transcript.jsonl 失败：{exc}",
                        hint="磁盘可能已满或目录被移除。会话已停止 —— 记不下来的会话无法审计。",
                    ) from exc
        # 在锁外广播：订阅者做的是终端输出，不应阻塞其他线程的事件落盘
        self.bus.publish(event)
        return event

    def warn(self, code: str, message: str) -> Event:
        """记一条告警。告警不是错误，会话继续，但用户必须看见。"""
        self.warnings.append(f"[{code}] {message}")
        return self.emit("warning", code=code, message=message)

    # ───────────────────── JSON 落盘 ─────────────────────

    def write_json(self, relpath: str, payload: Any) -> Path:
        path = self.root / relpath
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(redact(payload), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError) as exc:
            raise PersistenceError(f"写入 {path} 失败：{exc}") from exc
        return path

    def write_text(self, relpath: str, text: str) -> Path:
        path = self.root / relpath
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise PersistenceError(f"写入 {path} 失败：{exc}") from exc
        return path

    def set_status(self, status: str) -> None:
        """更新 meta.json 的 status 字段（running / completed / failed / interrupted）。"""
        path = self.root / "meta.json"
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"读取 {path} 失败，无法更新会话状态：{exc}") from exc
        meta["status"] = status
        meta["finished_at"] = now_iso()
        self.write_json("meta.json", meta)

    # ───────────────────── LLM 调用快照 ─────────────────────

    def begin_llm_call(self, payload: dict[str, Any], *, purpose: str) -> int:
        """在调用**之前**保存请求快照，返回本次调用的编号。

        编号从 1 开始，四位补零，与 ``llm-calls/`` 下的文件名一致。
        调用方必须把这个编号一路带到 ``finish_llm_call``，
        **不能**回头读 ``call_count`` —— 并发下它可能已经被别的线程推进了。
        """
        with self._lock:
            self._call_index += 1
            index = self._call_index
            self.write_json(
                f"llm-calls/{index:04d}-request.json",
                {"purpose": purpose, "at": now_iso(), "payload": redact(payload)},
            )
        return index

    def finish_llm_call(self, index: int, *, content: str, usage: dict[str, Any]) -> None:
        """保存响应正文与用量。失败路径不调用它 —— 但 request 快照已经在盘上了。"""
        self.write_text(f"llm-calls/{index:04d}-response.txt", content)
        self.write_json(f"llm-calls/{index:04d}-usage.json", usage)

    def add_usage(self, usage: Usage) -> None:
        """累计用量。并发调用，必须持锁。"""
        with self._lock:
            self.usage.add(usage)

    @property
    def call_count(self) -> int:
        return self._call_index

    @property
    def event_count(self) -> int:
        return self._seq


def _version() -> str:
    from . import __version__

    return __version__


__all__ = ["Session", "slugify", "redact"]
