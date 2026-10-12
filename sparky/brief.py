"""S0 立案：把用户的口语描述变成结构化 ``Brief``。

职责
    1. 抽取四元组：目标 / 约束 / 非目标 / 未知项 —— **缺的部分标 unknown，不许编**
    2. 抽取设计维度：做这个设计需要考虑哪些方面
    3. 标出"用户没说但显然重要"的维度，来源记为 ``inferred``

输入
    用户的原始一句话 + 可选的 ``--context-dir`` 材料

输出
    ``Brief``；同时落盘 ``brief.json`` 并发出 ``brief.ready`` 事件

依赖
    ``llm``（单次调用）· ``config``（提示词与材料装载）· ``events`` · ``models``

失败模式
    LLM 输出非 JSON / 结构不符 → 由 ``llm`` 与 ``models`` 抛出，本模块不吞。
    维度数不在 3–8 之间 → 抛 ``SchemaError``（少于 3 条说明分析不够，
    多于 8 条说明抽得太细，两种都会拖坏后面的组阁）。
    维度 id 重复 → 抛 ``SchemaError``。
    输入过短（如"做一个 app"）→ **不追问**，把一切标 unknown，
    并把"确定产品形态"本身列为第一个未知项 —— 让专家组去收敛它。

对应设计
    docs/design.md §4.1
"""

from __future__ import annotations

import json
from pathlib import Path

from . import events
from .config import RuntimeConfig, build_prompt, load_context_dir, load_prompt
from .errors import SchemaError
from .llm import LlmClient
from .models import Brief
from .session import Session

MIN_DIMENSIONS = 3
MAX_DIMENSIONS = 8


def run(client: LlmClient, runtime: RuntimeConfig, session: Session) -> Brief:
    """执行 S0。返回校验通过的 ``Brief``。"""
    system = load_prompt("brief", "system")
    user = build_prompt(
        "brief",
        "user",
        raw_input=runtime.raw_input,
        context_digest=build_context_digest(runtime),
    )
    data, _ = client.complete_json(
        system=system,
        user=user,
        purpose="S0 立案",
        model=runtime.model_strong,
        temperature=0.2,
    )
    brief = Brief.from_llm(runtime.raw_input, data)
    _validate(brief)

    session.brief = brief
    session.write_json("brief.json", brief.to_dict())
    session.emit(events.BRIEF_READY, brief=brief.to_dict())
    return brief


def build_context_digest(runtime: RuntimeConfig) -> str:
    """把 ``--context-dir`` 的材料渲染成一段可进 prompt 的文本。

    没配 ``--context-dir`` 时返回一句**如实**的说明，而不是空字符串 ——
    空字符串会让提示词里出现一个突兀的空白段，模型可能自行脑补。
    """
    if runtime.context_dir is None:
        return "（本次未提供额外材料，全部结论将基于用户描述与模型既有知识。）"

    files, skipped = load_context_dir(runtime.context_dir)
    if not files:
        return (
            f"（已指定 {runtime.context_dir}，但其中没有可用的文本材料；"
            f"{len(skipped)} 个非文本文件被跳过。）"
        )

    parts = [f"用户提供了以下材料（来自 {runtime.context_dir}）：", ""]
    parts.extend(item.render() for item in files)
    if skipped:
        parts.append("")
        parts.append(f"（另有 {len(skipped)} 个非文本文件被跳过，未纳入分析：{skipped}）")
    return "\n".join(parts)


def _validate(brief: Brief) -> None:
    count = len(brief.dimensions)
    if count < MIN_DIMENSIONS:
        raise SchemaError(
            f"只抽出了 {count} 个设计维度，少于下限 {MIN_DIMENSIONS}",
            hint=(
                "维度太少说明分析深度不够。这不是可以容忍的偏差 —— "
                "维度决定了组阁，维度不够就等于整场讨论都没覆盖到该考虑的面。"
            ),
        )
    if count > MAX_DIMENSIONS:
        raise SchemaError(
            f"抽出了 {count} 个设计维度，超过上限 {MAX_DIMENSIONS}",
            hint="维度过细会让专家组人数膨胀并开始互相重复。请把相近的维度合并。",
        )
    ids = [d.id for d in brief.dimensions]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SchemaError(f"维度 id 重复：{duplicates}")


def load_brief(path: Path) -> Brief:
    """从 ``brief.json`` 读回 ``Brief``。供测试与后续的 ``--resume`` 使用。"""
    from .errors import PersistenceError

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistenceError(f"读取 {path} 失败：{exc}") from exc
    return Brief.from_llm(payload["raw_input"], payload)
