"""终端渲染：把事件流打成人能读的进度时间线。

职责
    订阅事件总线，把每条事件翻译成一行终端输出。
    这是表现层的默认实现（M3 的 SSE 服务是另一个订阅者，两者并存互不干扰）。

输入
    ``Event``

输出
    stdout 文本

依赖
    仅标准库 ``sys``。**不得 import ``llm``** —— 表现层不认识模型层，
    这是 docs/architecture.md §2 的分层禁令。

失败模式
    事件里缺字段 → 抛 ``KeyError``（不吞）：
    表现层静默失败会让用户看到一个安静但错误的界面。
    写入流失败（管道被关）→ 同样抛出。

设计要点
    M1 是纯终端。颜色只在 stdout 是 tty 时启用，避免污染被重定向的日志。
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from .events import Event


class TerminalRenderer:
    """把事件流渲染成中文进度行。"""

    def __init__(self, *, stream: TextIO | None = None, quiet: bool = False) -> None:
        self._out = stream or sys.stdout
        self._quiet = quiet
        self._color = bool(getattr(self._out, "isatty", lambda: False)())

    # ───────────────────── 供编排层调用的进度提示 ─────────────────────

    def note(self, text: str) -> None:
        """打一行"正在做什么"。长任务里没有它，用户会以为程序卡死了。"""
        if not self._quiet:
            self._write(f"  … {text}")

    def blank(self) -> None:
        if not self._quiet:
            self._write("")

    def done(self, text: str) -> None:
        self._write(self._paint(text, "32"))  # 绿色

    def fail(self, text: str) -> None:
        self._write(self._paint(text, "31"), err=True)  # 红色

    # ───────────────────── 事件分发 ─────────────────────

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type.replace('.', '_')}", None)
        if handler is None:
            # 事件类型合法但没有渲染分支 —— 这是实现遗漏，必须暴露
            raise KeyError(f"渲染器没有处理事件类型 {event.type!r}")
        handler(event.data)

    def _on_session_start(self, data: dict[str, Any]) -> None:
        self.blank()
        self._write(self._paint(f"◆ 会话 {data['session_id']}", "1"))
        self._write(f"  输入：{data['raw_input']}")

    def _on_brief_ready(self, data: dict[str, Any]) -> None:
        brief = data["brief"]
        self.blank()
        self._write(self._paint("[立案] ", "36") + brief["goal"])
        for dim in brief["dimensions"]:
            mark = "推断" if dim["source"] == "inferred" else "用户"
            self._write(f"  · {dim['id']} {dim['name']}（{mark}）")
        inferred = sum(
            1
            for group in ("constraints", "non_goals")
            for item in brief[group]
            if item["source"] == "inferred"
        )
        self._write(
            f"  约束 {len(brief['constraints'])} 条 · 非目标 {len(brief['non_goals'])} 条 · "
            f"未知项 {len(brief['unknowns'])} 条 · 其中 {inferred} 条为推断"
            f" · 不确定性 {brief['uncertainty']}"
        )

    def _on_panel_ready(self, data: dict[str, Any]) -> None:
        panel = data["panel"]
        self.blank()
        self._write(
            self._paint("[组阁] ", "36") + f"判定需要 {panel['panel_size']} 人"
        )
        if panel.get("panel_size_reasoning"):
            self._write(f"  理由：{panel['panel_size_reasoning']}")
        for persona in panel["personas"]:
            adversary = " ★反方" if persona["role_type"] == "adversary" else ""
            self._write(
                f"  · {persona['id']} {persona['name']} · {persona['title']}"
                f"（{persona['role_type']}）{adversary}"
            )
            self._write(f"      立场：{persona['stance']}")
            self._write(f"      偏见：{persona['bias']}")

    def _on_persona_assigned(self, data: dict[str, Any]) -> None:
        # 详情已由 panel.ready 统一打印，这里不重复
        return

    def _on_recon_start(self, data: dict[str, Any]) -> None:
        self._write(
            f"      调研 {data['persona_id']}：{data['questions'][0]}"
            if data.get("questions")
            else f"      调研 {data['persona_id']}"
        )

    def _on_recon_memo(self, data: dict[str, Any]) -> None:
        memo = data["memo"]
        self._write(
            self._paint("  ✓ ", "32")
            + f"{data['persona_id']}：{memo['core_claim']}"
        )

    def _on_recon_failed(self, data: dict[str, Any]) -> None:
        self.fail(f"  ✗ {data['persona_id']} {data['name']} 调研失败：{data['reason']}")

    def _on_synthesis_start(self, data: dict[str, Any]) -> None:
        self.blank()
        self._write(
            self._paint("[定稿] ", "36") + f"正在装配 design.md（{len(data['sections'])} 节）"
        )

    def _on_doc_ready(self, data: dict[str, Any]) -> None:
        self.done(f"[定稿] 已写出 {data['path']}（{data['chars']} 字）")

    def _on_client_interjection(self, data: dict[str, Any]) -> None:
        self.blank()
        self._write(self._paint("◆ 你插话：", "35") + data["text"])
        self._write("  （将在本轮结束时送达专家）")

    def _on_client_interjection_delivered(self, data: dict[str, Any]) -> None:
        self._write(
            f"  → 你的插话已在第 {data['round']} 轮送达：{data['text']}"
        )

    def _on_warning(self, data: dict[str, Any]) -> None:
        self._write(self._paint(f"  ⚠ [{data['code']}] {data['message']}", "33"))

    def _on_session_error(self, data: dict[str, Any]) -> None:
        self.fail(f"  ✗ [{data['stage']}] {data['message']}")

    def _on_session_end(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        self.blank()
        self._write(
            self._paint(f"◆ 会话结束：{data['status']}", "1")
            + f" · LLM 调用 {usage.get('calls', 0)} 次"
            + f" · token {usage.get('total_tokens', 0)}"
            + f"（其中推理 {usage.get('reasoning_tokens', 0)}）"
        )

    # ── S3 交锋 / S4 收敛 ──

    def _on_round_start(self, data: dict[str, Any]) -> None:
        self.blank()
        mode = data.get("mode", "focus")
        label = "全员立场陈述" if mode == "opening" else "定向加轮"
        self._write(self._paint(f"[交锋] 第 {data['round']} 轮 · {label}", "36"))
        if mode != "opening":
            topics = data.get("topics") or {}
            for disagreement_id in data.get("focus", []):
                self._write(f"  · 聚焦 {disagreement_id}：{topics.get(disagreement_id, '')}")
        participants = data.get("participants") or []
        self._write(f"  发言者：{'、'.join(participants)}")

    def _on_turn_start(self, data: dict[str, Any]) -> None:
        return

    def _on_turn_token(self, data: dict[str, Any]) -> None:
        return

    def _on_turn_end(self, data: dict[str, Any]) -> None:
        turn = data["turn"]
        if turn.get("abstained"):
            reason = turn.get("abstain_reason") or "无发言"
            self._write(self._paint(f"  ○ {turn['persona_id']} 弃权（{reason}）", "33"))
            return
        self._write(f"  {turn['persona_id']}：{turn.get('summary', '')}")

    def _on_round_end(self, data: dict[str, Any]) -> None:
        digest = data.get("digest") or {}
        self._write(
            f"  第 {data['round']} 轮结束 · 分歧 {digest.get('total', 0)} 项"
            f"（已解决 {digest.get('resolved', 0)}、取舍 {digest.get('tradeoff', 0)}、"
            f"未决 {digest.get('open', 0) + digest.get('stalemate', 0)}）"
        )

    def _on_disagreement_open(self, data: dict[str, Any]) -> None:
        parties = "、".join(data.get("parties") or [])
        self._write(
            self._paint(f"  ⚡ {data['id']}", "35") + f" {data['topic']}（{parties}）"
        )

    def _on_disagreement_update(self, data: dict[str, Any]) -> None:
        patch = data.get("patch") or {}
        if patch.get("status") == "stalemate":
            self._write(
                self._paint(f"  ⊘ {data['id']} 记为僵局：{data.get('topic', '')}", "33")
            )

    def _on_disagreement_close(self, data: dict[str, Any]) -> None:
        status = data.get("status", "")
        if status == "resolved":
            resolution = data.get("resolution") or {}
            how = "被说服" if resolution.get("kind") == "convinced" else "让步"
            self._write(
                self._paint(f"  ✓ {data['id']} 已解决", "32")
                + f"（{resolution.get('by')} {how}）"
            )
        elif status == "tradeoff":
            tradeoff = data.get("tradeoff") or {}
            self._write(
                self._paint(f"  ⇄ {data['id']} 降级为取舍", "36")
                + f"：选「{tradeoff.get('choose', '')}」"
            )

    def _on_convergence_decision(self, data: dict[str, Any]) -> None:
        kind = data.get("decision", "")
        if kind == "another_round":
            label = self._paint("[收敛] 继续讨论 → ", "36")
        elif kind == "done":
            label = self._paint("[收敛] 达成 → ", "32")
        else:
            label = self._paint("[收敛] 收口（未完全收敛）→ ", "33")
        self._write(f"  {label}{data.get('detail') or data.get('reason', '')}")

    # ───────────────────── 底层写入 ─────────────────────

    def _write(self, text: str, *, err: bool = False) -> None:
        stream = sys.stderr if err else self._out
        stream.write(text + "\n")
        stream.flush()

    def _paint(self, text: str, code: str) -> str:
        if not self._color:
            return text
        return f"\033[{code}m{text}\033[0m"


__all__ = ["TerminalRenderer"]
