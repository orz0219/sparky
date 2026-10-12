"""流式 ``say`` 抽取的测试。

``SayExtractor`` 是打字机效果的核心：它要从**流式 JSON 的增量**里
把人话抽出来，而不是把裸 JSON 逐字显示给用户。

它最容易错的地方是**分片边界**：网络分片可能正好切在转义序列中间
（``\\`` 与 ``n`` 之间、``\\u12`` 与 ``34`` 之间）。猜错会显示出一个
不存在的字符，而且这种 bug 只在真实网络下偶发 —— 所以必须专门测。
"""

from __future__ import annotations

import unittest

from sparky.errors import LlmError
from sparky.llm import SayExtractor


def feed_all(chunks: list[str]) -> tuple[str, SayExtractor]:
    extractor = SayExtractor()
    out = ""
    for chunk in chunks:
        out += extractor.feed(chunk)
    return out, extractor


class TestBasicExtraction(unittest.TestCase):
    def test_extracts_say_value(self):
        text, _ = feed_all(['{"say": "我反对这个方案。", "responds_to": []}'])
        self.assertEqual(text, "我反对这个方案。")

    def test_stops_at_closing_quote(self):
        text, extractor = feed_all(
            ['{"say": "说完了。", "new_points": ["不该显示这句"]}']
        )
        self.assertEqual(text, "说完了。")
        self.assertTrue(extractor.closed)

    def test_nothing_before_the_key(self):
        text, extractor = feed_all(['{"ro'])
        self.assertEqual(text, "")
        self.assertFalse(extractor.started)

    def test_no_say_field_yields_nothing(self):
        """模型没按 schema 写时不吐任何东西 —— 界面停在"正在生成…"，
        这不是兜底失败，是如实反映"还没有可显示的话"。"""
        text, extractor = feed_all(['{"responds_to": [{"disagreement_id": "x1"}]}'])
        self.assertEqual(text, "")
        self.assertFalse(extractor.started)

    def test_chunked_by_character(self):
        """极端情况：一个字一个字地喂（真实网络就是这样）。"""
        payload = '{"say": "逐字到达也要正确。", "x": 1}'
        text, _ = feed_all(list(payload))
        self.assertEqual(text, "逐字到达也要正确。")

    def test_after_closing_quote_nothing_more(self):
        extractor = SayExtractor()
        extractor.feed('{"say": "第一句。"}')
        self.assertEqual(extractor.feed(', "x": "不该出现"}'), "")


class TestEscapeBoundaries(unittest.TestCase):
    """分片切在转义序列中间 —— 这是最容易出错、也最难复现的一类。"""

    def test_backslash_split_from_escape_char(self):
        text, _ = feed_all(['{"say": "换行\\', 'n后面"}'])
        self.assertEqual(text, "换行\n后面")

    def test_unicode_escape_split(self):
        text, _ = feed_all(['{"say": "\\u00', '41结尾"}'])
        self.assertEqual(text, "A结尾")

    def test_unicode_escape_split_into_three(self):
        text, _ = feed_all(['{"say": "\\u', '00', '4', '1"}'])
        self.assertEqual(text, "A")

    def test_escaped_quote_does_not_close_the_string(self):
        text, extractor = feed_all(['{"say": "他说\\"不行\\"。", "x": 1}'])
        self.assertEqual(text, '他说"不行"。')
        self.assertTrue(extractor.closed)

    def test_escaped_backslash(self):
        text, _ = feed_all(['{"say": "路径 C:\\\\tmp"}'])
        self.assertEqual(text, "路径 C:\\tmp")

    def test_tab_and_newline_escapes(self):
        text, _ = feed_all(['{"say": "甲\\t乙\\n丙"}'])
        self.assertEqual(text, "甲\t乙\n丙")

    def test_invalid_unicode_escape_is_an_error(self):
        """非法 \\u 转义不猜 —— 猜错会显示一个不存在的字符。"""
        with self.assertRaises(LlmError):
            feed_all(['{"say": "\\uZZZZ"}'])


class TestBufferBound(unittest.TestCase):
    def test_buffer_does_not_grow_forever_without_the_key(self):
        extractor = SayExtractor()
        for _ in range(500):
            extractor.feed("填充" * 100)
        self.assertLess(len(extractor._buf), 20000, "say 键一直不出现时 buffer 要有上限")

    def test_key_still_found_after_truncation(self):
        """截断只发生在键还没出现的阶段，键一旦出现就不会再截断。"""
        extractor = SayExtractor()
        extractor.feed("{" + "x" * 10000)
        out = extractor.feed('"say": "找到了。"}')
        self.assertEqual(out, "找到了。")


class TestProperties(unittest.TestCase):
    def test_started_and_closed_flags(self):
        extractor = SayExtractor()
        self.assertFalse(extractor.started)
        extractor.feed('{"say": "甲')
        self.assertTrue(extractor.started)
        self.assertFalse(extractor.closed)
        extractor.feed('乙"}')
        self.assertTrue(extractor.closed)


if __name__ == "__main__":
    unittest.main()
