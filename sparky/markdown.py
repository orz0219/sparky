"""Markdown → HTML 片段渲染器（服务端，供本地网页查看 design.md）。

职责
    把会话产出的 ``design.md`` 渲染成 HTML 片段，交给 ``sparky/server.py``
    塞进页面。**只做渲染**：不读文件、不写文件、不认识会话与模型层。

输入
    ``str``：Markdown 子集文本。

输出
    ``str``：HTML **片段**（不含 ``<html>`` / ``<body>``），块与块之间用 ``\\n``
    分隔。空输入或全空白输入 → ``""``。

依赖
    仅标准库 ``re``。零第三方依赖是本项目的硬约定（见 AGENTS.md 硬性约定 4），
    因此渲染放在服务端而不是前端 —— 前端是零构建的原生 JS，行数预算很紧。

失败模式
    无 IO，所以没有网络与磁盘失败面。两类输入硬报错，不静默降级：
      · 输入不是 ``str`` → 抛 ``TypeError``。
      · 围栏代码块没有闭合（起止标记配不成对）→ 抛 ``ValueError``。
        不把剩下的文档猜成代码块：那会悄悄改变其后所有内容的渲染方式，
        用户看到的是一个安静但错误的页面。

支持的构造
    块级：ATX 标题、无序列表、有序列表、管道表格、围栏代码块、引用块、段落、水平线。
    行内：``**粗体**`` 与 `` `行内代码` ``。
    **其余构造一律按普通文本原样（转义后）输出，绝不丢弃** —— 这是
    「前端永不隐藏已发生的事实」在渲染器上的延伸，链接、图片、斜体、
    删除线都会带着原始记号出现在页面上。

边界（写清楚是为了可验证，不是为了"尽力而为"）
    · 标题：``#`` 的数量超过 6 时按 ``<h6>`` 处理（``####### x`` → ``<h6>x</h6>``），
      因为 HTML 只有 h1–h6。``#`` 后面必须跟空格，``#标签`` 不是标题。
    · 表格：分隔行每格只由 ``-``、``:``、空格组成，且**至少含一个 ``-`` 与一个 ``|``**
      （要求 ``|`` 是为了不与「整行 ``---`` 即水平线」抢判定）。
      表体持续吸收紧接着的、非空且含 ``|`` 的行。
      数据行单元格数与表头不一致时**如实补齐（补空 ``<td></td>``）或截断到表头列数**：
      多出来的格被截掉、少掉的格补空，顺序不变 —— 既不丢原内容也不凭空多造列。
      分隔行自身的格数不参与校验。
    · 围栏代码块：起始标记后的语言标注被忽略；块内只做 HTML 转义，
      **不做行内解析**（``**x**`` 与反引号在代码块里保持字面）。
    · 列表标记必须在行首（``- `` / ``* `` / ``1. ``）；缩进的列表标记不是列表项，
      会作为普通段落文本原样输出。列表不嵌套。
    · 段落：段内换行合并成一个空格。
    · 水平线：整行只由 3 个及以上 ``-`` 组成。

对应设计
    docs/design.md 的产物预览 · docs/web-ui.md
"""

from __future__ import annotations

import re

__all__ = ["render"]

#: 围栏代码块的起止标记。
_FENCE = "```"

#: 水平线：整行只由 3 个及以上 ``-`` 组成（表格分隔行含 ``|``，不会走到这里）。
_HR_RE = re.compile(r"^-{3,}$")

#: 无序列表项：行首 ``- `` 或 ``* ``。
_UL_RE = re.compile(r"^[-*] +(.*)$")

#: 有序列表项：行首任意数字 + ``. ``。
_OL_RE = re.compile(r"^\d+\. +(.*)$")

#: ATX 标题：行首 1 个及以上 ``#`` 加空格。
_HEADING_RE = re.compile(r"^(#+)\s+(.*)$")

#: 引用块：行首 ``>``，其后可有可无一个空格。
_BLOCKQUOTE_RE = re.compile(r"^> ?(.*)$")

#: 行内构造。**一次扫描同时匹配两种**：代码在前，粗体在后。
#: 分两轮替换会让代码块里的 ``**x**`` 被第二轮误判成粗体。
_INLINE_RE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*")

#: 标题最大级别。HTML 没有 h7。
_MAX_HEADING_LEVEL = 6


def render(text: str) -> str:
    """把 Markdown 子集渲染成 HTML 片段。"""
    if not isinstance(text, str):
        raise TypeError(f"render() 需要 str，实际收到 {type(text).__name__}")

    lines = text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        if line.lstrip().startswith(_FENCE):
            block, index = _parse_fence(lines, index)
        elif _HEADING_RE.match(line):
            block, index = _parse_heading(line), index + 1
        elif _BLOCKQUOTE_RE.match(line):
            block, index = _parse_blockquote(lines, index)
        elif _UL_RE.match(line) or _OL_RE.match(line):
            block, index = _parse_list(lines, index)
        elif _starts_table(lines, index):
            block, index = _parse_table(lines, index)
        elif _HR_RE.match(line.strip()):
            block, index = "<hr>", index + 1
        else:
            block, index = _parse_paragraph(lines, index)
        blocks.append(block)

    return "\n".join(blocks)


# ───────────────────────── 块级解析 ─────────────────────────


def _parse_heading(line: str) -> str:
    match = _HEADING_RE.match(line)
    hashes, title = match.group(1), match.group(2)
    level = min(len(hashes), _MAX_HEADING_LEVEL)
    return f"<h{level}>{_inline(title.strip())}</h{level}>"


def _parse_fence(lines: list[str], start: int) -> tuple[str, int]:
    body: list[str] = []
    index = start + 1
    while index < len(lines) and not lines[index].lstrip().startswith(_FENCE):
        body.append(lines[index])
        index += 1
    if index >= len(lines):
        raise ValueError(f"围栏代码块没有闭合：起始于第 {start + 1} 行")
    return f"<pre><code>{_escape(chr(10).join(body))}</code></pre>", index + 1


def _parse_blockquote(lines: list[str], start: int) -> tuple[str, int]:
    parts: list[str] = []
    index = start
    while index < len(lines):
        match = _BLOCKQUOTE_RE.match(lines[index])
        if match is None:
            break
        parts.append(match.group(1).strip())
        index += 1
    return f"<blockquote><p>{_inline(' '.join(parts))}</p></blockquote>", index


def _parse_list(lines: list[str], start: int) -> tuple[str, int]:
    pattern = _UL_RE if _UL_RE.match(lines[start]) else _OL_RE
    tag = "ul" if pattern is _UL_RE else "ol"
    items: list[str] = []
    index = start
    while index < len(lines):
        match = pattern.match(lines[index])
        if match is None:
            break
        items.append(match.group(1).strip())
        index += 1
    body = "".join(f"<li>{_inline(item)}</li>" for item in items)
    return f"<{tag}>{body}</{tag}>", index


def _parse_table(lines: list[str], start: int) -> tuple[str, int]:
    header = _split_row(lines[start])
    width = len(header)
    rows: list[list[str]] = []
    index = start + 2
    while index < len(lines) and lines[index].strip() and "|" in lines[index]:
        rows.append(_split_row(lines[index]))
        index += 1

    head = "".join(f"<th>{_inline(cell)}</th>" for cell in header)
    body_parts: list[str] = []
    for row in rows:
        cells = row[:width]
        if len(cells) < width:
            cells = cells + [""] * (width - len(cells))
        body_parts.append(
            "<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in cells) + "</tr>"
        )
    return (
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>",
        index,
    )


def _parse_paragraph(lines: list[str], start: int) -> tuple[str, int]:
    parts = [lines[start].strip()]
    index = start + 1
    while index < len(lines) and lines[index].strip() and not _starts_block(lines, index):
        parts.append(lines[index].strip())
        index += 1
    return f"<p>{_inline(' '.join(parts))}</p>", index


# ───────────────────────── 块级判定 ─────────────────────────


def _starts_block(lines: list[str], index: int) -> bool:
    """判断这一行是否开启一个新块 —— 段落的收集靠它决定何时收口。"""
    line = lines[index]
    if line.lstrip().startswith(_FENCE):
        return True
    if _HEADING_RE.match(line):
        return True
    if _BLOCKQUOTE_RE.match(line):
        return True
    if _UL_RE.match(line) or _OL_RE.match(line):
        return True
    if _HR_RE.match(line.strip()):
        return True
    return _starts_table(lines, index)


def _starts_table(lines: list[str], index: int) -> bool:
    if "|" not in lines[index]:
        return False
    if index + 1 >= len(lines):
        return False
    return _is_separator_row(lines[index + 1])


def _is_separator_row(line: str) -> bool:
    """表格分隔行：含 ``|``，且每格只由 ``-`` ``:`` 空格组成并至少含一个 ``-``。"""
    if "|" not in line:
        return False
    for cell in _split_row(line):
        if "-" not in cell:
            return False
        if any(char not in "-: " for char in cell):
            return False
    return True


def _split_row(line: str) -> list[str]:
    """按 ``|`` 切出单元格，去掉最外层的管道符与每格的空白。"""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


# ───────────────────────── 行内解析 ─────────────────────────


def _inline(text: str) -> str:
    """先整体转义，再做行内替换 —— 顺序反了就会产生注入。"""
    escaped = _escape(text)

    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code is not None:
            return f"<code>{code}</code>"
        return f"<strong>{match.group(2)}</strong>"

    return _INLINE_RE.sub(replace, escaped)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
