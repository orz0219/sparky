"use strict";

/* Sparky 本地网页前端。
   零构建、零外链、零依赖 —— 只用原生 DOM、EventSource 与 fetch。
   它**不认识 LLM**，只消费后端推来的事件流（docs/architecture.md §2 的分层禁令）。

   两个视图，靠 location.hash 切换：
     #/            提案列表（历史 + 进行中 + 新建表单）
     #/p/<提案 id>  提案详情（讨论流 + 设计文档）

   详情视图的信息架构是「会议纪要」而不是「聊天室」：
     侧栏 = 进行到哪 + 在争什么（人类最先想知道的两件事）
     主区 = 按轮分组的讨论，历史轮可折叠
     底部 = 插话输入（收到即记，轮间送达）

   看**历史**提案走的是同一条路：后端把 transcript.jsonl 重放成同一套事件，
   于是这里一行都不用改。 */

const $ = (id) => document.getElementById(id);

const els = {
  goal: $("goal"), roundBadge: $("round-badge"), status: $("status"),
  stop: $("stop"), toggleAll: $("toggle-all"), banner: $("banner"),
  viewList: $("view-list"), viewDetail: $("view-detail"),
  idea: $("idea"), composeForm: $("compose-form"), create: $("create"),
  panelSize: $("panel-size"), maxRounds: $("max-rounds"), composeHint: $("compose-hint"),
  proposalList: $("proposal-list"), proposalEmpty: $("proposal-empty"),
  proposalCount: $("proposal-count"), refresh: $("refresh"),
  tabMeeting: $("tab-meeting"), tabDoc: $("tab-doc"), detailStatus: $("detail-status"),
  reading: $("reading"),
  paneMeeting: $("pane-meeting"), paneDoc: $("pane-doc"),
  doc: $("doc"), docHint: $("doc-hint"),
  stages: $("stages"), ledger: $("ledger"), ledgerCount: $("ledger-count"),
  peopleBlock: $("people-block"), people: $("people"),
  memosBlock: $("memos-block"), memosToggle: $("memos-toggle"),
  memos: $("memos"), memosCount: $("memos-count"),
  mineBlock: $("mine-block"), mine: $("mine"),
  stream: $("stream"), composer: $("composer"),
  say: $("say"), send: $("send"), sayHint: $("say-hint"),
};

const state = {
  view: "list", // list | detail
  sessionId: null,
  headerFor: null, // 顶栏已经取过哪一份提案的详情
  source: null, // EventSource
  pollTimer: null,
  personas: new Map(), // id -> { name, title }
  memoCount: 0,
  rounds: new Map(), // round -> { el, body, foot, speeches: Map, collapsed }
  currentRound: 0,
  ledger: new Map(), // id -> { el, round }
  mine: new Map(), // 插话序号 -> { railEl, streamEl, round }
  pending: new Map(), // persona_id -> 待写入的增量（按帧批量渲染）
  ended: false,
  closedNotice: false,
  docLoaded: false,
};

const STATUS_TEXT = { open: "未决", resolved: "已解决", tradeoff: "取舍", stalemate: "僵局" };
const STATUS_MARK = { open: "●", resolved: "✓", tradeoff: "⇄", stalemate: "⊘" };

/* 提案的六种状态。后端归一化后只给这几种，前端只负责翻译成人话。 */
const PROPOSAL_LABEL = {
  running: "● 进行中",
  completed: "✓ 已完成",
  interrupted: "⊘ 已中断",
  stale: "⊘ 未跑完",
  failed: "✗ 已失败",
  broken: "⚠ 损坏",
  unknown: "? 未知",
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
  return `hsl(${h}, 58%, 60%)`;
}

/* 时间戳是带时区的 ISO 串。只取「月-日 时:分」——清单里不需要年份和秒。 */
function fmtTime(iso) {
  if (!iso) return "";
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : String(iso);
}

function setStage(index) {
  els.stages.querySelectorAll("li").forEach((li, i) => {
    li.classList.toggle("active", i === index);
    li.classList.toggle("done", index >= 0 && i < index);
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

function clearBanner() {
  els.banner.hidden = true;
  els.banner.textContent = "";
}

function scrollStream() {
  // 只在用户本来就贴着底部时自动滚动，免得打断向上翻阅
  const nearBottom =
    window.innerHeight + window.scrollY >= document.body.scrollHeight - 180;
  if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
}

/* ───────────── 路由 ───────────── */

function route() {
  // #/                      提案列表
  // #/p/<提案 id>            提案详情 · 讨论
  // #/p/<提案 id>/doc        提案详情 · 设计文档
  // 把页签也放进 hash，刷新页面才能停在同一页签上，链接也能直接发给别人。
  const matched = (location.hash || "#/").match(/^#\/p\/([^/]+)(?:\/(doc))?$/);
  if (matched) openDetail(decodeURIComponent(matched[1]), matched[2] || "meeting");
  else openList();
}

function hashFor(sessionId, tab) {
  return `#/p/${encodeURIComponent(sessionId)}/${tab}`;
}

function openList() {
  state.view = "list";
  closeSource();
  els.viewList.hidden = false;
  els.viewDetail.hidden = true;
  els.composer.hidden = true;
  els.goal.textContent = "提案工作台";
  els.status.textContent = "";
  els.roundBadge.hidden = true;
  els.stop.hidden = true;
  els.toggleAll.hidden = true;
  clearBanner();
  refreshList();
}

async function refreshList() {
  clearTimeout(state.pollTimer);
  try {
    const response = await fetch("/api/proposals");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    if (state.view !== "list") return;
    renderList(body.proposals);
    // 有提案在跑就每 3 秒刷一次清单。没有就不刷 —— 空转的轮询只是噪音。
    if (body.proposals.some((p) => p.live)) {
      state.pollTimer = setTimeout(refreshList, 3000);
    }
  } catch (err) {
    showBanner(`✗ 读不到提案清单：${err.message}`, true);
  }
}

function renderList(proposals) {
  els.proposalList.replaceChildren();
  els.proposalEmpty.hidden = proposals.length > 0;
  els.proposalCount.textContent = proposals.length ? `${proposals.length} 份` : "";
  for (const p of proposals) els.proposalList.appendChild(proposalCard(p));
}

function proposalCard(p) {
  const card = node("a", "proposal");
  card.href = `#/p/${encodeURIComponent(p.session_id)}`;
  card.dataset.status = p.status;

  const head = node("div", "proposal-head");
  head.appendChild(node("span", "pstatus", PROPOSAL_LABEL[p.status] || p.status));
  head.appendChild(node("span", "ptime", fmtTime(p.started_at)));
  card.appendChild(head);

  card.appendChild(node("div", "pgoal", p.goal || p.raw_input || "（没有留下想法描述）"));

  const facts = [];
  if (p.panel_size) facts.push(`${p.panel_size} 人`);
  if (p.round_count) facts.push(`${p.round_count} 轮`);
  if (p.disagreements) {
    const unresolved = (p.disagreements.open || 0) + (p.disagreements.stalemate || 0);
    facts.push(`分歧 ${p.disagreements.total}（未决 ${unresolved}）`);
  }
  facts.push(p.has_doc ? `文档 ${p.doc_chars} 字` : "还没有文档");
  card.appendChild(node("div", "pmeta", facts.join(" · ")));

  if (p.status_detail && p.status !== "completed") {
    card.appendChild(node("div", "pdetail", p.status_detail));
  }
  return card;
}

async function openDetail(sessionId, tab) {
  state.view = "detail";
  clearTimeout(state.pollTimer);
  els.viewList.hidden = true;
  els.viewDetail.hidden = false;
  els.composer.hidden = false;

  if (state.sessionId !== sessionId) {
    resetSession();
    state.sessionId = sessionId;
    connect(sessionId);
  }
  setTab(tab || "meeting", { updateHash: false });
  // 顶栏那行只在**换了一份提案**时去取。页签切换也会走这条路由，
  // 不判一下的话每点一次页签就白请求一次提案详情。
  if (state.headerFor !== sessionId) {
    state.headerFor = sessionId;
    loadHeader(sessionId);
  }
}

async function loadHeader(sessionId) {
  els.goal.textContent = "正在读取提案…";
  els.detailStatus.textContent = "";
  try {
    const response = await fetch(`/api/proposals/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const p = (await response.json()).proposal;
    if (state.sessionId !== sessionId) return;
    els.goal.textContent = p.goal || p.raw_input || sessionId;
    const facts = [PROPOSAL_LABEL[p.status] || p.status, fmtTime(p.started_at)];
    if (p.panel_size) facts.push(`${p.panel_size} 人`);
    if (p.round_count) facts.push(`${p.round_count} 轮`);
    els.detailStatus.textContent = facts.filter(Boolean).join(" · ");
    if (p.has_doc) els.docHint.textContent = `共 ${p.doc_chars} 字`;
  } catch (err) {
    showBanner(`✗ 读不到这份提案：${err.message}`, true);
  }
}

/* ───────────── 详情：连接与重置 ───────────── */

function connect(sessionId) {
  closeSource();
  state.source = new EventSource(`/events?session=${encodeURIComponent(sessionId)}`);
  for (const type of Object.keys(handlers)) {
    state.source.addEventListener(type, (e) => {
      handlers[type](JSON.parse(e.data).data);
    });
  }
  state.source.onopen = () => {
    if (!state.ended) els.status.textContent = "已连接";
  };
  state.source.onerror = () => {
    if (state.ended || state.view !== "detail") return;
    // **必须显式关掉。** EventSource 在连接断开时会自动重连，而这场讨论
    // 已经不会再产生事件了（后端补完历史就收口）—— 不关的话浏览器会
    // 一遍遍重连、一遍遍重放，页面上的每一段发言都会多出一份。
    // 这不是兜底：后端已经明确表示"没有实时流了"，重连在构造上就没有意义。
    closeSource();
    if (state.closedNotice) return;
    state.closedNotice = true;
    // **不清屏** —— 已发生的事实必须留在屏幕上。
    showBanner(
      "✗ 与本地服务的连接结束了，而这场讨论没有正常收口。" +
        "下面是已落盘的全部内容；发起它的进程可能已经退出。",
      true
    );
  };
}

function closeSource() {
  if (state.source) {
    state.source.close();
    state.source = null;
  }
}

function resetSession() {
  closeSource();
  state.personas.clear();
  state.rounds.clear();
  state.ledger.clear();
  state.mine.clear();
  state.pending.clear();
  state.memoCount = 0;
  state.currentRound = 0;
  state.headerFor = null;
  state.ended = false;
  state.closedNotice = false;
  state.docLoaded = false;

  els.stream.replaceChildren();
  els.ledger.replaceChildren();
  els.mine.replaceChildren();
  els.memos.replaceChildren();
  els.people.replaceChildren();
  els.doc.replaceChildren();
  els.docHint.textContent = "";
  els.peopleBlock.hidden = true;
  els.memosBlock.hidden = true;
  els.memos.hidden = true;
  els.memosToggle.setAttribute("aria-expanded", "false");
  els.memosCount.textContent = "";
  els.ledgerCount.textContent = "";
  els.mineBlock.hidden = true;
  els.roundBadge.hidden = true;
  els.toggleAll.hidden = true;
  els.stop.hidden = true;
  els.stop.disabled = false;
  els.stop.textContent = "停止";
  els.say.value = "";
  els.say.disabled = false;
  els.send.disabled = false;
  els.say.placeholder = "你随时可以说，意见会在本轮结束时送达专家…";
  els.sayHint.textContent = "";
  clearBanner();
  setStage(-1);
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
  "session.start"() {
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
    if (item && status) {
      const previous = item.el.querySelector(".meta").textContent.split(" · ")[1] || "";
      setAgendaStatus(item, status, previous);
    }
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
    // 定稿之后文档才存在：让「设计文档」页签重新去取一次。
    state.docLoaded = false;
    els.docHint.textContent = `共 ${d.chars} 字`;
    if (!els.paneDoc.hidden) loadDoc();
  },

  "warning"(d) {
    showBanner(`⚠ [${d.code}] ${d.message}`);
  },

  "session.error"(d) {
    showBanner(`✗ [${d.stage}] ${d.message}`, true);
    els.status.textContent = "已失败";
  },

  "session.end"(d) {
    state.ended = true;
    const usage = d.usage || {};
    els.status.textContent =
      `会话结束（${d.status}）· LLM ${usage.calls || 0} 次 · token ${usage.total_tokens || 0}`;
    els.stop.hidden = true;
    els.say.disabled = true;
    els.send.disabled = true;
    els.say.placeholder = "会话已结束，插话关闭。";
    els.sayHint.textContent = "";
    closeSource();
    setStage(d.status === "completed" ? 6 : 5);
  },
};

/* ───────────── 设计文档 ───────────── */

function setTab(name, { updateHash = true } = {}) {
  els.tabMeeting.classList.toggle("active", name === "meeting");
  els.tabDoc.classList.toggle("active", name === "doc");
  els.paneMeeting.hidden = name !== "meeting";
  els.paneDoc.hidden = name !== "doc";
  // 设计文档里全是四五列的表格，解除 660px 的对话限宽（见 style.css）
  els.reading.classList.toggle("wide", name === "doc");
  if (updateHash && state.sessionId) location.hash = hashFor(state.sessionId, name);
  if (name === "doc") loadDoc();
}

async function loadDoc() {
  if (state.docLoaded || !state.sessionId) return;
  els.docHint.textContent = "正在读取…";
  try {
    const response = await fetch(
      `/api/proposals/${encodeURIComponent(state.sessionId)}/doc`
    );
    if (response.status === 404) {
      els.docHint.textContent =
        "这份提案还没有产出设计文档 —— 讨论可能还在进行，或者它没能跑完。";
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    // 服务端渲染时已把原文里的 & < > 全部转义，这里注入是安全的。
    els.doc.innerHTML = body.html;
    els.docHint.textContent = `共 ${body.chars} 字`;
    state.docLoaded = true;
  } catch (err) {
    els.docHint.textContent = "读不到设计文档";
    showBanner(`✗ 读设计文档失败：${err.message}`, true);
  }
}

/* ───────────── 交互 ───────────── */

els.composeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const idea = els.idea.value.trim();
  if (!idea) return;
  els.create.disabled = true;
  els.composeHint.textContent = "正在发起…";
  fetch("/api/proposals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      idea,
      panel_size: els.panelSize.value,
      max_rounds: els.maxRounds.value,
    }),
  })
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) throw new Error(body.message || "服务拒绝了这份提案");
      els.idea.value = "";
      els.composeHint.textContent = "";
      location.hash = `#/p/${encodeURIComponent(body.proposal.session_id)}`;
    })
    .catch((err) => {
      // 发不出去必须说出来 —— 用户写的东西不能悄悄消失
      els.composeHint.textContent = "没发出去";
      showBanner(`✗ 提案没发起：${err.message}`, true);
    })
    .finally(() => { els.create.disabled = false; });
});

els.refresh.addEventListener("click", refreshList);
els.tabMeeting.addEventListener("click", () => setTab("meeting"));
els.tabDoc.addEventListener("click", () => setTab("doc"));
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
  if (!state.sessionId) return;
  els.stop.disabled = true;
  els.stop.textContent = "已请求停止…";
  fetch(`/api/proposals/${encodeURIComponent(state.sessionId)}/stop`, { method: "POST" })
    .then((r) => r.json().then((body) => ({ ok: r.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) throw new Error(body.message || "服务没有接受停止请求");
    })
    .catch((err) => {
      els.stop.textContent = "停止";
      els.stop.disabled = false;
      showBanner(`✗ 停止请求没生效：${err.message}`, true);
    });
});

function sendInterjection() {
  const text = els.say.value.trim();
  if (!text || state.ended || !state.sessionId) return;
  els.send.disabled = true;
  els.sayHint.textContent = "发送中…";
  fetch(`/api/proposals/${encodeURIComponent(state.sessionId)}/interject`, {
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
    .finally(() => { els.send.disabled = state.ended; });
}

els.send.addEventListener("click", sendInterjection);
els.say.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) sendInterjection();
});

// 前端自身出错也要看得见，而不是留一个安静的白屏
window.addEventListener("error", (e) => {
  showBanner(`✗ 前端脚本出错：${e.message || "未知错误"}`, true);
});

window.addEventListener("hashchange", route);
route();
