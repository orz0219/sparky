"""本地 HTTP 服务：提案 API + SSE 推送 + 静态资源。

职责
    用标准库 ``http.server.ThreadingHTTPServer`` 提供：
      · ``GET  /``                              → ``web/index.html``
      · ``GET  /app.js`` ``/style.css``         → 静态资源
      · ``GET  /api/proposals``                 → 提案清单（历史 + 进行中）
      · ``GET  /api/proposals/<id>``            → 单份提案的详情
      · ``GET  /api/proposals/<id>/doc``        → 最终文档，**渲染成 HTML**
      · ``POST /api/proposals``                 → 发起一份新提案
      · ``POST /api/proposals/<id>/stop``       → 请求优雅停止
      · ``POST /api/proposals/<id>/interject``  → 插话
      · ``GET  /events?session=<id>``           → SSE 事件流（先补历史，再进实时）

输入
    一个 ``ProposalApi``（结构上匹配，不 import ``workspace``）

输出
    HTTP 响应。**只监听 127.0.0.1** —— 这个工具没有任何鉴权，不该暴露到局域网。

依赖
    仅标准库 ``http.server`` / ``socketserver`` / ``queue`` / ``threading`` / ``json``。
    表现层**不得 import ``llm``**（docs/architecture.md §2 的分层禁令）。

失败模式
    端口占用 → 抛 ``ConfigError``，**报错退出并提示换端口**，绝不静默换端口
    （静默换端口会让用户对着旧地址刷新，问题更难查）。
    静态资源缺失 → 500 并写 stderr，**不返回空白页**。
    ``/events`` 缺 ``?session=`` → 400。**不做"自动挑一个会话"的兜底**：
    挑错了会让用户看着别人的提案以为是自己的。
    提案不存在 → 404；``session_id`` 含路径分隔符 → 当作不存在（防目录穿越）。
    编排层抛 ``SparkyError`` → 按其退出码映射成 400/500，并把 ``hint`` 一起返回，
    用户才能在页面上看到"下一步怎么办"。
    SSE 客户端断开 → 这是 SSE 唯一的正常收尾方式，不视为错误；
    只捕获 ``BrokenPipeError`` / ``ConnectionResetError`` 这两个具体类型。

设计要点
    **历史补发的去重靠 ``seq``**：订阅时先拿到历史快照与当时的 ``seq`` 水位，
    之后从队列里丢弃 ``seq`` 不高于水位的实时事件。
    这样刷新页面既不会丢事件、也不会把同一件事显示两遍。

    **已结束的提案不会挂住连接**：快照里出现 ``session.end``、
    或该提案在本进程里已经没有活的运行句柄，写完快照就收口。

对应设计
    docs/web-ui.md · docs/architecture.md §5
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from . import markdown
from .errors import (
    EXIT_CONFIG,
    EXIT_PERSISTENCE,
    EXIT_RUNTIME,
    EXIT_USAGE,
    ConfigError,
    SparkyError,
    UsageError,
)
from .events import EPHEMERAL_EVENT_TYPES, Event

#: SSE keepalive 间隔（秒）。中间有代理时不发会被判超时断开。
KEEPALIVE_SECONDS = 15.0

#: ``serve_forever`` 的轮询间隔（秒）。
#:
#: 它决定的是**关闭延迟**：``BaseServer.shutdown()`` 只是置一个标志，
#: 然后等 ``serve_forever`` 那一轮醒来。用默认的 0.5 秒时，
#: 每次退出（包括 Ctrl-C）都会白等半秒 —— 用户按了 Ctrl-C 却看到
#: 进程愣一下，是能感觉到的。
SERVE_POLL_SECONDS = 0.1

#: 静态资源目录。
WEB_DIR = Path(__file__).resolve().parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

#: 允许直接取用的静态资源白名单。刻意用白名单而不是"拼路径"——
#: 后者会让 ``/../`` 这类请求有机会读到仓库里的任何文件。
_STATIC_FILES = frozenset({"index.html", "app.js", "style.css"})

#: ``SparkyError.exit_code`` → HTTP 状态码。
_STATUS_BY_EXIT = {
    EXIT_USAGE: 400,
    EXIT_CONFIG: 500,
    EXIT_PERSISTENCE: 500,
    EXIT_RUNTIME: 500,
}


class ProposalApi(Protocol):
    """网页服务对编排层的**最小**依赖面。

    刻意用 ``Protocol`` 而不是 import ``workspace``：``server`` 是表现层，
    它不该知道提案是怎么跑起来的（docs/architecture.md §2 禁令 1）。
    副作用是测试可以直接塞一个假实现进来，不必起流水线。
    """

    def list_proposals(self) -> list[dict[str, Any]]: ...

    def get_proposal(self, session_id: str) -> dict[str, Any] | None: ...

    def create_proposal(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def stop_proposal(self, session_id: str) -> bool: ...

    def interject(self, session_id: str, text: str) -> bool: ...

    def is_live(self, session_id: str) -> bool: ...

    def unsubscribe(self, session_id: str, target: queue.Queue[Event]) -> None: ...

    def read_design_doc(self, session_id: str) -> str | None: ...

    def subscribe(
        self, session_id: str
    ) -> tuple[queue.Queue[Event], list[Event]] | None: ...


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sparky"
    protocol_version = "HTTP/1.1"

    # ── 日志：请求是噪音，错误必须可见 ──

    def log_message(self, fmt: str, *args: object) -> None:
        """默认实现会把每个请求打到 stderr。那是访问噪音，不是错误。"""
        return

    def log_error(self, fmt: str, *args: object) -> None:
        """真正的服务端错误必须可见 —— 见 AGENTS.md「禁止静默处理异常」。"""
        sys.stderr.write(f"[sparky web] {fmt % args}\n")

    # ── 路由 ──

    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定命名
        parsed = urlparse(self.path)
        # 先按 / 切分再逐个 unquote：整体 unquote 会把 %2F 变成 /，
        # 那样一个提案 id 就能伪造出额外的路径层级。
        segments = [unquote(part) for part in parsed.path.split("/") if part]

        if segments == ["events"]:
            self._serve_events(parse_qs(parsed.query))
            return

        try:
            if not segments:
                self._send_file("index.html")
            elif len(segments) == 1 and segments[0] in _STATIC_FILES:
                self._send_file(segments[0])
            elif segments[:2] == ["api", "proposals"]:
                self._get_proposals(segments[2:])
            else:
                self._send_text(404, f"没有这个地址。可用：/ · /app.js · /style.css · "
                                     f"/api/proposals · /events?session=<提案 id>")
        except SparkyError as exc:
            self._send_sparky_error(exc)
        except Exception as exc:  # noqa: BLE001 —— 未预期异常必须可见，不能变成静默 500
            self.log_error("处理 %s 时崩溃：%s", self.path, exc)
            traceback.print_exc()
            self._send_text(500, f"服务端内部错误：{exc}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            if segments == ["api", "proposals"]:
                self._create_proposal()
                return
            if len(segments) == 4 and segments[:2] == ["api", "proposals"]:
                session_id, action = segments[2], segments[3]
                if action == "stop":
                    self._stop_proposal(session_id)
                    return
                if action == "interject":
                    self._interject(session_id)
                    return
            self._send_text(
                404,
                "没有这个地址。可用：POST /api/proposals · "
                "POST /api/proposals/<id>/stop · POST /api/proposals/<id>/interject",
            )
        except SparkyError as exc:
            self._send_sparky_error(exc)
        except Exception as exc:  # noqa: BLE001
            self.log_error("处理 %s 时崩溃：%s", self.path, exc)
            traceback.print_exc()
            self._send_text(500, f"服务端内部错误：{exc}")

    # ── 提案 API ──

    def _get_proposals(self, rest: list[str]) -> None:
        api: ProposalApi = self.server.api  # type: ignore[attr-defined]
        if not rest:
            self._send_json(200, {"proposals": api.list_proposals()})
            return
        session_id = rest[0]
        if len(rest) == 1:
            proposal = api.get_proposal(session_id)
            if proposal is None:
                self._send_api_error(404, f"没有这个提案：{session_id}")
                return
            self._send_json(200, {"proposal": proposal})
            return
        if len(rest) == 2 and rest[1] == "doc":
            text = api.read_design_doc(session_id)
            if text is None:
                self._send_api_error(404, "这份提案还没有产出设计文档（可能还没定稿）。")
                return
            self._send_json(
                200,
                {
                    "chars": len(text),
                    "markdown": text,
                    "html": markdown.render(text),
                },
            )
            return
        self._send_api_error(404, f"没有这个地址：/api/proposals/{'/'.join(rest)}")

    def _create_proposal(self) -> None:
        api: ProposalApi = self.server.api  # type: ignore[attr-defined]
        payload = self._read_json_body()
        proposal = api.create_proposal(payload)
        self._send_json(201, {"proposal": proposal})

    def _stop_proposal(self, session_id: str) -> None:
        api: ProposalApi = self.server.api  # type: ignore[attr-defined]
        if not api.stop_proposal(session_id):
            self._send_api_error(
                409,
                "这份提案不在本进程里跑（已经结束，或是上一个进程留下的），停不了。",
            )
            return
        self._send_json(200, {"ok": True, "message": "已请求停止：当前轮结束后进入定稿"})

    def _interject(self, session_id: str) -> None:
        api: ProposalApi = self.server.api  # type: ignore[attr-defined]
        payload = self._read_json_body()
        text = str(payload.get("text", "")).strip()
        if not text:
            self._send_api_error(400, "插话内容不能为空。")
            return
        if not api.interject(session_id, text):
            self._send_api_error(409, "这份提案没在跑（或已经定稿），插话没有送达。")
            return
        self._send_json(200, {"ok": True, "message": "已收到：会在本轮结束时送达专家"})

    def _read_json_body(self) -> dict[str, Any]:
        """读并解析请求体。**不猜、不修补**：不是合法 JSON 就如实报错。"""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise UsageError("请求体是空的。")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise UsageError(f"请求体必须是 JSON 对象，实际是 {type(payload).__name__}")
        return payload

    # ── 静态资源 ──

    def _send_file(self, name: str) -> None:
        path = WEB_DIR / name
        if not path.is_file():
            # 不返回空白页：缺资源是安装不完整，必须看得见
            self.log_error("静态资源缺失：%s", path)
            self._send_text(500, f"静态资源缺失：{name}")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        # 开发期不缓存，避免改了前端刷新还是旧的
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_api_error(self, status: int, message: str) -> None:
        """``/api/*`` 的**所有**响应都必须是 JSON —— 成功和失败都一样。

        前端对每个响应都调 ``r.json()``。失败时回一段纯文本，
        前端会在解析那一步抛异常，于是**真正的原因被解析错误盖掉** ——
        用户看到"Unexpected token"，而不是"这份提案没在跑"。
        """
        self._send_json(status, {"message": message})

    def _send_sparky_error(self, exc: SparkyError) -> None:
        self._send_json(
            _STATUS_BY_EXIT.get(exc.exit_code, 500),
            {"message": exc.message, "hint": exc.hint},
        )

    # ── SSE ──

    def _serve_events(self, query: dict[str, list[str]]) -> None:
        """把一个提案的事件流推给浏览器。

        前置校验必须在**写响应头之前**做完 —— 头一旦发出去就只能靠断开连接报错。
        """
        api: ProposalApi = self.server.api  # type: ignore[attr-defined]
        try:
            session_id = (query.get("session") or [""])[0]
            if not session_id:
                self._send_text(
                    400,
                    "必须指定 ?session=<提案 id>。刻意不自动挑一个会话 —— "
                    "挑错了你会看着别人的提案以为是自己发起的。",
                )
                return
            subscription = api.subscribe(session_id)
            if subscription is None:
                self._send_text(404, f"没有这个提案：{session_id}")
                return
            live = api.is_live(session_id)
        except SparkyError as exc:
            self._send_sparky_error(exc)
            return

        target, snapshot = subscription
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        # SSE 没有 Content-Length，**必须**显式声明收尾方式：
        # 已经结束的提案补完历史就返回，此时浏览器只能靠连接关闭判断"流到头了"。
        # 不声明的话 HTTP/1.1 会保持连接，页面就一直挂在"连接中"。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        watermark = 0
        ended = False
        try:
            # 1. 先补发历史 —— 刷新页面才能恢复到当前状态
            for event in snapshot:
                self._write_event(event)
                watermark = max(watermark, event.seq)
                if event.type == "session.end":
                    ended = True
            # 2. 已经收口的提案不发实时流：否则页面会一直挂着一个
            #    "连接中"的连接，而那边永远不会再来事件。
            if ended or not live:
                return
            # 3. 再进实时流；丢弃 seq 不高于水位的（那些已经在快照里了）
            while True:
                try:
                    event = target.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event.seq <= watermark:
                    continue
                self._write_event(event)
                watermark = event.seq
                if event.type == "session.end":
                    break
        except (BrokenPipeError, ConnectionResetError):
            # 客户端关掉页面 —— 这是 SSE 唯一的正常收尾方式，不是错误。
            # 只捕获这两个具体类型，不吞其它异常。
            pass
        finally:
            self.server.api.unsubscribe(session_id, target)  # type: ignore[attr-defined]

    def _write_event(self, event: Event) -> None:
        payload = json.dumps(event.to_dict(), ensure_ascii=False)
        self.wfile.write(f"event: {event.type}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


class SparkyServer(ThreadingHTTPServer):
    """带提案 API 的本机 HTTP 服务。"""

    daemon_threads = True
    #: 允许重用处于 TIME_WAIT 的地址。
    #:
    #: 这里曾经写成 ``False``，注释还说"端口占用必须报错" —— 那是**误解**：
    #: ``SO_REUSEADDR`` 只影响 TIME_WAIT 残留，**不会**让两个活跃的服务
    #: 共用同一个端口（那是 ``SO_REUSEPORT``）。关掉它的实际后果是
    #: 服务停掉之后端口在一分钟内无法重用，重启直接失败。
    #: 而"另一个进程正在监听时必须报错"这条性质，开着它也照样成立。
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: ProposalApi) -> None:
        self.api = api
        super().__init__(address, _Handler)


def start(
    api: ProposalApi,
    *,
    host: str,
    port: int,
) -> tuple[SparkyServer, threading.Thread]:
    """绑定端口并在后台线程里开始服务。

    端口占用 → 抛 ``ConfigError``（**不换端口**）。
    """
    try:
        server = SparkyServer((host, port), api)
    except OSError as exc:
        raise ConfigError(
            f"无法在 {host}:{port} 上启动本地网页：{exc}",
            hint=(
                "端口被占用时不会自动换端口 —— 那样你会对着旧地址刷新，问题更难查。\n"
                "请用 --port 指定另一个端口，或用 --no-web 只输出到终端。"
            ),
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever,
        args=(SERVE_POLL_SECONDS,),
        name="sparky-web",
        daemon=True,
    )
    thread.start()
    return server, thread


def stop(server: SparkyServer | None) -> None:
    """关掉服务。进程退出前调用。"""
    if server is None:
        return
    server.shutdown()
    server.server_close()


def open_browser(url: str) -> bool:
    """尝试打开浏览器。返回是否成功。

    失败**不抛异常**：服务已经起来了，用户手敲地址即可。
    调用方负责把这件事**告诉用户**（不静默）。
    """
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 —— webbrowser 在各平台会抛各种东西
        sys.stderr.write(f"[sparky web] 打开浏览器失败：{exc}\n")
        return False


__all__ = [
    "ProposalApi",
    "SparkyServer",
    "start",
    "stop",
    "open_browser",
    "KEEPALIVE_SECONDS",
    "SERVE_POLL_SECONDS",
    "WEB_DIR",
]
