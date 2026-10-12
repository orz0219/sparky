"use strict";

/* Sparky 本地网页前端。
   零构建、零外链、零依赖 —— 只用原生 DOM 与 EventSource。
   它**不认识 LLM**，只消费后端推来的事件流（docs/architecture.md §2 的分层禁令）。

   信息架构是「会议纪要」而不是「聊天室」：
     侧栏 = 进行到哪 + 在争什么（人类最先想知道的两件事）
     主区 = 按轮分组的讨论，历史轮可折叠
     底部 = 插话输入（收到即记，轮间送达） */

const $ = (id) => document.getElementById(id);

const els = {
  goal: $("goal"), roundBadge: $("round-badge"), status: $("status"),
  stop: $("stop"), toggleAll: $("toggle-all"), banner: $("banner"),
  stages: $("stages"), ledger: $("ledger"), ledgerCount: $("ledger-count"),
  peopleBlock: $("people-block"), people: $("people"),
  memosBlock: $("memos-block"), memosToggle: $("memos-toggle"),
  memos: $("memos"), memosCount: $("memos-count"),
  mineBlock: $("mine-block"), mine: $("mine"),
  stream: $("stream"), say: $("say"), send: $("send"), sayHint: $("say-hint"),
};

const state = {
  personas: new Map(), // id -> { name, title }
  memoCount: 0,
  rounds: new Map(), // round -> { el, body, foot, speeches: Map, collapsed }
  currentRound: 0,
  ledger: new Map(), // id -> { el, round }
  mine: new Map(), // 插话序号 -> { railEl, streamEl, round }
  pending: new Map(), // persona_id -> 待写入的增量（按帧批量渲染）
  finished: false,
};

const STATUS_TEXT = { open: "未决", resolved: "已解决", tradeoff: "取舍", stalemate: "僵局" };
const STATUS_MARK = { open: "●", resolved: "✓", tradeoff: "⇄", stalemate: "⊘" };

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
  return `hsl(${h}, 58%, 60%)`;
}

function setStage(index) {
  els.stages.querySelectorAll("li").forEach((li, i) => {
    li.classList.toggle("active", i === index);
    li.classList.toggle("done", i < index);
    li.querySelector(".mark").textContent = i < index ? "✓" : i === index ? "●" : "○";
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

function scrollStream() {
  // 只在用户本来就贴着底部时自动滚动，免得打断向上翻阅
  const nearBottom =
    window.innerHeight + window.scrollY >= document.body.scrollHeight - 180;
  if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
}

/* ───────────── 参会 / 调研结论 ───────────── */

function renderPeople(panel) {
  els.peopleBlock.hidden = false;
  els.people.replaceChildren();
  state.personas.clear();
  for (const p of panel.personas) {
    const item = node("span", "person" + (p.role_type === "adversary" ? " adv" : ""));
    const dot = node("span", "dot");
    dot.style.background = hueFor(p.id);
    item.appendChild(dot);
    item.appendChild(node("span", "pname", p.name));
    item.appendChild(node("span", "prole", p.role_type === "adversary" ? "唱反调者" : p.title));
    els.people.appendChild(item);
    state.personas.set(p.id, { name: p.name, title: p.title });
  }
}

function addMemo(personaId, memo) {
  els.memosBlock.hidden = false;
  const p = state.personas.get(personaId) || { name: personaId, title: "" };
  const item = node("div", "memo");
  item.style.setProperty("--hue", hueFor(personaId));
  item.title = "点开看完整论证";

  const head = node("div", "memo-head");
  head.appendChild(node("span", "mname", p.name));
  head.appendChild(node("span", "mrole", p.title));
  item.appendChild(head);
  item.appendChild(node("div", "memo-claim", memo.core_claim));

  const detail = node("div", "memo-detail");
  detail.hidden = true;
  detail.appendChild(node("div", null, "优点：" + (memo.pros || []).join("；")));
  detail.appendChild(node("div", null, "代价：" + (memo.cons || []).join("；")));
  detail.appendChild(node("div", null, "让步条件：" + memo.would_concede_if));
  const unverified = (memo.evidence || []).filter((e) => e.unverified).length;
  if (unverified) {
    detail.appendChild(node("div", null, `⚠️ 其中 ${unverified} 条论断未经验证`));
  }
  item.appendChild(detail);
  item.addEventListener("click", () => { detail.hidden = !detail.hidden; });

  els.memos.appendChild(item);
  state.memoCount += 1;
  els.memosCount.textContent = `${state.memoCount} 人`;
}

/* ───────────── 讨论流：按轮分组 ───────────── */

function roundFor(roundNo, label, meta) {
  let round = state.rounds.get(roundNo);
  if (round) return round;

  const el = node("section", "round");
  el.dataset.round = String(roundNo);

  const head = node("button", "round-head");
  head.type = "button";
  head.appendChild(node("span", "round-title", `第 ${roundNo} 轮 · ${label}`));
  head.appendChild(node("span", "round-meta", meta));
  head.appendChild(node("span", "round-fold", "收起"));
  el.appendChild(head);

  const body = node("div", "round-body");
  el.appendChild(body);
  const foot = node("div", "round-foot");
  foot.hidden = true;
  el.appendChild(foot);

  head.addEventListener("click", () => setCollapsed(roundNo, !round.collapsed));

  round = { el, body, foot, speeches: new Map(), collapsed: false };
  state.rounds.set(roundNo, round);
  els.stream.appendChild(el);
  return round;
}

function setCollapsed(roundNo, collapsed) {
  const round = state.rounds.get(roundNo);
  if (!round) return;
  round.collapsed = collapsed;
  round.el.classList.toggle("collapsed", collapsed);
  round.el.querySelector(".round-fold").textContent = collapsed ? "展开" : "收起";
  const anyCollapsed = [...state.rounds.values()].some((r) => r.collapsed);
  els.toggleAll.hidden = !anyCollapsed;
}

function collapseAllBefore(roundNo) {
  for (const [no, round] of state.rounds) {
    if (no < roundNo && !round.collapsed) setCollapsed(no, true);
  }
}

function speechFor(roundNo, personaId) {
  const round = roundFor(roundNo, "进行中", "");
  const existing = round.speeches.get(personaId);
  if (existing) return existing;

  const p = state.personas.get(personaId) || { name: personaId, title: "" };
  const el = node("article", "speech pending");
  el.style.setProperty("--hue", hueFor(personaId));

  const head = node("div", "speech-head");
  head.appendChild(node("span", "speech-name", p.name));
  head.appendChild(node("span", "speech-role", p.title));
  el.appendChild(head);
  el.appendChild(node("p", "speech-text", ""));
  round.body.appendChild(el);

  round.speeches.set(personaId, el);
  return el;
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
      const el = speechFor(state.currentRound, personaId);
      el.querySelector(".speech-text").textContent += text;
    }
    state.pending.clear();
    scrollStream();
  });
}

/* 系统消息与插话：进当前轮；还没有轮次时进流本身 */
function addNote(text, className) {
  const host = state.currentRound
    ? roundFor(state.currentRound, "进行中", "").body
    : els.stream;
  const el = node("div", "note" + (className ? " " + className : ""), text);
  host.appendChild(el);
  scrollStream();
  return el;
}

/* ───────────── 议程 ───────────── */

function agendaFor(id) {
  let item = state.ledger.get(id);
  if (item) return item;
  const el = node("button", "agenda");
  el.type = "button";
  el.dataset.status = "open";
  el.appendChild(node("span", "mark", STATUS_MARK.open));
  el.appendChild(node("span", "topic", ""));
  el.appendChild(node("span", "meta", ""));
  els.ledger.appendChild(el);
  item = { el, round: 0 };
  state.ledger.set(id, item);
  return item;
}

function setAgendaStatus(item, status, meta) {
  item.el.dataset.status = status;
  item.el.querySelector(".mark").textContent = STATUS_MARK[status] || "●";
  item.el.querySelector(".meta").textContent = `${STATUS_TEXT[status] || status} · ${meta}`;
}

function updateLedgerCount() {
  els.ledgerCount.textContent = state.ledger.size ? `${state.ledger.size} 项` : "";
}

/* ───────────── 事件处理 ───────────── */

const handlers = {
  "session.start"(d) {
    els.status.textContent = "会话已开始";
    setStage(0);
  },

  "brief.ready"(d) {
    els.goal.textContent = d.brief.goal;
    const novel = d.brief.dimensions.filter((x) => x.source === "inferred").length;
    addNote(
      `立案：抽出 ${d.brief.dimensions.length} 个设计维度，其中 ${novel} 个是你没说过的` +
        `（${d.brief.dimensions.map((x) => x.name).join("、")}）`
    );
    setStage(1);
  },

  "panel.ready"(d) {
    renderPeople(d.panel);
    addNote(`组阁：判定需要 ${d.panel.panel_size} 人。${d.panel.panel_size_reasoning}`);
    setStage(2);
  },

  "persona.assigned"() {
    /* 详情已由 panel.ready 统一渲染 */
  },

  "recon.start"() {
    /* 每人的调研状态由人设卡与调研结论区承载，不刷讨论流 */
  },

  "recon.memo"(d) {
    addMemo(d.persona_id, d.memo);
  },

  "recon.failed"(d) {
    showBanner(`⚠ ${d.name} 调研失败，会缺席定稿：${d.reason}`);
  },

  "round.start"(d) {
    state.currentRound = d.round;
    collapseAllBefore(d.round);
    const label = d.mode === "opening" ? "全员立场陈述" : "定向加轮";
    const meta =
      d.mode === "opening"
        ? `${d.participants.length} 人发言`
        : `${d.participants.length} 人 · 只召集未决分歧的当事双方`;
    const round = roundFor(d.round, label, meta);
    els.roundBadge.hidden = false;
    els.roundBadge.textContent = `第 ${d.round} 轮 · ${label}`;
    els.status.textContent = `${label} · ${d.participants.length} 人`;

    if (d.mode !== "opening") {
      const topics = d.topics || {};
      const names = d.focus.map((id) => `${id}（${topics[id] || ""}）`).join("、");
      round.body.appendChild(node("div", "note", `聚焦：${names}`));
    }
    setStage(3);
  },

  "turn.start"(d) {
    speechFor(d.round || state.currentRound, d.persona_id);
  },

  "turn.token"(d) {
    if (d.reset) {
      state.pending.delete(d.persona_id);
      const el = speechFor(state.currentRound, d.persona_id);
      el.querySelector(".speech-text").textContent = "";
      return;
    }
    state.pending.set(d.persona_id, (state.pending.get(d.persona_id) || "") + d.delta);
    scheduleFlush();
  },

  "turn.end"(d) {
    const t = d.turn;
    const el = speechFor(t.round || state.currentRound, t.persona_id);
    state.pending.delete(t.persona_id);
    el.classList.remove("pending");
    if (t.abstained) {
      el.classList.add("abstain");
      el.querySelector(".speech-text").textContent =
        "弃权，本轮没有发言" + (t.abstain_reason ? `（${t.abstain_reason}）` : "");
      return;
    }
    el.querySelector(".speech-text").textContent = t.summary;
    if (t.actions) el.appendChild(node("div", "speech-acts", t.actions));
    scrollStream();
  },

  "round.end"(d) {
    const round = state.rounds.get(d.round);
    if (!round) return;
    const g = d.digest || {};
    const unresolved = (g.open || 0) + (g.stalemate || 0);
    round.foot.hidden = false;
    round.foot.textContent =
      `本轮结束 · 分歧 ${g.total || 0} 项` +
      `（已解决 ${g.resolved || 0}、取舍 ${g.tradeoff || 0}、未决 ${unresolved}）`;
    els.status.textContent = `第 ${d.round} 轮结束`;
    setStage(4);
  },

  "disagreement.open"(d) {
    const item = agendaFor(d.id);
    item.round = d.round || 0;
    item.el.querySelector(".topic").textContent = d.topic;
    setAgendaStatus(item, "open", d.parties.join(" ↔ "));
    item.el.addEventListener("click", () => {
      const round = state.rounds.get(item.round);
      if (!round) return;
      setCollapsed(item.round, false);
      round.el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    updateLedgerCount();
  },

  "disagreement.update"(d) {
    const item = state.ledger.get(d.id);
    const status = (d.patch || {}).status;
    if (item && status) setAgendaStatus(item, status, item.el.querySelector(".meta").textContent.split(" · ")[1] || "");
  },

  "disagreement.close"(d) {
    const item = agendaFor(d.id);
    if (d.resolution) {
      const how = d.resolution.kind === "convinced" ? "被说服" : "让步";
      setAgendaStatus(item, d.status, `${d.resolution.by} ${how}`);
    } else if (d.tradeoff) {
      setAgendaStatus(item, d.status, "已写明翻转条件");
    } else {
      setAgendaStatus(item, d.status, "");
    }
    item.el.title = d.tradeoff
      ? `选「${d.tradeoff.choose}」；反过来选的条件：${d.tradeoff.when_to_flip}`
      : d.resolution
        ? d.resolution.because
        : "";
  },

  "convergence.decision"(d) {
    const kind = d.decision;
    if (kind === "another_round") {
      addNote(`收敛判定：继续讨论 —— ${d.detail}`);
    } else if (kind === "done") {
      addNote(`收敛判定：全部谈拢 —— ${d.detail}`, "strong");
    } else {
      addNote(`收敛判定：收口（未完全收敛）—— ${d.detail}`, "strong");
      showBanner("⚠ 讨论收口时仍有未决项，它们会如实进入文档第 8 节。");
    }
    setStage(4);
  },

  "client.interjection"(d) {
    const round = state.currentRound;
    // 流里留一份（"当时发生的事"），侧栏留一份（永不隐藏的索引）
    const streamEl = addNote("", "mine");
    streamEl.appendChild(node("span", "mine-label", "你"));
    streamEl.appendChild(node("span", null, d.text));
    streamEl.appendChild(node("span", "mine-hint", "已记下，会在本轮结束时送达专家"));

    els.mineBlock.hidden = false;
    const railEl = node("div", "mine-item");
    railEl.dataset.delivered = "false";
    railEl.appendChild(node("div", "mine-text", d.text));
    railEl.appendChild(node("div", "mine-status", "待送达：本轮结束后交给专家"));
    railEl.addEventListener("click", () => {
      if (!round) return;
      setCollapsed(round, false);
      state.rounds.get(round).el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    els.mine.appendChild(railEl);

    state.mine.set(state.mine.size, { railEl, streamEl, round });
  },

  "client.interjection.delivered"(d) {
    // 找到最近一条还没送达的 —— 事件按顺序到达，所以取第一个未送达的就是它
    for (const entry of state.mine.values()) {
      if (entry.railEl.dataset.delivered === "true") continue;
      entry.railEl.dataset.delivered = "true";
      entry.railEl.querySelector(".mine-status").textContent = `已在第 ${d.round} 轮送达`;
      const hint = entry.streamEl.querySelector(".mine-hint");
      if (hint) hint.textContent = `已在第 ${d.round} 轮送达专家`;
      return;
    }
  },

  "synthesis.start"(d) {
    els.status.textContent = `正在定稿（${d.sections.length} 节）`;
    addNote("讨论结束，正在装配设计文档并做合规校验…");
    setStage(5);
  },

  "doc.ready"(d) {
    els.status.textContent = `已写出 ${d.chars} 字的文档`;
    addNote(`定稿完成 → ${d.path}`, "strong");
    setStage(6);
  },

  "warning"(d) {
    showBanner(`⚠ [${d.code}] ${d.message}`);
  },

  "error"(d) {
    showBanner(`✗ [${d.stage}] ${d.message}`, true);
    els.status.textContent = "已失败";
  },

  "session.end"(d) {
    state.finished = true;
    const usage = d.usage || {};
    els.status.textContent =
      `会话结束（${d.status}）· LLM ${usage.calls || 0} 次 · token ${usage.total_tokens || 0}`;
    els.stop.hidden = true;
    els.say.disabled = true;
    els.send.disabled = true;
    els.say.placeholder = "会话已结束，插话关闭。";
    els.sayHint.textContent = "";
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

/* ───────────── 交互 ───────────── */

els.memosToggle.addEventListener("click", () => {
  const open = els.memosToggle.getAttribute("aria-expanded") === "true";
  els.memosToggle.setAttribute("aria-expanded", String(!open));
  els.memos.hidden = open;
});

els.toggleAll.addEventListener("click", () => {
  for (const no of state.rounds.keys()) setCollapsed(no, false);
  els.toggleAll.hidden = true;
});

els.stop.addEventListener("click", () => {
  els.stop.disabled = true;
  els.stop.textContent = "已请求停止…";
  fetch("/stop", { method: "POST" }).catch(() => {
    showBanner("✗ 停止请求没发出去（服务可能已经退出）。", true);
  });
});

function sendInterjection() {
  const text = els.say.value.trim();
  if (!text || state.finished) return;
  els.send.disabled = true;
  els.sayHint.textContent = "发送中…";
  fetch("/interject", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  })
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) throw new Error(body.message || "服务拒绝了这条插话");
      els.say.value = "";
      els.sayHint.textContent = "";
    })
    .catch((err) => {
      // 发不出去必须说出来 —— 用户写的东西不能悄悄消失
      els.sayHint.textContent = "没发出去";
      showBanner(`✗ 插话没送达：${err.message}`, true);
    })
    .finally(() => { els.send.disabled = state.finished; });
}

els.send.addEventListener("click", sendInterjection);
els.say.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendInterjection();
});

// 前端自身出错也要看得见，而不是留一个安静的白屏
window.addEventListener("error", (e) => {
  showBanner(`✗ 前端脚本出错：${e.message || "未知错误"}`, true);
});
