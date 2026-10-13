"""提案工作台的测试。

工作台是"读盘 + 起线程"的模块，所以这里的测试分两类：
  · **读**：造几个真的会话目录，验证清单与详情的字段、状态归一、损坏报告
  · **跑**：注入一条**不调模型的假流水线**，验证线程生命周期、
    失败收口、停止与插话的接受条件

假流水线是显式注入的（``Workspace(run_pipeline=...)``），不是打补丁 ——
测试读起来能直接看到"这条流水线被换掉了"，不必去猜 mock 在哪生效。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from sparky.config import RuntimeConfig
from sparky.errors import PersistenceError, SparkyError, UsageError
from sparky.render import TerminalRenderer
from sparky.workspace import PANEL_MAX, Workspace


def make_runtime(root: Path, **overrides) -> RuntimeConfig:
    """造一份可直接用的运行配置。**不读环境变量**：测试不该依赖机器上的密钥。"""
    values: dict = {
        "raw_input": "测试想法",
        "api_key": "sk-test",
        "base_url": "https://example.invalid/v1",
        "model_strong": "test-model",
        "model_fast": "test-model",
        "api_key_env": "SPARKY_TEST_KEY",
        "panel_size": None,
        "max_rounds": 2,
        "budget_tokens": None,
        "concurrency": 1,
        "context_dir": None,
        "out_root": root / "sessions",
        "timeout": 5.0,
        "max_tokens": 1000,
        "temperature": 0.7,
        "quiet": True,
        "web": True,
        "port": 8765,
        "open_browser": False,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


class BlockingPipeline:
    """一条**不调模型**的假流水线：进来先举手，然后等测试放行。

    这样测试能稳定地观察到"正在跑"这个中间状态 ——
    真的流水线跑完只要几十毫秒，测试会在它结束后才去断言，什么都测不到。
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.runtimes: list[RuntimeConfig] = []
        self.fail_with = fail_with

    def __call__(self, **kwargs):
        self.runtimes.append(kwargs["runtime"])
        self.started.set()
        self.release.wait(timeout=5)
        session = kwargs["session"]
        if self.fail_with is not None:
            raise self.fail_with
        session.set_status("completed")
        session.emit("session.end", status="completed", usage={"calls": 1, "total_tokens": 10})
        return session.root / "design.md"

    def finish(self) -> None:
        self.release.set()


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root)
        self._running: list[tuple[Workspace, BlockingPipeline]] = []
        # 必须在临时目录被删**之前**把所有运行线程收干净 ——
        # 否则线程还在往会话目录里写文件，rmtree 会撞上"目录非空"。
        # addCleanup 是后进先出，所以这一条会先于上面的 tmp.cleanup 执行。
        self.addCleanup(self._drain_runs)
        self.pipeline = BlockingPipeline()
        self.workspace = self.make_workspace(self.pipeline)

    def make_workspace(self, pipeline: BlockingPipeline) -> Workspace:
        """造一个注入假流水线的工作台，并登记进清理清单。"""
        workspace = Workspace(
            runtime=self.runtime,
            renderer=TerminalRenderer(stream=io.StringIO(), quiet=True),
            run_pipeline=pipeline,
        )
        self._running.append((workspace, pipeline))
        return workspace

    def make_workspace_with(self, **runtime_overrides) -> tuple[Workspace, BlockingPipeline]:
        """按另一份运行配置造工作台（用来测"表单回落到启动配置"）。"""
        pipeline = BlockingPipeline()
        workspace = Workspace(
            runtime=make_runtime(self.root, **runtime_overrides),
            renderer=TerminalRenderer(stream=io.StringIO(), quiet=True),
            run_pipeline=pipeline,
        )
        self._running.append((workspace, pipeline))
        return workspace, pipeline

    def _drain_runs(self) -> None:
        for workspace, pipeline in self._running:
            pipeline.finish()
            with workspace._lock:
                handles = list(workspace._handles.values())
            for handle in handles:
                if handle.thread is not None:
                    handle.thread.join(timeout=5)

    # ── 造盘上的会话 ──

    def make_session_dir(
        self,
        session_id: str,
        *,
        meta: dict | None = None,
        personas: dict | None = None,
        ledger: dict | None = None,
        rounds: int = 0,
        doc: str | None = None,
        transcript: list[dict] | None = None,
    ) -> Path:
        root = self.runtime.out_root / session_id
        (root / "rounds").mkdir(parents=True)
        default_meta = {
            "session_id": session_id,
            "raw_input": f"想法 {session_id}",
            "started_at": "2026-10-12T10:00:00+08:00",
            "status": "completed",
            "config": {"model_strong": "test-model", "max_rounds": 3},
        }
        (root / "meta.json").write_text(
            json.dumps(default_meta if meta is None else meta, ensure_ascii=False),
            encoding="utf-8",
        )
        if personas is not None:
            (root / "personas.json").write_text(json.dumps(personas), encoding="utf-8")
        if ledger is not None:
            (root / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        for index in range(1, rounds + 1):
            (root / f"rounds/r{index}.json").write_text("{}", encoding="utf-8")
        if doc is not None:
            (root / "design.md").write_text(doc, encoding="utf-8")
        lines = transcript if transcript is not None else []
        (root / "transcript.jsonl").write_text(
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
            encoding="utf-8",
        )
        return root


class TestListing(WorkspaceTestCase):
    def test_missing_out_root_is_an_empty_list_not_an_error(self):
        """还没发起过任何提案不是错误 —— 报错会让第一次打开工作台的人困惑。"""
        self.assertEqual(self.workspace.list_proposals(), [])

    def test_reads_facts_from_small_files_only(self):
        self.make_session_dir(
            "20261012-100000-甲",
            personas={"panel_size": 4, "personas": []},
            ledger={"digest": {"total": 3, "open": 1, "resolved": 1, "tradeoff": 1, "stalemate": 0}},
            rounds=2,
            doc="# 文档\n正文",
        )
        item = self.workspace.list_proposals()[0]
        self.assertEqual(item["panel_size"], 4)
        self.assertEqual(item["round_count"], 2)
        self.assertEqual(item["doc_chars"], len("# 文档\n正文"))
        self.assertTrue(item["has_doc"])
        self.assertEqual(item["disagreements"]["total"], 3)
        self.assertEqual(item["model"], "test-model")
        self.assertEqual(item["status"], "completed")

    def test_goal_prefers_the_brief_over_the_raw_input(self):
        root = self.make_session_dir("20261012-100000-甲")
        (root / "brief.json").write_text(
            json.dumps({"goal": "立案书里的目标"}), encoding="utf-8"
        )
        self.assertEqual(self.workspace.list_proposals()[0]["goal"], "立案书里的目标")

    def test_newest_first(self):
        self.make_session_dir("20261012-100000-甲")
        self.make_session_dir("20261012-120000-丙")
        self.make_session_dir("20261012-110000-乙")
        ids = [item["session_id"] for item in self.workspace.list_proposals()]
        self.assertEqual(ids, ["20261012-120000-丙", "20261012-110000-乙", "20261012-100000-甲"])

    def test_broken_meta_is_reported_not_hidden(self):
        """一条坏记录不该废掉整份清单，但也**不能**悄悄消失。

        实测会发生的场景：进程在 ``Session.create`` 写 meta.json 的中途被杀，
        留下一个半截文件。用户需要看到"这一份坏了"，而不是它凭空不见。
        """
        root = self.runtime.out_root / "20261012-100000-坏"
        root.mkdir(parents=True)
        (root / "meta.json").write_text("{半截", encoding="utf-8")
        self.make_session_dir("20261012-110000-好")
        items = {item["session_id"]: item for item in self.workspace.list_proposals()}
        self.assertEqual(items["20261012-100000-坏"]["status"], "broken")
        self.assertIn("meta.json", items["20261012-100000-坏"]["status_detail"])
        self.assertEqual(items["20261012-110000-好"]["status"], "completed")

    def test_running_without_a_handle_is_stale(self):
        """进程重启后，之前"进行中"的提案必须如实变成"未跑完"。

        继续显示"进行中"会让用户一直等一场永远不会结束的讨论。
        """
        self.make_session_dir("20261012-100000-甲", meta={
            "session_id": "20261012-100000-甲", "raw_input": "x",
            "started_at": "2026-10-12T10:00:00+08:00", "status": "running", "config": {},
        })
        item = self.workspace.list_proposals()[0]
        self.assertEqual(item["status"], "stale")
        self.assertIn("进程已退出", item["status_detail"])
        self.assertFalse(item["live"])

    def test_failed_status_keeps_the_exception_name(self):
        self.make_session_dir("20261012-100000-甲", meta={
            "session_id": "20261012-100000-甲", "raw_input": "x",
            "started_at": "2026-10-12T10:00:00+08:00",
            "status": "failed: SchemaError", "config": {},
        })
        item = self.workspace.list_proposals()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["status_detail"], "SchemaError")


class TestDetail(WorkspaceTestCase):
    def test_unknown_proposal_is_none(self):
        self.assertIsNone(self.workspace.get_proposal("没有这一份"))

    def test_traversal_ids_are_rejected(self):
        """``session_id`` 来自 URL，必须挡掉路径穿越。"""
        self.make_session_dir("20261012-100000-甲")
        for bad in ("..", "../..", ".", "..%2F..", "a/b", "a\\b", ""):
            with self.subTest(session_id=bad):
                self.assertIsNone(self.workspace.get_proposal(bad))

    def test_read_transcript_replays_events_in_order(self):
        self.make_session_dir("20261012-100000-甲", transcript=[
            {"type": "session.start", "session_id": "20261012-100000-甲", "seq": 1,
             "ts": "t1", "data": {"raw_input": "x"}},
            {"type": "session.end", "session_id": "20261012-100000-甲", "seq": 2,
             "ts": "t2", "data": {"status": "completed"}},
        ])
        events = self.workspace.read_transcript("20261012-100000-甲")
        self.assertEqual([e.type for e in events], ["session.start", "session.end"])
        self.assertEqual(events[0].data["raw_input"], "x")
        self.assertEqual(events[1].seq, 2)

    def test_corrupt_transcript_line_names_the_line_number(self):
        """**不跳过**坏行：跳过会让你看到一场缺了一段的历史，而你无从知道。"""
        root = self.make_session_dir("20261012-100000-甲")
        (root / "transcript.jsonl").write_text(
            json.dumps({"type": "session.start", "session_id": "s", "seq": 1,
                        "ts": "t", "data": {}}) + "\n{半截\n",
            encoding="utf-8",
        )
        with self.assertRaises(PersistenceError) as ctx:
            self.workspace.read_transcript("20261012-100000-甲")
        self.assertIn(":2", str(ctx.exception))

    def test_event_missing_fields_is_reported_with_the_line_number(self):
        """合法 JSON 但缺字段 —— 也要指出行号，而不是抛一个光秃秃的 KeyError。

        KeyError 会被 HTTP 层当成"未预期异常"，用户看到的是
        ``服务端内部错误：'ts'``，完全看不出是哪一份提案的哪一行坏了。
        """
        root = self.make_session_dir("20261012-100000-甲")
        (root / "transcript.jsonl").write_text(
            json.dumps({"type": "session.start"}) + "\n", encoding="utf-8"
        )
        with self.assertRaises(PersistenceError) as ctx:
            self.workspace.read_transcript("20261012-100000-甲")
        self.assertIn(":1", str(ctx.exception))
        self.assertIn("缺少字段", str(ctx.exception))

    def test_doc_is_none_before_synthesis(self):
        self.make_session_dir("20261012-100000-甲")
        self.assertIsNone(self.workspace.read_design_doc("20261012-100000-甲"))

    def test_doc_is_read_when_present(self):
        self.make_session_dir("20261012-100000-甲", doc="# 文档\n")
        self.assertEqual(self.workspace.read_design_doc("20261012-100000-甲"), "# 文档\n")


class TestCreate(WorkspaceTestCase):
    def test_empty_idea_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            self.workspace.create_proposal({"idea": "   "})

    def test_out_of_range_parameters_are_rejected(self):
        for payload in (
            {"idea": "x", "panel_size": 2},
            {"idea": "x", "panel_size": PANEL_MAX + 1},
            {"idea": "x", "max_rounds": 0},
            {"idea": "x", "max_rounds": 999},
            {"idea": "x", "panel_size": "四个"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(UsageError):
                    self.workspace.create_proposal(payload)

    def test_create_writes_meta_before_the_run_finishes(self):
        """目录与 meta.json 必须在流水线开跑**之前**落地。"""
        described = self.workspace.create_proposal({"idea": "一个命令行番茄钟"})
        self.assertTrue(self.pipeline.started.wait(timeout=5))
        root = self.runtime.out_root / described["session_id"]
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["raw_input"], "一个命令行番茄钟")
        self.assertEqual(meta["status"], "running")
        self.assertIn("key_hint", meta["config"])
        self.assertNotIn("api_key", meta["config"])
        self.pipeline.finish()

    def test_create_reports_the_proposal_as_running(self):
        described = self.workspace.create_proposal({"idea": "一个命令行番茄钟"})
        self.assertEqual(described["status"], "running")
        self.assertTrue(described["live"])
        self.pipeline.finish()

    def test_form_defaults_fall_back_to_the_boot_config(self):
        """表单留空 → 回落到启动工作台时那份配置，而不是内置默认值。"""
        workspace, pipeline = self.make_workspace_with(panel_size=5, max_rounds=7)
        workspace.create_proposal({"idea": "回落到启动配置"})
        self.assertTrue(pipeline.started.wait(timeout=5))
        self.assertEqual(pipeline.runtimes[0].panel_size, 5)
        self.assertEqual(pipeline.runtimes[0].max_rounds, 7)
        pipeline.finish()

    def test_form_values_override_the_boot_config(self):
        workspace, pipeline = self.make_workspace_with(panel_size=5, max_rounds=7)
        workspace.create_proposal({"idea": "覆盖启动配置", "panel_size": 3, "max_rounds": 2})
        self.assertTrue(pipeline.started.wait(timeout=5))
        self.assertEqual(pipeline.runtimes[0].panel_size, 3)
        self.assertEqual(pipeline.runtimes[0].max_rounds, 2)
        pipeline.finish()


class TestRunLifecycle(WorkspaceTestCase):
    def test_successful_run_reaches_completed(self):
        described = self.workspace.create_proposal({"idea": "会跑完的提案"})
        self.pipeline.finish()
        handle = self.workspace._handles[described["session_id"]]
        self.assertTrue(handle.finished.wait(timeout=5))
        self.assertIsNone(handle.error)
        self.assertFalse(self.workspace.is_live(described["session_id"]))
        item = self.workspace.get_proposal(described["session_id"])
        self.assertEqual(item["status"], "completed")

    def test_failed_run_is_recorded_everywhere(self):
        """失败必须同时出现在三处：句柄、meta.json、transcript。

        少任何一处，用户都会在某个界面看到"还在跑"的假象。
        """
        pipeline = BlockingPipeline(fail_with=SparkyError("模型不认这个输入"))
        workspace = self.make_workspace(pipeline)
        with contextlib.redirect_stderr(io.StringIO()):
            described = workspace.create_proposal({"idea": "注定失败的提案"})
            pipeline.finish()
            handle = workspace._handles[described["session_id"]]
            self.assertTrue(handle.finished.wait(timeout=5))
        self.assertIn("模型不认这个输入", handle.error)
        self.assertEqual(handle.exit_code, 1)
        item = workspace.get_proposal(described["session_id"])
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["status_detail"], "SparkyError")

    def test_unexpected_exception_is_not_swallowed(self):
        """未预期异常是 bug：要记下来、要打到终端，但**不拖垮工作台**。"""
        pipeline = BlockingPipeline(fail_with=RuntimeError("内部 bug"))
        workspace = self.make_workspace(pipeline)
        with contextlib.redirect_stderr(io.StringIO()):
            described = workspace.create_proposal({"idea": "会内部崩溃的提案"})
            pipeline.finish()
            handle = workspace._handles[described["session_id"]]
            self.assertTrue(handle.finished.wait(timeout=5))
        self.assertIn("内部异常", handle.error)
        self.assertEqual(handle.exit_code, 1)
        self.assertEqual(workspace.get_proposal(described["session_id"])["status"], "failed")

    def test_stop_requests_a_graceful_stop(self):
        described = self.workspace.create_proposal({"idea": "要被停止的提案"})
        self.assertTrue(self.pipeline.started.wait(timeout=5))
        self.assertTrue(self.workspace.stop_proposal(described["session_id"]))
        self.pipeline.finish()

    def test_stop_is_refused_for_finished_or_unknown(self):
        described = self.workspace.create_proposal({"idea": "会跑完的提案"})
        self.pipeline.finish()
        self.workspace._handles[described["session_id"]].finished.wait(timeout=5)
        self.assertFalse(self.workspace.stop_proposal(described["session_id"]))
        self.assertFalse(self.workspace.stop_proposal("没有这一份"))

    def test_interjection_is_accepted_only_while_running(self):
        described = self.workspace.create_proposal({"idea": "要插话的提案"})
        session_id = described["session_id"]
        self.assertTrue(self.pipeline.started.wait(timeout=5))
        self.assertTrue(self.workspace.interject(session_id, "离线是硬要求"))
        handle = self.workspace._handles[session_id]
        self.assertEqual(handle.session.interjections[0]["text"], "离线是硬要求")
        self.assertEqual(handle.interjections.pending_count, 1)
        self.pipeline.finish()
        handle.finished.wait(timeout=5)
        self.assertFalse(self.workspace.interject(session_id, "太晚了"))
        self.assertFalse(self.workspace.interject("没有这一份", "谁"))

    def test_subscribe_of_a_live_proposal_uses_its_hub(self):
        described = self.workspace.create_proposal({"idea": "要订阅的提案"})
        self.pipeline.started.wait(timeout=5)
        subscription = self.workspace.subscribe(described["session_id"])
        self.assertIsNotNone(subscription)
        queue_, _history = subscription
        self.workspace._handles[described["session_id"]].session.emit(
            "warning", code="demo", message="来一条"
        )
        self.assertEqual(queue_.get(timeout=5).type, "warning")
        self.pipeline.finish()

    def test_subscribe_of_a_finished_proposal_replays_the_transcript(self):
        self.make_session_dir("20261012-100000-甲", transcript=[
            {"type": "session.start", "session_id": "20261012-100000-甲", "seq": 1,
             "ts": "t", "data": {}},
            {"type": "session.end", "session_id": "20261012-100000-甲", "seq": 2,
             "ts": "t", "data": {"status": "completed"}},
        ])
        queue_, history = self.workspace.subscribe("20261012-100000-甲")
        self.assertEqual([e.type for e in history], ["session.start", "session.end"])
        self.assertTrue(queue_.empty(), "重放历史不该有实时事件")
        self.assertFalse(self.workspace.is_live("20261012-100000-甲"))

    def test_subscribe_of_unknown_proposal_is_none(self):
        self.assertIsNone(self.workspace.subscribe("没有这一份"))

    def test_unsubscribe_is_safe_for_a_replay_only_subscription(self):
        """重放历史没有在任何 hub 里登记过，退订必须是无事可做而不是报错。"""
        import queue as queue_mod

        self.make_session_dir("20261012-100000-甲")
        self.workspace.unsubscribe("20261012-100000-甲", queue_mod.Queue())
        self.workspace.unsubscribe("没有这一份", queue_mod.Queue())

    def test_interrupt_all_marks_running_proposals(self):
        described = self.workspace.create_proposal({"idea": "会被 Ctrl-C 打断的提案"})
        self.pipeline.started.wait(timeout=5)
        interrupted = self.workspace.interrupt_all()
        self.assertEqual(interrupted, [described["session_id"]])
        item = self.workspace.get_proposal(described["session_id"])
        self.assertEqual(item["status"], "interrupted")
        self.pipeline.finish()

    def test_interrupt_all_is_empty_when_nothing_runs(self):
        self.assertEqual(self.workspace.interrupt_all(), [])


if __name__ == "__main__":
    unittest.main()
