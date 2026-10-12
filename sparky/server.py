"""本地 HTTP 服务 + SSE 推送 + 静态资源。

职责
    用标准库 ``http.server.ThreadingHTTPServer`` 提供：
      · ``GET /``                  → ``web/index.html``
      · ``GET /app.js`` ``/style.css`` → 静态资源
      · ``GET /events``            → SSE 事件流（**先补发历史，再进实时**）
      · ``POST /stop``             → 请求优雅停止（当前轮结束后进入定稿）

输入
    事件总线（订阅）+ 会话（用于在页面上显示会话 id）

输出
    HTTP 响应。**只监听 127.0.0.1** —— 这个工具没有任何鉴权，不该暴露到局域网。

依赖
    仅标准库 ``http.server`` / ``socketserver`` / ``queue`` / ``threading`` / ``json``。
    表现层**不得 import ``llm``**（docs/architecture.md §2 的分层禁令）。

失败模式
    端口占用 → 抛 ``ConfigError``，**报错退出并提示换端口**，绝不静默换端口
    （静默换端口会让用户对着旧地址刷新，问题更难查）。
    静态资源缺失 → 500 并写 stderr，**不返回空白页**。
    SSE 客户端断开 → 这是 SSE 唯一的正常收尾方式，不视为错误；
    只捕获 ``BrokenPipeError`` / ``ConnectionResetError`` 这两个具体类型。

设计要点
    **历史补发的去重靠 ``seq``**：订阅时先拿到历史快照与当时的 ``seq`` 水位，
    之后从队列里丢弃 ``seq`` 不高于水位的实时事件。
    这样刷新页面既不会丢事件、也不会把同一件事显示两遍。

    临时事件（``turn.token``）不在历史里 —— 它们本来就不落盘，
    新连上来的页面拿不到过去的打字机增量，也不需要（完整正文在 ``turn.end`` 里）。

对应设计
    docs/web-ui.md · docs/architecture.md §5
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError
from .events import EPHEMERAL_EVENT_TYPES, Event

#: SSE keepalive 间隔（秒）。中间有代理时不发会被判超时断开。
KEEPALIVE_SECONDS = 15.0

#: 静态资源目录。
WEB_DIR = Path(__file__).resolve().parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


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
        path = urlparse(self.path).path
        if path == "/":
            self._send_file("index.html")
        elif path in ("/app.js", "/style.css"):
            self._send_file(path.lstrip("/"))
        elif path == "/events":
            self._serve_events()
        else:
            self._send_text(404, "没有这个地址。可用：/ · /app.js · /style.css · /events")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/stop":
            self._send_text(404, "没有这个地址。可用：POST /stop")
            return
        self.server.request_stop()  # type: ignore[attr-defined]
        self._send_json(200, {"ok": True, "message": "已请求停止：当前轮结束后进入定稿"})

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

    # ── SSE ──

    def _serve_events(self) -> None:
        target, snapshot = self.server.hub.subscribe()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        watermark = 0
        try:
            # 1. 先补发历史 —— 刷新页面才能恢复到当前状态
            for event in snapshot:
                self._write_event(event)
                watermark = max(watermark, event.seq)
            # 2. 再进实时流；丢弃 seq 不高于水位的（那些已经在快照里了）
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
            self.server.hub.unsubscribe(target)  # type: ignore[attr-defined]

    def _write_event(self, event: Event) -> None:
        payload = json.dumps(event.to_dict(), ensure_ascii=False)
        self.wfile.write(f"event: {event.type}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


class SparkyServer(ThreadingHTTPServer):
    """带事件扇出与停止信号的本机 HTTP 服务。"""

    daemon_threads = True
    #: 允许重用处于 TIME_WAIT 的地址。
    #:
    #: 这里曾经写成 ``False``，注释还说"端口占用必须报错" —— 那是**误解**：
    #: ``SO_REUSEADDR`` 只影响 TIME_WAIT 残留，**不会**让两个活跃的服务
    #: 共用同一个端口（那是 ``SO_REUSEPORT``）。关掉它的实际后果是
    #: 服务停掉之后端口在一分钟内无法重用，重启直接失败。
    #: 而"另一个进程正在监听时必须报错"这条性质，开着它也照样成立。
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], hub: EventHub) -> None:
        self.hub = hub
        self._stop_event = threading.Event()
        super().__init__(address, _Handler)

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()


def start(hub: EventHub, *, host: str, port: int) -> tuple[SparkyServer, threading.Thread]:
    """绑定端口并在后台线程里开始服务。

    端口占用 → 抛 ``ConfigError``（**不换端口**）。
    """
    try:
        server = SparkyServer((host, port), hub)
    except OSError as exc:
        raise ConfigError(
            f"无法在 {host}:{port} 上启动本地网页：{exc}",
            hint=(
                "端口被占用时不会自动换端口 —— 那样你会对着旧地址刷新，问题更难查。\n"
                "请用 --port 指定另一个端口，或用 --no-web 只输出到终端。"
            ),
        ) from exc

    thread = threading.Thread(target=server.serve_forever, name="sparky-web", daemon=True)
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
    "EventHub",
    "SparkyServer",
    "start",
    "stop",
    "open_browser",
    "KEEPALIVE_SECONDS",
    "WEB_DIR",
]


def make_stop_check(server: SparkyServer | None) -> Callable[[], bool] | None:
    """把 ``server`` 包装成给编排层用的 ``should_stop`` 回调。"""
    if server is None:
        return None
    return lambda: server.stop_requested
