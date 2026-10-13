"""编排层：S0 立案 → S1 组阁 → S2 调研 → S3 交锋 → S4 收敛 → S5 定稿。

职责
    把六个阶段按顺序串起来，并负责**阶段之间**的三件事：
      · 每步开跑前给用户一句人话进度（``renderer.note``）
      · 零分歧检出 —— 「大家一致同意」是失败信号，必须显式暴露
      · 收口：写 ``completed`` 状态、发 ``session.end``

    它**不认识 HTTP，也不认识命令行**。谁来触发一次运行、跑完之后网页怎么办，
    都不是它的事（docs/architecture.md §2 的三条禁令之一）。

输入
    已建好的 ``Session``（目录与 ``meta.json`` 已在盘上）+ ``RuntimeConfig``

输出
    ``design.md`` 的路径。过程中所有事件都经 ``session.emit`` 落盘并广播。

依赖
    ``brief`` / ``personas`` / ``recon`` / ``debate`` / ``consensus`` / ``synthesis``
    + ``llm`` / ``render``。**不得 import ``server``**。

失败模式
    任一阶段抛 ``SparkyError`` → **原样向上抛**，由调用方决定怎么写失败状态。
    这里不捕获、不降级、不续跑 —— 缺了 S0 就没有 S1，硬跑只会产出一份残缺文档。

设计要点
    这个模块是 M3.2 从 ``__main__`` 里抽出来的。抽出来的**理由**不是"文件太长"：
    网页工作台要能在**同一条流水线**上发起新提案，如果流水线只存在于 ``main()``
    的函数体里，工作台就只能复制一份 —— 那两份迟早会漂移，
    而漂移的后果是"命令行跑得好好的，网页上跑出来的文档不一样"。

对应设计
    docs/architecture.md §3 数据流 · §6 一次完整会话的时序
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from . import brief as brief_stage
from . import consensus, debate as debate_stage, events
from . import personas as personas_stage
from . import recon as recon_stage
from . import synthesis
from .config import RuntimeConfig, mask_secret
from .errors import SparkyError
from .llm import LlmClient
from .render import TerminalRenderer
from .session import Session


def config_snapshot(runtime: RuntimeConfig) -> dict[str, object]:
    """写进 ``meta.json`` 的配置快照。**不含密钥真值**。

    字段名刻意避开 ``api_key*`` 前缀（用 ``key_hint`` / ``key_source``）：
    ``session.redact()`` 会保守地把任何像密钥字段的名字擦成 ``<已擦除>``，
    连"用的是哪个环境变量"这种非机密的诊断信息也会一起擦掉。
    """
    return {
        "base_url": runtime.base_url,
        "key_source": runtime.api_key_env,
        "key_hint": mask_secret(runtime.api_key),
        "model_strong": runtime.model_strong,
        "model_fast": runtime.model_fast,
        "panel_size": runtime.panel_size,
        "max_rounds": runtime.max_rounds,
        "budget_tokens": runtime.budget_tokens,
        "concurrency": runtime.concurrency,
        "context_dir": str(runtime.context_dir) if runtime.context_dir else None,
        "max_tokens": runtime.max_tokens,
        "temperature": runtime.temperature,
        "timeout": runtime.timeout,
    }


def run(
    *,
    runtime: RuntimeConfig,
    session: Session,
    renderer: TerminalRenderer,
    interjections: object | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """跑完一次头脑风暴会议，返回 ``design.md`` 的路径。

    前置条件：``session`` 已经建好（目录 + ``meta.json`` 在盘上）。
    这一点由调用方保证 —— 见 ``session.Session.create`` 的 docstring。

    ``interjections`` 是 ``server.InterjectionQueue``；这里只把它透传给
    ``debate``，**不 import ``server``**（那会破坏分层，且会让 ``--no-web``
    的路径平白依赖 HTTP 模块）。传 ``None`` 表示本次不接受插话。
    """
    renderer.note(
        f"供应商 {runtime.base_url} · 模型 {runtime.model_strong}"
        f" · 密钥 {mask_secret(runtime.api_key)}"
    )

    session.emit(
        events.SESSION_START,
        session_id=session.session_id,
        raw_input=runtime.raw_input,
        base_url=runtime.base_url,
        model_strong=runtime.model_strong,
        model_fast=runtime.model_fast,
        concurrency=runtime.concurrency,
    )

    client = LlmClient(runtime, session)

    # ── S0 立案 ──
    renderer.note("S0 立案：正在把你的描述拆成结构化立案书…")
    brief = brief_stage.run(client, runtime, session)

    # ── S1 组阁 ──
    renderer.note("S1 组阁：正在决定开几个人、都是谁…")
    panel = personas_stage.run(client, runtime, session, brief)

    # ── S2 调研 ──
    renderer.note(f"S2 调研：{len(panel.personas)} 条信息线并发取证…")
    outcome = recon_stage.run(client, runtime, session, brief, panel)
    if outcome.failures:
        renderer.note(
            f"{len(outcome.failures)} 位专家调研失败，将缺席定稿（名单会写进附录 A）"
        )

    # ── S3 交锋 + S4 收敛 ──
    renderer.note(f"S3 交锋：第 1 轮全员立场陈述（{len(panel.personas)} 人并发）…")
    result = debate_stage.run(
        client,
        runtime,
        session,
        brief,
        panel,
        outcome,
        should_stop=should_stop,
        interjections=interjections,
    )

    # 「零分歧」是失败信号而不是顺利信号，必须显式暴露 ——
    # 否则用户会以为"大家一致同意"，而真相是"根本没辩起来"。
    if consensus.is_harmonious(result.ledger, result):
        session.warn(
            "harmonious_session",
            f"{result.round_count} 轮讨论下来一项分歧都没有产生。"
            f"这通常意味着人设过于同质，或问题约束强到只剩一条路 —— "
            f"文档顶部会插入告警横幅。",
        )
    else:
        renderer.note(f"S4 收敛：{consensus.describe(result.ledger)}")

    # ── S5 定稿 ──
    renderer.note("S5 定稿：正在装配设计文档并做合规校验…")
    path = synthesis.run(client, runtime, session, brief, panel, outcome)

    session.set_status("completed")
    session.emit(
        events.SESSION_END,
        status="completed",
        doc_path=str(path),
        usage=session.usage.to_dict(),
    )
    renderer.blank()
    if session.warnings:
        renderer.note(
            f"本次有 {len(session.warnings)} 条告警，详见会话目录的 transcript.jsonl"
        )
    return path


def mark_failed(session: Session | None, exc: SparkyError | None) -> None:
    """把失败写进 ``meta.json`` 与 transcript。

    写入本身也可能失败（磁盘满、目录被删）。此时**原始错误才是要报的**，
    所以不再向上抛 —— 但也**绝不静默**：二次失败会打印到 stderr。
    AGENTS.md 禁止 ``except: pass``，这条路径不例外。
    """
    if session is None:
        return
    try:
        session.set_status(f"failed: {type(exc).__name__}" if exc else "failed: internal")
        session.emit(
            events.ERROR,
            stage="pipeline",
            message=exc.message if exc else "未预期的内部异常",
            detail=repr(exc) if exc else "",
        )
    except Exception as secondary:  # noqa: BLE001
        print(
            f"警告：失败状态没能写进会话目录（{type(secondary).__name__}: {secondary}）。\n"
            f"     {session.root} 可能不完整。上面那条才是原始错误。",
            file=sys.stderr,
        )


__all__ = ["run", "config_snapshot", "mark_failed"]
