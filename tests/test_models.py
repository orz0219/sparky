"""数据模型的严格解析测试，以及会话持久化的并发安全测试。

这一组测的是本项目的两条底线：
  1. **不填充默认值** —— 缺字段就报错，不悄悄降低产出质量
  2. **并发下事件溯源不出错** —— transcription 行序与内容必须完整
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from sparky.errors import SchemaError
from sparky.events import EventBus, Event, KNOWN_EVENT_TYPES
from sparky.models import Brief, InfoScope, Persona, Usage
from sparky.session import redact

from .support import TempSession, make_brief, persona_payload


class TestStrictParsing(unittest.TestCase):
    def test_missing_key_field_reports_path_and_actual_keys(self):
        payload = persona_payload(1)
        del payload["bias"]
        with self.assertRaises(SchemaError) as ctx:
            Persona.from_dict(payload, "personas[0].")
        message = str(ctx.exception)
        self.assertIn("personas[0].bias", message)
        self.assertIn("实际拿到的键", message)

    def test_blank_string_is_rejected(self):
        """空白字符串和缺失一样有害：模型看似给了字段，实则敷衍。"""
        with self.assertRaises(SchemaError) as ctx:
            Persona.from_dict(persona_payload(1, stance="   "), "p.")
        self.assertIn("空白", str(ctx.exception))

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(SchemaError) as ctx:
            Persona.from_dict(persona_payload(1, cares_about="不是数组"), "p.")
        self.assertIn("期望是数组", str(ctx.exception))

    def test_enum_value_is_enforced(self):
        with self.assertRaises(SchemaError) as ctx:
            Persona.from_dict(persona_payload(1, role_type="随便什么角色"), "p.")
        self.assertIn("取值必须是", str(ctx.exception))

    def test_assigned_files_is_optional(self):
        """assigned_files 由代码分配，模型不产出它，因此解析时必须可缺省。"""
        payload = persona_payload(1)
        self.assertNotIn("assigned_files", payload["info_scope"])
        persona = Persona.from_dict(payload, "p.")
        self.assertEqual(persona.info_scope.assigned_files, [])

    def test_questions_must_not_be_empty(self):
        with self.assertRaises(SchemaError) as ctx:
            InfoScope.from_dict({"questions": [], "search_queries": []}, "s.")
        self.assertIn("至少需要 1 条", str(ctx.exception))

    def test_brief_requires_at_least_one_unknown(self):
        payload = {
            "goal": "目标",
            "constraints": [],
            "non_goals": [],
            "unknowns": [],
            "dimensions": [{"id": "d1", "name": "维度", "why": "因为", "source": "user"}],
            "uncertainty": "medium",
        }
        with self.assertRaises(SchemaError) as ctx:
            Brief.from_llm("输入", payload)
        self.assertIn("unknowns", str(ctx.exception))

    def test_brief_inferred_items_tracks_both_groups(self):
        brief = make_brief(inferred_items=2)
        self.assertEqual(len(brief.inferred_items), 2)


class TestUsage(unittest.TestCase):
    def test_accumulates(self):
        total = Usage()
        total.add(Usage(calls=1, prompt_tokens=10, completion_tokens=5, reasoning_tokens=3))
        total.add(Usage(calls=1, prompt_tokens=20, completion_tokens=7, reasoning_tokens=4))
        self.assertEqual(total.calls, 2)
        self.assertEqual(total.prompt_tokens, 30)
        self.assertEqual(total.completion_tokens, 12)
        self.assertEqual(total.reasoning_tokens, 7)
        self.assertEqual(total.total_tokens, 42)


class TestEventBus(unittest.TestCase):
    def test_unknown_event_type_is_rejected(self):
        """静默丢弃未知事件会让前端永久卡在某个状态。"""
        bus = EventBus()
        with self.assertRaises(ValueError) as ctx:
            bus.publish(Event(type="不存在的.事件", session_id="s", seq=1))
        self.assertIn("未登记", str(ctx.exception))

    def test_every_renderer_event_is_known(self):
        from sparky.render import TerminalRenderer

        renderer = TerminalRenderer(quiet=True)
        for event_type in KNOWN_EVENT_TYPES:
            handler = f"_on_{event_type.replace('.', '_')}"
            self.assertTrue(
                hasattr(renderer, handler),
                f"事件 {event_type} 没有渲染分支（缺 {handler}）",
            )

    def test_subscriber_error_propagates(self):
        bus = EventBus()

        def boom(_event):
            raise RuntimeError("表现层炸了")

        bus.subscribe(boom)
        with self.assertRaises(RuntimeError):
            bus.publish(Event(type="session.start", session_id="s", seq=1))


class TestSessionPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session

    def test_session_directory_is_created_before_pipeline(self):
        """目录与 meta 必须在流水线开跑前落地：崩溃后也要知道这次想干什么。"""
        self.assertTrue((self.session.root / "meta.json").exists())
        self.assertTrue((self.session.root / "transcript.jsonl").exists())
        meta = json.loads((self.session.root / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "running")
        self.assertEqual(meta["raw_input"], "测试输入")

    def test_transcript_is_line_delimited_json(self):
        self.session.emit("session.start", raw_input="x")
        self.session.emit("session.end", status="completed")
        lines = (self.session.root / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["type"], "session.start")
        self.assertEqual([json.loads(line)["seq"] for line in lines], [1, 2])

    def test_concurrent_emit_keeps_transcript_intact(self):
        """S2 阶段 N 人并发发事件：行不能被撕开，seq 不能重复或跳号。"""
        threads = [
            threading.Thread(target=self.session.emit, args=("recon.memo",), kwargs={"i": i})
            for i in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = (self.session.root / "transcript.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 40)
        seqs = []
        for line in lines:
            payload = json.loads(line)  # 撕裂的行会在这里抛 JSONDecodeError
            seqs.append(payload["seq"])
        self.assertEqual(sorted(seqs), list(range(1, 41)))
        self.assertEqual(len(set(seqs)), 40)

    def test_concurrent_llm_call_indices_are_unique(self):
        """调用编号必须唯一 —— 否则两次调用的快照会互相覆盖。"""
        indices: list[int] = []
        lock = threading.Lock()

        def book():
            index = self.session.begin_llm_call({"payload": 1}, purpose="并发测试")
            with lock:
                indices.append(index)

        threads = [threading.Thread(target=book) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(indices), list(range(1, 21)))
        files = sorted(p.name for p in (self.session.root / "llm-calls").glob("*-request.json"))
        self.assertEqual(len(files), 20)

    def test_secrets_are_redacted_on_disk(self):
        self.session.write_json(
            "probe.json",
            {"api_key": "sk-should-not-appear", "nested": {"authorization": "Bearer x"}},
        )
        text = (self.session.root / "probe.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-should-not-appear", text)
        self.assertNotIn("Bearer x", text)
        self.assertIn("已擦除", text)

    def test_usage_counters_are_not_redacted(self):
        """token 用量不是机密 —— 擦掉它会让附录 D 的成本统计变成一串 <已擦除>。"""
        self.session.write_json(
            "usage.json",
            {
                "prompt_tokens": 120,
                "completion_tokens": 40,
                "reasoning_tokens": 30,
                "total_tokens": 160,
                "api_key_env": "DEEPSEEK_API_KEY",
            },
        )
        payload = json.loads((self.session.root / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["prompt_tokens"], 120)
        self.assertEqual(payload["completion_tokens"], 40)
        self.assertEqual(payload["reasoning_tokens"], 30)
        self.assertEqual(payload["total_tokens"], 160)
        # 变量名不是机密，但它长得像，所以被擦了 —— "宁可多擦"是有意的
        self.assertEqual(payload["api_key_env"], "<已擦除>")

    def test_real_auth_names_are_still_redacted(self):
        for name in ("access_token", "refresh_token", "auth_token", "secret", "password"):
            with self.subTest(field=name):
                payload = redact({name: "sensitive-value"})
                self.assertEqual(payload[name], "<已擦除>")

    def test_duplicate_session_id_is_an_error(self):
        """同秒同输入会撞目录名 —— 必须报错而不是覆盖掉前一次的会话。"""
        from datetime import datetime

        from sparky.errors import PersistenceError
        from sparky.session import Session

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 12, 10, 30, 0)

        with mock.patch("sparky.session.datetime", FixedDatetime):
            out_root = self.session.root.parent
            Session.create(out_root, "同一个输入", bus=EventBus(), config_snapshot={})
            with self.assertRaises(PersistenceError):
                Session.create(out_root, "同一个输入", bus=EventBus(), config_snapshot={})


if __name__ == "__main__":
    unittest.main()
