"""合规校验与文档装配的测试。

`validate_document` 是纯函数，是这个项目里最好测、也最该测的部分 ——
它决定了"一份文档能不能被交付"。每一项检查都要有**通过**与**不通过**两侧。
"""

from __future__ import annotations

import unittest

from sparky.synthesis import HEADINGS, MAX_SUMMARY_CHARS, validate_document

from .support import (
    VALID_DOCUMENT,
    TempSession,
    make_brief,
    make_panel,
    make_single_panel,
)


def check(document: str, *, brief=None, panel=None, tradeoffs: int = 0, stalemate: int = 0):
    # 默认用**单人**专家组 —— 与 VALID_DOCUMENT 的附录 A（1 条）对齐。
    # 需要制造不一致的用例自己传 panel=。
    return validate_document(
        document,
        brief=brief or make_brief(),
        panel=panel or make_single_panel(),
        tradeoff_count=tradeoffs,
        stalemate_count=stalemate,
    )


class TestValidDocument(unittest.TestCase):
    def test_fixture_passes_every_check(self):
        """夹具本身必须全过 —— 否则下面所有"破坏性"测试都失去意义。"""
        self.assertEqual(check(VALID_DOCUMENT), [])

    def test_fixture_has_no_missing_section(self):
        for heading in HEADINGS.values():
            self.assertIn(heading, VALID_DOCUMENT, f"夹具缺少 {heading}")


class TestSummaryCheck(unittest.TestCase):
    def test_missing_summary(self):
        doc = VALID_DOCUMENT.replace(HEADINGS["summary"], "## 0bis 别的")
        self.assertTrue(any("摘要" in p for p in check(doc)))

    def test_empty_summary(self):
        doc = VALID_DOCUMENT.replace(
            "这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。", ""
        )
        self.assertTrue(any("摘要为空" in p for p in check(doc)))

    def test_overlong_summary(self):
        doc = VALID_DOCUMENT.replace(
            "这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。",
            "字" * (MAX_SUMMARY_CHARS + 1),
        )
        problems = check(doc)
        self.assertTrue(any("超过上限" in p for p in problems))

    def test_summary_at_limit_passes(self):
        doc = VALID_DOCUMENT.replace(
            "这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。",
            "字" * MAX_SUMMARY_CHARS,
        )
        self.assertEqual(check(doc), [])


class TestCandidateChecks(unittest.TestCase):
    def test_single_candidate_rejected(self):
        doc = VALID_DOCUMENT.replace("| B | 方案B的思路 | 优点B | 缺点B | 否决 |\n", "")
        self.assertTrue(any("少于下限 2" in p for p in check(doc)))

    def test_missing_rejected_subsection(self):
        # 把 3.2 整个拿掉（换成别的编号），而不只是改标题文字
        doc = VALID_DOCUMENT.replace("### 3.2 被否决的方案", "### 3.9 其他")
        self.assertTrue(any("被否决的方案" in p for p in check(doc)))

    def test_rejected_subsection_without_reason(self):
        doc = VALID_DOCUMENT.replace(
            "- 否决理由：成本过高，且没有解决核心问题，现有数据不支持它的前提。\n",
            "",
        )
        self.assertTrue(any("被否决的方案" in p for p in check(doc)))


class TestTamperChecks(unittest.TestCase):
    """防篡改检查：这三项必须严格相等，是本项目拦截 LLM 篡改台账的核心机制。"""

    def test_tradeoff_row_count_must_match(self):
        doc = VALID_DOCUMENT.replace(
            "本次会话没有登记任何取舍项。",
            "| 取舍点 | 最终选择 | 放弃了什么 | 什么条件下该反过来选 |\n"
            "|---|---|---|---|\n"
            "| 甲 | A | B | C |\n",
        )
        problems = check(doc, tradeoffs=0)
        self.assertTrue(any("不一致" in p and "取舍" in p for p in problems))
        # 把台账条数改成与文档一致，就应该通过
        self.assertEqual(check(doc, tradeoffs=1), [])

    def test_open_question_count_must_match(self):
        doc = VALID_DOCUMENT.replace(
            "本次会话未运行讨论阶段。",
            "### 8.1 某议题\n\n- 甲方立场：A\n",
        )
        problems = check(doc, stalemate=0)
        self.assertTrue(any("不一致" in p and "未决" in p for p in problems))
        self.assertEqual(check(doc, stalemate=1), [])

    def test_appendix_persona_count_must_match(self):
        problems = check(VALID_DOCUMENT, panel=make_panel())  # 文档只有 1 人，队伍有 3 人
        self.assertTrue(any("附录 A" in p and "不一致" in p for p in problems))


class TestFailureModeTable(unittest.TestCase):
    def test_missing_633_subsection(self):
        doc = VALID_DOCUMENT.replace("### 6.3 失败模式表", "### 6.9 其他")
        self.assertTrue(any("6.3" in p for p in check(doc)))

    def test_too_few_rows(self):
        doc = VALID_DOCUMENT.replace("| 上游改格式 | 解析失败 | 解析失败率告警 |\n", "")
        self.assertTrue(any("少于下限 3" in p for p in check(doc)))

    def test_separator_rows_are_not_counted_as_data(self):
        """分隔行 |---|---| 不能被算成数据行，否则少一行也检测不出来。"""
        doc = VALID_DOCUMENT.replace(
            "| 上游改格式 | 解析失败 | 解析失败率告警 |\n",
            "|---|---|---|\n",
        )
        self.assertTrue(any("少于下限 3" in p for p in check(doc)))


class TestPlaceholderCheck(unittest.TestCase):
    def test_todo_token_anywhere_is_rejected(self):
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "TODO: 这里以后补")
        self.assertTrue(any("TODO" in p for p in check(doc)))

    def test_fixme_token_anywhere_is_rejected(self):
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "（FIXME 待查）")
        self.assertTrue(any("FIXME" in p for p in check(doc)))

    def test_whole_line_placeholder_is_rejected(self):
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "待补充")
        self.assertTrue(any("占位词" in p for p in check(doc)))

    def test_whole_line_ellipsis_is_rejected(self):
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "...")
        self.assertTrue(any("占位词" in p for p in check(doc)))

    def test_whole_line_tbd_with_period_is_rejected(self):
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "待定。")
        self.assertTrue(any("占位词" in p for p in check(doc)))

    def test_inline_pending_value_is_allowed(self):
        """表格里写「待定」是**诚实的**：表示讨论没把这个值定下来，不是没写完。

        实测证据：一次真实会话的参数表里就有
        `| vad.min_speech_ratio | 未给数值 | 待定 |`。
        """
        doc = VALID_DOCUMENT.replace(
            "本项目按阶段推进。",
            "| 参数 | 依据 | 取值 |\n|---|---|---|\n| vad.min_speech_ratio | 未给数值 | 待定 |",
        )
        self.assertEqual(check(doc), [])

    def test_inline_pending_word_in_prose_is_allowed(self):
        doc = VALID_DOCUMENT.replace(
            "本项目按阶段推进。", "具体阈值待定，需要实测后再填。"
        )
        self.assertEqual(check(doc), [])

    def test_trailing_ellipsis_in_prose_is_allowed(self):
        """中文里的"等等…"不该被误判成占位行。"""
        doc = VALID_DOCUMENT.replace("本项目按阶段推进。", "这里还有别的东西，等等…")
        self.assertEqual(check(doc), [])


class TestInferredMarkerCheck(unittest.TestCase):
    def test_marker_count_must_match_brief(self):
        problems = check(VALID_DOCUMENT, brief=make_brief(inferred_items=2))
        self.assertTrue(any("(推断)" in p for p in problems))

    def test_extra_marker_rejected(self):
        doc = VALID_DOCUMENT.replace("- 离线运行", "- 离线运行 `(推断)`")
        problems = check(doc)
        self.assertTrue(any("(推断)" in p for p in problems))

    def test_zero_inferred_items(self):
        doc = VALID_DOCUMENT.replace("- 推断前提0 `(推断)`", "（本次没有产生额外的推断性前提。）")
        self.assertEqual(check(doc, brief=make_brief(inferred_items=0)), [])


class TestSectionContract(unittest.TestCase):
    """模板与代码的契约必须同步。

    这一组测的是**漂移**：改了 `prompts/synthesis.md` 却忘了改
    `SECTION_INPUTS`（或反之），会在测试阶段就被拦下，
    而不是等到某次真实运行烧掉几个调用之后才炸。
    """

    def test_every_llm_section_declares_its_inputs(self):
        from sparky.synthesis import LLM_SECTIONS, SECTION_INPUTS

        for key, _purpose in LLM_SECTIONS:
            self.assertIn(key, SECTION_INPUTS, f"{key} 没有声明所需占位符")
            self.assertTrue(SECTION_INPUTS[key], f"{key} 的占位符列表是空的")

    def test_declared_inputs_match_template_placeholders_exactly(self):
        from sparky.config import _PLACEHOLDER, load_prompt
        from sparky.synthesis import SECTION_INPUTS

        for key, declared in SECTION_INPUTS.items():
            with self.subTest(section=key):
                template = load_prompt("synthesis", key)
                actual = set(_PLACEHOLDER.findall(template))
                self.assertEqual(
                    set(declared),
                    actual,
                    f"{key}：SECTION_INPUTS 声明 {sorted(set(declared))}，"
                    f"模板实际需要 {sorted(actual)}",
                )

    def test_context_actually_supplies_everything_declared(self):
        from sparky.synthesis import SECTION_INPUTS, _build_context

        context = _build_context(make_brief(), make_single_panel(), _empty_outcome())
        for key, declared in SECTION_INPUTS.items():
            for name in declared:
                self.assertIn(name, context, f"{key} 声明了 {name}，但 _build_context 没给")

    def test_every_llm_section_declares_thinking(self):
        """每节都要明确"要不要推理"。漏了会默认开推理，可能烧光预算。"""
        from sparky.synthesis import LLM_SECTIONS, SECTION_THINKING

        for key, _purpose in LLM_SECTIONS:
            self.assertIn(key, SECTION_THINKING, f"{key} 没有声明是否开推理")
            self.assertIsInstance(SECTION_THINKING[key], bool)

    def test_mechanical_sections_have_thinking_off(self):
        """转写/制表类的节必须关推理 —— 实测它们会把预算全烧在 reasoning 上。"""
        from sparky.synthesis import SECTION_THINKING

        for key in ("summary", "problem", "candidates", "milestones"):
            with self.subTest(section=key):
                self.assertFalse(
                    SECTION_THINKING[key],
                    f"{key} 是机械性转写，应当关闭推理",
                )


def _empty_outcome():
    from sparky.models import ReconOutcome

    return ReconOutcome()


class TestSummaryRewrite(unittest.TestCase):
    """摘要超长时要求模型**重写**，而不是截断。

    实测证据：一次真实会话因摘要 456 字（超上限 14%）把整场 16 次调用判废。
    截断会切在句子中间并替模型决定丢掉什么信息，所以只能让它自己压缩。
    """

    class FakeClient:
        def __init__(self, replies):
            self._replies = list(replies)
            self.systems: list[str] = []

        def complete(self, *, system, user, purpose, **kwargs):  # noqa: D102
            from sparky.llm import LlmReply
            from sparky.models import Usage

            self.systems.append(system)
            text = self._replies.pop(0)
            return LlmReply(
                content=text,
                usage=Usage(calls=1),
                finish_reason="stop",
                model="fake",
                call_index=len(self.systems),
            )

    def setUp(self):
        from types import SimpleNamespace

        self.temp = TempSession()
        self.addCleanup(self.temp.cleanup)
        self.session = self.temp.session
        self.runtime = SimpleNamespace(model_strong="fake-model")
        self.context = {"brief_json": "{}", "panel_summary": "x", "stances_summary": "y"}

    def _run(self, replies):
        from sparky.synthesis import _fill_summary

        client = self.FakeClient(replies)
        body = _fill_summary(
            client, self.runtime, "SYSTEM", self.context, self.session
        )
        return body, client

    def test_within_limit_is_returned_as_is(self):
        body, client = self._run(["短摘要。"])
        self.assertEqual(body, "短摘要。")
        self.assertEqual(len(client.systems), 1, "没超限就不该重写")

    def test_overlong_triggers_one_rewrite(self):
        body, client = self._run(["字" * 500, "字" * 300])
        self.assertEqual(len(body), 300)
        self.assertEqual(len(client.systems), 2, "应当重写一次")
        self.assertIn("400", client.systems[1], "重写的 system 里要说明上限")
        self.assertIn("500", client.systems[1], "重写的 system 里要给出实际字数")
        self.assertTrue(any("summary_overlong" in w for w in self.session.warnings))

    def test_still_overlong_after_retry_falls_through_to_validation(self):
        """两次都超长就交给合规校验报错 —— 不在这一层假装成功。"""
        body, client = self._run(["字" * 500, "字" * 480])
        self.assertEqual(len(body), 480)
        self.assertEqual(len(client.systems), 2)
        problems = check(VALID_DOCUMENT.replace(
            "这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。", body
        ))
        self.assertTrue(any("超过上限" in p for p in problems))

    def test_whitespace_does_not_count_toward_limit(self):
        body, client = self._run(["字" * 300 + "\n" * 50])
        self.assertEqual(len(client.systems), 1, "空白不该触发重写")


class TestCharCount(unittest.TestCase):
    def test_ignores_whitespace(self):
        from sparky.synthesis import char_count

        self.assertEqual(char_count("你好 世界\n\t！"), 5)

    def test_validator_and_rewrite_use_the_same_definition(self):
        """两处算法若不一致，会出现"重写通过但校验不通过"这种无法解释的情况。"""
        from sparky.synthesis import MAX_SUMMARY_CHARS, char_count

        body = "字 " * MAX_SUMMARY_CHARS  # 字之间有空格，有效字数正好等于上限
        self.assertEqual(char_count(body), MAX_SUMMARY_CHARS)
        doc = VALID_DOCUMENT.replace(
            "这是一个用于测试的最小项目，给测试用，核心思路是尽量简单。", body
        )
        self.assertEqual(check(doc), [])


if __name__ == "__main__":
    unittest.main()
