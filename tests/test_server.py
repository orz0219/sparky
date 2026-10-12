"""本地网页服务的测试。

包含一个**真实 HTTP 的集成测试** —— SSE 的分帧、历史补发、静态资源的
Content-Type 这些都不是纯函数，只有真的起一个服务、真的发请求才能验证。
"""

from __future__ import annotations

import json
import socket
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from sparky import server as server_mod
from sparky.errors import ConfigError
from sparky.events import EPHEMERAL_EVENT_TYPES, Event, TURN_TOKEN

WEB_DIR = Path(server_mod.WEB_DIR)


def make_event(seq: int, event_type: str) -> Event:
    return Event(type=event_type, session_id="s", seq=seq, data={"seq": seq})


class TestEventHub(unittest.TestCase):
    def test_non_ephemeral_events_are_kept_as_history(self):
        hub = server_mod.EventHub()
        hub.publish(make_event(1, "session.start"))
        hub.publish(make_event(2, "brief.ready"))
        _, history = hub.subscribe()
        self.assertEqual([e.seq for e in history], [1, 2])

    def test_ephemeral_events_are_not_history(self):
        """turn.token 不落盘、也不进历史 —— 否则刷新页面会重放上千条增量。"""
        hub = server_mod.EventHub()
        hub.publish(make_event(1, "session.start"))
        hub.publish(make_event(2, TURN_TOKEN))
        _, history = hub.subscribe()
        self.assertEqual([e.seq for e in history], [1])

    def test_subscribe_returns_snapshot_and_live_queue(self):
        hub = server_mod.EventHub()
        hub.publish(make_event(1, "session.start"))
        queue_, history = hub.subscribe()
        hub.publish(make_event(2, "brief.ready"))
        self.assertEqual([e.seq for e in history], [1])
        self.assertEqual(queue_.get_nowait().seq, 2)

    def test_every_subscriber_gets_every_event(self):
        hub = server_mod.EventHub()
        first, _ = hub.subscribe()
        second, _ = hub.subscribe()
        hub.publish(make_event(1, "session.start"))
        self.assertEqual(first.get_nowait().seq, 1)
        self.assertEqual(second.get_nowait().seq, 1)

    def test_unsubscribe_stops_delivery(self):
        hub = server_mod.EventHub()
        queue_, _ = hub.subscribe()
        hub.unsubscribe(queue_)
        hub.publish(make_event(1, "session.start"))
        self.assertTrue(queue_.empty())
        self.assertEqual(hub.client_count, 0)

    def test_ephemeral_event_still_reaches_live_clients(self):
        """不落盘 ≠ 不广播。界面靠这些增量做打字机。"""
        hub = server_mod.EventHub()
        queue_, _ = hub.subscribe()
        hub.publish(make_event(1, TURN_TOKEN))
        self.assertEqual(queue_.get_nowait().type, TURN_TOKEN)


class ServerTestCase(unittest.TestCase):
    """起一个真实服务，用真实 HTTP 请求测。"""

    def setUp(self):
        self.hub = server_mod.EventHub()
        self.server, _ = server_mod.start(self.hub, host="127.0.0.1", port=0)
        self.addCleanup(server_mod.stop, self.server)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path: str, *, lines: int = 0):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            if lines:
                return response, [response.readline().decode("utf-8") for _ in range(lines)]
            return response, response.read().decode("utf-8")


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
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        error = ctx.exception
        self.addCleanup(error.close)
        self.assertEqual(error.code, 404)
        self.assertIn("没有这个地址", error.read().decode("utf-8"))

    def test_all_three_assets_exist(self):
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((WEB_DIR / name).is_file(), f"缺少 {name}")


class TestSse(ServerTestCase):
    def test_history_is_replayed_on_connect(self):
        self.hub.publish(make_event(1, "session.start"))
        self.hub.publish(make_event(2, "brief.ready"))
        _, lines = self.get("/events", lines=6)
        self.assertEqual(lines[0], "event: session.start\n")
        self.assertEqual(lines[3], "event: brief.ready\n")
        payload = json.loads(lines[4][len("data: "):])
        self.assertEqual(payload["seq"], 2)
        self.assertEqual(payload["type"], "brief.ready")

    def test_ephemeral_events_are_not_replayed(self):
        """刷新页面不该重放上千条打字机增量。"""
        self.hub.publish(make_event(1, "session.start"))
        self.hub.publish(make_event(2, TURN_TOKEN))
        self.hub.publish(make_event(3, "brief.ready"))
        _, lines = self.get("/events", lines=6)
        types = [line[len("event: "):].strip() for line in lines if line.startswith("event:")]
        self.assertEqual(types, ["session.start", "brief.ready"])

    def test_watermark_dedup(self):
        """订阅之后、快照之前发生的事件只能出现一次（靠 seq 水位去重）。"""
        self.hub.publish(make_event(1, "session.start"))
        _, lines = self.get("/events", lines=3)
        self.assertEqual(lines[0], "event: session.start\n")

    def test_session_end_closes_the_stream(self):
        self.hub.publish(make_event(1, "session.end"))
        _, lines = self.get("/events", lines=3)
        self.assertEqual(lines[0], "event: session.end\n")


class TestStopEndpoint(ServerTestCase):
    def test_stop_sets_the_flag(self):
        self.assertFalse(self.server.stop_requested)
        request = urllib.request.Request(self.base + "/stop", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertTrue(self.server.stop_requested)

    def test_make_stop_check_returns_none_without_server(self):
        self.assertIsNone(server_mod.make_stop_check(None))

    def test_make_stop_check_reads_the_server(self):
        check = server_mod.make_stop_check(self.server)
        self.assertFalse(check())
        self.server.request_stop()
        self.assertTrue(check())


class TestPortReuse(unittest.TestCase):
    def test_same_port_can_be_reused_after_stop(self):
        """回归：allow_reuse_address=False 会让服务停掉后端口一分钟内无法重用。

        注意这与"另一个进程正在监听时必须报错"**不冲突** ——
        SO_REUSEADDR 只影响 TIME_WAIT 残留，不影响活跃监听。
        """
        hub = server_mod.EventHub()
        first, _ = server_mod.start(hub, host="127.0.0.1", port=0)
        port = first.server_address[1]
        # 先制造一个真实连接，让端口进入 TIME_WAIT
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            response.read()
        server_mod.stop(first)
        second, _ = server_mod.start(hub, host="127.0.0.1", port=port)
        self.addCleanup(server_mod.stop, second)
        self.assertEqual(second.server_address[1], port)


class TestPortConflict(unittest.TestCase):
    def test_port_in_use_raises_instead_of_switching(self):
        """静默换端口会让用户对着旧地址刷新，问题更难查。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            hub = server_mod.EventHub()
            with self.assertRaises(ConfigError) as ctx:
                server_mod.start(hub, host="127.0.0.1", port=busy_port)
            # hint 是单独一段给用户看的"下一步怎么办"，render() 会把它拼上
            rendered = ctx.exception.render()
            self.assertIn(str(busy_port), rendered)
            self.assertIn("--port", rendered)
            self.assertIn("不会自动换端口", rendered)


class TestFrontendConstraints(unittest.TestCase):
    """前端的两条硬约束：零外链、总量 ≤1000 行。"""

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

    def test_total_line_budget(self):
        total = sum(len(self._read(n).splitlines()) for n in ("index.html", "app.js", "style.css"))
        self.assertLessEqual(total, 1000, f"前端共 {total} 行，超过 1000 行的上限")

    def test_every_known_event_type_has_a_handler(self):
        """后端会发的事件，前端都要有处理分支 —— 否则界面会静默停在某个状态。"""
        from sparky.events import KNOWN_EVENT_TYPES

        body = self._read("app.js")
        for event_type in KNOWN_EVENT_TYPES:
            with self.subTest(event=event_type):
                self.assertIn(f'"{event_type}"', body, f"前端没有处理 {event_type}")

    def test_ephemeral_events_are_exactly_the_high_frequency_ones(self):
        self.assertEqual(EPHEMERAL_EVENT_TYPES, frozenset({TURN_TOKEN}))


class TestDemoFixture(unittest.TestCase):
    """前端开发夹具（tools/demo_server.py）不能与事件协议脱节。

    它注入的是**合成事件** —— 如果事件类型或字段名跟后端对不上，
    改前端的人会照着错的样例调，然后被真实运行打脸。
    """

    def _events(self):
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "tools" / "demo_server.py"
        spec = importlib.util.spec_from_file_location("demo_server", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.build_events()

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


if __name__ == "__main__":
    unittest.main()
