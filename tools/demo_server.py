#!/usr/bin/env python3
"""前端开发夹具：用**合成事件**回放一场讨论，不调用任何 LLM。

用途
    改前端时不必真跑一次头脑风暴（那要 4 分钟和几毛钱）。
    这个脚本起一个本地服务，伪装成提案工作台，浏览器里就能看到
    完整的三条路径 —— 历史提案、进行中的提案、新建提案 ——
    而且**结果完全可复现**，适合用来核对某个事件到底渲染成什么样。

用法
    python3 tools/demo_server.py            # 起服务并打开浏览器
    python3 tools/demo_server.py --port 9000 --no-browser

它**不是**产品的一部分：只是给改前端的人省时间。
它实现的是 ``server.ProposalApi`` 那套结构 —— 前端无法分辨真假，
所以这里看到的排版就是真实运行时的排版。
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sparky import server as server_stage  # noqa: E402
from sparky.errors import UsageError  # noqa: E402
from sparky.events import Event, EventHub  # noqa: E402
from sparky.session import slugify  # noqa: E402

PERSONAS = [
    ("p1", "郑维安", "端侧推理工程师", "specialist", "能本地跑就绝不联网"),
    ("p2", "沈知微", "语音识别评测研究员", "specialist", "准确率是唯一不能让步的东西"),
    ("p3", "陆承宇", "播客工具链集成顾问", "generalist", "下游格式不认的精度等于零"),
    ("p4", "韩灼", "故障复盘工程师", "adversary", "默认所有方案都会在真实素材上崩"),
]

#: 三份演示提案的 id。带中文是为了顺带验证 URL 里的百分号编码没写错。
LIVE_ID = "20261012-171530-命令行番茄钟-演示"
DONE_ID = "20261012-142656-播客章节与文字稿工具-演示"
STALE_ID = "20261012-160102-团队知识库-演示"

#: 演示用的设计文档。刻意把 Markdown 子集全部用上一遍 ——
#: 标题、列表、表格、围栏代码、引用、粗体、行内代码、水平线。
DEMO_DOC = """\
# 命令行番茄钟 · 设计文档

> 这份文档由演示夹具合成，用来核对排版，不是真实产出。

## 0. 摘要

一个**单文件**的命令行番茄钟，不联网、不写配置文件，状态落在 `~/.pomodoro.json`。
讨论中真正的分歧只有一处：计时是靠**常驻进程**还是靠**文件里的时间戳**。

## 1. 问题与目标

### 1.1 目标

1. 一条命令开始一个 25 分钟的番茄钟，中途可以暂停。
2. 到点用系统通知提醒，不需要盯着终端。
3. 状态可恢复：关掉终端再打开，正在跑的番茄钟还在。

### 1.2 非目标

- 不做图形界面
- 不做统计报表
- 不做多设备同步

## 2. 候选方案对比

| 方案 | 核心思路 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| 常驻进程 | 一个 daemon 持有计时器，客户端只发指令 | 计时精确；通知准时 | 要管进程生命周期；崩溃后状态丢 | 否决 |
| 文件时间戳 | 只记录 `started_at`，剩余时间每次现算 | 无进程可崩；重启天然恢复 | 依赖系统时钟；休眠跨天要单独处理 | 采纳 |
| 系统 cron | 交给系统定时器 | 完全不占资源 | 粒度粗；暂停与恢复没法表达 | 否决 |

## 3. 关键取舍

```python
# 剩余时间永远现算，不存"还剩多少秒"
remaining = total - (time.monotonic() - started_monotonic)
```

引用一条讨论里的原话：

> 存剩余秒数就是在存一个必然会过期的缓存，迟早会有人忘了刷新它。

## 4. 验收方式

- `pomodoro start` 之后立刻 `pomodoro status`，输出剩余时间误差小于 1 秒
- 关掉终端 10 分钟后重新 `status`，剩余时间与真实流逝一致

---

## 附录 A：参会人设

- 郑维安 · 端侧推理工程师
- 沈知微 · 语音识别评测研究员
"""


def build_events() -> list[tuple[str, dict]]:
    """一段写死的、覆盖全部四块区域的事件序列。"""
    seq: list[tuple[str, dict]] = [
        ("session.start", {"session_id": LIVE_ID, "raw_input": "（演示用会话，没有调用任何模型）"}),
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
    for pid, _name, _title, _role, _stance in PERSONAS:
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
        "summary": "我先把底线摆出来：文字稿错一个字就是废品，所以我只认大模型，速度用整夜批处理解决。",
        "actions": "新开 2 项", "abstained": False, "abstain_reason": None,
    }}))
    seq.append(("turn.end", {"turn": {
        "round": 1, "persona_id": "p2",
        "say": "降级到小模型的那条路我不接受，除非有人拿出实测数据打我的脸。",
        "summary": "降级到小模型的那条路我不接受，除非有人拿出实测数据打我的脸。",
        "actions": "rebut x1、新开 1 项", "abstained": False, "abstain_reason": None,
    }}))
    seq.append(("turn.end", {"turn": {
        "round": 1, "persona_id": "p4",
        "say": "",
        "summary": "弃权，本轮没有发言（发言失败：LlmError: 演示用的失败）",
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

    # 委托人插话：收到即发，下一轮送达
    seq.append(("client.interjection", {"text": "离线是硬要求，不要为了质量牺牲它。", "received_round": 1}))
    seq.append(("client.interjection.delivered", {"round": 2, "text": "离线是硬要求，不要为了质量牺牲它。"}))

    seq.append(("round.start", {
        "round": 2, "mode": "focus", "focus": ["x1", "x2", "x3"],
        "participants": ["p1", "p2", "p3", "p4"],
        "topics": {"x1": "默认模型档位", "x2": "章节切分要不要语义模型", "x3": "分发体积"},
    }))
    seq.append(("turn.end", {"turn": {
        "round": 2, "persona_id": "p2",
        "say": "x1 我撤回一半：机器吃得下就走大模型，吃不下才允许降级，但产物必须带降级标记。",
        "summary": "x1 我撤回一半：机器吃得下就走大模型，吃不下才允许降级，但产物必须带降级标记。",
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
    seq.append(("doc.ready", {"path": f".sparky/sessions/{DONE_ID}/design.md", "chars": len(DEMO_DOC), "sections": []}))
    seq.append(("session.end", {
        "status": "completed", "doc_path": f".sparky/sessions/{DONE_ID}/design.md",
        "usage": {"calls": 24, "prompt_tokens": 112265, "completion_tokens": 55527,
                  "reasoning_tokens": 55527, "total_tokens": 167792},
    }))
    return seq


#: 进行中的提案只播到"插话已送达"为止 —— 第 2 轮与定稿故意不播，
#: 这样列表里同时有"进行中"和"已完成"两种提案可看。
#: 用数出来的下标而不是写死的数字：事件序列一改，这里自动跟着走。
def _live_prefix() -> int:
    for index, (event_type, _data) in enumerate(build_events(), start=1):
        if event_type == "client.interjection.delivered":
            return index
    raise AssertionError("演示序列里必须有 client.interjection.delivered —— 否则夹具演示不了插话")


LIVE_PREFIX = _live_prefix()

#: 插话事件的 seq 从一个高位起算，免得与回放的历史撞号被水位去重丢掉。
_INTERJECTION_SEQ_BASE = 100_000


def _events_for(prefix: int, *, session_id: str, goal: str | None = None) -> list[Event]:
    """取事件序列的前 ``prefix`` 条，并把会话身份换成 ``session_id``。

    ``goal`` 非空时同时改写 ``session.start`` 的原始输入与 ``brief.ready`` 的目标 ——
    否则三份演示提案会共用同一句"演示数据"目标，点进去看到的标题
    和列表卡片上写的完全对不上。
    """
    events = []
    for index, (event_type, data) in enumerate(build_events()[:prefix], start=1):
        if goal is not None and event_type == "session.start":
            data = {**data, "session_id": session_id, "raw_input": goal}
        elif goal is not None and event_type == "brief.ready":
            data = {**data, "brief": {**data["brief"], "goal": goal}}
        events.append(Event(type=event_type, session_id=session_id, seq=index, data=data))
    return events


def _proposal(session_id: str, goal: str, status: str, **extra: Any) -> dict:
    base = {
        "session_id": session_id,
        "raw_input": goal,
        "goal": goal,
        "started_at": "2026-10-12T17:15:30+08:00",
        "finished_at": None,
        "status": status,
        "status_detail": "",
        "live": status == "running",
        "panel_size": 4,
        "round_count": 2,
        "doc_chars": 0,
        "has_doc": False,
        "disagreements": {"total": 3, "open": 1, "resolved": 1, "tradeoff": 1, "stalemate": 1},
        "model": "deepseek-v4.1-flash",
        "max_rounds": 3,
    }
    base.update(extra)
    return base


class DemoApi:
    """``server.ProposalApi`` 的假实现。**只存在于夹具里**，产品代码里没有它。"""

    def __init__(self, *, delay: float) -> None:
        self._delay = delay
        self._hub = EventHub()
        self._lock = threading.Lock()
        self._live_ids: set[str] = set()
        self._interjection_seq = _INTERJECTION_SEQ_BASE
        self._proposals: list[dict] = [
            _proposal(
                LIVE_ID, "做一个命令行番茄钟，单文件、不联网（演示：进行中）", "running",
                status_detail="本进程正在跑", started_at="2026-10-12T17:15:30+08:00",
            ),
            _proposal(
                DONE_ID, "给播客自动生成章节和文字稿的小工具，本地跑，不要云服务（演示：已完成）",
                "completed", status_detail="已完成", started_at="2026-10-12T14:26:56+08:00",
                finished_at="2026-10-12T14:37:29+08:00", has_doc=True, doc_chars=len(DEMO_DOC),
                live=False,
            ),
            _proposal(
                STALE_ID, "给团队做一个内部知识库（演示：进程已退出）", "stale",
                status_detail="发起它的进程已退出，讨论停在这里",
                started_at="2026-10-12T16:01:02+08:00", round_count=1, live=False,
                disagreements={"total": 2, "open": 2, "resolved": 0, "tradeoff": 0, "stalemate": 0},
            ),
        ]

    # ── ProposalApi ──

    def list_proposals(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._proposals]

    def get_proposal(self, session_id: str) -> dict | None:
        with self._lock:
            for item in self._proposals:
                if item["session_id"] == session_id:
                    return dict(item)
        return None

    def create_proposal(self, payload: dict) -> dict:
        idea = str(payload.get("idea", "")).strip()
        if not idea:
            raise UsageError("想法不能为空。")
        # id 会出现在 URL 里，必须 slug 过 —— 否则想法里的 / 会把路由切坏。
        session_id = f"20261012-{int(time.time()) % 1000000:06d}-{slugify(idea)}"
        item = _proposal(session_id, idea, "running", status_detail="本进程正在跑",
                         started_at=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        item["disagreements"] = None
        item["round_count"] = 0
        with self._lock:
            self._proposals.insert(0, item)
            self._live_ids.add(session_id)
        threading.Thread(target=self.play, args=(session_id,), daemon=True).start()
        return dict(item)

    def stop_proposal(self, session_id: str) -> bool:
        with self._lock:
            if session_id not in self._live_ids:
                return False
            self._live_ids.discard(session_id)
            for item in self._proposals:
                if item["session_id"] == session_id:
                    item["live"] = False
                    item["status"] = "interrupted"
                    item["status_detail"] = "（演示）被停止请求收口了"
        self._hub.publish(Event(
            type="session.end", session_id=session_id, seq=self._next_seq(),
            data={"status": "interrupted", "usage": {"calls": 3, "total_tokens": 12000}},
        ))
        return True

    def interject(self, session_id: str, text: str) -> bool:
        if not self.is_live(session_id):
            return False
        self._hub.publish(Event(
            type="client.interjection", session_id=session_id, seq=self._next_seq(),
            data={"text": text, "received_round": 1},
        ))
        return True

    def is_live(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._live_ids

    def unsubscribe(self, session_id: str, target: queue.Queue) -> None:
        if session_id == LIVE_ID:
            self._hub.unsubscribe(target)

    def read_design_doc(self, session_id: str) -> str | None:
        proposal = self.get_proposal(session_id)
        if proposal is None or not proposal["has_doc"]:
            return None
        return DEMO_DOC

    def subscribe(self, session_id: str) -> tuple[queue.Queue[Event], list[Event]] | None:
        if session_id == LIVE_ID:
            return self._hub.subscribe()
        proposal = self.get_proposal(session_id)
        if proposal is None:
            return None
        # 已结束的提案：把整段历史当快照回放。未跑完的那份**不含 session.end**，
        # 正是为了验证"连接结束了但讨论没收口"这条路径在界面上长什么样。
        prefix = len(build_events()) if proposal["status"] == "completed" else LIVE_PREFIX
        return queue.Queue(), _events_for(
            prefix, session_id=session_id, goal=proposal["goal"]
        )

    # ── 内部 ──

    def _next_seq(self) -> int:
        with self._lock:
            self._interjection_seq += 1
            return self._interjection_seq

    def play(self, session_id: str) -> None:
        time.sleep(0.6)  # 等页面连上
        proposal = self.get_proposal(session_id) or {}
        for event in _events_for(
            LIVE_PREFIX, session_id=session_id, goal=proposal.get("goal")
        ):
            self._hub.publish(event)
            time.sleep(self._delay)
        # 播完仍然保持"进行中"：夹具要让列表里一直有一份在跑的提案，
        # 否则每 3 秒的清单轮询这条路径就没得看。


def main() -> int:
    parser = argparse.ArgumentParser(description="Sparky 前端开发夹具（不调用 LLM）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--delay", type=float, default=0.45, help="每条事件之间的间隔秒数")
    args = parser.parse_args()

    api = DemoApi(delay=args.delay)
    server, _ = server_stage.start(api, host="127.0.0.1", port=args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"演示服务已启动：{url}")
    print("（这是合成事件，没有调用任何模型）")
    print(f"  提案列表：{url}/#/")
    print(f"  进行中的提案：{url}/#/p/{LIVE_ID}")

    # 一上来就开播，这样列表页上立刻能看到"进行中"那一份
    threading.Thread(target=api.play, args=(LIVE_ID,), daemon=True).start()

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
