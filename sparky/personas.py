"""S1 组阁：决定开几个人、都是谁、各自知道什么。

职责
    1. 按 ``panel_size = clamp(3, 7, ceil(维度数 / 1.5))`` 定人数
    2. 为每个维度生成一个有立场、有偏见、有盲点的 ``Persona``
    3. 为每个 ``Persona`` 分配 ``InfoScope``（追问清单 / 检索词 / 预算）
    4. **校验并强制反方**：无人 ``role_type == "adversary"`` 时自动补位

输入
    ``Brief``

输出
    ``Panel``；同时落盘 ``personas.json``、发出 ``panel.ready`` 与逐人的
    ``persona.assigned`` 事件

依赖
    ``llm``（两次调用：先定人数，再展开）· ``config`` · ``events`` · ``models``

失败模式
    有维度没人负责 → 抛 ``SchemaError``（维度真空洞会让整场讨论漏掉一整面）。
    两个人的追问清单重复 → 抛 ``SchemaError``（信息面隔离被破坏，
    讨论会退化成对同一份输入的重复采样 —— 见 ADR 0004）。
    维度 id 在维度分配里缺失 → 抛 ``SchemaError``。
    无人是反方 → **自动补位**并告警（设计规则，不是兜底逻辑）。
    人数越界 → 裁剪到 [3, 7] 并告警。**裁剪绝不切掉反方**，
    也绝不让某个维度失去负责人；真裁不动就保留超员并告警。

对应设计
    docs/design.md §4.2 · docs/personas.md
"""

from __future__ import annotations

import json

from . import events
from .config import RuntimeConfig, build_prompt, load_prompt
from .errors import SchemaError
from .llm import LlmClient
from .models import (
    ROLE_ADVERSARY,
    Brief,
    InfoScope,
    Panel,
    Persona,
)
from .session import Session

PANEL_MIN = 3
PANEL_MAX = 7

#: 打分公式里的除数。见 docs/design.md §11 未决问题 1 —— 这个系数是拍脑袋定的，
#: 需要真实案例校准。放在这里是为了让它在代码里显眼、好调。
DIMENSIONS_PER_PERSON = 1.5


def run(client: LlmClient, runtime: RuntimeConfig, session: Session, brief: Brief) -> Panel:
    """执行 S1。返回校验通过的 ``Panel``。"""
    brief_json = json.dumps(brief.to_dict(), ensure_ascii=False, indent=2)
    system = load_prompt("persona", "system")

    # ── 第一步：定人数与维度分配 ──
    override = ""
    if runtime.panel_size is not None:
        override = (
            f"**本次人数已被用户强制指定为 {runtime.panel_size}。**\n"
            f"请直接采用这个人数，不要再套用下面的公式与修正项；"
            f"但你仍然必须完成下面要求的维度分配。"
        )
    size_data, _ = client.complete_json(
        system=system,
        user=build_prompt(
            "persona", "size_user", brief_json=brief_json, panel_size_override=override
        ),
        purpose="S1 组阁·定人数",
        model=runtime.model_strong,
        temperature=0.4,
    )
    expected_size, assignment = _read_sizing(size_data, brief, runtime)

    # ── 第二步：逐人展开 ──
    expand_data, _ = client.complete_json(
        system=system,
        user=build_prompt(
            "persona",
            "expand_user",
            brief_json=brief_json,
            assignment_json=json.dumps(assignment, ensure_ascii=False, indent=2),
        ),
        purpose="S1 组阁·展开人设",
        model=runtime.model_strong,
        temperature=0.8,
    )
    personas = _read_personas(expand_data)
    personas = _enforce_adversary(personas, brief, session)
    # 裁剪放在强制反方**之后**：先保证有反方，再由裁剪逻辑保护它不被切掉
    personas = _clamp_size(personas, brief, session)
    _validate_panel(personas, brief)

    panel = Panel(
        personas=personas,
        panel_size_reasoning=str(size_data.get("reasoning", "")).strip()
        or "（模型未给出人数理由）",
        auto_added_adversary=any(p.id.startswith("px") for p in personas),
    )
    if len(personas) != expected_size:
        session.warn(
            "panel_size_mismatch",
            f"第一步算出 {expected_size} 人，第二步实际生成 {len(personas)} 人；"
            f"以实际生成为准（{len(personas)} 人）。",
        )

    session.panel = panel
    session.write_json("personas.json", panel.to_dict())
    for persona in panel.personas:
        session.emit(
            events.PERSONA_ASSIGNED,
            persona_id=persona.id,
            name=persona.name,
            title=persona.title,
            role_type=persona.role_type,
        )
    session.emit(events.PANEL_READY, panel=panel.to_dict())
    return panel


# ───────────────────── 第一步：定人数 ─────────────────────


def _read_sizing(
    data: dict, brief: Brief, runtime: RuntimeConfig
) -> tuple[int, dict[str, list[str]]]:
    """读回人数与维度分配，并做**可执行**的校验。"""
    raw_assignment = data.get("dimension_assignment")
    if not isinstance(raw_assignment, dict):
        raise SchemaError(
            f"dimension_assignment 期望是对象，实际是 {type(raw_assignment).__name__}"
        )

    known_ids = {d.id for d in brief.dimensions}
    assignment: dict[str, list[str]] = {}
    for dim_id, owners in raw_assignment.items():
        if not isinstance(owners, list) or not all(isinstance(o, str) and o.strip() for o in owners):
            raise SchemaError(
                f"dimension_assignment[{dim_id}] 期望是非空字符串数组，实际是 {owners!r}"
            )
        assignment[str(dim_id)] = [o.strip() for o in owners]

    missing = sorted(known_ids - set(assignment))
    if missing:
        raise SchemaError(
            f"这些维度没有分配负责人：{missing}",
            hint=(
                "维度真空洞意味着整场讨论会漏掉这一整面考虑。"
                f"已知维度：{sorted(known_ids)}"
            ),
        )
    unknown = sorted(set(assignment) - known_ids)
    if unknown:
        raise SchemaError(
            f"dimension_assignment 里出现了不存在的维度 id：{unknown}；"
            f"已知维度：{sorted(known_ids)}"
        )

    distinct_owners = {o for owners in assignment.values() for o in owners}
    model_size = data.get("panel_size")
    if not isinstance(model_size, int):
        raise SchemaError(f"panel_size 期望是整数，实际是 {model_size!r}")

    if runtime.panel_size is not None:
        # 用户强制指定时，以用户为准；但仍要求维度分配覆盖完整
        return runtime.panel_size, assignment

    if model_size != len(distinct_owners):
        # 模型自报的人数与分配结果打架时，以**分配结果**为准 ——
        # 后者是实际算出来的，前者是它自己报的数
        return len(distinct_owners), assignment
    return model_size, assignment


# ───────────────────── 第二步：读人设 ─────────────────────


def _read_personas(data: dict) -> list[Persona]:
    raw = data.get("personas")
    if not isinstance(raw, list) or not raw:
        raise SchemaError(f"personas 期望是非空数组，实际是 {type(raw).__name__}")
    personas = [Persona.from_dict(item, f"personas[{i}].") for i, item in enumerate(raw)]

    ids = [p.id for p in personas]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SchemaError(f"人设 id 重复：{duplicates}")

    # 信息面隔离：任意两人的追问清单不得重复（精确重复即视为失败）
    seen: dict[str, str] = {}
    for persona in personas:
        for question in persona.info_scope.questions:
            key = "".join(question.split()).lower()
            if key in seen:
                raise SchemaError(
                    f"{persona.id} 与 {seen[key]} 的追问清单重复：{question!r}",
                    hint=(
                        "信息面隔离是让讨论有内容的前提（见 ADR 0004）。"
                        "两个人问同一个问题，就是在对同一份输入重复采样。"
                    ),
                )
            seen[key] = persona.id
    return personas


def _enforce_adversary(personas: list[Persona], brief: Brief, session: Session) -> list[Persona]:
    """确保专家组里至少有一个唱反调者。

    这是设计规则，不是兜底逻辑：它保证「没有反方就不开会」这条不变量。
    反方一旦缺席，讨论就会退化成互相附和。
    """
    if any(p.is_adversary for p in personas):
        return personas

    owner_counts: dict[str, int] = {d.id: 0 for d in brief.dimensions}
    for persona in personas:
        for dim_id in persona.dimensions:
            if dim_id in owner_counts:
                owner_counts[dim_id] += 1
    # 优先盯住"只有一个人管"的维度 —— 那里最缺第二视角
    thin = [d for d, n in owner_counts.items() if n == 1] or [d.id for d in brief.dimensions]

    index = len(personas) + 1
    fallback = Persona(
        id=f"px{index}",
        name="白弈",
        title="唱反调者",
        role_type=ROLE_ADVERSARY,
        stance="默认所有方案都不可靠，直到被证明为止",
        bias="倾向于把最坏情况当作常态",
        blind_spot=None,
        cares_about=["失败场景覆盖率", "被忽略的用户群体", "未验证的假设"],
        dimensions=thin[:2],
        info_scope=InfoScope(
            questions=["这个方案在什么条件下会彻底失效，而用户又不会立刻察觉？"],
            search_queries=[],
            budget={"searches": 0, "tokens": 8000},
        ),
    )
    session.warn(
        "adversary_auto_added",
        f"模型没有生成唱反调者，已自动补位（{fallback.name}）。"
        f"这可能说明组阁提示词的约束被忽略了，值得复查。",
    )
    return [*personas, fallback]


def _clamp_size(
    personas: list[Persona], brief: Brief, session: Session
) -> list[Persona]:
    """把人数裁剪到 [3, 7]。越界不静默接受。

    裁剪策略（三条，按优先级）：

    1. **绝不裁掉唱反调者。** 曾经这里是从尾部一刀切，
       而模型把反方放在最后一位时就被切掉了，随后校验直接报
       "专家组里没有唱反调者"。对抗性比省一次调用重要得多。
    2. **优先裁掉贡献最小的人**：维度最少的那个。
    3. **不破坏维度覆盖**：只裁那些"少了他，所有维度仍有人负责"的人。

    若三条约束导致无法继续裁剪（剩下的每个人都不可替代），
    就**保留超员并告警** —— 让一个 8 人小组多花一点钱，
    好过要么砍掉反方、要么让某个维度没人管。
    """
    if len(personas) < PANEL_MIN:
        raise SchemaError(
            f"只生成了 {len(personas)} 个人设，少于下限 {PANEL_MIN}",
            hint="少于 3 人形不成三方博弈，讨论会因为缺少第三方而卡在二元对喷。",
        )
    if len(personas) <= PANEL_MAX:
        return personas

    original_count = len(personas)
    kept = list(personas)
    dropped: list[Persona] = []
    while len(kept) > PANEL_MAX:
        victim = _pick_droppable(kept, brief)
        if victim is None:
            break
        kept.remove(victim)
        dropped.append(victim)

    if dropped:
        session.warn(
            "panel_size_truncated",
            f"人数 {original_count} 超过上限 {PANEL_MAX}，已裁掉 {len(dropped)} 人："
            f"{[p.name for p in dropped]}（优先裁掉维度最少的非反方成员）",
        )
    if len(kept) > PANEL_MAX:
        session.warn(
            "panel_size_over_max",
            f"人数仍为 {len(kept)}，超过上限 {PANEL_MAX}："
            f"剩下的成员个个都不可替代（裁掉任何一个都会让某个维度没人负责）。"
            f"保留超员。",
        )
    return kept


def _pick_droppable(kept: list[Persona], brief: Brief) -> Persona | None:
    """挑一个可以安全裁掉的人：维度最少、且少了他维度仍全覆盖、且不是反方。"""
    needed = {d.id for d in brief.dimensions}
    best: Persona | None = None
    for candidate in kept:
        if candidate.is_adversary:
            continue  # 规则 1：反方不裁
        others = [p for p in kept if p is not candidate]
        covered = {dim for p in others for dim in p.dimensions}
        if not needed <= covered:
            continue  # 规则 3：裁了他会出现维度空洞
        if best is None or len(candidate.dimensions) < len(best.dimensions):
            best = candidate  # 规则 2：优先裁贡献最小的
    return best


def _validate_panel(personas: list[Persona], brief: Brief) -> None:
    """维度覆盖与角色构成校验。"""
    covered = {dim for persona in personas for dim in persona.dimensions}
    known = {d.id for d in brief.dimensions}
    missing = sorted(known - covered)
    if missing:
        raise SchemaError(
            f"这些维度没有任何人设负责：{missing}",
            hint=f"人设负责的维度：{sorted(covered)}",
        )
    if not any(p.is_adversary for p in personas):
        raise SchemaError("专家组里没有唱反调者（这是不应该发生的内部错误）")
