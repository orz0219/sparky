"""S2 调研：N 人并发取证，产出各自的立场备忘。

职责
    按每个 ``Persona`` 的 ``InfoScope`` 独立分析，产出 ``StanceMemo``：
    核心主张 + 优点 + 缺点 + 依据来源 + 让步条件。

输入
    ``Brief``（公共层）+ ``Panel``（含每人独立的信息面）+ 可选上下文材料

输出
    ``ReconOutcome``；每人落盘 ``stances/<id>.json`` 并发出 ``recon.memo``

依赖
    ``llm``（N 并发）· ``config`` · ``events`` · ``models``
    仅标准库 ``concurrent.futures`` / ``json``。

失败模式
    某人调研失败 → 记入 ``ReconOutcome.failures`` 并发出 ``recon.failed`` 事件，
    会话继续；最终文档的附录 A 会**显式标注其缺席**（不假装他参与了）。
    全部失败 → 抛 ``LlmError``（没有材料就无法定稿，这不是可降级的情形）。
    ``--context-dir`` 里的文件没有任何人认领 → 记入告警并写进附录 B。

    ★ 硬约束：本阶段任何人的 ``StanceMemo`` **不得**进入他人的 prompt。
      实现上由「一人一次调用、模板只带自己的 InfoScope」保证，
      并由 ``_build_recon_user`` 的断言守住。违反它，整个工具就退化成
      "多开几个 ChatGPT 窗口"。

对应设计
    docs/design.md §4.3 · docs/personas.md §6 · ADR 0004
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from . import events
from .config import ContextFile, RuntimeConfig, build_prompt, load_context_dir, load_prompt
from .errors import LlmError, SparkyError
from .llm import LlmClient
from .models import Brief, Panel, Persona, ReconOutcome, StanceMemo
from .session import Session

#: M1 没有检索能力。这里如实声明，而不是让模型自己"假装搜过"。
NO_SEARCH_NOTE = "本次未使用外部检索，全部结论基于模型的既有知识推演。"


def run(
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    brief: Brief,
    panel: Panel,
) -> ReconOutcome:
    """执行 S2。并发调研，按人设顺序回收结果。"""
    brief_json = json.dumps(brief.to_dict(), ensure_ascii=False, indent=2)
    nosearch_note = load_prompt("recon", "nosearch_note")
    search_context = nosearch_note

    files_by_persona, unassigned = _assign_context_files(runtime, panel, brief)
    if unassigned:
        session.warn(
            "context_files_unassigned",
            f"以下材料没有被任何维度认领，未参与本次调研：{unassigned}",
        )

    for persona in panel.personas:
        session.emit(
            events.RECON_START,
            persona_id=persona.id,
            questions=persona.info_scope.questions,
            file_count=len(files_by_persona.get(persona.id, [])),
        )

    # 并发执行，但**按人设顺序回收** —— 这样 transcript 的行序与 panel 一致，
    # 不随线程调度抖动，会话因此可复现。
    results: dict[str, StanceMemo | str] = {}
    with ThreadPoolExecutor(max_workers=runtime.concurrency) as pool:
        futures = {
            persona.id: pool.submit(
                _one,
                client=client,
                runtime=runtime,
                session=session,
                persona=persona,
                brief_json=brief_json,
                search_context=search_context,
                assigned=files_by_persona.get(persona.id, []),
            )
            for persona in panel.personas
        }
        for persona_id, future in futures.items():
            try:
                results[persona_id] = future.result()
            except SparkyError as exc:
                results[persona_id] = f"{type(exc).__name__}: {exc}"

    outcome = ReconOutcome()
    for persona in panel.personas:
        result = results[persona.id]
        if isinstance(result, str):
            outcome.failures.append((persona.id, result))
            session.emit(
                events.RECON_FAILED,
                persona_id=persona.id,
                name=persona.name,
                reason=result,
            )
            continue
        outcome.memos.append(result)
        session.write_json(f"stances/{persona.id}.json", result.to_dict())
        session.emit(events.RECON_MEMO, persona_id=persona.id, memo=result.to_dict())

    if not outcome.memos:
        raise LlmError(
            f"{len(panel.personas)} 位专家的调研全部失败，没有任何可定稿的材料",
            hint="逐条失败原因见 transcript.jsonl 里的 recon.failed 事件。",
        )

    session.recon = outcome
    return outcome


def _one(
    *,
    client: LlmClient,
    runtime: RuntimeConfig,
    session: Session,
    persona: Persona,
    brief_json: str,
    search_context: str,
    assigned: list[ContextFile],
) -> StanceMemo:
    """一个人的调研。**只带他自己的信息面**。"""
    system = build_prompt(
        "recon",
        "system",
        name=persona.name,
        title=persona.title,
        stance=persona.stance,
        cares_about="、".join(persona.cares_about),
    )
    user = _build_recon_user(
        persona=persona,
        brief_json=brief_json,
        search_context=search_context,
        assigned=assigned,
        session=session,
    )
    data, _ = client.complete_json(
        system=system,
        user=user,
        purpose=f"S2 调研·{persona.id} {persona.name}",
        model=runtime.model_fast,
        temperature=0.6,
    )
    return StanceMemo.from_llm(
        persona.id,
        data,
        searched=False,
        search_note=NO_SEARCH_NOTE,
    )


def _build_recon_user(
    *,
    persona: Persona,
    brief_json: str,
    search_context: str,
    assigned: list[ContextFile],
    session: Session,
) -> str:
    """组装某个人的 user 提示词，并断言信息面隔离没被破坏。"""
    if assigned:
        assigned_text = "## 分配给你的材料\n\n" + "\n\n".join(f.render() for f in assigned)
    else:
        assigned_text = "（本次没有分配给你额外的本地材料。）"

    user = build_prompt(
        "recon",
        "user",
        brief_json=brief_json,
        questions="\n".join(f"{i}. {q}" for i, q in enumerate(persona.info_scope.questions, 1)),
        search_context=search_context,
        assigned_files=assigned_text,
    )

    # 硬约束的可执行版本：本阶段的 user 提示词只允许由 Brief 公共层
    # 与**这个人自己**的 InfoScope 组成。任何别的人设名字出现即视为隔离被破坏。
    for other in (session.panel.personas if session.panel else []):
        if other.id == persona.id:
            continue
        if other.name in user:
            raise LlmError(
                f"信息面隔离被破坏：{persona.id} 的提示词里出现了其他专家的名字 {other.name!r}",
                hint=(
                    "S2 阶段任何人的立场备忘都不得进入他人的上下文（ADR 0004）。"
                    "这通常意味着模板被改坏了。"
                ),
            )
    return user


# ───────────────────── 本地材料按维度分配 ─────────────────────


def _assign_context_files(
    runtime: RuntimeConfig, panel: Panel, brief: Brief
) -> tuple[dict[str, list[ContextFile]], list[str]]:
    """把 ``--context-dir`` 的材料按维度关键词分配给认领它的人设。

    规则是**确定性的关键词匹配**，不是模型判断 —— 分配结果需要可复现、
    也需要能对用户解释清楚"这份材料为什么给了这个人"。

    一份材料可以被多个维度认领（那说明它确实跨维度）；
    没人认领的进 ``unassigned``，会被写进附录 B 而不是悄悄丢掉。
    """
    if runtime.context_dir is None:
        return {}, []

    files, _skipped = load_context_dir(runtime.context_dir)
    if not files:
        return {}, []

    dimension_names: dict[str, str] = {d.id: d.name for d in brief.dimensions}
    by_persona: dict[str, list[ContextFile]] = {}
    unassigned: list[str] = []

    for item in files:
        owners: list[str] = []
        for persona in panel.personas:
            keywords = [dimension_names.get(d, "") for d in persona.dimensions]
            keywords = [k for k in keywords if k]
            if any(k in item.text or k in item.name for k in keywords):
                owners.append(persona.id)
        if owners:
            for owner in owners:
                by_persona.setdefault(owner, []).append(item)
        else:
            unassigned.append(item.name)

    return by_persona, unassigned
