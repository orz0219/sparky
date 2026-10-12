"""S3 交锋的测试：主持人登记台账的每一条规则。

主持人（``_register``）是纯代码 —— 台账是收敛判定的唯一依据，
让它经一次 LLM 转手就多一个"漏抄或篡改"的失败面。
所以这一层必须测得比提示词层更严。
"""

from __future__ import annotations

import unittest

from sparky.debate import (
    MAX_ABSTAIN_STREAK,
    RoundFingerprint,
    _fingerprint,
    _focus_items,
    _is_stagnant,
    _open_disagreement,
    _pick_participants,
    _register,
)
from sparky.errors import SchemaError
from sparky.models import (
    ACTION_REBUT,
    ACTION_SUPPORT,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_STALEMATE,
    STATUS_TRADEOFF,
)

from .support import (
    TempSession,
    make_close,
    make_disagreement,
    make_ledger,
    make_open,
    make_panel,
    make_persona,
    make_reply,
    make_tradeoff,
    make_turn,
)


class DebateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session
        self.panel = make_panel()  # p1, p2 specialist + p3 adversary


class TestOpenDisagreement(DebateTestCase):
    def test_creates_item_with_new_id(self):
        ledger = make_ledger()
        new_id = _open_disagreement(
            ledger, "p1", make_open(topic="要不要内置模型"), 1, self.session, self.panel
        )
        self.assertEqual(new_id, "x1")
        self.assertEqual(len(ledger.items), 1)
        item = ledger.items[0]
        self.assertEqual(item.parties, ["p1", "p2"])
        self.assertEqual(item.status, STATUS_OPEN)
        self.assertEqual(item.positions["p1"], "我这样")
        self.assertEqual(item.history[0]["action"], "open")
        self.assertEqual(ledger.opened_total, 1)

    def test_two_opens_get_distinct_ids(self):
        ledger = make_ledger()
        first = _open_disagreement(
            ledger, "p1", make_open(topic="议题甲"), 1, self.session, self.panel
        )
        second = _open_disagreement(
            ledger, "p2", make_open(topic="议题乙", with_persona="p3"), 1, self.session, self.panel
        )
        self.assertEqual((first, second), ("x1", "x2"))

    def test_self_conflict_is_ignored_with_warning(self):
        ledger = make_ledger()
        result = _open_disagreement(
            ledger, "p1", make_open(with_persona="p1"), 1, self.session, self.panel
        )
        self.assertIsNone(result)
        self.assertEqual(ledger.items, [])
        self.assertTrue(any("self_conflict" in w for w in self.session.warnings))

    def test_unknown_persona_is_ignored_with_warning(self):
        """一条坏引用不该废掉整场讨论 —— 记告警后忽略。"""
        ledger = make_ledger()
        result = _open_disagreement(
            ledger, "p1", make_open(with_persona="p99"), 1, self.session, self.panel
        )
        self.assertIsNone(result)
        self.assertTrue(any("unknown_persona_ref" in w for w in self.session.warnings))

    def test_duplicate_topic_between_same_parties_is_ignored(self):
        ledger = make_ledger()
        _open_disagreement(ledger, "p1", make_open(topic="同一个议题"), 1, self.session, self.panel)
        again = _open_disagreement(
            ledger, "p1", make_open(topic="同一个议题 "), 1, self.session, self.panel
        )
        self.assertIsNone(again)
        self.assertEqual(len(ledger.items), 1)
        self.assertTrue(any("duplicate_disagreement" in w for w in self.session.warnings))

    def test_same_topic_after_settlement_can_reopen(self):
        """已收敛的议题可以重新提出 —— 它可能是上一轮讨论暴露出的新层面。"""
        ledger = make_ledger([make_disagreement("x1", topic="同一个议题", status=STATUS_RESOLVED)])
        result = _open_disagreement(
            ledger, "p1", make_open(topic="同一个议题"), 2, self.session, self.panel
        )
        self.assertEqual(result, "x2")
        self.assertEqual(len(ledger.items), 2)


class TestRegisterCloses(DebateTestCase):
    def test_close_resolves_the_item(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p2", closes=[make_close("x1", by="convinced")])], 2,
                  self.session, self.panel)
        self.assertEqual(item.status, STATUS_RESOLVED)
        self.assertEqual(item.resolution["by"], "p2")
        self.assertEqual(item.resolution["kind"], "convinced")
        self.assertEqual(item.resolution["round"], 2)

    def test_close_of_unknown_id_is_ignored(self):
        ledger = make_ledger([make_disagreement("x1")])
        _register(ledger, [make_turn("p2", closes=[make_close("x99")])], 2,
                  self.session, self.panel)
        self.assertEqual(ledger.items[0].status, STATUS_OPEN)
        self.assertTrue(any("unknown_disagreement_ref" in w for w in self.session.warnings))

    def test_close_of_already_settled_is_ignored(self):
        item = make_disagreement("x1", status=STATUS_RESOLVED)
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p2", closes=[make_close("x1")])], 2,
                  self.session, self.panel)
        self.assertTrue(any("close_already_settled" in w for w in self.session.warnings))

    def test_close_and_tradeoff_on_same_item_prefers_close(self):
        """对同一项既说被说服又提议取舍 → 按更强的表态处理，并告警。"""
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        turn = make_turn("p2", closes=[make_close("x1")], tradeoffs=[make_tradeoff("x1")])
        _register(ledger, [turn], 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_RESOLVED)
        self.assertTrue(any("close_and_tradeoff_conflict" in w for w in self.session.warnings))

    def test_replies_are_recorded_in_history(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p1", responds_to=[make_reply("x1", reason="我的理由")])],
                  2, self.session, self.panel)
        entry = item.history[-1]
        self.assertEqual(entry["by"], "p1")
        self.assertEqual(entry["action"], ACTION_REBUT)
        self.assertEqual(entry["reason_key"], "我的理由")


class TestTradeoffAcceptance(DebateTestCase):
    """取舍的接受规则 —— 这是"把僵局变成条件判断"的落地点。"""

    def test_single_proposal_without_objection_is_accepted(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        turns = [
            make_turn("p1", tradeoffs=[make_tradeoff("x1", when_to_flip="如果体积不是瓶颈")]),
            make_turn("p2", responds_to=[make_reply("x1", action=ACTION_SUPPORT)]),
        ]
        _register(ledger, turns, 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_TRADEOFF)
        self.assertEqual(item.tradeoff["when_to_flip"], "如果体积不是瓶颈")
        self.assertIn("未明确反对", item.tradeoff["accepted_by"])

    def test_single_proposal_with_objection_is_not_accepted(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        turns = [
            make_turn("p1", tradeoffs=[make_tradeoff("x1")]),
            make_turn("p2", responds_to=[make_reply("x1", action=ACTION_REBUT)]),
        ]
        _register(ledger, turns, 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_OPEN)
        self.assertTrue(item.tradeoff, "提议本身要留痕，即使没被接受")

    def test_both_sides_proposing_is_accepted_even_with_rebuttal(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        turns = [
            make_turn("p1", tradeoffs=[make_tradeoff("x1", choose="选甲的")],
                      responds_to=[make_reply("x1", action=ACTION_REBUT)]),
            make_turn("p2", tradeoffs=[make_tradeoff("x1", choose="选乙的")],
                      responds_to=[make_reply("x1", action=ACTION_REBUT)]),
        ]
        _register(ledger, turns, 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_TRADEOFF)
        self.assertIn("双方都提了取舍", item.tradeoff["accepted_by"])

    def test_tradeoff_on_settled_item_is_ignored(self):
        item = make_disagreement("x1", status=STATUS_RESOLVED)
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p1", tradeoffs=[make_tradeoff("x1")])], 2,
                  self.session, self.panel)
        self.assertEqual(item.status, STATUS_RESOLVED)
        self.assertTrue(any("tradeoff_already_settled" in w for w in self.session.warnings))

    def test_accepted_tradeoff_emits_close_event(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        events: list[str] = []
        self.session.bus.subscribe(lambda e: events.append(e.type))
        _register(ledger, [make_turn("p1", tradeoffs=[make_tradeoff("x1")])], 2,
                  self.session, self.panel)
        self.assertIn("disagreement.close", events)


class TestStalemateDetection(DebateTestCase):
    """僵局：同一项分歧连续两轮的（谁、动作、归一化论据）完全一致。"""

    def test_identical_reasons_two_rounds_becomes_stalemate(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        turns = [
            make_turn("p1", responds_to=[make_reply("x1", reason="同样的理由")]),
            make_turn("p2", responds_to=[make_reply("x1", reason="另一个同样的理由")]),
        ]
        _register(ledger, turns, 1, self.session, self.panel)
        self.assertEqual(item.status, STATUS_OPEN, "只跑一轮还不该判僵局")
        _register(ledger, turns, 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_STALEMATE)

    def test_changed_reason_is_not_stalemate(self):
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p1", responds_to=[make_reply("x1", reason="理由一")])],
                  1, self.session, self.panel)
        _register(ledger, [make_turn("p1", responds_to=[make_reply("x1", reason="理由二")])],
                  2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_OPEN)

    def test_whitespace_differences_are_normalized(self):
        """归一化只做空白折叠 —— 模型把同一句话排个版不该被算成新论据。"""
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p1", responds_to=[make_reply("x1", reason="同 一个 理由")])],
                  1, self.session, self.panel)
        _register(ledger, [make_turn("p1", responds_to=[make_reply("x1", reason="同一个理由")])],
                  2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_STALEMATE)

    def test_empty_replies_do_not_trigger_stalemate(self):
        """两边都没回应（弃权）不该被当成"论据一致" —— 那是没有论据。"""
        item = make_disagreement("x1")
        ledger = make_ledger([item])
        _register(ledger, [make_turn("p1"), make_turn("p2")], 1, self.session, self.panel)
        _register(ledger, [make_turn("p1"), make_turn("p2")], 2, self.session, self.panel)
        self.assertEqual(item.status, STATUS_OPEN)


class TestPickParticipants(DebateTestCase):
    def test_first_round_includes_everyone(self):
        ledger = make_ledger()
        picked = _pick_participants(
            panel=self.panel, round_no=1, focus_ids=(), ledger=ledger,
            abstain_streak={}, session=self.session,
        )
        self.assertEqual(len(picked), len(self.panel.personas))

    def test_later_rounds_only_include_parties(self):
        ledger = make_ledger([make_disagreement("x1", parties=("p1", "p2"))])
        picked = _pick_participants(
            panel=self.panel, round_no=2, focus_ids=("x1",), ledger=ledger,
            abstain_streak={}, session=self.session,
        )
        self.assertEqual([p.id for p in picked], ["p1", "p2"])

    def test_uninvolved_persona_is_left_out(self):
        """定向加轮的核心收益：无关人等不来发言，也就不跑题。"""
        ledger = make_ledger([make_disagreement("x1", parties=("p1", "p3"))])
        picked = _pick_participants(
            panel=self.panel, round_no=2, focus_ids=("x1",), ledger=ledger,
            abstain_streak={}, session=self.session,
        )
        self.assertEqual([p.id for p in picked], ["p1", "p3"])

    def test_serial_abstainer_is_removed_with_warning(self):
        ledger = make_ledger([make_disagreement("x1", parties=("p1", "p2"))])
        picked = _pick_participants(
            panel=self.panel, round_no=3, focus_ids=("x1",), ledger=ledger,
            abstain_streak={"p1": MAX_ABSTAIN_STREAK}, session=self.session,
        )
        self.assertEqual([p.id for p in picked], ["p2"])
        self.assertTrue(any("persona_removed" in w for w in self.session.warnings))

    def test_all_parties_removed_yields_empty(self):
        ledger = make_ledger([make_disagreement("x1", parties=("p1", "p2"))])
        picked = _pick_participants(
            panel=self.panel, round_no=3, focus_ids=("x1",), ledger=ledger,
            abstain_streak={"p1": 9, "p2": 9}, session=self.session,
        )
        self.assertEqual(picked, [])


class TestFocusItems(DebateTestCase):
    def test_renders_only_own_items(self):
        ledger = make_ledger(
            [make_disagreement("x1", parties=("p1", "p2")),
             make_disagreement("x2", parties=("p2", "p3"))]
        )
        text = _focus_items(ledger, self.panel, ("x1", "x2"), persona_id="p1")
        self.assertIn("x1", text)
        self.assertNotIn("x2", text, "p1 不是 x2 的当事人，不该看到它的细节")

    def test_raises_when_persona_is_not_a_party(self):
        """口径不一致是内部错误，必须暴露而不是渲染出一段空内容。"""
        ledger = make_ledger([make_disagreement("x1", parties=("p2", "p3"))])
        with self.assertRaises(SchemaError):
            _focus_items(ledger, self.panel, ("x1",), persona_id="p1")

    def test_stalemate_is_flagged_to_the_model(self):
        ledger = make_ledger(
            [make_disagreement("x1", parties=("p1", "p2"), status=STATUS_STALEMATE)]
        )
        text = _focus_items(ledger, self.panel, ("x1",), persona_id="p1")
        self.assertIn("僵局", text)


class TestStagnation(unittest.TestCase):
    def test_single_round_is_never_stagnant(self):
        self.assertFalse(_is_stagnant([RoundFingerprint(round=1)]))

    def test_identical_triples_are_stagnant(self):
        first = RoundFingerprint(round=1, stances=(("x1", "against"),))
        second = RoundFingerprint(round=2, stances=(("x1", "against"),))
        self.assertTrue(_is_stagnant([first, second]))

    def test_changed_stance_is_not_stagnant(self):
        first = RoundFingerprint(round=1, stances=(("x1", "against"),))
        second = RoundFingerprint(round=2, stances=(("x1", "for"),))
        self.assertFalse(_is_stagnant([first, second]))

    def test_changed_closes_is_not_stagnant(self):
        first = RoundFingerprint(round=1, stances=(("x1", "against"),))
        second = RoundFingerprint(round=2, stances=(("x1", "against"),), closes=("p1:x1",))
        self.assertFalse(_is_stagnant([first, second]))

    def test_new_open_is_not_stagnant(self):
        first = RoundFingerprint(round=1, stances=(("x1", "against"),))
        second = RoundFingerprint(round=2, stances=(("x1", "against"),), opens=("x2",))
        self.assertFalse(_is_stagnant([first, second]))

    def test_fingerprint_ignores_unknown_disagreement_ids(self):
        """模型可能填一个不存在的编号 —— 它不该污染停滞检测。"""
        ledger = make_ledger([make_disagreement("x1")])
        turns = [make_turn("p1", stance_snapshot={"x1": "for", "x99": "against"})]
        fingerprint = _fingerprint(1, turns, [], ledger)
        self.assertEqual(fingerprint.stances, (("x1", "for"),))


class TestDisagreementCap(DebateTestCase):
    """分歧总数必须有上限，否则"真收敛"从设计上就不可达。

    实测教训（4 人、4 轮）：第 1 轮开 26 项，之后每轮每人再开约 1 项，
    总数到 50，12 项无解 —— `consensus.decide` 的 `done` 分支永远走不到。
    """

    def test_cap_formula(self):
        from sparky.debate import (
            MAX_DISAGREEMENTS_PER_PERSONA,
            MIN_DISAGREEMENT_CAP,
            disagreement_cap,
        )

        # 3 人 → 3×3=9，高于下限 8，取 9
        self.assertEqual(
            disagreement_cap(self.panel),
            max(MIN_DISAGREEMENT_CAP, MAX_DISAGREEMENTS_PER_PERSONA * len(self.panel.personas)),
        )
        # 加满到 7 人 → 21
        big = make_panel()
        while len(big.personas) < 7:
            big.personas.append(make_persona(len(big.personas) + 1))
        self.assertEqual(
            disagreement_cap(big), MAX_DISAGREEMENTS_PER_PERSONA * len(big.personas)
        )

    def test_cap_floor_applies_for_small_panels(self):
        """人少时下限兜底 —— 3 人以下的分歧空间被压得太小反而不好用。"""
        from sparky.debate import MIN_DISAGREEMENT_CAP, disagreement_cap

        tiny = make_panel()
        tiny.personas = tiny.personas[:2]
        self.assertEqual(disagreement_cap(tiny), MIN_DISAGREEMENT_CAP)

    def test_opens_below_cap_are_registered(self):
        from sparky.debate import disagreement_cap

        ledger = make_ledger()
        cap = disagreement_cap(self.panel)
        for index in range(cap):
            result = _open_disagreement(
                ledger, "p1", make_open(topic=f"议题{index}"), 1, self.session, self.panel
            )
            self.assertIsNotNone(result)
        self.assertEqual(len(ledger.items), cap)
        self.assertEqual(ledger.dropped_topics, [])

    def test_opens_beyond_cap_are_recorded_not_dropped(self):
        from sparky.debate import disagreement_cap

        cap = disagreement_cap(self.panel)
        ledger = make_ledger(
            [make_disagreement(f"x{i}", topic=f"已有{i}") for i in range(1, cap + 1)]
        )
        ledger.opened_total = cap
        result = _open_disagreement(
            ledger, "p1", make_open(topic="超出上限的议题"), 2, self.session, self.panel
        )
        self.assertIsNone(result)
        self.assertEqual(len(ledger.items), cap)
        self.assertEqual(
            ledger.dropped_topics, ["超出上限的议题"],
            "超出的议题必须留痕 —— 不能悄悄丢掉",
        )

    def test_register_emits_one_aggregated_warning_per_round(self):
        from sparky.debate import disagreement_cap

        cap = disagreement_cap(self.panel)
        ledger = make_ledger(
            [make_disagreement(f"x{i}", topic=f"已有{i}") for i in range(1, cap + 1)]
        )
        ledger.opened_total = cap
        turns = [
            make_turn("p1", opens=[make_open(topic="超出的甲"), make_open(topic="超出的乙")]),
            make_turn("p2", opens=[make_open(topic="超出的丙", with_persona="p3")]),
        ]
        _register(ledger, turns, 2, self.session, self.panel)
        cap_warnings = [w for w in self.session.warnings if "disagreement_cap_reached" in w]
        self.assertEqual(len(cap_warnings), 1, "同一轮只该发一条告警，不要刷屏")
        self.assertIn("超出的甲", cap_warnings[0])
        self.assertIn("超出的丙", cap_warnings[0])


class TestDroppedTopicsRendering(DebateTestCase):
    """超上限的议题要出现在附录 C 里，用户能看到它们。"""

    def test_appendix_lists_dropped_topics(self):
        from sparky.synthesis import _render_ledger_appendix

        ledger = make_ledger([make_disagreement("x1", topic="已登记的议题")])
        ledger.dropped_topics = ["没登记的甲", "没登记的乙"]
        text = "\n".join(_render_ledger_appendix(ledger, self.panel))
        self.assertIn("没登记的甲", text)
        self.assertIn("没登记的乙", text)
        self.assertIn("未被登记", text)

    def test_appendix_omits_section_when_nothing_dropped(self):
        from sparky.synthesis import _render_ledger_appendix

        ledger = make_ledger([make_disagreement("x1")])
        text = "\n".join(_render_ledger_appendix(ledger, self.panel))
        self.assertNotIn("未被登记", text)


class TestLedgerInvariants(DebateTestCase):
    def test_ledger_never_shrinks(self):
        ledger = make_ledger()
        ids = []
        for index in range(1, 5):
            ids.append(
                _open_disagreement(
                    ledger, "p1", make_open(topic=f"议题{index}"), 1, self.session, self.panel
                )
            )
        ledger.items[0].status = STATUS_RESOLVED
        self.assertEqual(len(ledger.items), 4, "已收敛的条目也必须留着")
        self.assertEqual(ids, ["x1", "x2", "x3", "x4"])

    def test_ids_are_not_recycled_after_settlement(self):
        ledger = make_ledger([make_disagreement("x1", status=STATUS_RESOLVED)])
        new_id = _open_disagreement(
            ledger, "p1", make_open(topic="新议题"), 2, self.session, self.panel
        )
        self.assertEqual(new_id, "x2", "编号不许回收 —— 回收会让历史记录指向错误的条目")


if __name__ == "__main__":
    unittest.main()
