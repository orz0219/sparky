"""S3 提示词组装的**集成级**防漂移测试。

单独测 `_focus_items`、`_open_disagreement` 都不能保证"提示词真的组装得起来"——
真正的失败点是：`debate.py` 传给 `build_prompt` 的关键字与
`prompts/turn.md` 里的占位符对不上。那种错误只会在真实运行、
烧掉几次 API 调用之后才炸出来。

所以这里用一个假客户端**完整跑一遍**发言的组装路径，
把"模板与代码脱节"这件事拦在测试阶段。
"""

from __future__ import annotations

import json
import unittest

from sparky.config import _PLACEHOLDER, load_prompt
from sparky.debate import _collect_turns, _system_for
from sparky.errors import ConfigError
from sparky.llm import LlmReply
from sparky.models import Usage

from .support import TempSession, make_disagreement, make_ledger, make_outcome, make_panel

#: 每个提示词块**应当**需要的占位符。改模板不改这里，或反之，都会失败。
TURN_PROMPT_CONTRACT = {
    "system": {"name", "title", "stance", "cares_about"},
    "system_adversary": {"name", "stance", "bias"},
    "opening": {"brief_json", "stances_overview", "your_memo"},
    "focus": {"brief_json", "focus_items", "your_concession"},
}


class FakeTurnClient:
    """假 LLM：原样记下提示词，返回一份合法的空发言。

    注意 `systems` / `users` 是**按并发完成顺序**追加的，不是人设顺序 ——
    测试要按人设取提示词时用 `user_for()`，不要按下标对齐。
    """

    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {
            "responds_to": [],
            "new_points": [],
            "opens": [],
            "closes": [],
            "tradeoff_proposals": [],
            "stance_snapshot": {},
        }
        self.systems: list[str] = []
        self.users: list[str] = []
        self.purposes: list[str] = []

    def complete_json(self, *, system, user, purpose, **kwargs):  # noqa: D102
        self.systems.append(system)
        self.users.append(user)
        self.purposes.append(purpose)
        return dict(self.payload), LlmReply(
            content=json.dumps(self.payload, ensure_ascii=False),
            usage=Usage(calls=1),
            finish_reason="stop",
            model="fake",
            call_index=len(self.systems),
        )

    def user_for(self, name: str) -> str:
        """按人设名字取回它的 user 提示词（system 里含名字）。"""
        for system, user in zip(self.systems, self.users):
            if name in system:
                return user
        raise AssertionError(f"没有找到 {name} 的提示词；已捕获 {len(self.users)} 条")


class TestPromptContract(unittest.TestCase):
    def test_blocks_exist_and_are_non_empty(self):
        for block in TURN_PROMPT_CONTRACT:
            with self.subTest(block=block):
                self.assertTrue(load_prompt("turn", block).strip())

    def test_placeholders_match_the_contract(self):
        for block, expected in TURN_PROMPT_CONTRACT.items():
            with self.subTest(block=block):
                actual = set(_PLACEHOLDER.findall(load_prompt("turn", block)))
                self.assertEqual(
                    actual,
                    expected,
                    f"{block}：模板实际需要 {sorted(actual)}，契约声明 {sorted(expected)}",
                )

    def test_there_is_no_plain_round_block(self):
        """后续轮永远是定向加轮。留一个"普通后续轮"的提示词块就是死代码。"""
        with self.assertRaises(ConfigError):
            load_prompt("turn", "round")


class TestSystemFor(unittest.TestCase):
    """反方用**独立的 system**，不是追加限制 —— 两者的行为准则互相冲突。"""

    def setUp(self):
        self.panel = make_panel()  # p1/p2 specialist + p3 adversary

    def test_specialist_gets_regular_system(self):
        text = _system_for(self.panel.by_id("p1"))
        self.assertIn("专家1", text)
        self.assertNotIn("不许提出建设性替代方案", text)

    def test_adversary_gets_adversary_system(self):
        text = _system_for(self.panel.by_id("p3"))
        self.assertIn("不许提出建设性替代方案", text)
        # 反方的 system 里不该混进普通成员的规则
        self.assertNotIn("你必须选边", text)

    def test_both_variants_render_without_leftover_placeholders(self):
        for persona in self.panel.personas:
            with self.subTest(persona=persona.id):
                self.assertNotIn("${", _system_for(persona))


class TestCollectTurnsIntegration(unittest.TestCase):
    """完整跑一遍第 1 轮与定向加轮的组装 + 回收路径。"""

    def setUp(self):
        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session
        self.panel = make_panel()
        self.outcome = make_outcome(self.panel)

    def _collect(self, *, round_no, ledger=None, focus_ids=(), history=None, client=None):
        client = client or FakeTurnClient()
        participants = self.panel.personas
        return (
            _collect_turns(
                client=client,
                runtime=type("R", (), {"concurrency": 2, "model_fast": "fake"})(),
                session=self.session,
                panel=self.panel,
                participants=participants,
                brief_json=json.dumps({"goal": "测试目标"}, ensure_ascii=False),
                outcome=self.outcome,
                ledger=ledger or make_ledger(),
                focus_ids=focus_ids,
                history=history or [],
                round_no=round_no,
            ),
            client,
        )

    def test_opening_round_renders_for_every_persona(self):
        turns, client = self._collect(round_no=1)
        self.assertEqual(len(turns), len(self.panel.personas))
        self.assertEqual(len(client.users), len(self.panel.personas))
        for user in client.users:
            self.assertNotIn("${", user, "有占位符没被替换掉")
            self.assertIn("测试目标", user)

    def test_opening_round_shows_other_stances_but_not_own(self):
        _, client = self._collect(round_no=1)
        for persona in self.panel.personas:
            user = client.user_for(persona.name)
            with self.subTest(persona=persona.id):
                # 看得到别人的核心主张（信息面隔离只作用于 S2，讨论阶段交换立场）
                others = [p for p in self.panel.personas if p.id != persona.id]
                self.assertIn(f"{others[0].name}的核心主张", user)
                # 自己的备忘以另一段形式给出
                self.assertIn("你自己的调研备忘", user)
                # 自己的核心主张只出现在"我自己的备忘"里，不该混进他人立场清单
                overview = user.split("你自己的调研备忘")[0]
                self.assertNotIn(f"{persona.name}的核心主张", overview)

    def test_focus_round_only_gathers_parties(self):
        ledger = make_ledger([make_disagreement("x1", parties=("p1", "p2"))])
        participants = [p for p in self.panel.personas if p.id in ("p1", "p2")]
        client = FakeTurnClient()
        turns = _collect_turns(
            client=client,
            runtime=type("R", (), {"concurrency": 2, "model_fast": "fake"})(),
            session=self.session,
            panel=self.panel,
            participants=participants,
            brief_json="{}",
            outcome=self.outcome,
            ledger=ledger,
            focus_ids=("x1",),
            history=[],
            round_no=2,
        )
        self.assertEqual([t.persona_id for t in turns], ["p1", "p2"])
        for persona in participants:
            user = client.user_for(persona.name)
            self.assertIn("x1", user)
            self.assertIn("的立场：", user, "定向加轮的提示词里要摊开双方立场")

    def test_focus_round_hides_other_disagreements(self):
        ledger = make_ledger(
            [
                make_disagreement("x1", parties=("p1", "p2"), topic="甲议题"),
                make_disagreement("x2", parties=("p2", "p3"), topic="乙议题"),
            ]
        )
        participants = [p for p in self.panel.personas if p.id in ("p1", "p2")]
        client = FakeTurnClient()
        _collect_turns(
            client=client,
            runtime=type("R", (), {"concurrency": 2, "model_fast": "fake"})(),
            session=self.session,
            panel=self.panel,
            participants=participants,
            brief_json="{}",
            outcome=self.outcome,
            ledger=ledger,
            focus_ids=("x1", "x2"),
            history=[],
            round_no=2,
        )
        p1_prompt = client.user_for(self.panel.by_id("p1").name)
        self.assertIn("甲议题", p1_prompt)
        self.assertNotIn("乙议题", p1_prompt, "p1 不是 x2 的当事人")

    def test_failed_persona_becomes_explicit_abstention(self):
        """某人调用失败 → 该人记为**显式弃权**并留下原因，不伪造发言。

        这里抛 ``LlmError``（可预期的模型侧失败）—— 非 ``SparkyError``
        属于代码 bug，本来就该向上传播。
        """
        from sparky.errors import LlmError

        class Boom(FakeTurnClient):
            def complete_json(self, *, system, user, purpose, **kwargs):
                if "专家1" in system:
                    raise LlmError("模拟调用失败")
                return super().complete_json(
                    system=system, user=user, purpose=purpose, **kwargs
                )

        # 有实质内容的发言，才不算弃权 —— 空发言本身也是一种弃权
        payload = {
            "responds_to": [],
            "new_points": ["我的主张"],
            "opens": [
                {
                    "topic": "与 p2 的冲突",
                    "with": "p2",
                    "your_position": "甲",
                    "their_position": "乙",
                }
            ],
            "closes": [],
            "tradeoff_proposals": [],
            "stance_snapshot": {},
        }
        turns, _ = self._collect(round_no=1, client=Boom(payload))
        by_id = {t.persona_id: t for t in turns}
        self.assertTrue(by_id["p1"].abstained)
        self.assertIn("模拟调用失败", by_id["p1"].abstain_reason or "")
        self.assertFalse(by_id["p2"].abstained, "一个人失败不该影响别人")
        self.assertTrue(any("turn_failed" in w for w in self.session.warnings))

    def test_unexpected_exception_propagates(self):
        """非 SparkyError 是代码 bug，必须炸出来而不是被当成"某人弃权"。"""

        class Broken(FakeTurnClient):
            def complete_json(self, *, system, user, purpose, **kwargs):
                raise RuntimeError("这是代码 bug，不是模型失败")

        with self.assertRaises(RuntimeError):
            self._collect(round_no=1, client=Broken())

    def test_partially_failed_memo_still_renders(self):
        """有人调研失败时，他的立场在别人眼里是"缺席"，而不是空白。"""
        outcome = make_outcome(self.panel, failing=("p2",))
        client = FakeTurnClient()
        _collect_turns(
            client=client,
            runtime=type("R", (), {"concurrency": 2, "model_fast": "fake"})(),
            session=self.session,
            panel=self.panel,
            participants=[self.panel.by_id("p1")],
            brief_json="{}",
            outcome=outcome,
            ledger=make_ledger(),
            focus_ids=(),
            history=[],
            round_no=1,
        )
        self.assertIn("调研失败缺席", client.users[0])

    def test_turns_are_returned_in_persona_order(self):
        """并发执行、**按序回收** —— 会话的可复现性依赖这一点。"""
        for _ in range(5):
            turns, _ = self._collect(round_no=1)
            self.assertEqual(
                [t.persona_id for t in turns], [p.id for p in self.panel.personas]
            )


if __name__ == "__main__":
    unittest.main()
