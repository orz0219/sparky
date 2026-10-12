#!/usr/bin/env python3
"""前端开发夹具：用**合成事件**回放一场讨论，不调用任何 LLM。

用途
    改前端时不必真跑一次头脑风暴（那要 4 分钟和几毛钱）。
    这个脚本起一个本地服务，注入一段写死的事件序列，
    浏览器里就能看到完整的四块区域 —— 而且**结果完全可复现**，
    适合用来核对某个事件到底渲染成什么样。

用法
    python3 tools/demo_server.py            # 起服务并打开浏览器
    python3 tools/demo_server.py --port 9000 --no-browser

它**不是**产品的一部分：只是给改前端的人省时间。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparky import server as server_stage  # noqa: E402
from sparky.events import Event, EventBus  # noqa: E402

PERSONAS = [
    ("p1", "郑维安", "端侧推理工程师", "specialist", "能本地跑就绝不联网"),
    ("p2", "沈知微", "语音识别评测研究员", "specialist", "准确率是唯一不能让步的东西"),
    ("p3", "陆承宇", "播客工具链集成顾问", "generalist", "下游格式不认的精度等于零"),
    ("p4", "韩灼", "故障复盘工程师", "adversary", "默认所有方案都会在真实素材上崩"),
]


def build_events() -> list[tuple[str, dict]]:
    """一段写死的、覆盖全部四块区域的事件序列。"""
    seq: list[tuple[str, dict]] = [
        ("session.start", {"session_id": "demo-20261012", "raw_input": "（演示用会话，没有调用任何模型）"}),
        ("brief.ready", {"brief": {
            "goal": "做一个本地运行的播客章节与文字稿工具（演示数据）",
            "dimensions": [
                {"id": "d1", "name": "离线可行性", "source": "user"},
                {"id": "d2", "name": "模型体积", "source": "inferred"},
                {"id": "d3", "name": "章节切分依据", "source": "inferred"},
                {"id": "d4", "name": "失败模式", "source": "inferred"},
            ],
        }}),
        ("panel.ready", {"panel": {
            "panel_size": len(PERSONAS),
            "panel_size_reasoning": "（演示数据）4 个维度，clamp(3,7,ceil(4/1.5))=3，"
                                    "不确定性 high 修正为 4。",
            "personas": [
                {"id": pid, "name": name, "title": title, "role_type": role, "stance": stance}
                for pid, name, title, role, stance in PERSONAS
            ],
        }}),
    ]
    for pid, name, _title, _role, _stance in PERSONAS:
        seq.append(("recon.start", {"persona_id": pid, "questions": ["（演示问题）"], "file_count": 0}))
    for pid, name, _title, _role, _stance in PERSONAS:
        seq.append(("recon.memo", {"persona_id": pid, "memo": {
            "core_claim": f"{name}的演示结论：本地可行，但要把档位按机器分档。",
            "pros": ["无隐私风险"], "cons": ["首次启动要等"], "reasoning": "演示",
            "evidence": [], "would_concede_if": "如果实测差距小于 3%", "searched": False,
            "search_note": "本次未使用外部检索",
        }}))

    seq.append(("round.start", {
        "round": 1, "mode": "opening", "focus": [],
        "participants": [p[0] for p in PERSONAS], "topics": {},
    }))
    # 第一轮：一个人正在打字（留一条 token 不收尾，验证打字机光标）
    seq.append(("turn.start", {"round": 1, "persona_id": "p1", "name": "郑维安", "title": "端侧推理工程师"}))
    for piece in ["我先把底线摆出来：", "文字稿错一个字就是废品，", "所以我只认大模型，速度用整夜批处理解决。"]:
        seq.append(("turn.token", {"round": 1, "persona_id": "p1", "delta": piece, "reset": False}))
    seq.append(("turn.end", {"turn": {
        "round": 1, "persona_id": "p1",
        "say": "我先把底线摆出来：文字稿错一个字就是废品，所以我只认大模型，速度用整夜批处理解决。",
        "actions": "新开 2 项", "abstained": False, "abstain_reason": None,
    }}))
    seq.append(("turn.end", {"turn": {
        "round": 1, "persona_id": "p2",
        "say": "降级到小模型的那条路我不接受，除非有人拿出实测数据打我的脸。",
        "actions": "rebut x1、新开 1 项", "abstained": False, "abstain_reason": None,
    }}))
    seq.append(("turn.end", {"turn": {
        "round": 1, "persona_id": "p4",
        "say": "（演示：这一轮调用失败，按弃权处理）",
        "actions": "弃权", "abstained": True,
        "abstain_reason": "发言失败：LlmError: 演示用的失败",
    }}))

    seq.append(("disagreement.open", {"id": "x1", "topic": "默认模型档位：large-v3 还是 medium 量化版", "parties": ["p1", "p2"], "round": 1}))
    seq.append(("disagreement.open", {"id": "x2", "topic": "章节切分要不要串一个语义模型", "parties": ["p2", "p3"], "round": 1}))
    seq.append(("disagreement.open", {"id": "x3", "topic": "分发体积是不是唯一紧的预算项", "parties": ["p3", "p4"], "round": 1}))
    seq.append(("round.end", {"round": 1, "open_count": 3, "digest": {"total": 3, "open": 3, "resolved": 0, "tradeoff": 0, "stalemate": 0}}))
    seq.append(("convergence.decision", {
        "decision": "another_round", "reason": "pending_disagreements",
        "focus": ["x1", "x2", "x3"], "detail": "还有 3 项未决，定向加轮。",
    }))

    seq.append(("round.start", {
        "round": 2, "mode": "focus", "focus": ["x1", "x2", "x3"],
        "participants": ["p1", "p2", "p3", "p4"],
        "topics": {"x1": "默认模型档位", "x2": "章节切分要不要语义模型", "x3": "分发体积"},
    }))
    seq.append(("turn.end", {"turn": {
        "round": 2, "persona_id": "p2",
        "say": "x1 我撤回一半：机器吃得下就走大模型，吃不下才允许降级，但产物必须带降级标记。",
        "actions": "amend x1、提议取舍 1 项", "abstained": False, "abstain_reason": None,
    }}))
    seq.append(("disagreement.close", {
        "id": "x1", "topic": "默认模型档位", "status": "tradeoff",
        "tradeoff": {"choose": "按机器分档", "when_to_flip": "如果实测差距小于 3%，改为统一小模型"},
    }))
    seq.append(("disagreement.close", {
        "id": "x3", "topic": "分发体积", "status": "resolved",
        "resolution": {"round": 2, "by": "p3", "kind": "convinced", "because": "他举出了 m4a 章节轨的写入成本"},
    }))
    seq.append(("disagreement.update", {"id": "x2", "topic": "章节切分要不要语义模型", "patch": {"status": "stalemate"}}))
    seq.append(("round.end", {"round": 2, "open_count": 1, "digest": {"total": 3, "open": 1, "resolved": 1, "tradeoff": 1, "stalemate": 1}}))
    seq.append(("convergence.decision", {
        "decision": "force_stop", "reason": "max_rounds",
        "focus": ["x2"], "detail": "达到轮数上限，仍有 1 项未决，会进入文档第 8 节。",
    }))

    seq.append(("warning", {"code": "demo_warning", "message": "（演示）这是一条告警，用来核对告警条的样子。"}))
    seq.append(("synthesis.start", {"sections": ["summary", "problem", "candidates", "final", "risks", "milestones"]}))
    seq.append(("doc.ready", {"path": ".sparky/sessions/demo/design.md", "chars": 34567, "sections": []}))
    seq.append(("session.end", {
        "status": "completed", "doc_path": ".sparky/sessions/demo/design.md",
        "usage": {"calls": 24, "prompt_tokens": 112265, "completion_tokens": 55527,
                  "reasoning_tokens": 55527, "total_tokens": 167792},
    }))
    return seq


def main() -> int:
    parser = argparse.ArgumentParser(description="Sparky 前端开发夹具（不调用 LLM）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--delay", type=float, default=0.45, help="每条事件之间的间隔秒数")
    args = parser.parse_args()

    hub = server_stage.EventHub()
    bus = EventBus()
    bus.subscribe(hub.publish)
    server, _ = server_stage.start(hub, host="127.0.0.1", port=args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"演示服务已启动：{url}")
    print("（这是合成事件，没有调用任何模型）")

    def play() -> None:
        time.sleep(0.6)  # 等页面连上
        for index, (event_type, data) in enumerate(build_events(), start=1):
            bus.publish(Event(type=event_type, session_id="demo", seq=index, data=data))
            time.sleep(args.delay)

    threading.Thread(target=play, daemon=True).start()

    if not args.no_browser and not server_stage.open_browser(url):
        print(f"没能自动打开浏览器，请手动访问 {url}", file=sys.stderr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n收工")
    finally:
        server_stage.stop(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
