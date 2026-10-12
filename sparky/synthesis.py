"""S5 定稿：装配模板、填充内容、合规校验、写盘。

职责
    1. **先用代码装配骨架**（固定章节），结构不由 LLM 决定
    2. 六节由 LLM 填充；其余各节由代码直接渲染
    3. 跑 9 项合规校验
    4. **校验全过才写盘**

输入
    ``Brief`` · ``Panel`` · ``ReconOutcome`` · ``Session``

输出
    ``design.md`` + ``doc.ready`` 事件

依赖
    ``llm``（6 次调用）· ``config`` · ``events`` · ``models``

失败模式
    任何一项合规校验不过 → 抛 ``SchemaError``，**不写盘**。
    一份缺了未经校验内容的文档，比明确告诉你"这次失败了"要糟糕得多 ——
    用户不知道它缺了什么。

    其中三项是**防篡改**检查，必须严格相等：
      · 取舍台账行数 == 台账中 tradeoff 条目数
      · 未决问题条数 == 台账中 stalemate 条目数
      · 附录 A 人数 == Panel 人数
    在 M1 里这三个数都是 0，检查仍然执行 —— 它们要到 M2 才真正吃紧，
    但接口现在就得是完整的。

    零分歧 / 未跑讨论的会话**不报错**，而是在文档顶部插入醒目的告警横幅。

对应设计
    docs/design.md §4.6 · docs/output-spec.md
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from . import events
from .config import RuntimeConfig, build_prompt, load_prompt
from .errors import SchemaError
from .llm import LlmClient
from .models import SOURCE_INFERRED, Brief, Panel, ReconOutcome
from .session import Session

#: 各节的规范标题。**代码写标题**，模型只写正文 ——
#: 若模型自己又写了一遍同名标题，会被 `_normalize_section` 去掉，
#: 避免同一份文档里出现 "## 0" 与 "## 0. 摘要" 两种写法。
HEADINGS: dict[str, str] = {
    "summary": "## 0. 摘要",
    "problem": "## 1. 问题与目标",
    "constraints": "## 2. 约束与假设",
    "candidates": "## 3. 候选方案对比",
    "final": "## 4. 最终方案",
    "tradeoffs": "## 5. 关键取舍台账",
    "risks": "## 6. 风险与失败模式",
    "milestones": "## 7. 里程碑与验收",
    "open_questions": "## 8. 未决问题",
}

#: 由 LLM 填充的节，按文档顺序。
LLM_SECTIONS: list[tuple[str, str]] = [
    ("summary", "S5 定稿·摘要"),
    ("problem", "S5 定稿·问题与目标"),
    ("candidates", "S5 定稿·候选方案对比"),
    ("final", "S5 定稿·最终方案"),
    ("risks", "S5 定稿·风险与失败模式"),
    ("milestones", "S5 定稿·里程碑与验收"),
]

#: 每一节需要哪些占位符。**这是一份声明式契约**，不是冗余 ——
#: ``build_prompt`` 会双向核对它与此节模板里的实际占位符：
#: 模板多要了、契约多给了，都会当场报错。
#: 改模板而不改这里（或反之）会被 `tests/test_synthesis.py` 直接拦下。
SECTION_INPUTS: dict[str, tuple[str, ...]] = {
    "summary": ("brief_json", "panel_summary", "stances_summary"),
    "problem": ("brief_json", "stances_summary"),
    "candidates": ("brief_json", "stances_json"),
    "final": ("brief_json", "stances_json"),
    "risks": ("brief_json", "stances_json"),
    "milestones": ("brief_json", "stances_summary"),
}

#: 每一节要不要开推理。``False`` = 关掉（仅 DeepSeek 系生效）。
#:
#: 判据是「这一节需不需要**产生新判断**」：
#:   · 转写、制表、压缩、排期 —— 材料已经由 S2 推理过了，再推一遍是浪费，
#:     而且实测会撞上"8000 token 全烧在 reasoning 上、正文为空"（见 llm.JSON_ATTEMPTS 附近的说明）
#:   · 组织最终方案、推演失败模式 —— 这两节是真正在做设计，保留推理
SECTION_THINKING: dict[str, bool] = {
    "summary": False,      # 压缩已有材料
    "problem": False,      # 从立案书推导目标与非目标
    "candidates": False,   # 把各人主张列成表
    "final": True,         # 真正在设计，需要推理
    "risks": True,         # 推演失败模式，需要推理
    "milestones": False,   # 排期与验收标准
}

#: 开发占位符标记：出现在**任何位置**都算文档没写完。
_FORBIDDEN_TOKENS = ("TODO", "FIXME", "<placeholder>")

#: 整行**只有**这些词才算占位。
#:
#: 中文的"待定 / 待补充"必须按行判定，不能像 TODO 那样全局禁。
#: 实测证据：一次真实会话的参数表里出现了
#: ``| `vad.min_speech_ratio` | 未给数值 | 待定 |`` ——
#: 这里的"待定"表示"专家们没有把这个值定下来"，是一条**有用且诚实**的信息。
#: 而整行只写"待定"则是"这一节我没写"。两者必须区分。
_PLACEHOLDER_LINE = re.compile(
    r"^\s*(?:\.{3,}|…+|待补充|待定|待确认|未定|TBD|略)\s*[。.]?\s*$",
    re.MULTILINE,
)

MAX_SUMMARY_CHARS = 400

#: 摘要超长时的重写次数。
#:
#: 为什么值得重试：摘要是**唯一有长度硬约束**的节，而模型经常超 10–20%。
#: 一次真实会话就因为摘要 456 字（超 14%）把整场 16 次调用判废 ——
#: 这个代价与收益完全不成比例。
#:
#: 为什么是"重写"而不是"截断"：截断会切在句子中间，而且会**替模型决定**
#: 丢掉哪些信息。要求它自己压缩一遍，既不篡改内容，也能真的保住信息。
SUMMARY_ATTEMPTS = 2

SUMMARY_LENGTH_CORRECTION = """

## 上一次尝试失败的原因

你上一次写的摘要有 ${actual} 字，超过了 ${limit} 字的上限。

请**重写**一遍，压到 ${limit} 字以内。
不要简单截断，也不要丢掉关键信息 —— 删掉冗余的修饰与铺垫，
保留「是什么 / 给谁用 / 核心思路」三件事。
"""


def run(
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    brief: Brief,
    panel: Panel,
    outcome: ReconOutcome,
    *,
    tradeoff_count: int = 0,
    stalemate_count: int = 0,
) -> Path:
    """执行 S5。返回 ``design.md`` 的路径。"""
    session.emit(events.SYNTHESIS_START, sections=[name for name, _ in LLM_SECTIONS])

    context = _build_context(brief, panel, outcome)
    system = load_prompt("synthesis", "system")

    bodies: dict[str, str] = {}
    for key, purpose in LLM_SECTIONS:
        if key == "summary":
            bodies[key] = _fill_summary(client, runtime, system, context, session)
        else:
            bodies[key] = _fill_section(client, runtime, system, key, purpose, context)

    bodies["constraints"] = _render_constraints(brief)
    bodies["tradeoffs"] = _render_tradeoffs(tradeoff_count)
    bodies["open_questions"] = _render_open_questions(stalemate_count, session)
    appendices = _render_appendices(brief, panel, outcome, session)

    document = _assemble(brief, panel, session, bodies, appendices)

    problems = validate_document(
        document,
        brief=brief,
        panel=panel,
        tradeoff_count=tradeoff_count,
        stalemate_count=stalemate_count,
    )
    if problems:
        raise SchemaError(
            f"定稿未通过合规校验（{len(problems)} 项）",
            hint=(
                "拒绝写盘 —— 一份缺了未经校验内容的文档比一份明确的失败更糟。\n"
                + "\n".join(f"  · {p}" for p in problems)
                + f"\n六节原始正文见 {session.root}/llm-calls/"
            ),
        )

    path = session.write_text("design.md", document)
    session.doc_path = path
    session.emit(
        events.DOC_READY,
        path=str(path),
        chars=len(document),
        sections=list(HEADINGS),
    )
    return path


# ───────────────────── 上下文准备 ─────────────────────


def _build_context(brief: Brief, panel: Panel, outcome: ReconOutcome) -> dict[str, str]:
    """组装各类提示词需要的状态切片。每节只喂它需要的部分。"""
    panel_lines = [
        f"- {p.name}（{p.title}，{p.role_type}）：{p.stance}" for p in panel.personas
    ]
    absent = {pid for pid, _ in outcome.failures}
    if absent:
        names = [p.name for p in panel.personas if p.id in absent]
        panel_lines.append(f"- ⚠️ 因调研失败缺席：{'、'.join(names)}")

    stances_lines: list[str] = []
    for persona in panel.personas:
        memo = outcome.memo_for(persona.id)
        if memo is None:
            continue
        stances_lines.append(f"### {persona.name}（{persona.title}）")
        stances_lines.append(f"核心主张：{memo.core_claim}")
        stances_lines.append("优点：" + "；".join(memo.pros))
        stances_lines.append("缺点：" + "；".join(memo.cons))
        stances_lines.append(f"让步条件：{memo.would_concede_if}")
        stances_lines.append("")

    return {
        "brief_json": json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
        "panel_summary": "\n".join(panel_lines),
        "stances_summary": "\n".join(stances_lines),
        "stances_json": json.dumps(
            [m.to_dict() for m in outcome.memos], ensure_ascii=False, indent=2
        ),
    }


def char_count(text: str) -> int:
    """数正文的有效字数（忽略空白）。

    重写判定与合规校验**必须用同一个函数** —— 两处算法不一致会让
    "重写通过了但校验不通过"这种无法解释的情况出现。
    """
    return len(re.sub(r"\s", "", text))


def _fill_summary(
    client: LlmClient,
    runtime: RuntimeConfig,
    system: str,
    context: dict[str, str],
    session: Session,
) -> str:
    """填摘要，并保证它满足长度硬约束。

    超长时**要求模型重写**（有界，见 ``SUMMARY_ATTEMPTS``），
    两次都超长就交给最后的合规校验去报错 —— 那是诚实的失败。
    """
    body = _fill_section(client, runtime, system, "summary", "S5 定稿·摘要", context)
    for attempt in range(1, SUMMARY_ATTEMPTS + 1):
        actual = char_count(body)
        if actual <= MAX_SUMMARY_CHARS:
            return body
        if attempt == SUMMARY_ATTEMPTS:
            return body  # 交给合规校验报错
        session.warn(
            "summary_overlong",
            f"摘要 {actual} 字，超过上限 {MAX_SUMMARY_CHARS} 字，"
            f"正在要求模型重写（第 {attempt} 次）。",
        )
        correction = SUMMARY_LENGTH_CORRECTION.replace("${actual}", str(actual)).replace(
            "${limit}", str(MAX_SUMMARY_CHARS)
        )
        body = _fill_section(
            client,
            runtime,
            system + correction,
            "summary",
            "S5 定稿·摘要（重写）",
            context,
        )
    return body


def _fill_section(
    client: LlmClient,
    runtime: RuntimeConfig,
    system: str,
    key: str,
    purpose: str,
    context: dict[str, str],
) -> str:
    """调用一次 LLM 填一节，并把它自己重复写的顶层标题去掉。

    只把 ``SECTION_INPUTS`` 声明的那几个切片喂给这一节 ——
    每节只看到自己需要的东西，比把全场材料一次性塞进去省 token，
    也让"某一节生成失败可以单独重试"成为可能。

    定稿各节要的是 **markdown 正文**，所以走 ``json_mode=False`` ——
    强迫长文塞进 JSON 字符串字段只会让模型更容易在转义上出错。
    """
    missing = [name for name in SECTION_INPUTS[key] if name not in context]
    if missing:
        raise SchemaError(
            f"定稿第 {key} 节需要 {missing}，但上下文里没有。"
            f"这是代码缺陷：请同步 synthesis.SECTION_INPUTS 与 _build_context。"
        )
    values = {name: context[name] for name in SECTION_INPUTS[key]}
    body = build_prompt("synthesis", key, **values)
    reply = client.complete(
        system=system,
        user=body,
        purpose=purpose,
        model=runtime.model_strong,
        json_mode=False,
        temperature=0.3,
        thinking=SECTION_THINKING[key],
    )
    return _normalize_section(reply.content, HEADINGS[key])


def _normalize_section(text: str, heading: str) -> str:
    """去掉模型自己写的同名顶层标题，再由调用方补上规范标题。"""
    lines = text.strip().splitlines()
    if lines:
        first = lines[0].strip()
        if first.startswith("## "):
            number = heading.split()[1].rstrip(".")
            if first.startswith(f"## {number}") or heading.split(". ", 1)[-1] in first:
                lines = lines[1:]
                while lines and not lines[0].strip():
                    lines = lines[1:]
    return "\n".join(lines).strip()


# ───────────────────── 代码直接渲染的各节 ─────────────────────


def _render_constraints(brief: Brief) -> str:
    """§2 约束与假设。纯数据，由代码渲染。

    映射方式（避免同一条内容在两处重复出现）：
      · §2.1 硬约束 ← source 为 user 的约束
      · §2.2 假设   ← source 为 inferred 的约束（推断出来的前提）
      · §2.3 未知项 ← unknowns
    """
    hard = [c for c in brief.constraints if c.source != SOURCE_INFERRED]
    assumed = brief.inferred_items

    lines = ["### 2.1 硬约束", ""]
    lines += [f"- {c.text}" for c in hard] or ["（用户没有给出明确约束。）"]
    lines += ["", "### 2.2 假设", ""]
    if assumed:
        lines.append("以下前提不是用户说的，而是本次分析推断出来的。**若其中任何一条不成立，方案需要重新评估。**")
        lines.append("")
        lines += [f"- {text} `(推断)`" for text in assumed]
    else:
        lines.append("（本次没有产生额外的推断性前提。）")

    lines += ["", "### 2.3 未知项", ""]
    lines += [f"- {u}" for u in brief.unknowns] or ["（没有记录到未知项。）"]
    lines += ["", f"整体不确定性：**{brief.uncertainty}**"]
    return "\n".join(lines)


def _render_tradeoffs(count: int) -> str:
    """§5 关键取舍台账。必须与台账条目数严格对齐。"""
    if count == 0:
        return (
            "本次会话没有登记任何取舍项。\n"
            "\n"
            "> 取舍台账中的每一项都来自讨论阶段的分歧记录。"
            "没有取舍项意味着**没有经过交锋检验的取舍**，"
            "不等于所有设计选择都已权衡充分。"
        )
    raise SchemaError(
        f"收到 {count} 项取舍，但 M1 尚未实现讨论阶段，无法渲染取舍台账。"
        "这说明调用方传入了不该出现的参数。"
    )


def _render_open_questions(count: int, session: Session) -> str:
    """§8 未决问题。必须与台账中的死锁条目数严格对齐。"""
    if count == 0:
        if not session.rounds:
            return (
                "本次会话**未运行讨论阶段**，因此没有经过交锋检验的议题，"
                "也就没有登记未决问题。\n"
                "\n"
                "> ⚠️ **这不等于所有问题都已解决。**"
                "本节为空只是因为讨论没有发生；各专家的独立结论之间的冲突"
                "尚未被显式检验过。请把它理解为「未知」，而不是「已决」。"
            )
        return "本次讨论的全部议题均已收敛，无未决问题。"
    raise SchemaError(
        f"收到 {count} 项死锁，但 M1 尚未实现讨论阶段，无法渲染未决问题。"
    )


def _render_appendices(
    brief: Brief, panel: Panel, outcome: ReconOutcome, session: Session
) -> str:
    """附录 A–D。全部由代码渲染。"""
    absent = {pid: reason for pid, reason in outcome.failures}

    lines = ["## 附录 A：参会人设", ""]
    for index, persona in enumerate(panel.personas, start=1):
        lines.append(f"### A.{index} {persona.name}（{persona.title}）")
        lines.append("")
        lines.append(f"- 角色类型：{persona.role_type}")
        lines.append(f"- 立场：{persona.stance}")
        lines.append(f"- 偏见：{persona.bias}")
        lines.append(f"- 盲点：{persona.blind_spot or '（反方不设盲点）'}")
        lines.append(f"- 关注指标：{'、'.join(persona.cares_about)}")
        lines.append(f"- 负责维度：{'、'.join(persona.dimensions)}")
        if persona.id in absent:
            lines.append(f"- ⚠️ **本次缺席**：{absent[persona.id]}")
        lines.append("")
    lines.append(f"参会人数：{len(panel.personas)} 人。")
    if panel.auto_added_adversary:
        lines.append("")
        lines.append("> 注：本次的唱反调角色由系统自动补位（模型未按要求生成）。")

    lines += ["", "## 附录 B：信息来源", ""]
    if not session.rounds:
        lines.append("> ⚠️ 本次会话**未运行讨论阶段**，以下为各专家独立调研时的信息面。")
        lines.append("")
    lines.append(f"> ⚠️ {outcome.memos[0].search_note if outcome.memos else '本次未使用外部检索。'}")
    lines.append("")
    for persona in panel.personas:
        memo = outcome.memo_for(persona.id)
        lines.append(f"### {persona.name}")
        lines.append("")
        lines.append("追问清单：")
        lines += [f"- {q}" for q in persona.info_scope.questions]
        if persona.info_scope.search_queries:
            lines.append("")
            lines.append("拟用检索词（本次未实际执行）：")
            lines += [f"- `{q}`" for q in persona.info_scope.search_queries]
        if memo:
            with_sources = [e for e in memo.evidence if e.source_url]
            unverified = memo.unverified_claims
            lines.append("")
            if with_sources:
                lines.append("引用来源：")
                lines += [f"- {e.claim} —— {e.source_url}" for e in with_sources]
            if unverified:
                lines.append("")
                lines.append(f"其中 {len(unverified)} 条论断标记为未验证，已在正文中标注。")
        lines.append("")

    lines += ["## 附录 C：讨论轮次摘要", ""]
    if not session.rounds:
        lines.append(
            "本次会话**未运行讨论阶段**（S3 交锋 / S4 收敛尚未实现）。"
            "因此本文档中的全部结论来自各专家的**独立调研**，"
            "没有经过互相反驳的检验。"
        )
    else:
        for index, turn_set in enumerate(session.rounds, start=1):
            lines.append(f"### 第 {index} 轮")
            lines.append("")
            lines.append(f"{len(turn_set)} 位专家发言。")

    usage = session.usage
    lines += [
        "",
        "## 附录 D：成本统计",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| LLM 调用次数 | {usage.calls} |",
        f"| 输入 token | {usage.prompt_tokens} |",
        f"| 输出 token | {usage.completion_tokens} |",
        f"| 其中推理 token | {usage.reasoning_tokens} |",
        f"| 合计 token | {usage.total_tokens} |",
        f"| 会话事件数 | {session.event_count} |",
        "",
    ]
    return "\n".join(lines)


def _render_banner(session: Session) -> str:
    """文档顶部横幅。未跑讨论时如实说明 —— 不许让读者以为吵过了。"""
    if session.rounds:
        return ""
    return (
        "> ⚠️ **本次会话未运行讨论阶段。**\n"
        "> 下文全部结论来自各专家的**独立调研**，彼此之间未经反驳检验，\n"
        "> 也**没有产生分歧台账**。请把「候选方案对比」一节理解为"
        "「各人独立给出的主张」，而非辩论后的裁决。\n"
    )


def _assemble(
    brief: Brief,
    panel: Panel,
    session: Session,
    bodies: dict[str, str],
    appendices: str,
) -> str:
    """按固定顺序拼接全文。结构由这里说了算，不由模型。"""
    header = [
        f"# {brief.goal} · 设计文档",
        "",
        f"> 由 Sparky 生成于 {session.started_at} ｜ 会话 `{session.session_id}`",
        f"> 参会 {len(panel.personas)} 人 · 讨论 {len(session.rounds)} 轮 · "
        f"未决 {_count_open_questions(bodies['open_questions'])} 项",
        "",
    ]
    banner = _render_banner(session)
    if banner:
        header += [banner, ""]

    order = ["summary", "problem", "constraints", "candidates", "final",
             "tradeoffs", "risks", "milestones", "open_questions"]
    body_parts: list[str] = []
    for key in order:
        body_parts.append(HEADINGS[key])
        body_parts.append("")
        body_parts.append(bodies[key].strip())
        body_parts.append("")

    footer = [
        "---",
        "",
        "*本文档由 Sparky 根据多专家独立调研自动生成；"
        "未经交锋检验的结论已在文中逐处标注。*",
        "",
        appendices,
    ]
    return "\n".join([*header, *body_parts, *footer])


def _count_open_questions(text: str) -> int:
    return len(re.findall(r"^### 8\.", text, re.MULTILINE))


# ───────────────────── 合规校验 ─────────────────────


def validate_document(
    document: str,
    *,
    brief: Brief,
    panel: Panel,
    tradeoff_count: int,
    stalemate_count: int,
) -> list[str]:
    """9 项合规校验。返回问题描述列表，空列表表示全部通过。

    这是纯函数，便于单测 —— 见 docs/output-spec.md §3。
    """
    problems: list[str] = []

    def section(heading: str) -> str | None:
        start = document.find(heading)
        if start == -1:
            return None
        nxt = document.find("\n## ", start + len(heading))
        return document[start: nxt if nxt != -1 else len(document)]

    # 1. 摘要非空且 ≤ 400 字
    summary = section(HEADINGS["summary"])
    if summary is None:
        problems.append("缺少第 0 节摘要")
    else:
        body = summary[len(HEADINGS["summary"]):]
        chars = char_count(body)
        if chars == 0:
            problems.append("第 0 节摘要为空")
        elif chars > MAX_SUMMARY_CHARS:
            problems.append(f"第 0 节摘要 {chars} 字，超过上限 {MAX_SUMMARY_CHARS} 字")

    # 2/3. 候选方案数 ≥ 2，且至少 1 个被否决
    candidates = section(HEADINGS["candidates"])
    if candidates is None:
        problems.append("缺少第 3 节候选方案对比")
    else:
        rows = _count_table_rows(candidates)
        if rows < 2:
            problems.append(f"第 3 节只有 {rows} 个候选方案，少于下限 2 个")
        rejected = _subsection(candidates, "### 3.2")
        if rejected is None or "否决" not in rejected or len(rejected.strip()) < 40:
            problems.append("第 3 节缺少「被否决的方案」及其否决理由")

    # 4. 取舍台账行数 == tradeoff 条目数（防篡改）
    tradeoffs = section(HEADINGS["tradeoffs"])
    if tradeoffs is None:
        problems.append("缺少第 5 节关键取舍台账")
    else:
        rows = _count_table_rows(tradeoffs)
        if rows != tradeoff_count:
            problems.append(
                f"第 5 节取舍台账有 {rows} 行，但台账里有 {tradeoff_count} 项取舍 —— 不一致"
            )

    # 5. 未决问题条数 == stalemate 条目数（防篡改）
    open_q = section(HEADINGS["open_questions"])
    if open_q is None:
        problems.append("缺少第 8 节未决问题")
    else:
        count = len(re.findall(r"^### 8\.", open_q, re.MULTILINE))
        if count != stalemate_count:
            problems.append(
                f"第 8 节有 {count} 项未决问题，但台账里有 {stalemate_count} 项死锁 —— 不一致"
            )

    # 6. 失败模式表 ≥ 3 行
    risks = section(HEADINGS["risks"])
    if risks is None:
        problems.append("缺少第 6 节风险与失败模式")
    else:
        failures = _subsection(risks, "### 6.3")
        if failures is None:
            problems.append("第 6 节缺少 6.3 失败模式表")
        else:
            rows = _count_table_rows(failures)
            if rows < 3:
                problems.append(f"6.3 失败模式表只有 {rows} 行，少于下限 3 行")

    # 7. 附录 A 人数 == Panel 人数（防篡改）
    appendix_a = document.find("## 附录 A")
    if appendix_a == -1:
        problems.append("缺少附录 A：参会人设")
    else:
        next_section = document.find("\n## 附录 B", appendix_a)
        block = document[appendix_a: next_section if next_section != -1 else len(document)]
        count = len(re.findall(r"^### A\.\d+", block, re.MULTILINE))
        if count != len(panel.personas):
            problems.append(
                f"附录 A 有 {count} 位参会人设，但专家组有 {len(panel.personas)} 人 —— 不一致"
            )

    # 8. 无占位符
    for token in _FORBIDDEN_TOKENS:
        if token in document:
            problems.append(f"文档里出现了开发占位符 {token!r}")
    placeholder = _PLACEHOLDER_LINE.search(document)
    if placeholder:
        problems.append(
            f"文档里出现了只有占位词的行：{placeholder.group(0).strip()!r}"
            f"（整行只有一个占位词说明这一节没有真的写）"
        )

    # 9. (推断) 标记数与 Brief 中的 inferred 条目数一致
    marked = document.count("(推断)")
    expected = len(brief.inferred_items)
    if marked != expected:
        problems.append(
            f"文档里标了 {marked} 处「(推断)」，但立案书里有 {expected} 条推断内容 —— 不一致"
        )

    return problems


def _subsection(text: str, marker: str) -> str | None:
    """取出 ``marker`` 之后的**正文**，直到下一个同级或更高级标题。

    标题行本身被丢掉。这一条很关键：若不丢，``"### 3.2 被否决的方案"``
    这行里的"否决"二字会让「是否含否决理由」的检查永远为真 ——
    检查看起来在跑，实际是空的。
    """
    start = text.find(marker)
    if start == -1:
        return None
    rest = text[start + len(marker):]
    newline = rest.find("\n")
    if newline != -1:
        rest = rest[newline + 1:]
    match = re.search(r"\n#{2,3} ", rest)
    return (rest[: match.start()] if match else rest).strip()


def _count_table_rows(text: str) -> int:
    """数 markdown 表格的数据行数（不含表头与分隔行）。"""
    rows = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or all(c == "" for c in cells):
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != ""):
            continue  # 分隔行 |---|---|
        rows += 1
    return max(0, rows - 1)  # 减去表头


__all__ = [
    "run",
    "validate_document",
    "char_count",
    "HEADINGS",
    "LLM_SECTIONS",
    "SECTION_INPUTS",
    "SECTION_THINKING",
    "MAX_SUMMARY_CHARS",
    "SUMMARY_ATTEMPTS",
]
