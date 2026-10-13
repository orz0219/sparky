"""Markdown 渲染器的测试：块级、行内与转义三层。

渲染器是纯函数（无 IO），特别值得测，因为它挡在**模型产出的原文**与
浏览器的 HTML 之间：

  1. 转义必须先于行内替换 —— 顺序反了，``design.md`` 里出现的任何
     ``<script>`` 就会真的在页面上执行；
  2. 不支持的构造（链接、图片、斜体……）必须原样出现在输出里 ——
     这是「前端永不隐藏已发生的事实」的底线，丢弃比渲染得难看严重得多。
"""

from __future__ import annotations

import unittest

from sparky.markdown import render


class TestHeadings(unittest.TestCase):
    def test_each_level(self):
        """# 到 ###### 逐级对应 h1–h6，级别错位会毁掉整篇文档的层次。"""
        for level in range(1, 7):
            with self.subTest(level=level):
                out = render("#" * level + " 标题")
                self.assertEqual(out, f"<h{level}>标题</h{level}>")

    def test_more_than_six_hashes_is_h6(self):
        """HTML 只有 h6：####### 及以上按 h6 处理，而不是丢弃整行。"""
        self.assertEqual(render("####### 太深了"), "<h6>太深了</h6>")
        self.assertEqual(render("########## 更深"), "<h6>更深</h6>")

    def test_hash_without_space_is_not_a_heading(self):
        """#标签 是普通文本，不是标题；判据是 # 后面必须有空格。"""
        self.assertEqual(render("#标签"), "<p>#标签</p>")


class TestLists(unittest.TestCase):
    def test_unordered_consecutive_lines_merge(self):
        out = render("- 甲\n- 乙\n* 丙")
        self.assertEqual(out, "<ul><li>甲</li><li>乙</li><li>丙</li></ul>")

    def test_ordered_any_number_consecutive_lines_merge(self):
        out = render("1. 甲\n2. 乙\n7. 丙")
        self.assertEqual(out, "<ol><li>甲</li><li>乙</li><li>丙</li></ol>")

    def test_list_types_do_not_merge_into_one_list(self):
        """无序号项与有序号项交替时必须断开，否则编号会被渲染成无序项。"""
        out = render("- 甲\n1. 乙")
        self.assertEqual(out, "<ul><li>甲</li></ul>\n<ol><li>乙</li></ol>")

    def test_blank_line_ends_the_list(self):
        out = render("- 甲\n\n- 乙")
        self.assertEqual(out, "<ul><li>甲</li></ul>\n<ul><li>乙</li></ul>")

    def test_plain_line_ends_the_list(self):
        out = render("- 甲\n收尾说明")
        self.assertEqual(out, "<ul><li>甲</li></ul>\n<p>收尾说明</p>")

    def test_indented_marker_is_not_a_list_item(self):
        """缩进的列表标记不是列表项（列表标记必须在行首），按段落原样输出。"""
        self.assertEqual(render("  - 缩进项"), "<p>- 缩进项</p>")

    def test_list_item_parses_inline_constructs(self):
        self.assertEqual(render("- 用 **粗体** 与 `代码`"), "<ul><li>用 <strong>粗体</strong> 与 <code>代码</code></li></ul>")


class TestTables(unittest.TestCase):
    def test_renders_header_and_body(self):
        out = render(
            "| 方案 | 结论 |\n"
            "|---|---|\n"
            "| 方案一 | 采纳 |\n"
            "| 方案二 | 否决 |"
        )
        self.assertEqual(
            out,
            "<table><thead><tr><th>方案</th><th>结论</th></tr></thead>"
            "<tbody>"
            "<tr><td>方案一</td><td>采纳</td></tr>"
            "<tr><td>方案二</td><td>否决</td></tr>"
            "</tbody></table>",
        )

    def test_extra_cells_are_truncated_to_header_width(self):
        """多出来的格截断到表头列数 —— 否则表体的列会和表头错位。"""
        out = render("| 甲 | 乙 |\n|---|---|\n| 一 | 二 | 三 |")
        self.assertIn("<tr><td>一</td><td>二</td></tr>", out)
        self.assertNotIn("三", out)

    def test_missing_cells_are_padded_to_header_width(self):
        """少掉的格补空 td —— 否则该行后面的列会整体左移。"""
        out = render("| 甲 | 乙 |\n|---|---|\n| 一 |")
        self.assertIn("<tr><td>一</td><td></td></tr>", out)

    def test_alignment_colons_are_accepted_in_separator(self):
        out = render("| 甲 | 乙 |\n|:---|---:|\n| 一 | 二 |")
        self.assertIn("<td>一</td><td>二</td>", out)

    def test_separator_row_is_not_a_horizontal_rule(self):
        """分隔行整行由 - 组成，但含 |，不能被误判成 <hr> 而吃掉整个表格。"""
        out = render("| 甲 |\n|---|\n| 一 |")
        self.assertIn("<table>", out)
        self.assertNotIn("<hr>", out)

    def test_body_absorbs_following_pipe_line(self):
        """表体规则是「非空且含 |」：紧跟其后、含 | 的行算数据行，不含的收口。"""
        out = render("| 甲 | 乙 |\n|---|---|\n| 一 | 二 |\n裸行 | 也有管道\n\n普通段落")
        self.assertIn("<tr><td>裸行</td><td>也有管道</td></tr>", out)
        self.assertIn("</table>", out)
        self.assertIn("<p>普通段落</p>", out)

    def test_table_cells_parse_inline_constructs(self):
        out = render("| 甲 |\n|---|\n| **重点** |")
        self.assertIn("<td><strong>重点</strong></td>", out)


class TestHorizontalRule(unittest.TestCase):
    def test_triple_dash_alone_is_a_rule(self):
        self.assertEqual(render("---"), "<hr>")

    def test_longer_dash_lines_are_rules(self):
        self.assertEqual(render("-----"), "<hr>")

    def test_two_dashes_are_not_a_rule(self):
        """不足 3 个 - 不构成水平线，按普通文本输出而不是丢弃。"""
        self.assertEqual(render("--"), "<p>--</p>")

    def test_dashes_with_text_are_not_a_rule(self):
        self.assertEqual(render("--- 不是水平线"), "<p>--- 不是水平线</p>")

    def test_rule_between_paragraphs(self):
        out = render("前文\n\n---\n\n后文")
        self.assertEqual(out, "<p>前文</p>\n<hr>\n<p>后文</p>")


class TestFencedCode(unittest.TestCase):
    def test_language_tag_is_ignored(self):
        out = render("```python\nprint(1)\n```")
        self.assertEqual(out, "<pre><code>print(1)</code></pre>")
        self.assertNotIn("python", out)

    def test_inner_constructs_are_not_parsed(self):
        """代码块里只转义、不解析：** 与反引号必须保持字面。"""
        out = render("```\n**不加粗** `也不代码`\n```")
        self.assertEqual(out, "<pre><code>**不加粗** `也不代码`</code></pre>")

    def test_content_is_escaped(self):
        out = render("```\n<div>a & b</div>\n```")
        self.assertEqual(out, "<pre><code>&lt;div&gt;a &amp; b&lt;/div&gt;</code></pre>")

    def test_multiline_content_keeps_line_breaks(self):
        out = render("```\n第一行\n第二行\n```")
        self.assertEqual(out, "<pre><code>第一行\n第二行</code></pre>")

    def test_unclosed_fence_raises(self):
        """未闭合就报错：把剩下的文档猜成代码块会悄悄改变其后所有内容。"""
        with self.assertRaises(ValueError) as ctx:
            render("```\n没有收尾")
        self.assertIn("没有闭合", str(ctx.exception))


class TestBlockquote(unittest.TestCase):
    def test_consecutive_lines_merge_into_one_paragraph(self):
        self.assertEqual(render("> 甲\n> 乙"), "<blockquote><p>甲 乙</p></blockquote>")

    def test_plain_line_ends_the_quote(self):
        out = render("> 甲\n收尾")
        self.assertEqual(out, "<blockquote><p>甲</p></blockquote>\n<p>收尾</p>")


class TestParagraph(unittest.TestCase):
    def test_wrapped_lines_merge_with_single_space(self):
        self.assertEqual(render("第一行\n第二行"), "<p>第一行 第二行</p>")

    def test_blank_line_separates_paragraphs(self):
        self.assertEqual(render("第一段\n\n第二段"), "<p>第一段</p>\n<p>第二段</p>")

    def test_heading_interrupts_paragraph(self):
        self.assertEqual(render("说明\n# 标题"), "<p>说明</p>\n<h1>标题</h1>")


class TestInline(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(render("这是 **重点**"), "<p>这是 <strong>重点</strong></p>")

    def test_inline_code(self):
        self.assertEqual(render("用 `code()` 调用"), "<p>用 <code>code()</code> 调用</p>")

    def test_bold_and_code_together(self):
        self.assertEqual(
            render("**重点**与`code`"),
            "<p><strong>重点</strong>与<code>code</code></p>",
        )

    def test_bold_inside_code_stays_literal(self):
        """单轮扫描的意义：代码里的 ** 不会被第二轮替换成 <strong>。"""
        self.assertEqual(render("`a**b**c`"), "<p><code>a**b**c</code></p>")

    def test_unpaired_markers_stay_literal(self):
        self.assertEqual(render("**没有收尾"), "<p>**没有收尾</p>")


class TestEscaping(unittest.TestCase):
    def test_script_tag_is_escaped(self):
        """原文里的 <script> 必须以实体形式出现在输出里，绝不真实成标签。"""
        out = render("<script>alert(1)</script>")
        self.assertEqual(out, "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
        self.assertNotIn("<script", out)

    def test_ampersand_in_heading(self):
        self.assertEqual(render("# 需求 & 约束"), "<h1>需求 &amp; 约束</h1>")

    def test_less_than_in_table_cell(self):
        out = render("| 甲 |\n|---|\n| <b> |")
        self.assertIn("<td>&lt;b&gt;</td>", out)

    def test_less_than_in_code_block(self):
        out = render("```\n<script>\n```")
        self.assertEqual(out, "<pre><code>&lt;script&gt;</code></pre>")

    def test_escape_runs_before_inline_substitution(self):
        """顺序证据：粗体记号里的标签被转义，说明先转义、后替换。"""
        out = render("**<b>x</b>**")
        self.assertEqual(out, "<p><strong>&lt;b&gt;x&lt;/b&gt;</strong></p>")

    def test_ampersand_is_not_double_escaped_in_plain_text(self):
        self.assertEqual(render("A & B"), "<p>A &amp; B</p>")


class TestUnsupportedSyntaxIsKept(unittest.TestCase):
    """不支持的构造按普通段落原样输出 —— 丢弃比难看严重得多。"""

    def test_link_markup_survives(self):
        self.assertEqual(render("[链接](http://x)"), "<p>[链接](http://x)</p>")

    def test_image_markup_survives(self):
        self.assertEqual(render("![图](y.png)"), "<p>![图](y.png)</p>")

    def test_italic_and_strikethrough_survive(self):
        self.assertEqual(render("*斜体* 与 ~~删除线~~"), "<p>*斜体* 与 ~~删除线~~</p>")

    def test_setext_heading_survives_as_paragraph(self):
        """Setext 标题不在子集内：两行合成一段，内容一字不少。"""
        self.assertEqual(render("标题\n==="), "<p>标题 ===</p>")


class TestOutputShape(unittest.TestCase):
    def test_blocks_are_separated_by_newline(self):
        self.assertEqual(render("# 标题\n\n段落"), "<h1>标题</h1>\n<p>段落</p>")

    def test_output_is_a_fragment(self):
        out = render("# 标题\n\n段落")
        self.assertNotIn("<html>", out)
        self.assertNotIn("<body>", out)

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(render(""), "")

    def test_whitespace_only_input_returns_empty_string(self):
        self.assertEqual(render("   \n\n\t\n  "), "")

    def test_non_string_input_raises_type_error(self):
        for value in (None, 123, b"# heading", ["# heading"], True):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    render(value)


if __name__ == "__main__":
    unittest.main()
