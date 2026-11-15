/* Shoot Clipper interface. Plain JavaScript, no build step, no network. */
"use strict";

// ── helpers ────────────────────────────────────────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

function fmt(t, dec = 0) {
  t = Math.max(0, +t || 0);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
  const ss = dec ? s.toFixed(dec).padStart(3 + dec, "0") : String(Math.floor(s)).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function parseTime(text) {
  const parts = String(text).trim().split(":").map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((a, p) => a * 60 + p, 0);
}
const stars = n => `<span class="stars">${"★".repeat(n)}<i>${"★".repeat(5 - n)}</i></span>`;

const ICONS = {
  home: '<path d="M3.5 10.5 12 4l8.5 6.5V20h-5.5v-6h-6v6H3.5z"/>',
  cut: '<circle cx="6" cy="6.5" r="2.5"/><circle cx="6" cy="17.5" r="2.5"/><path d="M8.2 7.8 20 17M8.2 16.2 20 7"/>',
  grid: '<rect x="4" y="3.5" width="6.5" height="8"/><rect x="13.5" y="3.5" width="6.5" height="8"/><rect x="4" y="14" width="6.5" height="6.5"/><rect x="13.5" y="14" width="6.5" height="6.5"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  moon: '<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  pause: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
};
function paintIcons(root = document) {
  $$("[data-ico]", root).forEach(el => { el.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[el.dataset.ico] || ""}</svg>`; });
}

async function call(name, ...args) {
  let res;
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && typeof bridge[name] === "function") {
    res = await bridge[name](...args);
  } else {
    const r = await fetch("/api/" + name, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Token": window.__TOKEN__ },
      body: JSON.stringify(args),
    });
    res = await r.json();
  }
  if (res && res.error) throw new Error(res.error);
  return res ? res.data : null;
}
/** Run a call and show any failure as a toast instead of an unhandled rejection. */
async function attempt(fn, okMsg) {
  try { const v = await fn(); if (okMsg) toast(okMsg); return v; }
  catch (e) { toast(e.message || String(e), true); return undefined; }
}
function toast(msg, isErr = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isErr ? 6500 : 2600);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { await call("copy_text", text); }
}
function askText(title, hint, okLabel) {
  return new Promise(resolve => {
    $("#modalTitle").textContent = title;
    $("#modalHint").textContent = hint;
    $("#modalOk").textContent = okLabel;
    $("#modalText").value = "";
    $("#modal").hidden = false;
    $("#modalText").focus();
    const done = v => { $("#modal").hidden = true; $("#modalOk").onclick = $("#modalCancel").onclick = null; resolve(v); };
    $("#modalOk").onclick = () => done($("#modalText").value);
    $("#modalCancel").onclick = () => done(null);
  });
}

// ── app state ──────────────────────────────────────────────────────────────
const S = { view: "home", st: null, rev: 0, logNext: 0, busy: false, steps: [], lib: "to_post" };
const R = { list: [], cid: null, d: null, start: 0, end: 0, mode: "source", saveTimer: null, playingSel: false, dirty: false };

// ── navigation and theme ───────────────────────────────────────────────────
function show(view) {
  S.view = view;
  $$(".view").forEach(v => { v.hidden = v.id !== "view-" + view; });
  $$(".rail-btn[data-view]").forEach(b => b.classList.toggle("on", b.dataset.view === view));
  if (view !== "review") $("#srcVideo").pause();
  refreshView();
}
async function refreshView() {
  if (S.view === "home") await loadHome();
  else if (S.view === "review") await loadReview();
  else if (S.view === "library") await loadLibrary();
  else if (S.view === "settings") await loadSettings();
}
function effectiveTheme() {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#themeBtn .ico").dataset.ico = effectiveTheme() === "dark" ? "sun" : "moon";
  paintIcons($("#themeBtn"));
  drawTimeline();
}

// ── status bar, stepper, log ───────────────────────────────────────────────
function renderSteps() {
  const names = ["screen", "download", "transcribe", "analyze", "render"];
  const labels = { screen: "Find stories", download: "Download", transcribe: "Transcribe", analyze: "Pick clips", render: "Render" };
  const by = Object.fromEntries(S.steps.map(s => [s.key, s]));
  $("#stepper").innerHTML = names.map(k => {
    const s = by[k];
    const state = s ? s.state : "pending";
    const pct = k === "transcribe" && state === "active" && S.progress > 0.01 ? ` <span class="pct">${Math.round(S.progress * 100)}%</span>` : "";
    return `<li class="${state}"><span class="node"></span>${labels[k]}${pct}</li>`;
  }).join("");
  const active = S.steps.find(s => s.state === "active");
  $("#sbSteps").textContent = S.busy && active ? active.label + (active.key === "transcribe" && S.progress > 0.01 ? ` ${Math.round(S.progress * 100)}%` : "") : "";
}
async function poll() {
  let p;
  try { p = await call("poll", S.logNext); } catch { return; }
  const wasBusy = S.busy;
  S.busy = p.busy; S.steps = p.steps; S.progress = p.progress;
  if (p.log.length) {
    const box = $("#logBox");
    const stick = $("#drawer").scrollTop + $("#drawer").clientHeight >= $("#drawer").scrollHeight - 30;
    box.append(p.log.join("\n") + "\n");
    if (stick) $("#drawer").scrollTop = $("#drawer").scrollHeight;
    $("#sbLast").textContent = p.log[p.log.length - 1].trim();
    S.logNext = p.next;
  }
  $("#sbDot").className = "dot" + (p.busy ? " busy" : p.error ? " err" : "");
  $("#sbLabel").textContent = p.busy ? p.label : p.error ? "Stopped with an error" : "Idle";
  setGoButton(p.busy, p.label);
  $("#stopBtn").hidden = !p.busy;
  $$('[data-make], [data-act="make"]').forEach(b => { b.disabled = p.busy; });
  const upd = $("#updBtn");
  upd.hidden = !p.update || p.busy;
  if (p.update) upd.textContent = `Update to ${p.update}`;
  renderSteps();
  if (p.rev !== S.rev) {
    const first = S.rev === 0;
    S.rev = p.rev;
    if (!first) {
      if (wasBusy && !p.busy && p.error) toast(p.error, true);
      if (S.view === "review" && R.cid) { await loadReview(true); }
      else await refreshView();
    }
  }
}

// ── HOME ───────────────────────────────────────────────────────────────────
function setGoButton(busy, label) {
  const b = $("#goBtn");
  const want = busy ? "stop" : "go";
  if (b.dataset.state === want) return;
  b.dataset.state = want;
  b.classList.toggle("primary", !busy);
  b.classList.toggle("stop", busy);
  b.innerHTML = busy ? `<span class="ico" data-ico="stop"></span>Stop` : `<span class="ico" data-ico="play"></span>Make clips`;
  b.title = busy ? `Stop ${(label || "the current job").toLowerCase()}. Anything that finished is kept.` : "";
  paintIcons(b);
}
async function stopJob() {
  await attempt(() => call("stop"));
}
function views(n) {
  if (!n) return "";
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M views` : n >= 1e3 ? `${Math.round(n / 1e3)}K views` : `${n} views`;
}
async function loadHome() {
  const st = await attempt(() => call("get_state"));
  if (!st) return;
  S.st = st;
  const c = st.counts, cfg = st.cfg;
  const mode = cfg.analysis_mode !== "api" ? "Paste mode: queue a link, then copy the packet into your Claude Project."
    : cfg.pick_mode === "ask" ? "Finishes anything you've queued; otherwise finds interviews for you to choose from."
    : "Picks interviews and clips on its own.";
  const warn = !st.ffmpeg ? "ffmpeg is missing. Install it, then restart. " : (cfg.analysis_mode === "api" && !st.has_key ? "Add your API key in Settings to start. " : "");
  $("#heroHint").textContent = `${warn}${mode} Renders the best ${cfg.auto_clips} from the next interview. ${c.rendered} ready to post, ${c.scheduled} scheduled.`;
  const sel = $("#presetSel");
  if (sel.options.length !== st.presets.length) sel.innerHTML = st.presets.map(p => `<option>${esc(p)}</option>`).join("");
  setBadge("review", c.suggested);
  setBadge("library", c.rendered);

  $("#ivCount").textContent = st.interviews.length ? `${st.interviews.length} total` : "";
  $("#ivList").innerHTML = st.interviews.length ? st.interviews.map(i => {
    const chip = i.status === "error" ? "hot" : (i.status === "analyzed" || i.status === "done") ? "ok" : i.status === "needs_paste" ? "warn" : "";
    let actions = "";
    if (i.status === "needs_paste") actions = `<button class="btn sm" data-act="packet" data-id="${i.id}">Copy packet</button><button class="btn sm" data-act="paste" data-id="${i.id}">Paste reply</button>`;
    else if (i.status === "analyzed" || i.status === "done") actions = `<button class="btn sm" data-act="review" data-id="${i.id}">Review</button>`;
    else actions = `<button class="btn sm" data-act="make" data-id="${i.id}" title="Download, transcribe, pick clips and render the best ones">${i.status === "error" ? "Try again" : "Make clips"}</button>`;
    actions += `<button class="btn sm" data-act="folder" data-id="${i.id}" title="Open this interview's folder">Folder</button><button class="btn sm danger" data-act="remove" data-id="${i.id}" title="Delete this interview and its downloaded files">Delete</button>`;
    const meta = [`<span class="chip ${chip}">${esc(i.status_label)}</span>`];
    if (i.clips) meta.push(`<span>${i.clips} clip${i.clips === 1 ? "" : "s"}</span>`);
    if (i.duration) meta.push(`<span>${fmt(i.duration)}</span>`);
    if (i.topic) meta.push(`<span>For: ${esc(i.topic)}</span>`);
    if (i.status === "error" && i.error) meta.push(`<span class="iv-err" title="${esc(i.error)}">${esc(i.error)}</span>`);
    return `<div class="iv"><div class="iv-title" title="${esc(i.title)}">${esc(i.title)}</div><div class="iv-meta">${meta.join("")}</div><div class="iv-actions">${actions}</div></div>`;
  }).join("") : `<div class="empty">Nothing here yet. Pick one under Found interviews, or paste a link above.</div>`;

  $("#foundList").innerHTML = st.picks.length ? st.picks.map(p => {
    const sc = p.screen;
    const count = sc ? `${sc.story_count} ${sc.story_count === 1 ? "story" : "stories"}, about ${Math.max(1, Math.round(sc.story_minutes))} min` : "";
    const meta = [p.channel, views(p.views), p.topic ? `For: ${p.topic}` : ""].filter(Boolean).join(", ");
    const stories = sc && sc.top && sc.top.length
      ? `<p class="found-stories" title="${esc(sc.stories.filter(s => s.strength >= 3).map(s => s.title).join("\n"))}"><b>${esc(count)}:</b> ${esc(sc.top.join("; "))}</p>`
      : `<p class="found-why">${esc(p.why)}${sc === null ? " (Captions couldn't be read, so its stories aren't counted.)" : ""}</p>`;
    return `<div class="found">
      <button class="found-thumb" data-watch="${esc(p.url)}" title="Watch on YouTube">${p.thumb ? `<img src="${esc(p.thumb)}" alt="" loading="lazy" onerror="this.remove()">` : ""}<span class="len">${fmt(p.duration || 0)}</span></button>
      <div class="found-body"><div class="found-title" title="${esc(p.title)}">${esc(p.title)}</div><div class="found-meta">${esc(meta)}</div>${stories}</div>
      <div class="found-actions"><button class="btn sm primary" data-make="${esc(p.url)}">Make clips</button><div class="row"><button class="btn sm" data-pick="${esc(p.url)}" title="Add to Your interviews for later">Queue</button><button class="btn sm" data-dismiss="${esc(p.url)}" title="Don't suggest this again">Not this</button></div></div>
    </div>`;
  }).join("") : `<div class="empty">${cfg.analysis_mode === "api" ? "Press Make clips or Find more and Shoot Clipper searches YouTube for interviews full of stories, tied to this week's news. You pick which one becomes clips." : "Finding interviews needs API mode (Settings). In Paste mode, paste a link above."}</div>`;

  const n = st.news;
  let html = "";
  if (n && n.topics && n.topics.length) {
    html += `<div class="sub-h">Checked ${esc((n.fetched_at || "").replace("T", " ").slice(0, 16))}</div>`;
    html += [...n.topics].sort((a, b) => b.urgency - a.urgency).map(t =>
      `<div class="topic"><div class="topic-head"><span class="heat" title="Urgency ${t.urgency} of 5">${[1, 2, 3, 4, 5].map(k => `<i class="${k <= t.urgency ? "on" : ""}"></i>`).join("")}</span><b>${esc(t.topic)}</b></div><p>${esc(t.why_now)}</p></div>`).join("");
  } else {
    html += `<div class="empty">No news yet. In API mode, Make clips checks what fans are talking about before it picks an interview. Refresh runs that check now.</div>`;
  }
  $("#newsList").innerHTML = html;
  $$('[data-make], [data-act="make"]').forEach(b => { b.disabled = S.busy; });
}
function setBadge(view, n) {
  const b = $(`.rail-btn[data-view="${view}"]`);
  let el = $(".badge", b);
  if (!n) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("span"); el.className = "badge"; b.append(el); }
  el.textContent = n;
}
function wireHome() {
  $("#goBtn").onclick = () => S.busy ? stopJob() : attempt(() => call("make_clips"));
  $("#stopBtn").onclick = stopJob;
  const add = async () => {
    const v = $("#urlInput").value;
    if (await attempt(() => call("add_url", v, $("#presetSel").value), "Queued. Make clips will use it next.")) $("#urlInput").value = "";
  };
  $("#addUrlBtn").onclick = add;
  $("#urlInput").onkeydown = e => { if (e.key === "Enter") add(); };
  $("#addFilesBtn").onclick = () => attempt(() => call("add_files", $("#presetSel").value));
  $("#scoutBtn").onclick = () => attempt(() => call("scout_news"));
  $("#discoverBtn").onclick = () => attempt(() => call("discover"));
  $("#foundList").onclick = e => {
    const b = e.target.closest("[data-make],[data-pick],[data-dismiss],[data-watch]");
    if (!b) return;
    if (b.dataset.make) attempt(() => call("make_from_pick", b.dataset.make));
    else if (b.dataset.pick) attempt(() => call("queue_pick", b.dataset.pick), "Queued under Your interviews");
    else if (b.dataset.dismiss) { b.closest(".found").remove(); attempt(() => call("dismiss_pick", b.dataset.dismiss)); }
    else if (b.dataset.watch) attempt(() => call("open_url", b.dataset.watch));
  };
  $("#ivList").onclick = async e => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const id = +b.dataset.id, act = b.dataset.act;
    if (act === "make") attempt(() => call("make_interview", id));
    else if (act === "folder") attempt(() => call("open_folder", "interview", id));
    else if (act === "review") { R.cid = null; R.wantIv = id; show("review"); }
    else if (act === "remove") { if (confirm("Delete this interview and its downloaded video and transcript? Finished clips stay in Library.")) attempt(() => call("remove_interview", id)); }
    else if (act === "packet") {
      const p = await attempt(() => call("packet", id));
      if (p) { await copy(p.text); toast("Packet copied. Paste it into your Claude Project."); }
    } else if (act === "paste") {
      const text = await askText("Paste Claude's reply", "Paste the whole reply. The JSON block is pulled out automatically.", "Load clips");
      if (text) { if (await attempt(() => call("paste_reply", id, text), "Clips loaded")) { R.wantIv = id; show("review"); } }
    }
  };
}

// ── REVIEW ─────────────────────────────────────────────────────────────────
async function loadReview(keep = false) {
  const list = await attempt(() => call("review_list"));
  if (!list) return;
  R.list = list;
  const all = list.flatMap(i => i.clips);
  if (R.cid && !all.some(c => c.id === R.cid)) R.cid = null;
  if (!R.cid && all.length) {
    const iv = list.find(i => i.id === R.wantIv) || list[0];
    const first = iv.clips.find(c => c.status === "suggested") || iv.clips[0];
    R.wantIv = null;
    renderClipList();
    if (first) return selectClip(first.id);
  }
  renderClipList();
  if (R.cid && keep) {
    // A render may have finished: refresh the output without disturbing edits in progress.
    const d = await attempt(() => call("clip_detail", R.cid));
    if (d) {
      const had = R.d && R.d.output_url;
      R.d.output_url = d.output_url; R.d.clip.status = d.clip.status; R.d.gameplay_name = d.gameplay_name;
      if (d.source_url !== R.d.source_url) { R.d.source_url = d.source_url; loadSource(); }
      renderFinished();
      if (d.output_url && d.output_url !== had) setMode("finished");
    }
  }
  if (!all.length) showEditor(false);
}
function renderClipList() {
  $("#clipList").innerHTML = R.list.length ? R.list.map(i =>
    `<div class="iv-group" title="${esc(i.title)}">${esc(i.title)}</div>` + i.clips.map(c => {
      const chip = { approved: "ok", rendered: "ok", scheduled: "ok", posted: "", rejected: "" }[c.status];
      const st = c.status === "suggested" ? "" : `<span class="chip ${chip || ""}">${c.status}</span>`;
      return `<button class="clip ${c.id === R.cid ? "on" : ""} ${c.status}" data-cid="${c.id}"><div class="clip-title">${esc(c.title)}</div><div class="clip-meta">${stars(c.rating)}<span>${fmt(c.end - c.start)}</span>${c.timely ? '<span class="chip hot">timely</span>' : ""}${st}</div></button>`;
    }).join("")).join("") : `<div class="empty">Nothing to review yet. Clips appear here after an interview has been analyzed.</div>`;
}
function showEditor(on) {
  $("#inspBody").hidden = !on; $("#inspFoot").hidden = !on; $("#inspEmpty").hidden = on;
  $("#hIn").hidden = !on; $("#hOut").hidden = !on;
  if (!on) { $("#stageTitle").textContent = "Pick a clip"; $("#transcript").innerHTML = ""; }
}
async function selectClip(cid) {
  await flushSave();
  const seq = R.seq = (R.seq || 0) + 1;
  const d = await attempt(() => call("clip_detail", cid));
  if (!d || seq !== R.seq) return;   // a newer click won
  R.cid = cid; R.d = d; R.start = d.clip.start; R.end = d.clip.end;
  const c = d.clip;
  showEditor(true);
  renderClipList();
  $("#stageTitle").textContent = c.title;
  $("#iStars").innerHTML = stars(c.rating);
  $("#iWhy").textContent = c.why + (c.series ? ` Series: ${c.series}.` : "");
  $("#iFlags").hidden = !d.flags.length;
  $("#iFlags").textContent = d.flags.length ? "Handle with care: " + d.flags.join("; ") : "";
  $("#iSpeaker").value = c.speaker || "";
  $("#iCaption").value = c.caption || "";
  $("#iTags").value = c.hashtags || "";
  $("#iHook").value = c.hook || "";
  $("#iCard").value = c.context_card || "";
  setFraming(c.framing || "face", false);
  $("#saveNote").textContent = "";
  $("#transcript").innerHTML = d.words.map((w, i) => `<span data-i="${i}">${esc(w.w)}</span> `).join("") || `<span class="hint">No transcript for this stretch.</span>`;
  const partial = d.gaps && d.gaps.length > 0;
  $("#moreTranscript").hidden = !partial;
  $("#transcriptNote").textContent = partial ? "Only the story sections are transcribed." : "Click a word to start there. Shift-click to end there.";
  $("#transcript").scrollTop = 0;
  loadSource();
  renderFinished();
  setMode("source");
  syncTimes(true);
}
function loadSource() {
  const v = $("#srcVideo"), d = R.d;
  $("#playerMsg").hidden = true;
  if (!d.source_url) { playerMessage("The source file for this interview is missing.", false); v.removeAttribute("src"); return; }
  if (v.dataset.url !== d.source_url) { v.dataset.url = d.source_url; v.src = d.source_url; }
  const seek = () => { v.currentTime = R.start; };
  if (v.readyState >= 1) seek(); else v.addEventListener("loadedmetadata", seek, { once: true });
}
function playerMessage(text, offerProxy) {
  const m = $("#playerMsg");
  m.hidden = false;
  m.innerHTML = `<span>${esc(text)}</span>` + (offerProxy ? `<button class="btn sm" id="proxyBtn">Make a preview copy</button>` : "");
  if (offerProxy) $("#proxyBtn").onclick = () => attempt(() => call("make_proxy", R.d.interview.id), "Making a preview copy. The player reloads when it's ready.");
}
function renderFinished() {
  const d = R.d; if (!d) return;
  const v = $("#outVideo");
  $$("#modeSeg button")[1].disabled = !d.output_url;
  if (d.output_url) {
    if (v.dataset.url !== d.output_url) { v.dataset.url = d.output_url; v.src = d.output_url; }
    $("#finishedNote").textContent = `Rendered. This is the file in Ready to post.${d.gameplay_name ? ` 2K capture: ${d.gameplay_name} at ${d.speed}x.` : ""}`;
  } else { v.removeAttribute("src"); delete v.dataset.url; }
}
function setMode(mode) {
  if (mode === "finished" && !(R.d && R.d.output_url)) mode = "source";
  R.mode = mode;
  $$("#modeSeg button").forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
  $("#sourcePane").hidden = mode !== "source";
  $("#finishedPane").hidden = mode !== "finished";
  if (mode === "source") $("#outVideo").pause(); else $("#srcVideo").pause();
}
function setFraming(f, save = true) {
  $$("#framingSeg button").forEach(b => b.classList.toggle("on", b.dataset.f === f));
  R.framing = f;
  $("#gpRow").hidden = f !== "split";
  $("#gpName").textContent = R.d && R.d.gameplay_name ? R.d.gameplay_name : (R.d && R.d.clip.gameplay_hint ? `Auto: ${R.d.clip.gameplay_hint}` : "Matched by name at render");
  if (save) queueSave();
}

/** Push R.start / R.end into the inputs, transcript and timeline. */
function syncTimes(resetInputs = false) {
  if (!R.d) return;
  if (resetInputs || document.activeElement !== $("#iStart")) $("#iStart").value = fmt(R.start, 1);
  if (resetInputs || document.activeElement !== $("#iEnd")) $("#iEnd").value = fmt(R.end, 1);
  $("#tcLen").textContent = `${(R.end - R.start).toFixed(1)}s selected`;
  const words = R.d.words;
  let firstIn = -1, lastIn = -1;
  $$("#transcript span[data-i]").forEach((el, i) => {
    const w = words[i], inside = w.s >= R.start - 0.06 && w.e <= R.end + 0.06;
    el.classList.toggle("in", inside);
    el.classList.remove("edge-in", "edge-out");
    if (inside) { if (firstIn < 0) firstIn = i; lastIn = i; }
  });
  const spans = $$("#transcript span[data-i]");
  if (firstIn >= 0) { spans[firstIn].classList.add("edge-in"); spans[lastIn].classList.add("edge-out"); }
  if (resetInputs && firstIn >= 0) spans[firstIn].scrollIntoView({ block: "center" });
  drawTimeline();
}
function setRange(start, end, save = true) {
  const [w0, w1] = R.d.window;
  start = clamp(start, w0, w1 - 1); end = clamp(end, start + 1, w1);
  R.start = Math.round(start * 100) / 100; R.end = Math.round(end * 100) / 100;
  syncTimes();
  if (save) queueSave();
}
function queueSave() {
  R.dirty = true;
  $("#saveNote").textContent = "Saving";
  clearTimeout(R.saveTimer);
  R.saveTimer = setTimeout(flushSave, 500);
}
async function flushSave() {
  clearTimeout(R.saveTimer);
  if (!R.dirty || !R.cid) return;
  R.dirty = false;
  const fields = {
    start: R.start, end: R.end, framing: R.framing, speaker: $("#iSpeaker").value.trim(),
    caption: $("#iCaption").value.trim(), hashtags: $("#iTags").value.trim(),
    hook: $("#iHook").value.trim(), context_card: $("#iCard").value.trim(),
  };
  const ok = await attempt(() => call("update_clip", R.cid, fields));
  $("#saveNote").textContent = ok ? "Saved" : "Not saved";
  const item = R.list.flatMap(i => i.clips).find(c => c.id === R.cid);
  if (item) { item.start = R.start; item.end = R.end; item.framing = R.framing; }
}

// timeline
const TL = { drag: null };
function tx(t) { const [w0, w1] = R.d.window; return (t - w0) / (w1 - w0) * $("#tl").clientWidth; }
function tAt(x) { const [w0, w1] = R.d.window; return w0 + clamp(x / $("#tl").clientWidth, 0, 1) * (w1 - w0); }
function drawTimeline() {
  const c = $("#tl");
  if (!R.d || !c.clientWidth) return;
  const css = getComputedStyle(document.documentElement);
  const col = n => css.getPropertyValue(n).trim();
  const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
  if (c.width !== Math.round(W * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const [w0, w1] = R.d.window, span = w1 - w0;
  const x0 = tx(R.start), x1 = tx(R.end), rulerH = 18, mid = rulerH + (H - rulerH) / 2, amp = (H - rulerH) / 2 - 5;
  g.fillStyle = col("--accent-wash"); g.fillRect(x0, 0, x1 - x0, H);
  // ruler
  const stepOpts = [1, 2, 5, 10, 15, 30, 60, 120];
  const step = stepOpts.find(s => span / s <= 10) || 300;
  g.font = "10.5px Inter, Segoe UI, sans-serif"; g.textBaseline = "top";
  for (let t = Math.ceil(w0 / step) * step; t < w1; t += step) {
    const x = Math.round(tx(t)) + 0.5;
    g.strokeStyle = col("--line"); g.beginPath(); g.moveTo(x, 0); g.lineTo(x, 6); g.stroke();
    g.fillStyle = col("--faint"); g.fillText(fmt(t), x + 4, 3);
  }
  g.strokeStyle = col("--line"); g.beginPath(); g.moveTo(0, rulerH + 0.5); g.lineTo(W, rulerH + 0.5); g.stroke();
  // waveform
  const pk = R.d.peaks, per = pk.length / W, bar = 2, gap = 1;
  for (let x = 0; x < W; x += bar + gap) {
    let m = 0;
    const a = Math.floor(x * per), b = Math.max(a + 1, Math.floor((x + bar + gap) * per));
    for (let i = a; i < b && i < pk.length; i++) if (pk[i] > m) m = pk[i];
    const h = Math.max(1, m / 100 * amp);
    g.fillStyle = x >= x0 && x <= x1 ? col("--accent") : col("--wave");
    g.fillRect(x, mid - h, bar, h * 2);
  }
  // playhead
  const v = $("#srcVideo");
  if (v.currentTime >= w0 && v.currentTime <= w1) {
    const px = Math.round(tx(v.currentTime)) + 0.5;
    g.strokeStyle = col("--text"); g.lineWidth = 1.5; g.beginPath(); g.moveTo(px, 0); g.lineTo(px, H); g.stroke(); g.lineWidth = 1;
  }
  $("#hIn").style.left = x0 + "px"; $("#hOut").style.left = x1 + "px";
}
function wireTimeline() {
  const wrap = $("#tlWrap");
  const startDrag = (which, e) => { TL.drag = which; e.target.setPointerCapture?.(e.pointerId); e.preventDefault(); e.stopPropagation(); };
  $("#hIn").onpointerdown = e => startDrag("in", e);
  $("#hOut").onpointerdown = e => startDrag("out", e);
  wrap.onpointerdown = e => { if (!R.d || e.target.closest(".handle")) return; TL.drag = "seek"; wrap.setPointerCapture(e.pointerId); seekTo(tAt(e.clientX - wrap.getBoundingClientRect().left)); };
  wrap.onpointermove = e => {
    if (!TL.drag || !R.d) return;
    const t = tAt(e.clientX - wrap.getBoundingClientRect().left);
    if (TL.drag === "in") { setRange(Math.min(t, R.end - 1), R.end, false); seekTo(R.start, false); }
    else if (TL.drag === "out") { setRange(R.start, Math.max(t, R.start + 1), false); seekTo(R.end, false); }
    else seekTo(t);
  };
  const end = () => { if (TL.drag === "in" || TL.drag === "out") queueSave(); TL.drag = null; };
  wrap.onpointerup = end; wrap.onpointercancel = end;
  new ResizeObserver(drawTimeline).observe(wrap);
}
function seekTo(t, redraw = true) {
  const v = $("#srcVideo");
  if (v.readyState >= 1) v.currentTime = clamp(t, 0, v.duration || t);
  if (redraw) { $("#tcNow").textContent = fmt(t, 1); drawTimeline(); }
}
function togglePlay(fromStart = false) {
  if (R.mode !== "source") { const o = $("#outVideo"); o.paused ? o.play() : o.pause(); return; }
  const v = $("#srcVideo");
  if (!R.d || !v.src) return;
  if (!v.paused) { v.pause(); return; }
  if (fromStart || v.currentTime < R.start - 0.05 || v.currentTime >= R.end - 0.05) v.currentTime = R.start;
  R.playingSel = true;
  v.play().catch(() => {});
}
let lastWord = -1;
function tick() {
  const v = $("#srcVideo");
  if (R.d && S.view === "review" && R.mode === "source") {
    const t = v.currentTime;
    if (!v.paused) {
      if (R.playingSel && t >= R.end) { v.pause(); v.currentTime = R.end; }
      $("#tcNow").textContent = fmt(t, 1);
      drawTimeline();
    }
    const words = R.d.words;
    let idx = -1;
    if (!v.paused || TL.drag === "seek") {
      let lo = 0, hi = words.length - 1;
      while (lo <= hi) { const m = (lo + hi) >> 1; if (words[m].e < t) lo = m + 1; else if (words[m].s > t) hi = m - 1; else { idx = m; break; } }
    }
    if (idx !== lastWord) {
      const spans = $("#transcript").children;
      const old = $("#transcript span.now"); if (old) old.classList.remove("now");
      if (idx >= 0 && spans[idx]) {
        spans[idx].classList.add("now");
        const box = $("#transcript"), el = spans[idx];
        if (el.offsetTop < box.scrollTop + 20 || el.offsetTop > box.scrollTop + box.clientHeight - 50) box.scrollTop = el.offsetTop - box.clientHeight / 2;
      }
      lastWord = idx;
    }
  }
  requestAnimationFrame(tick);
}
function wireReview() {
  $("#clipList").onclick = e => { const b = e.target.closest("[data-cid]"); if (b) selectClip(+b.dataset.cid); };
  $("#modeSeg").onclick = e => { const b = e.target.closest("button"); if (b && !b.disabled) setMode(b.dataset.mode); };
  $("#framingSeg").onclick = e => { const b = e.target.closest("button"); if (b) setFraming(b.dataset.f); };
  ["iSpeaker", "iCaption", "iTags", "iHook", "iCard"].forEach(id => { $("#" + id).oninput = queueSave; });
  const commitTime = which => {
    const t = parseTime($(which === "start" ? "#iStart" : "#iEnd").value);
    if (t === null) return syncTimes(true);
    which === "start" ? setRange(t, Math.max(R.end, t + 1)) : setRange(Math.min(R.start, t - 1), t);
    syncTimes(true);
  };
  $("#iStart").onchange = () => commitTime("start");
  $("#iEnd").onchange = () => commitTime("end");
  $$(".nudge button").forEach(b => b.onclick = e => {
    const [which, d] = b.dataset.n.split(",");
    const step = (e.shiftKey ? 0.1 : 0.5) * Math.sign(+d);
    which === "start" ? setRange(R.start + step, R.end) : setRange(R.start, R.end + step);
    syncTimes(true);
    seekTo(which === "start" ? R.start : R.end);
  });
  $("#snapBtn").onclick = async () => {
    const r = await attempt(() => call("snap", R.cid, R.start, R.end));
    if (r) { setRange(r[0], r[1]); syncTimes(true); }
  };
  $("#playSelBtn").onclick = () => { setMode("source"); $("#srcVideo").pause(); togglePlay(true); };
  $("#playBtn").onclick = () => togglePlay();
  $("#moreTranscript").onclick = () => R.cid && attempt(() => call("extend_transcript", R.cid));
  $("#transcript").onclick = e => {
    const el = e.target.closest("span[data-i]"); if (!el || !R.d) return;
    const w = R.d.words[+el.dataset.i];
    if (e.shiftKey) setRange(Math.min(R.start, w.e - 1), w.e + 0.2); else setRange(Math.max(0, w.s - 0.1), Math.max(R.end, w.s + 1));
    syncTimes(true);
    seekTo(e.shiftKey ? R.end : R.start);
  };
  const v = $("#srcVideo");
  v.onplay = v.onpause = () => { $("#playBtn .ico").dataset.ico = v.paused ? "play" : "pause"; paintIcons($("#playBtn")); if (v.paused) R.playingSel = false; };
  v.onerror = () => { if (R.d && R.d.source_url && !R.d.is_proxy) playerMessage("This file's format won't play in the built-in player. Rendering still works.", true); };
  v.onseeked = () => { $("#tcNow").textContent = fmt(v.currentTime, 1); drawTimeline(); };
  $("#gpAuto").onclick = async () => { await flushSave(); const n = await attempt(() => call("auto_match", R.cid)); if (n) { R.d.gameplay_name = n; setFraming("split", false); R.framing = "split"; toast("Matched " + n); } };
  $("#gpChoose").onclick = async () => { await flushSave(); const n = await attempt(() => call("choose_gameplay", R.cid)); if (n) { R.d.gameplay_name = n; setFraming("split", false); } };
  $("#renderBtn").onclick = async () => { await flushSave(); if (await attempt(() => call("render_clip", R.cid))) toast("Rendering. The finished clip opens here when it's done."); };
  $("#approveBtn").onclick = async () => { await flushSave(); await attempt(() => call("set_clip_status", R.cid, "approved"), "Approved"); };
  $("#rejectBtn").onclick = async () => { await flushSave(); await attempt(() => call("set_clip_status", R.cid, "rejected")); };
  $("#renderApprovedBtn").onclick = () => attempt(() => call("render_approved"));
  $("#openOutBtn").onclick = () => attempt(() => call("open_clip_file", R.cid));
  $("#copyCapBtn2").onclick = async () => { const t = await attempt(() => call("post_text", R.cid)); if (t) { await copy(t); toast("Caption copied"); } };
  wireTimeline();
  document.addEventListener("keydown", e => {
    if (S.view !== "review" || !R.d || !$("#modal").hidden) return;
    if (e.target.matches("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
    const v = $("#srcVideo"), k = e.key.toLowerCase();
    if (k === " ") { e.preventDefault(); togglePlay(); }
    else if (k === "i") { setRange(Math.min(v.currentTime, R.end - 1), R.end); syncTimes(true); }
    else if (k === "o") { setRange(R.start, Math.max(v.currentTime, R.start + 1)); syncTimes(true); }
    else if (k === "j" || k === "l") { v.pause(); seekTo(v.currentTime + (k === "j" ? -1 : 1) * (e.shiftKey ? 0.1 : 0.5)); }
  });
}

// ── LIBRARY ────────────────────────────────────────────────────────────────
async function loadLibrary() {
  const L = await attempt(() => call("library", S.lib));
  if (!L) return;
  const ab = L.ab;
  let stats = `<span><b>${L.banked}</b> banked</span><span><b>${L.runway_weeks}</b> weeks of runway at ${L.per_week} a week</span>`;
  if (ab.split.n && ab.plain.n) stats += `<span>Average views: <b>${ab.split.avg.toLocaleString()}</b> with 2K (${ab.split.n}), <b>${ab.plain.avg.toLocaleString()}</b> without (${ab.plain.n})</span>`;
  else stats += `<span>Log views on posted clips to compare 2K against face-only</span>`;
  $("#libStats").innerHTML = stats;
  const empty = { to_post: "Nothing waiting. Rendered clips land here, ready to upload.", scheduled: "Nothing scheduled. Use Schedule on a rendered clip to give it the next posting slot.", posted: "Nothing posted yet. Mark clips as posted to keep a history and log their views.", all: "No rendered clips yet." }[S.lib];
  $("#libGrid").innerHTML = L.items.length ? L.items.map(c => {
    const sub = c.status === "scheduled" ? `Slot: ${esc(c.scheduled_for)}` : c.status === "posted" ? `Posted ${esc((c.posted_at || "").slice(0, 10))}` : esc(c.interview);
    let actions = `<button class="btn sm" data-a="copy" data-id="${c.id}">Copy caption</button>`;
    if (c.status === "rendered") actions += `<button class="btn sm" data-a="schedule" data-id="${c.id}">Schedule</button>`;
    if (c.status !== "posted") actions += `<button class="btn sm" data-a="posted" data-id="${c.id}">Mark posted</button>`;
    const views = c.status === "posted" ? `<div class="views-in"><input data-views="${c.id}" inputmode="numeric" placeholder="Views" value="${c.views || ""}"><span>views</span><button class="btn sm" data-a="unpost" data-id="${c.id}" title="Move back to To post">Undo</button></div>` : "";
    return `<article class="card"><div class="thumb" data-video="${esc(c.video)}" data-id="${c.id}" data-iv="${c.interview_id}" style="${c.thumb ? `background-image:url('${c.thumb}')` : ""}" title="Hover to play. Click to open in Review.">${c.missing ? '<div class="gone">The video file was moved or deleted</div>' : ""}<span class="lay">${c.framing === "split" ? "2K" : c.framing === "wide" ? "Wide" : "Face"}</span><span class="len">${fmt(c.length)}</span></div><div class="card-body"><div class="card-title" title="${esc(c.title)}">${esc(c.title)}</div><div class="card-sub">${sub}</div><div class="card-actions">${actions}</div>${views}</div></article>`;
  }).join("") : `<div class="empty">${empty}</div>`;
}
function wireLibrary() {
  $("#libSeg").onclick = e => { const b = e.target.closest("button"); if (!b) return; S.lib = b.dataset.w; $$("#libSeg button").forEach(x => x.classList.toggle("on", x === b)); loadLibrary(); };
  $("#openReadyBtn").onclick = () => attempt(() => call("open_folder", "ready"));
  const grid = $("#libGrid");
  grid.addEventListener("mouseover", e => {
    const t = e.target.closest(".thumb");
    if (!t || !t.dataset.video || $("video", t)) return;
    const v = document.createElement("video");
    v.src = t.dataset.video; v.muted = true; v.loop = true; v.playsInline = true; v.autoplay = true;
    t.prepend(v);
  });
  grid.addEventListener("mouseout", e => {
    const t = e.target.closest(".thumb");
    if (t && !t.contains(e.relatedTarget)) { const v = $("video", t); if (v) { v.pause(); v.remove(); } }
  });
  grid.onclick = async e => {
    const t = e.target.closest(".thumb");
    if (t) { R.cid = +t.dataset.id; R.d = null; show("review"); await selectClip(+t.dataset.id); if (R.d && R.d.output_url) setMode("finished"); return; }
    const b = e.target.closest("[data-a]"); if (!b) return;
    const id = +b.dataset.id, a = b.dataset.a;
    if (a === "copy") { const txt = await attempt(() => call("post_text", id)); if (txt) { await copy(txt); toast("Caption and hashtags copied"); } }
    else if (a === "schedule") { const slot = await attempt(() => call("schedule", id)); if (slot) { toast("Slotted for " + slot); loadLibrary(); } }
    else if (a === "posted") { await attempt(() => call("mark_posted", id), "Marked as posted"); loadLibrary(); }
    else if (a === "unpost") { await attempt(() => call("unpost", id)); loadLibrary(); }
  };
  grid.addEventListener("change", e => { const i = e.target.closest("[data-views]"); if (i) attempt(() => call("set_views", +i.dataset.views, i.value), "Views saved"); });
}

// ── SETTINGS ───────────────────────────────────────────────────────────────
const SETTINGS = [
  ["Claude", [
    { custom: "key" },
    { key: "analysis_mode", label: "Analysis", type: "select", options: [["api", "API (automatic)"], ["paste", "Paste into my Project (free)"]], hint: "API costs a few dollars a month at most. Paste mode is free but you queue links yourself." },
    { key: "model", label: "Model", type: "select", options: "models", hint: "Sonnet is the best value. Opus picks slightly better clips at about twice the cost." },
    { key: "news_refresh_days", label: "Check the news every", type: "number", unit: "days" },
    { custom: "paste_news" },
  ]],
  ["Make clips", [
    { key: "pick_mode", label: "Choosing interviews", type: "select", options: [["ask", "Show me what it found, I pick"], ["auto", "Pick the best one automatically"]], hint: "Queued interviews always go first. The daily run always picks automatically." },
    { key: "auto_clips", label: "Clips per interview", type: "number", hint: "How many Make clips renders from each interview." },
    { key: "auto_min_rating", label: "Lowest rating to render", type: "number", unit: "of 5", hint: "If nothing rates this high, the single best clip is rendered." },
    { key: "open_folder_when_done", label: "Open the folder when done", type: "bool" },
    { custom: "autopilot" },
  ]],
  ["2K footage", [
    { key: "gameplay_dir", label: "2K captures folder", type: "folder", hint: "Name captures after the matchup, like “Ventura vs Hogan.mp4”, or keep a folder per wrestler." },
    { key: "gameplay_speed", label: "2K speed", type: "number", unit: "x", step: "0.05", hint: "Every capture plays at this speed. The match is timed so the finish lands as the clip ends." },
    { key: "framing_mode", label: "Layout for Make clips", type: "select", options: [["split when matched", "2K on top when a capture matches"], ["alternate", "Alternate, to compare views"], ["face only", "Face only"]] },
    { key: "find_match_end", label: "Find the finish", type: "bool", hint: "Claude looks at the end of each capture once and finds the winner screen, so captures don't need trimming." },
    { key: "speaker_tracking", label: "Follow the speaker", type: "bool", hint: "On shots with two or more people, the interview crop cuts to whoever is talking." },
    { key: "interview_zoom", label: "Interview zoom", type: "number", step: "0.05", unit: "x", hint: "How tight the interview panel is under the 2K. 1.35 matches your edits." },
  ]],
  ["Captions and look", [
    { key: "caption_style", label: "Caption style", type: "select", options: [["seam", "Three words, current one yellow"], ["single", "One big word at a time"]] },
    { key: "caption_size", label: "Caption size", type: "number", hint: "116 matches the caption size in your CapCut exports." },
    { key: "font", label: "Caption font", type: "text", hint: "Poppins Black ships with the app. Any installed font works, such as Komika Axis." },
    { key: "font_italic", label: "Italic captions", type: "bool" },
    { key: "name_plate", label: "Name plate", type: "bool", hint: "Speaker and source slide in for the first few seconds." },
    { key: "hook_text", label: "Hook text", type: "bool", hint: "A headline over the top panel." },
    { key: "context_cards", label: "Context card", type: "bool", hint: "A short setup card before the clip starts." },
    { key: "mask_swears", label: "Mask swears in captions", type: "bool", hint: "Shown as F*CK. Audio is untouched." },
    { key: "handle", label: "Channel handle", type: "text", hint: "Printed in the bar that covers source watermarks in face-only clips." },
  ]],
  ["Transcription", [
    { key: "transcribe_mode", label: "Transcribe", type: "select", options: [["stories", "Only the story sections (much faster)"], ["full", "The whole interview"]], hint: "Story sections come from a quick read of YouTube's captions before the download. Files you add yourself, and videos without captions, are always transcribed in full." },
    { key: "whisper_model", label: "Whisper model", type: "select", options: [["auto", "Automatic"], ["small", "small"], ["medium", "medium"], ["large-v3-turbo", "large-v3-turbo"], ["base", "base"]], hint: "Automatic uses large-v3-turbo with an NVIDIA GPU, and small on CPU." },
    { key: "device", label: "Run on", type: "select", options: [["auto", "Automatic"], ["cuda", "NVIDIA GPU"], ["cpu", "CPU"]] },
    { custom: "wordlists" },
  ]],
  ["Posting and files", [
    { key: "post_days", label: "Posting days", type: "days" },
    { key: "post_time", label: "Posting time", type: "text", hint: "24-hour, like 19:30. Used for the slots in Library." },
    { key: "workspace", label: "Workspace folder", type: "folder", hint: "Downloads, transcripts and finished clips live here." },
    { custom: "files" },
  ]],
  ["Storage", [
    { custom: "storage" },
    { key: "cleanup_downloads_days", label: "Delete interview videos", type: "select", options: [["0", "Never"], ["3", "3 days after clips are made"], ["7", "A week after clips are made"], ["14", "2 weeks after clips are made"], ["30", "A month after clips are made"]], hint: "Checked when the app opens and after each Make clips. Transcripts stay, and a video downloads again by itself if you re-render one of its clips." },
    { key: "cleanup_clips_days", label: "Delete finished clips", type: "select", options: [["0", "Never"], ["14", "2 weeks after rendering"], ["30", "A month after rendering"], ["60", "2 months after rendering"], ["90", "3 months after rendering"]], hint: "Scheduled clips are never deleted. Posted clips keep their view counts in History." },
  ]],
  ["Updates", [
    { custom: "update" },
  ]],
  ["Appearance", [
    { key: "theme", label: "Theme", type: "select", options: [["system", "Match Windows"], ["light", "Light"], ["dark", "Dark"]] },
  ]],
];
function gb(b) { return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`; }
async function loadStorage() {
  const u = await attempt(() => call("storage"));
  const el = $("#storageUse");
  if (!u || !el) return;
  el.textContent = `Interview videos ${gb(u.downloads)} (${u.n_downloads}), finished clips ${gb(u.clips)} (${u.n_clips})`;
}
async function loadSettings() {
  const st = await attempt(() => call("get_state"));
  if (!st) return;
  S.st = st;
  const cfg = st.cfg;
  const row = f => {
    if (f.custom === "key") return `<div class="set-row"><span class="lbl">API key</span><span class="ctl"><input id="keyInput" type="password" placeholder="${st.has_key ? "A key is saved. Paste to replace it" : "sk-ant-…"}" autocomplete="off"><button class="btn" id="keySave">Save</button></span><span class="hint">${st.has_key ? "Saved in Windows Credential Manager." : "Create one at platform.claude.com and add a few dollars of credit."}</span></div>`;
    if (f.custom === "paste_news") return `<div class="set-row span"><span class="lbl">Paste mode news</span><span class="ctl"><button class="btn" id="newsCopy">Copy news request</button><button class="btn" id="newsPaste">Paste news reply</button></span></div>`;
    if (f.custom === "autopilot") return `<div class="set-row"><span class="lbl">Daily run</span><span class="ctl"><input id="autoTime" value="16:00" style="width:70px"><button class="btn" id="autoOn">Turn on</button><button class="btn" id="autoOff">Turn off</button></span><span class="hint">Runs Make clips at this time every day, so clips are waiting when you get home. The PC has to be on.</span></div>`;
    if (f.custom === "wordlists") return `<div class="set-row span"><span class="lbl">Name spelling</span><span class="ctl"><button class="btn" data-file="names">Edit names and nicknames</button><button class="btn" data-file="fixes">Edit auto-corrections</button></span></div>`;
    if (f.custom === "update") return st.installed
      ? `<div class="set-row"><span class="lbl">Version</span><span class="ctl"><span>${esc(st.version || "unknown")}</span><button class="btn" id="checkUpdate">Check for updates</button></span><span class="hint">Shoot Clipper checks when it opens and shows Update in the bottom bar when there's a new version. Settings, your library and your lists carry over.</span></div>`
      : `<div class="set-row"><span class="lbl">Version</span><span class="ctl"><span>${esc(st.version || "unknown")}</span><button class="btn" id="installUpdate">Install update…</button></span><span class="hint">Pick the ShootClipper.zip you downloaded. Settings, your library and your lists stay as they are; the app restarts when it's done.</span></div>`;
    if (f.custom === "storage") return `<div class="set-row span"><span class="lbl">Space used</span><span class="ctl"><span id="storageUse" class="nowrap">Measuring…</span><button class="btn" id="cleanNow" title="Apply the rules below now. Old files are also cleaned when the app opens and after each Make clips.">Clean up now</button><button class="btn" id="openWs">Open folder</button></span></div>`;
    if (f.custom === "files") return `<div class="set-row span"><span class="lbl">Lists and logs</span><span class="ctl"><button class="btn" data-file="sources">Source presets</button><button class="btn" data-file="channels">Preferred channels</button><button class="btn" data-file="blocked">Blocked channels</button><button class="btn" data-file="log">Open app log</button><button class="btn" id="openData">Open data folder</button></span></div>`;
    const v = cfg[f.key];
    let ctl = "";
    if (f.type === "bool") ctl = `<input type="checkbox" class="switch" data-key="${f.key}" ${v ? "checked" : ""}>`;
    else if (f.type === "select") {
      const opts = f.options === "models" ? st.models.map(m => [m, m]) : f.options;
      ctl = `<select data-key="${f.key}">${opts.map(([val, lab]) => `<option value="${esc(val)}" ${String(v) === val ? "selected" : ""}>${esc(lab)}</option>`).join("")}</select>`;
    } else if (f.type === "number") ctl = `<input type="number" step="${f.step || 1}" data-key="${f.key}" value="${esc(v)}" style="width:90px">${f.unit ? `<span class="hint">${f.unit}</span>` : ""}`;
    else if (f.type === "folder") ctl = `<input data-key="${f.key}" value="${esc(v)}" placeholder="Not set"><button class="btn" data-browse="${f.key}">Browse</button>`;
    else if (f.type === "days") ctl = `<span class="days" data-days>${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => `<button data-d="${i}" class="${(v || []).includes(i) ? "on" : ""}">${d[0]}</button>`).join("")}</span>`;
    else ctl = `<input data-key="${f.key}" value="${esc(v)}">`;
    return `<div class="set-row"><span class="lbl">${f.label}</span><span class="ctl">${ctl}</span><span class="hint">${f.hint || ""}</span></div>`;
  };
  $("#settingsBody").innerHTML = SETTINGS.map(([title, fields]) => `<section class="panel set-group"><header class="ph"><h2>${title}</h2></header><div class="set-rows">${fields.map(row).join("")}</div></section>`).join("");
  loadStorage();
}
async function saveSetting(key, value) {
  const cfg = await attempt(() => call("save_settings", { [key]: value }), "Saved");
  if (!cfg) return loadSettings();
  S.st.cfg = cfg;
  if (key === "theme") applyTheme(value);
}
function wireSettings() {
  const body = $("#settingsBody");
  body.addEventListener("change", e => {
    const el = e.target.closest("[data-key]"); if (!el) return;
    saveSetting(el.dataset.key, el.type === "checkbox" ? el.checked : el.value);
  });
  body.addEventListener("click", async e => {
    const t = e.target;
    const day = t.closest("[data-d]");
    if (day) { day.classList.toggle("on"); return saveSetting("post_days", $$("[data-days] .on", body).map(b => +b.dataset.d)); }
    const br = t.closest("[data-browse]");
    if (br) { const p = await attempt(() => call("choose_folder", br.dataset.browse)); if (p) { $(`[data-key="${br.dataset.browse}"]`, body).value = p; saveSetting(br.dataset.browse, p); } return; }
    const f = t.closest("[data-file]");
    if (f) return attempt(() => call("edit_data_file", f.dataset.file));
    if (t.id === "cleanNow") { await attempt(() => call("cleanup_now")); setTimeout(loadStorage, 1500); return; }
    if (t.id === "openWs") return attempt(() => call("open_folder", "workspace"));
    if (t.id === "checkUpdate") {
      const info = await attempt(() => call("check_update"));
      if (!info) return;
      if (!info.available) return toast(`You're on the newest version (${info.current}).`);
      toast(`Version ${info.latest} is out. Press Update in the bottom bar.`);
      return poll();
    }
    if (t.id === "installUpdate") { const m = await attempt(() => call("install_update")); if (m) toast(m); return; }
    if (t.id === "openData") return attempt(() => call("open_folder", "data"));
    if (t.id === "keySave") { const msg = await attempt(() => call("save_key", $("#keyInput").value)); if (msg) { toast(msg); loadSettings(); } return; }
    if (t.id === "autoOn") { const m = await attempt(() => call("autopilot", true, $("#autoTime").value)); if (m) toast(m); return; }
    if (t.id === "autoOff") { const m = await attempt(() => call("autopilot", false)); if (m) toast(m); return; }
    if (t.id === "newsCopy") { const p = await attempt(() => call("news_packet")); if (p) { await copy(p); toast("Copied. Paste it into your Project with web search on."); } return; }
    if (t.id === "newsPaste") { const text = await askText("Paste the news reply", "Paste Claude's whole reply.", "Load topics"); if (text) attempt(() => call("paste_news", text), "Topics loaded"); }
  });
}

// ── boot ───────────────────────────────────────────────────────────────────
function boot() {
  paintIcons();
  $$(".rail-btn[data-view]").forEach(b => b.onclick = async () => { await flushSave(); show(b.dataset.view); });
  $("#themeBtn").onclick = () => { const t = effectiveTheme() === "dark" ? "light" : "dark"; applyTheme(t); attempt(() => call("set_theme", t)); };
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(document.documentElement.dataset.theme));
  $("#logBtn").onclick = () => { const d = $("#drawer"); d.hidden = !d.hidden; $("#logBtn").setAttribute("aria-expanded", String(!d.hidden)); if (!d.hidden) d.scrollTop = d.scrollHeight; drawTimeline(); };
  $("#updBtn").onclick = async () => { const m = await attempt(() => call("install_update")); if (m) toast(m); poll(); };
  wireHome(); wireReview(); wireLibrary(); wireSettings();
  showEditor(false);
  applyTheme(document.documentElement.dataset.theme || "system");
  const hash = location.hash.slice(1);
  show(["home", "review", "library", "settings"].includes(hash) ? hash : "home");
  poll(); setInterval(poll, 500);
  requestAnimationFrame(tick);
  window.addEventListener("beforeunload", flushSave);
}
// With pywebview the native bridge arrives slightly after load; calls fall back to HTTP until then.
document.addEventListener("DOMContentLoaded", boot);
