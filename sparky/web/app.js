"use strict";

/* Sparky 本地网页前端。
   零构建、零外链、零依赖 —— 只用原生 DOM 与 EventSource。
   它**不认识 LLM**，只消费后端推来的事件流（docs/architecture.md §2 的分层禁令）。 */

const $ = (id) => document.getElementById(id);

const els = {
  goal: $("goal"),
  roundBadge: $("round-badge"),
  status: $("status"),
  stop: $("stop"),
  banner: $("banner"),
  panelSection: $("panel-section"),
  panel: $("panel"),
  stream: $("stream"),
  ledger: $("ledger"),
  ledgerCount: $("ledger-count"),
  timelineNote: $("timeline-note"),
};

const state = {
  personas: new Map(), // id -> { name, el }
  bubbles: new Map(), // persona_id -> 当前轮的气泡元素
  pending: new Map(), // persona_id -> 待写入的增量（按帧批量渲染）
  ledger: new Map(), // 分歧 id -> 台账条目元素
  round: 0,
  finished: false,
};

/* ───────────── 小工具 ───────────── */

function node(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

function hueFor(id) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 58%)`;
}

function setStage(index) {
  document.querySelectorAll(".timeline .stage").forEach((el, i) => {
    el.classList.toggle("active", i === index);
    el.classList.toggle("done", i < index);
  });
}

function showBanner(text, isError) {
  // 错误条**不清空**已有内容 —— 用户必须能看到出错前发生了什么。
  els.banner.hidden = false;
  els.banner.classList.toggle("error", !!isError);
  els.banner.textContent = els.banner.textContent
    ? els.banner.textContent + "\n" + text
    : text;
}

function addSystem(text, roundLabel) {
  if (roundLabel) {
    const sep = node("div", "round-sep");
    sep.appendChild(node("span", null, roundLabel));
    els.stream.appendChild(sep);
  }
  if (text) els.stream.appendChild(node("div", "bubble system", text));
  scrollStream();
}

function scrollStream() {
  // 只在用户本来就贴着底部时自动滚动，免得打断向上翻阅
  const nearBottom =
    window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
  if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
}

/* ───────────── 人设卡 ───────────── */

function renderPanel(panel) {
  els.panelSection.hidden = false;
  els.panel.replaceChildren();
  state.personas.clear();
  for (const p of panel.personas) {
    const card = node("div", "card");
    card.style.setProperty("--hue", hueFor(p.id));
    const head = node("div");
    head.appendChild(node("span", "name", p.name));
    if (p.role_type === "adversary") head.appendChild(node("span", "tag adv", "反方"));
    else head.appendChild(node("span", "tag", p.title));
    card.appendChild(head);
    card.appendChild(node("div", "title", p.id + " · " + p.title));
    card.appendChild(node("div", "stance", p.stance));
    card.appendChild(node("div", "state", "待调研"));
    els.panel.appendChild(card);
    // title 也要存 —— 气泡抬头要用它，只存 name 会渲染出 "undefined"
    state.personas.set(p.id, { name: p.name, title: p.title, el: card });
  }
}

function personaState(id, text, cls) {
  const p = state.personas.get(id);
  if (!p) return;
  const stateEl = p.el.querySelector(".state");
  stateEl.textContent = text;
  p.el.classList.remove("done", "failed");
  if (cls) p.el.classList.add(cls);
}

/* ───────────── 讨论流 ───────────── */

function bubbleFor(personaId) {
  const existing = state.bubbles.get(personaId);
  if (existing) return existing;
  const persona = state.personas.get(personaId);
  const bubble = node("div", "bubble pending");
  bubble.style.setProperty("--hue", hueFor(personaId));

  const avatar = node("div", "avatar", persona ? persona.name.slice(0, 1) : "?");
  const body = node("div", "body");
  const who = node("div", "who");
  who.appendChild(node("b", null, persona ? persona.name : personaId));
  if (persona) who.appendChild(node("span", null, " · " + persona.title));
  body.appendChild(who);
  body.appendChild(node("div", "text", ""));
  bubble.appendChild(avatar);
  bubble.appendChild(body);

  state.bubbles.set(personaId, bubble);
  els.stream.appendChild(bubble);
  return bubble;
}

/* 打字机：按帧批量写入。
   一次发言有上千个 turn.token，逐个写 DOM 会卡住页面。 */
let flushScheduled = false;
function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  requestAnimationFrame(() => {
    flushScheduled = false;
    for (const [personaId, text] of state.pending) {
      const bubble = bubbleFor(personaId);
      const target = bubble.querySelector(".text");
      target.textContent += text;
    }
    state.pending.clear();
    scrollStream();
  });
}

/* ───────────── 分歧台账 ───────────── */

const STATUS_TEXT = {
  open: "未决",
  resolved: "已解决",
  tradeoff: "降级为取舍",
  stalemate: "僵局（未决）",
};

function ledgerItem(id) {
  let item = state.ledger.get(id);
  if (item) return item;
  item = node("div", "d-item");
  item.dataset.status = "open";
  const head = node("div", "d-head");
  head.appendChild(node("span", "d-id", id));
  head.appendChild(node("span", "d-topic", ""));
  item.appendChild(head);
  item.appendChild(node("div", "d-parties", ""));
  item.appendChild(node("div", "d-status", ""));
  els.ledger.appendChild(item);
  state.ledger.set(id, item);
  return item;
}

function updateLedgerCount() {
  els.ledgerCount.textContent = state.ledger.size ? "共 " + state.ledger.size + " 项" : "";
}

/* ───────────── 事件处理 ───────────── */

const handlers = {
  "session.start"(d) {
    els.status.textContent = "会话 " + d.session_id;
    setStage(0);
  },

  "brief.ready"(d) {
    els.goal.textContent = d.brief.goal;
    const dims = d.brief.dimensions.map((x) => x.name + (x.source === "inferred" ? "（推断）" : ""));
    addSystem("立案：抽出 " + d.brief.dimensions.length + " 个设计维度 —— " + dims.join("、"));
    setStage(1);
  },

  "panel.ready"(d) {
    renderPanel(d.panel);
    addSystem("组阁：判定需要 " + d.panel.panel_size + " 人。" + d.panel.panel_size_reasoning);
  },

  "persona.assigned"() {
    /* 详情已由 panel.ready 统一渲染，不重复 */
  },

  "recon.start"(d) {
    personaState(d.persona_id, "调研中…");
  },

  "recon.memo"(d) {
    personaState(d.persona_id, "✓ 已取证", "done");
    addSystem((state.personas.get(d.persona_id) || {}).name + "：" + d.memo.core_claim);
    setStage(2);
  },

  "recon.failed"(d) {
    personaState(d.persona_id, "✗ 调研失败，将缺席", "failed");
    showBanner("⚠ " + d.name + " 调研失败，会缺席定稿：" + d.reason);
  },

  "round.start"(d) {
    state.round = d.round;
    state.bubbles.clear();
    const label = d.mode === "opening" ? "全员立场陈述" : "定向加轮";
    els.roundBadge.hidden = false;
    els.roundBadge.textContent = "第 " + d.round + " 轮 · " + label;
    els.status.textContent = label + " · " + d.participants.length + " 人发言";
    addSystem(
      d.mode === "opening"
        ? ""
        : "本轮只召集 " + d.participants.join("、") + "（未决分歧的当事双方）",
      "第 " + d.round + " 轮 · " + label
    );
    setStage(3);
  },

  "turn.start"(d) {
    bubbleFor(d.persona_id);
  },

  "turn.token"(d) {
    if (d.reset) {
      state.pending.delete(d.persona_id);
      const bubble = state.bubbles.get(d.persona_id);
      if (bubble) bubble.querySelector(".text").textContent = "";
      return;
    }
    state.pending.set(d.persona_id, (state.pending.get(d.persona_id) || "") + d.delta);
    scheduleFlush();
  },

  "turn.end"(d) {
    const t = d.turn;
    const bubble = bubbleFor(t.persona_id);
    state.pending.delete(t.persona_id);
    bubble.classList.remove("pending");
    bubble.querySelector(".text").textContent = t.summary;
    if (t.abstained) {
      bubble.classList.add("abstain");
      bubble.querySelector(".text").textContent = "（弃权）" + (t.abstain_reason || "");
      return;
    }
    if (t.actions) bubble.appendChild(node("div", "actions", t.actions));
    scrollStream();
  },

  "round.end"(d) {
    const g = d.digest || {};
    els.status.textContent =
      "第 " + d.round + " 轮结束 · 分歧 " + (g.total || 0) + " 项";
    setStage(4);
  },

  "disagreement.open"(d) {
    const item = ledgerItem(d.id);
    item.querySelector(".d-topic").textContent = d.topic;
    item.querySelector(".d-parties").textContent = "当事人：" + d.parties.join(" · ");
    item.dataset.status = "open";
    item.querySelector(".d-status").textContent = STATUS_TEXT.open;
    updateLedgerCount();
  },

  "disagreement.update"(d) {
    const item = ledgerItem(d.id);
    const status = (d.patch || {}).status;
    if (status) {
      item.dataset.status = status;
      item.querySelector(".d-status").textContent =
        STATUS_TEXT[status] || status;
    }
  },

  "disagreement.close"(d) {
    const item = ledgerItem(d.id);
    item.dataset.status = d.status;
    let text = STATUS_TEXT[d.status] || d.status;
    if (d.resolution) {
      text += "：" + d.resolution.by + " " +
        (d.resolution.kind === "convinced" ? "被说服" : "让步") +
        " —— " + d.resolution.because;
    } else if (d.tradeoff) {
      text += "：选「" + d.tradeoff.choose + "」";
      if (d.tradeoff.when_to_flip) {
        item.appendChild(
          node("div", "d-flip", "反过来选的条件：" + d.tradeoff.when_to_flip)
        );
      }
    }
    item.querySelector(".d-status").textContent = text;
  },

  "convergence.decision"(d) {
    const kind = d.decision;
    if (kind === "another_round") {
      els.status.textContent = "继续讨论";
      addSystem("收敛判定：继续讨论 —— " + d.detail);
    } else if (kind === "done") {
      els.status.textContent = "已达成一致";
      addSystem("收敛判定：全部谈拢 —— " + d.detail);
    } else {
      els.status.textContent = "收口（未完全收敛）";
      showBanner("⚠ 讨论收口时仍有未决项，它们会进入文档第 8 节。\n" + d.detail);
      addSystem("收敛判定：收口 —— " + d.detail);
    }
    setStage(4);
  },

  "synthesis.start"(d) {
    els.status.textContent = "正在定稿（" + d.sections.length + " 节）";
    setStage(5);
  },

  "doc.ready"(d) {
    els.status.textContent = "已写出 " + d.chars + " 字的文档";
    els.timelineNote.textContent = d.path;
    addSystem("定稿完成 → " + d.path);
    setStage(6);
  },

  "warning"(d) {
    showBanner("⚠ [" + d.code + "] " + d.message);
  },

  "error"(d) {
    showBanner("✗ [" + d.stage + "] " + d.message, true);
    els.status.textContent = "已失败";
  },

  "session.end"(d) {
    state.finished = true;
    const usage = d.usage || {};
    els.status.textContent = "会话结束（" + d.status + "）· LLM " +
      (usage.calls || 0) + " 次 · token " + (usage.total_tokens || 0);
    els.stop.hidden = true;
    source.close();
    setStage(d.status === "completed" ? 6 : 5);
  },
};

/* ───────────── 连接 ───────────── */

const source = new EventSource("/events");

for (const type of Object.keys(handlers)) {
  source.addEventListener(type, (e) => {
    handlers[type](JSON.parse(e.data).data);
  });
}

source.onopen = () => {
  if (!state.finished) els.status.textContent = "已连接";
  els.stop.hidden = false;
};

source.onerror = () => {
  if (state.finished) return;
  // 后端进程没了，或网络断了。**不清屏** —— 已发生的事实必须留在屏幕上。
  showBanner(
    "✗ 与本地服务的连接中断了。已落盘的内容保留在 .sparky/sessions/ 下，" +
      "重开页面可以重新连上（服务还在的话）。",
    true
  );
};

els.stop.addEventListener("click", () => {
  els.stop.disabled = true;
  els.stop.textContent = "已请求停止…";
  fetch("/stop", { method: "POST" }).catch(() => {
    showBanner("✗ 停止请求没发出去（服务可能已经退出）。", true);
  });
});

// 前端自身出错也要看得见，而不是留一个安静的白屏
window.addEventListener("error", (e) => {
  showBanner("✗ 前端脚本出错：" + (e.message || "未知错误"), true);
});
