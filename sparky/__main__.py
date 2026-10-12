"""进程入口：``python3 -m sparky "你的想法"`` 的落点。

职责
    组装最外层调用链：解析参数 → 装载配置 → 建会话 → 跑流水线 → 收口，
    并把失败翻译成明确的中文信息与退出码。

输入
    ``sys.argv``

输出
    进程退出码：
        0  正常结束（**包含未收敛的 force_stop**，那不是失败）
        2  命令行参数错误
        3  配置错误（缺密钥等）
        4  会话持久化错误
        1  运行期错误（LLM 报错、校验不过等）

依赖
    ``cli`` · ``config`` · ``session`` · ``llm`` · ``render``
    以及四个阶段模块 ``brief`` · ``personas`` · ``recon`` · ``synthesis``

失败模式
    任何 ``SparkyError`` 都不许被吞掉 —— 必须打印明确的中文原因后
    以对应退出码退出。
    键盘中断（Ctrl-C）：保留已落盘内容，把会话标记为 interrupted，
    并以非零码退出。
    未被 ``SparkyError`` 覆盖的异常视为 bug：打印完整回溯，退出码 1
    （不假装正常结束）。

对应设计
    docs/design.md §3.2 · docs/architecture.md §1 · §6
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Sequence

from . import brief as brief_stage
from . import (
    cli,
    consensus,
    debate as debate_stage,
    events,
    personas as personas_stage,
    recon as recon_stage,
    synthesis,
)
from .config import load_runtime_config, mask_secret
from .errors import EXIT_OK, EXIT_RUNTIME, SparkyError
from .events import EventBus
from .llm import LlmClient
from .render import TerminalRenderer
from .session import Session


def main(argv: Sequence[str] | None = None) -> int:
    """跑完一次头脑风暴会议。返回进程退出码。"""
    renderer = TerminalRenderer()
    session: Session | None = None

    try:
        config = cli.parse_args(argv)
        runtime = load_runtime_config(config)
        renderer = TerminalRenderer(quiet=runtime.quiet)
        renderer.note(
            f"供应商 {runtime.base_url} · 模型 {runtime.model_strong}"
            f" · 密钥 {mask_secret(runtime.api_key)}"
        )

        bus = EventBus()
        bus.subscribe(renderer)
        session = Session.create(
            runtime.out_root,
            runtime.raw_input,
            bus=bus,
            config_snapshot=_config_snapshot(runtime, config),
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
                f"{len(outcome.failures)} 位专家调研失败，将缺席定稿"
                f"（名单会写进附录 A）"
            )

        # ── S3 交锋 + S4 收敛 ──
        renderer.note(
            f"S3 交锋：第 1 轮全员立场陈述（{len(panel.personas)} 人并发）…"
        )
        result = debate_stage.run(client, runtime, session, brief, panel, outcome)

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
            renderer.note(f"本次有 {len(session.warnings)} 条告警，详见会话目录的 transcript.jsonl")
        return EXIT_OK

    except KeyboardInterrupt:
        _handle_interrupt(session, renderer)
        return EXIT_RUNTIME
    except SparkyError as exc:
        _mark_failed(session, exc)
        renderer.fail("")
        renderer.fail(exc.render())
        return exc.exit_code
    except Exception:  # noqa: BLE001 —— 未预期的异常是 bug，必须带回溯暴露
        _mark_failed(session, None)
        renderer.fail("")
        renderer.fail("错误：遇到未预期的内部异常（这是 bug，不是你的用法问题）")
        traceback.print_exc()
        return EXIT_RUNTIME


def _config_snapshot(runtime, config) -> dict[str, object]:
    """写进 meta.json 的配置快照。**不含密钥真值**。

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
        "concurrency": runtime.concurrency,
        "context_dir": str(runtime.context_dir) if runtime.context_dir else None,
        "max_tokens": runtime.max_tokens,
        "temperature": runtime.temperature,
        "timeout": runtime.timeout,
    }


def _mark_failed(session: Session | None, exc: SparkyError | None) -> None:
    """把失败写进 meta.json 与 transcript。

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


def _handle_interrupt(session: Session | None, renderer: TerminalRenderer) -> None:
    """Ctrl-C：保住已落盘的内容，并告诉用户怎么找回它。"""
    renderer.blank()
    if session is None:
        renderer.fail("已中断（还没有建立会话，没有内容落盘）。")
        return
    try:
        session.set_status("interrupted")
        session.emit(events.SESSION_END, status="interrupted", usage=session.usage.to_dict())
    except Exception as secondary:  # noqa: BLE001
        renderer.fail(
            f"已中断；但状态写入失败（{type(secondary).__name__}: {secondary}）。"
            f"{session.root} 可能不完整。"
        )
        return
    renderer.fail(f"已中断。已落盘内容保留在 {session.root}")


if __name__ == "__main__":
    sys.exit(main())
