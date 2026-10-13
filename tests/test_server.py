"""本地网页服务的测试。

包含一个**真实 HTTP 的集成测试** —— 路由、状态码、SSE 的分帧与收尾
这些都不是纯函数，只有真的起一个服务、真的发请求才能验证。

刻意**不**起真的工作台：这一层要测的是"网页协议"，掺进流水线只会让
失败原因变模糊。工作台本身在 ``tests/test_workspace.py`` 里单独测。
"""

from __future__ import annotations

import json
import queue
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from sparky import server as server_mod
from sparky.errors import ConfigError, UsageError
from sparky.events import Event, EventHub

WEB_DIR = Path(server_mod.WEB_DIR)


def make_event(seq: int, event_type: str) -> Event:
    return Event(type=event_type, session_id="s", seq=seq, data={"seq": seq})


class FakeApi:
    """``server.ProposalApi`` 的最小假实现。

    它同时是**接口的形状守卫**：真实工作台少实现一个方法，
    这里会因为 AttributeError 立刻暴露，而不是等到线上 500。
    """

    def __init__(self) -> None:
        self.hubs: dict[str, EventHub] = {}
        #: 与上面那条 "running" 的提案保持一致 —— 状态和 live 集合不能各说各话。
        self.live: set[str] = {"20261012-110000-乙"}
        self.doc: str | None = None
        self.created: list[dict] = []
        self.stopped: list[str] = []
        self.interjections: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.proposals: dict[str, dict] = {
            "20261012-100000-甲": {
                "session_id": "20261012-100000-甲",
                "goal": "一个本地优先的记账工具",
                "status": "completed",
                "live": False,
                "doc_chars": 12,
                "has_doc": True,
            },
            "20261012-110000-乙": {
                "session_id": "20261012-110000-乙",
                "goal": "一个命令行番茄钟",
                "status": "running",
                "live": True,
                "doc_chars": 0,
                "has_doc": False,
            },
        }

    # ── ProposalApi ──

    def list_proposals(self) -> list[dict]:
        self.calls.append("list")
        return list(self.proposals.values())

    def get_proposal(self, session_id: str) -> dict | None:
        self.calls.append(f"get:{session_id}")
        return self.proposals.get(session_id)

    def create_proposal(self, payload: dict) -> dict:
        self.calls.append("create")
        idea = str(payload.get("idea", "")).strip()
        if not idea:
            raise UsageError("想法不能为空。")
        self.created.append(payload)
        item = {"session_id": "20261012-120000-新", "goal": idea, "status": "running",
                "live": True, "doc_chars": 0, "has_doc": False}
        self.proposals[item["session_id"]] = item
        return item

    def stop_proposal(self, session_id: str) -> bool:
        self.calls.append(f"stop:{session_id}")
        if session_id not in self.live:
            return False
        self.stopped.append(session_id)
        return True

    def interject(self, session_id: str, text: str) -> bool:
        self.calls.append(f"interject:{session_id}")
        if session_id not in self.live:
            return False
        self.interjections.append((session_id, text))
        return True

    def is_live(self, session_id: str) -> bool:
        self.calls.append(f"is_live:{session_id}")
        return session_id in self.live

    def unsubscribe(self, session_id: str, target: queue.Queue) -> None:
        hub = self.hubs.get(session_id)
        if hub is not None:
            hub.unsubscribe(target)

    def read_design_doc(self, session_id: str) -> str | None:
        self.calls.append(f"doc:{session_id}")
        if session_id not in self.proposals or not self.proposals[session_id]["has_doc"]:
            return None
        return self.doc

    def subscribe(self, session_id: str) -> tuple[queue.Queue[Event], list[Event]] | None:
        self.calls.append(f"subscribe:{session_id}")
        if session_id not in self.proposals:
            return None
        hub = self.hubs.get(session_id)
        if hub is not None:
            return hub.subscribe()
        return queue.Queue(), []

    # ── 测试用的布置 ──

    def seed(self, session_id: str, events: list[Event]) -> EventHub:
        hub = EventHub()
        for event in events:
            hub.publish(event)
        self.hubs[session_id] = hub
        self.live.add(session_id)
        return hub


class ServerTestCase(unittest.TestCase):
    """起一个真实服务，用真实 HTTP 请求测。"""

    def setUp(self):
        self.api = FakeApi()
        self.server, _ = server_mod.start(self.api, host="127.0.0.1", port=0)
        self.addCleanup(server_mod.stop, self.server)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    @staticmethod
    def q(session_id: str) -> str:
        """提案 id 里带中文，URL 路径必须百分号编码 —— 浏览器会编码，测试也得编码。"""
        return quote(session_id, safe="")

    def get(self, path: str, *, lines: int = 0):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            if lines:
                return response, [response.readline().decode("utf-8") for _ in range(lines)]
            return response, response.read().decode("utf-8")

    def get_json(self, path: str):
        response, body = self.get(path)
        return response, json.loads(body)

    def post_json(self, path: str, payload: dict | None):
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            self.addCleanup(error.close)
            return error.code, json.loads(error.read().decode("utf-8"))

    def expect_http_error(self, path: str) -> urllib.error.HTTPError:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(path)
        error = ctx.exception
        self.addCleanup(error.close)
        return error


class TestStaticResources(ServerTestCase):
    def test_index_is_served(self):
        response, body = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("Sparky", body)

    def test_js_and_css_are_served(self):
        for path, expect in (("/app.js", "javascript"), ("/style.css", "text/css")):
            with self.subTest(path=path):
                response, body = self.get(path)
                self.assertIn(expect, response.headers["Content-Type"])
                self.assertTrue(body.strip())

    def test_unknown_path_is_404_not_blank(self):
        error = self.expect_http_error("/nope")
        self.assertEqual(error.code, 404)
        self.assertIn("没有这个地址", error.read().decode("utf-8"))

    def test_all_three_assets_exist(self):
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((WEB_DIR / name).is_file(), f"缺少 {name}")

    def test_traversal_is_not_a_route(self):
        """``/../`` 既不该读到仓库文件，也不该被当成提案 id。"""
        for path in ("/../AGENTS.md", "/api/proposals/..%2F..%2Fetc"):
            with self.subTest(path=path):
                self.assertIn(self.expect_http_error(path).code, (400, 404))


class TestProposalApi(ServerTestCase):
    def test_list_returns_every_proposal(self):
        response, body = self.get_json("/api/proposals")
        self.assertEqual(response.status, 200)
        self.assertEqual(len(body["proposals"]), 2)

    def test_detail_returns_one(self):
        _, body = self.get_json(f"/api/proposals/{self.q('20261012-100000-甲')}")
        self.assertEqual(body["proposal"]["goal"], "一个本地优先的记账工具")

    def test_unknown_proposal_is_404(self):
        error = self.expect_http_error(f"/api/proposals/{self.q('没有这一份')}")
        self.assertEqual(error.code, 404)

    def test_doc_is_rendered_to_html(self):
        """文档由服务端渲染（sparky/markdown.py），前端只负责贴上去。"""
        self.api.doc = "# 标题\n\n- 甲\n- 乙\n"
        _, body = self.get_json(f"/api/proposals/{self.q('20261012-100000-甲')}/doc")
        self.assertIn("<h1>标题</h1>", body["html"])
        self.assertIn("<li>甲</li>", body["html"])
        self.assertEqual(body["chars"], len(self.api.doc))
        self.assertEqual(body["markdown"], self.api.doc)

    def test_doc_escapes_html(self):
        self.api.doc = "正常文本 <script>alert(1)</script>"
        _, body = self.get_json(f"/api/proposals/{self.q('20261012-100000-甲')}/doc")
        self.assertNotIn("<script>", body["html"])
        self.assertIn("&lt;script&gt;", body["html"])

    def test_proposal_without_doc_is_404(self):
        error = self.expect_http_error(f"/api/proposals/{self.q('20261012-110000-乙')}/doc")
        self.assertEqual(error.code, 404)
        self.assertIn("还没有产出设计文档", error.read().decode("utf-8"))

    def test_create_returns_201(self):
        status, body = self.post_json("/api/proposals", {"idea": "一个命令行番茄钟"})
        self.assertEqual(status, 201)
        self.assertEqual(body["proposal"]["goal"], "一个命令行番茄钟")
        self.assertEqual(self.api.created[0]["idea"], "一个命令行番茄钟")

    def test_create_with_empty_idea_is_400(self):
        """空想法是用法错误，不是服务端故障 —— 状态码要能区分这两者。"""
        status, body = self.post_json("/api/proposals", {"idea": "   "})
        self.assertEqual(status, 400)
        self.assertIn("不能为空", body["message"])

    def test_create_with_broken_body_is_400(self):
        request = urllib.request.Request(
            self.base + "/api/proposals", data="{不是 JSON".encode("utf-8"), method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        error = ctx.exception
        self.addCleanup(error.close)
        self.assertEqual(error.code, 400)

    def test_stop_live_proposal(self):
        status, body = self.post_json(f"/api/proposals/{self.q('20261012-110000-乙')}/stop", None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.api.stopped, ["20261012-110000-乙"])

    def test_stop_finished_proposal_is_409(self):
        status, body = self.post_json(f"/api/proposals/{self.q('20261012-100000-甲')}/stop", None)
        self.assertEqual(status, 409)
        self.assertIn("停不了", body["message"])

    def test_interject_delivers_text(self):
        status, body = self.post_json(
            f"/api/proposals/{self.q('20261012-110000-乙')}/interject", {"text": "离线是硬要求"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.api.interjections, [("20261012-110000-乙", "离线是硬要求")])

    def test_interject_with_empty_text_is_400(self):
        status, _ = self.post_json(
            f"/api/proposals/{self.q('20261012-110000-乙')}/interject", {"text": "  "}
        )
        self.assertEqual(status, 400)

    def test_interject_to_finished_proposal_is_409(self):
        status, body = self.post_json(
            f"/api/proposals/{self.q('20261012-100000-甲')}/interject", {"text": "晚了"}
        )
        self.assertEqual(status, 409)
        self.assertIn("没有送达", body["message"])


class TestSse(ServerTestCase):
    def test_session_parameter_is_required(self):
        """刻意不"自动挑一个会话" —— 挑错了用户会看着别人的提案。"""
        error = self.expect_http_error("/events")
        self.assertEqual(error.code, 400)
        self.assertIn("session", error.read().decode("utf-8"))

    def test_unknown_session_is_404(self):
        error = self.expect_http_error(f"/events?session={self.q('没有这一份')}")
        self.assertEqual(error.code, 404)

    def test_history_is_replayed_on_connect(self):
        self.api.seed("20261012-110000-乙", [
            make_event(1, "session.start"),
            make_event(2, "brief.ready"),
        ])
        _, lines = self.get(f"/events?session={self.q('20261012-110000-乙')}", lines=6)
        self.assertEqual(lines[0], "event: session.start\n")
        self.assertEqual(lines[3], "event: brief.ready\n")
        payload = json.loads(lines[4][len("data: "):])
        self.assertEqual(payload["seq"], 2)
        self.assertEqual(payload["type"], "brief.ready")

    def test_live_events_arrive_after_history(self):
        """补完历史之后，实时事件必须继续推过来 —— 这是"看着它们吵"的前提。"""
        hub = self.api.seed("20261012-110000-乙", [make_event(1, "session.start")])
        with urllib.request.urlopen(
            self.base + f"/events?session={self.q('20261012-110000-乙')}", timeout=5
        ) as response:
            self.assertEqual(response.readline(), b"event: session.start\n")
            response.readline()  # data
            response.readline()  # 空行分隔
            hub.publish(make_event(2, "brief.ready"))
            self.assertEqual(response.readline(), b"event: brief.ready\n")

    def test_finished_session_closes_the_stream(self):
        """已结束的提案补完历史就收口，否则页面永远挂着"连接中"。"""
        self.api.hubs["20261012-100000-甲"] = EventHub()
        self.api.hubs["20261012-100000-甲"].publish(make_event(1, "session.end"))
        with urllib.request.urlopen(
            self.base + f"/events?session={self.q('20261012-100000-甲')}", timeout=5
        ) as response:
            lines = []
            while True:
                line = response.readline()
                if not line:
                    break
                lines.append(line.decode("utf-8"))
        self.assertEqual(lines[0], "event: session.end\n")
        self.assertEqual(response.headers.get("Connection"), "close")

    def test_not_live_session_without_session_end_still_closes(self):
        """进程被杀留下的会话没有 session.end —— 连接也必须收口。

        不收口的话，用户会对着一个"连接中"的页面一直等一场永远不会继续的讨论。
        """
        self.api.hubs["20261012-100000-甲"] = EventHub()
        self.api.hubs["20261012-100000-甲"].publish(make_event(1, "session.start"))
        with urllib.request.urlopen(
            self.base + f"/events?session={self.q('20261012-100000-甲')}", timeout=5
        ) as response:
            lines = [response.readline().decode("utf-8") for _ in range(3)]
            self.assertEqual(response.readline(), b"")
        self.assertEqual(lines[0], "event: session.start\n")


class TestPortReuse(unittest.TestCase):
    def test_same_port_can_be_reused_after_stop(self):
        """回归：allow_reuse_address=False 会让服务停掉后端口一分钟内无法重用。

        注意这与"另一个进程正在监听时必须报错"**不冲突** ——
        SO_REUSEADDR 只影响 TIME_WAIT 残留，不影响活跃监听。
        """
        api = FakeApi()
        first, _ = server_mod.start(api, host="127.0.0.1", port=0)
        port = first.server_address[1]
        # 先制造一个真实连接，让端口进入 TIME_WAIT
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            response.read()
        server_mod.stop(first)
        second, _ = server_mod.start(api, host="127.0.0.1", port=port)
        self.addCleanup(server_mod.stop, second)
        self.assertEqual(second.server_address[1], port)


class TestPortConflict(unittest.TestCase):
    def test_port_in_use_raises_instead_of_switching(self):
        """静默换端口会让用户对着旧地址刷新，问题更难查。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            with self.assertRaises(ConfigError) as ctx:
                server_mod.start(FakeApi(), host="127.0.0.1", port=busy_port)
            # hint 是单独一段给用户看的"下一步怎么办"，render() 会把它拼上
            rendered = ctx.exception.render()
            self.assertIn(str(busy_port), rendered)
            self.assertIn("--port", rendered)
            self.assertIn("不会自动换端口", rendered)


class TestFrontendConstraints(unittest.TestCase):
    """前端的硬约束：零外链、零构建、总量有上限、事件全覆盖。"""

    def _read(self, name: str) -> str:
        return (WEB_DIR / name).read_text(encoding="utf-8")

    def test_no_external_resources(self):
        """断网后页面仍要能正常工作 —— 任何外链都会破坏这一条。"""
        for name in ("index.html", "app.js", "style.css"):
            body = self._read(name)
            for pattern in ("http://", "https://", "//cdn", "@import"):
                with self.subTest(file=name, pattern=pattern):
                    self.assertNotIn(pattern, body, f"{name} 里有外链 {pattern}")

    def test_no_build_step_markers(self):
        """零构建：不许出现打包器/框架的痕迹。"""
        for name in ("index.html", "app.js"):
            body = self._read(name)
            for pattern in ("import ", "require(", "type=\"module\"", "jsx"):
                with self.subTest(file=name, pattern=pattern):
                    self.assertNotIn(pattern, body)

    #: 前端总行数上限。这是一条**可维护性护栏**，不是技术限制 ——
    #: 它的作用是拦住"再堆一点就该上构建步骤了"的那一刻。
    #:
    #: 三次上调，每次都有具体理由：
    #:   1000 → 1200  重做阅读体验（议程侧栏、可折叠轮次、调研结论区、插话输入框）
    #:   1200 → 1800  M3.2 提案工作台（提案列表、新建表单、页签、设计文档排版）
    #: 工作台这一步涨了约 500 行，全部是**新的界面**而不是把旧界面写复杂：
    #: 多了一整个列表视图和一份 Markdown 渲染结果的样式。
    #: 真正该警惕的信号不是行数，而是"开始出现重复的样板代码" ——
    #: 那时才该考虑拆文件或上构建，而不是提前为了行数好看去拆。
    FRONTEND_LINE_BUDGET = 1800

    def test_total_line_budget(self):
        total = sum(len(self._read(n).splitlines()) for n in ("index.html", "app.js", "style.css"))
        self.assertLessEqual(
            total, self.FRONTEND_LINE_BUDGET,
            f"前端共 {total} 行，超过 {self.FRONTEND_LINE_BUDGET} 行的护栏",
        )

    def test_every_known_event_type_has_a_handler(self):
        """后端会发的事件，前端都要有处理分支 —— 否则界面会静默停在某个状态。"""
        from sparky.events import KNOWN_EVENT_TYPES

        body = self._read("app.js")
        for event_type in KNOWN_EVENT_TYPES:
            with self.subTest(event=event_type):
                self.assertIn(f'"{event_type}"', body, f"前端没有处理 {event_type}")

    def test_views_are_routed_by_hash(self):
        """两个视图靠 location.hash 切换 —— 刷新页面要能回到同一份提案。"""
        body = self._read("app.js")
        self.assertIn("hashchange", body)
        self.assertIn("#\\/p\\/", body)

    def test_no_domain_event_collides_with_a_dom_event_name(self):
        """SSE 的 ``event:`` 名与 ``EventSource`` 上的 DOM 事件**同名同姓**。

        实测踩过：业务错误事件曾经叫 ``error``，于是前端注册的
        ``addEventListener("error", …)`` 会同时收到浏览器自己发的连接错误
        （没有 ``data``），``JSON.parse(undefined)`` 抛异常，
        **真正的错误被解析错误盖掉**，用户看到的是"undefined is not valid JSON"。
        """
        from sparky.events import KNOWN_EVENT_TYPES

        for reserved in ("error", "open", "message"):
            with self.subTest(event=reserved):
                self.assertNotIn(
                    reserved, KNOWN_EVENT_TYPES,
                    f"事件名 {reserved!r} 与 EventSource 的 DOM 事件撞名，前端会收到两种东西",
                )


class TestDemoFixture(unittest.TestCase):
    """前端开发夹具（tools/demo_server.py）不能与事件协议脱节。

    它注入的是**合成事件** —— 如果事件类型或字段名跟后端对不上，
    改前端的人会照着错的样例调，然后被真实运行打脸。
    """

    def _module(self):
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "tools" / "demo_server.py"
        spec = importlib.util.spec_from_file_location("demo_server", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _events(self):
        return self._module().build_events()

    def test_every_event_type_is_known(self):
        from sparky.events import KNOWN_EVENT_TYPES

        for event_type, _data in self._events():
            with self.subTest(event=event_type):
                self.assertIn(event_type, KNOWN_EVENT_TYPES)

    def test_sequence_ends_with_session_end(self):
        events = self._events()
        self.assertEqual(events[-1][0], "session.end")

    def test_turn_tokens_carry_the_fields_the_frontend_reads(self):
        for event_type, data in self._events():
            if event_type == "turn.token":
                self.assertIn("persona_id", data)
                self.assertIn("delta", data)
                self.assertIn("reset", data)

    def test_covers_all_four_regions(self):
        """夹具要能同时演示四块区域，否则改某一块时没得看。"""
        types = {event_type for event_type, _ in self._events()}
        for needed in ("panel.ready", "turn.end", "disagreement.open", "round.start"):
            with self.subTest(event=needed):
                self.assertIn(needed, types)

    def test_turn_payloads_carry_every_field_the_frontend_reads(self):
        """夹具的 turn payload 必须与后端发的同形。

        实测踩过：夹具只写了 ``say`` 而漏了 ``summary``，
        前端读 ``t.summary`` 拿到 undefined，**发言正文整段空白** ——
        页面看起来"结构对了"，但最该读的那部分没了。
        """
        needed = {"persona_id", "round", "summary", "abstained", "actions"}
        for event_type, data in self._events():
            if event_type != "turn.end":
                continue
            turn = data["turn"]
            with self.subTest(persona=turn.get("persona_id")):
                missing = needed - set(turn)
                self.assertFalse(missing, f"turn payload 缺少 {sorted(missing)}")

    def test_demonstrates_interjection(self):
        """插话是核心交互，夹具必须能演示它。"""
        types = {event_type for event_type, _ in self._events()}
        self.assertIn("client.interjection", types)
        self.assertIn("client.interjection.delivered", types)

    def test_api_implements_the_protocol(self):
        """夹具必须实现网页服务的全套接口，否则前端会照着假接口调。"""
        api = self._module().DemoApi(delay=0)
        for name in ("list_proposals", "get_proposal", "create_proposal",
                     "stop_proposal", "interject", "is_live", "unsubscribe",
                     "read_design_doc", "subscribe"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(api, name, None)), f"夹具缺少 {name}")

    def test_demo_has_three_kinds_of_proposal(self):
        """夹具要能演示"历史 / 进行中 / 新建"三条路径，缺一条就没得看。"""
        api = self._module().DemoApi(delay=0)
        statuses = {p["status"] for p in api.list_proposals()}
        self.assertIn("running", statuses)
        self.assertIn("completed", statuses)
        self.assertIn("stale", statuses)

    def test_demo_doc_exercises_the_markdown_renderer(self):
        """演示文档要用上渲染器支持的每种构造，否则改排版时看不到效果。"""
        from sparky import markdown

        html = markdown.render(self._module().DEMO_DOC)
        for fragment in ("<h1>", "<h2>", "<ul>", "<ol>", "<table>", "<pre>",
                         "<blockquote>", "<strong>", "<code>", "<hr>"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, html)


if __name__ == "__main__":
    unittest.main()
