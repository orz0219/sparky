"""S4 收敛判定的测试。

``consensus.decide`` 是纯函数，是本项目最该测的模块 ——
它决定"讨论什么时候停"，而终止条件一旦出错，症状是
「要么早早停下、要么永远不停」，两种都很难在事后察觉。

**六个判定分支逐个覆盖**，外加**判断顺序**（顺序错了会在边界情况下误判）。
"""

from __future__ import annotations

import unittest

from sparky import consensus
from sparky.consensus import (
    MAX_FOCUS_PER_ITEM,
    REASON_BUDGET,
    REASON_DEADLOCK,
    REASON_MAX_ROUNDS,
    REASON_STAGNATION,
    decide,
    finalize,
    is_harmonious,
)
from sparky.models import (
    DECISION_ANOTHER_ROUND,
    DECISION_DONE,
    DECISION_FORCE_STOP,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_STALEMATE,
    STATUS_TRADEOFF,
    Budget,
    DebateResult,
    Ledger,
)

from .support import make_disagreement, make_ledger


def budget(*, tokens: int = 100, limit: int | None = None) -> Budget:
    return Budget(total_tokens=tokens, limit=limit)


class TestBranchOneDone(unittest.TestCase):
    """分支 1：没有未决分歧 → done。"""

    def test_empty_ledger_is_done(self):
        decision = decide(ledger=Ledger(), round_no=1, budget=budget(), max_rounds=4)
        self.assertEqual(decision.kind, DECISION_DONE)

    def test_only_settled_items_is_done(self):
        ledger = make_ledger(
            [
                make_disagreement("x1", status=STATUS_RESOLVED),
                make_disagreement("x2", status=STATUS_TRADEOFF),
            ]
        )
        decision = decide(ledger=ledger, round_no=2, budget=budget(), max_rounds=4)
        self.assertEqual(decision.kind, DECISION_DONE)

    def test_open_item_prevents_done(self):
        ledger = make_ledger([make_disagreement("x1", status=STATUS_OPEN)])
        decision = decide(ledger=ledger, round_no=1, budget=budget(), max_rounds=4)
        self.assertNotEqual(decision.kind, DECISION_DONE)

    def test_stalemate_prevents_done(self):
        """僵局也是未决 —— 它必须进入文档的第 8 节，不能被当成收敛。"""
        ledger = make_ledger([make_disagreement("x1", status=STATUS_STALEMATE)])
        decision = decide(ledger=ledger, round_no=1, budget=budget(), max_rounds=4)
        self.assertNotEqual(decision.kind, DECISION_DONE)


class TestBranchOrder(unittest.TestCase):
    """判断顺序是规格的一部分。

    ``done`` 必须排在预算与轮数之前 —— 否则"刚好在最后一轮谈拢"会被
    误判成"没收敛"，用户就会看到一份带着虚假未决项的文档。
    """

    def test_done_beats_exhausted_budget(self):
        ledger = make_ledger([make_disagreement("x1", status=STATUS_RESOLVED)])
        decision = decide(
            ledger=ledger, round_no=99, budget=budget(tokens=999, limit=1), max_rounds=1
        )
        self.assertEqual(decision.kind, DECISION_DONE)

    def test_budget_beats_max_rounds(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(
            ledger=ledger, round_no=99, budget=budget(tokens=999, limit=1), max_rounds=1
        )
        self.assertEqual(decision.kind, DECISION_FORCE_STOP)
        self.assertEqual(decision.reason, REASON_BUDGET)

    def test_max_rounds_beats_stagnation(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(
            ledger=ledger,
            round_no=4,
            budget=budget(),
            max_rounds=4,
            stagnation=True,
        )
        self.assertEqual(decision.reason, REASON_MAX_ROUNDS)

    def test_stagnation_beats_deadlock(self):
        ledger = make_ledger(
            [make_disagreement("x1")], focus_counts={"x1": MAX_FOCUS_PER_ITEM}
        )
        decision = decide(
            ledger=ledger, round_no=2, budget=budget(), max_rounds=9, stagnation=True
        )
        self.assertEqual(decision.reason, REASON_STAGNATION)


class TestBranchTwoToFive(unittest.TestCase):
    """分支 2–5：四种 force_stop。"""

    def test_branch_two_budget_exhausted(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(
            ledger=ledger, round_no=1, budget=budget(tokens=500, limit=500), max_rounds=9
        )
        self.assertEqual(decision.kind, DECISION_FORCE_STOP)
        self.assertEqual(decision.reason, REASON_BUDGET)
        self.assertIn("x1", decision.detail)

    def test_budget_without_limit_never_exhausts(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(
            ledger=ledger, round_no=1, budget=budget(tokens=10**9, limit=None), max_rounds=9
        )
        self.assertEqual(decision.kind, DECISION_ANOTHER_ROUND)

    def test_branch_three_max_rounds(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(ledger=ledger, round_no=4, budget=budget(), max_rounds=4)
        self.assertEqual(decision.kind, DECISION_FORCE_STOP)
        self.assertEqual(decision.reason, REASON_MAX_ROUNDS)

    def test_branch_four_stagnation(self):
        ledger = make_ledger([make_disagreement("x1")])
        decision = decide(
            ledger=ledger, round_no=2, budget=budget(), max_rounds=9, stagnation=True
        )
        self.assertEqual(decision.kind, DECISION_FORCE_STOP)
        self.assertEqual(decision.reason, REASON_STAGNATION)

    def test_branch_five_deadlock(self):
        ledger = make_ledger(
            [make_disagreement("x1"), make_disagreement("x2")],
            focus_counts={"x1": MAX_FOCUS_PER_ITEM, "x2": MAX_FOCUS_PER_ITEM + 3},
        )
        decision = decide(ledger=ledger, round_no=3, budget=budget(), max_rounds=9)
        self.assertEqual(decision.kind, DECISION_FORCE_STOP)
        self.assertEqual(decision.reason, REASON_DEADLOCK)
        self.assertIn("x1", decision.detail)
        self.assertIn("x2", decision.detail)

    def test_one_item_under_cap_prevents_deadlock(self):
        ledger = make_ledger(
            [make_disagreement("x1"), make_disagreement("x2")],
            focus_counts={"x1": MAX_FOCUS_PER_ITEM, "x2": MAX_FOCUS_PER_ITEM - 1},
        )
        decision = decide(ledger=ledger, round_no=3, budget=budget(), max_rounds=9)
        self.assertEqual(decision.kind, DECISION_ANOTHER_ROUND)
        self.assertEqual(decision.focus, ("x2",))


class TestBranchSixAnotherRound(unittest.TestCase):
    """分支 6：定向加轮。**focus 永远非空** —— 从不"让全员再发一轮"。"""

    def test_another_round_carries_focus(self):
        ledger = make_ledger([make_disagreement("x1"), make_disagreement("x2")])
        decision = decide(ledger=ledger, round_no=1, budget=budget(), max_rounds=4)
        self.assertEqual(decision.kind, DECISION_ANOTHER_ROUND)
        self.assertEqual(decision.focus, ("x1", "x2"))

    def test_settled_items_are_not_focused(self):
        ledger = make_ledger(
            [
                make_disagreement("x1", status=STATUS_RESOLVED),
                make_disagreement("x2", status=STATUS_OPEN),
            ]
        )
        decision = decide(ledger=ledger, round_no=1, budget=budget(), max_rounds=4)
        self.assertEqual(decision.focus, ("x2",))

    def test_stalemate_is_still_focusable(self):
        """僵局正是定向加轮要处理的对象 —— 它不该被跳过。"""
        ledger = make_ledger([make_disagreement("x1", status=STATUS_STALEMATE)])
        decision = decide(ledger=ledger, round_no=2, budget=budget(), max_rounds=4)
        self.assertEqual(decision.kind, DECISION_ANOTHER_ROUND)
        self.assertEqual(decision.focus, ("x1",))

    def test_force_stop_also_reports_all_pending(self):
        ledger = make_ledger(
            [make_disagreement("x1"), make_disagreement("x2", status=STATUS_STALEMATE)]
        )
        decision = decide(ledger=ledger, round_no=4, budget=budget(), max_rounds=4)
        self.assertEqual(set(decision.focus), {"x1", "x2"})


class TestPurity(unittest.TestCase):
    """纯函数：不许改输入、不许有副作用。"""

    def test_decide_does_not_mutate_ledger(self):
        ledger = make_ledger([make_disagreement("x1")], focus_counts={"x1": 1})
        before = (len(ledger.items), dict(ledger.focus_counts), ledger.items[0].status)
        decide(ledger=ledger, round_no=1, budget=budget(), max_rounds=4)
        after = (len(ledger.items), dict(ledger.focus_counts), ledger.items[0].status)
        self.assertEqual(before, after)

    def test_same_input_same_output(self):
        ledger = make_ledger([make_disagreement("x1")])
        first = decide(ledger=ledger, round_no=2, budget=budget(), max_rounds=4)
        second = decide(ledger=ledger, round_no=2, budget=budget(), max_rounds=4)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_consensus_does_not_import_llm(self):
        """一旦它调了模型，终止条件就不可复现了。"""
        import sparky.consensus as module

        source = module.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("from .llm", text)
        self.assertNotIn("import llm", text)


class TestFinalize(unittest.TestCase):
    """收口：``open`` → ``stalemate``。已收敛的条目一律不动。"""

    def test_open_becomes_stalemate(self):
        item = make_disagreement("x1", status=STATUS_OPEN)
        ledger = make_ledger([item])
        changed = finalize(ledger, round_no=3)
        self.assertEqual(item.status, STATUS_STALEMATE)
        self.assertEqual([c.id for c in changed], ["x1"])
        self.assertTrue(item.history, "收口也要留下历史记录")

    def test_settled_items_are_untouched(self):
        resolved = make_disagreement("x1", status=STATUS_RESOLVED)
        tradeoff = make_disagreement("x2", status=STATUS_TRADEOFF)
        ledger = make_ledger([resolved, tradeoff])
        changed = finalize(ledger, round_no=3)
        self.assertEqual(changed, [])
        self.assertEqual(resolved.status, STATUS_RESOLVED)
        self.assertEqual(tradeoff.status, STATUS_TRADEOFF)

    def test_finalize_is_idempotent(self):
        ledger = make_ledger([make_disagreement("x1")])
        finalize(ledger, round_no=1)
        history_len = len(ledger.items[0].history)
        finalize(ledger, round_no=2)
        self.assertEqual(len(ledger.items[0].history), history_len, "第二次收口不该再记一笔")


class TestHarmonyDetection(unittest.TestCase):
    """零分歧是**失败信号**。它必须能被检出并暴露出来。"""

    def test_empty_ledger_is_harmonious(self):
        self.assertTrue(is_harmonious(Ledger(), DebateResult(ledger=Ledger())))

    def test_any_disagreement_is_not_harmonious(self):
        ledger = make_ledger([make_disagreement("x1")])
        self.assertFalse(is_harmonious(ledger, DebateResult(ledger=ledger)))

    def test_resolved_only_is_not_harmonious(self):
        """吵过又谈拢，不算"没吵起来" —— 那恰恰是最好的一种结果。"""
        ledger = make_ledger([make_disagreement("x1", status=STATUS_RESOLVED)])
        self.assertFalse(is_harmonious(ledger, DebateResult(ledger=ledger)))


class TestDescribe(unittest.TestCase):
    def test_describe_counts_every_status(self):
        ledger = make_ledger(
            [
                make_disagreement("x1", status=STATUS_RESOLVED),
                make_disagreement("x2", status=STATUS_TRADEOFF),
                make_disagreement("x3", status=STATUS_STALEMATE),
                make_disagreement("x4", status=STATUS_OPEN),
            ]
        )
        text = consensus.describe(ledger)
        self.assertIn("分歧 4 项", text)
        self.assertIn("已解决 1", text)
        self.assertIn("取舍 1", text)
        # 「未决」= open + stalemate，与终端渲染、文档章节用的是同一个口径
        self.assertIn("未决 2", text)
        self.assertIn("僵局 1", text)


if __name__ == "__main__":
    unittest.main()
