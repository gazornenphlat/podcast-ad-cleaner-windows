/* Podcast Cleaner web pages: talk to the engine's API (../api/...), refresh about once a second. */
"use strict";

const API = new URL("../api/", location.href).href;     // works with or without a /<token>/ in front
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function get(path) {
  const r = await fetch(API + path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
async function post(path, body = {}) {
  const r = await fetch(API + path, {
    method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json", "X-Podcast-Cleaner": "1" },
    body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok || out.ok === false) throw new Error(out.error || `${r.status}`);
  return out;
}
const act = (name, args) => post(`action/${name}`, args).then((o) => { poll(); return o.result; })
  .catch((e) => { toast("Didn't work", e.message); throw e; });

function toast(title, msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<b>${esc(title)}</b>${esc(msg || "")}`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6000);
}

/* ---------- time helpers ---------- */
function clock(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function hours(sec) {
  const h = (sec || 0) / 3600;
  return h >= 10 ? `${Math.round(h)} h` : h >= 1 ? `${h.toFixed(1)} h` : `${Math.round((sec || 0) / 60)} min`;
}
function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (isNaN(s)) return "";
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  const today = new Date().toDateString() === d.toDateString();
  return today ? `today ${d.toTimeString().slice(0, 5)}` : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toTimeString().slice(0, 5);
}
const RESULT = { done: "Cleaned", failed: "Failed", skipped: "Skipped", cancelled: "Stopped", stopped: "Stopped" };

/* ---------- pages ---------- */
const views = ["activity", "podcasts", "choose", "browse", "rules", "ads", "stats", "ai", "transcription", "settings", "log"];
let view = "activity";
function show() {
  view = views.includes(location.hash.slice(1)) ? location.hash.slice(1) : "activity";
  for (const v of views) $(`#view-${v}`).hidden = v !== view;
  let main = false;
  for (const a of document.querySelectorAll("#tabs a")) {
    a.classList.toggle("on", a.dataset.view === view);
    if (a.dataset.view === view && a.classList.contains("main")) main = true;
  }
  $("#more-btn").classList.toggle("on", !main);
  $("#more-sheet").open && $("#more-sheet").close();
  loaders[view]?.(true);
}
window.addEventListener("hashchange", show);
$("#more-btn").onclick = () => {
  $("#more-links").innerHTML = [...document.querySelectorAll("#tabs a:not(.main)")]
    .map((a) => `<a href="${a.getAttribute("href")}">${esc(a.querySelector("span").textContent)}</a>`).join("");
  $("#more-sheet").showModal();
};
$("#more-links").onclick = (e) => { if (e.target.closest("a")) $("#more-sheet").close(); };

/* this PC only: open folders and files, pick files */
let system = { local: false };
const desktop = () => window.pywebview && window.pywebview.api;
async function openOnPC(what, extra = {}) {
  try { await post("do/open", { what, ...extra }); } catch (e) { toast("Couldn't open it", e.message); }
}

/* ================= status (every page) ================= */
let status = {}, seenNote = -1, changed = null;
async function poll() {
  let st;
  try { st = await get(`status?since=${Math.max(seenNote, 0)}`); }
  catch (e) {
    setPill("error", "Can't reach Podcast Cleaner");
    return;
  }
  const first = seenNote < 0;
  for (const n of st.notes || []) {
    if (!first) toast(n.title, n.message);
    seenNote = Math.max(seenNote, n.id);
  }
  if (first) seenNote = Math.max(seenNote, 0);
  status = st;
  const state = st.paused ? "paused" : st.state;
  setPill(state, st.status);
  $("#btn-pause").textContent = st.paused ? "Resume" : "Pause";
  $("#btn-pause").className = `btn ${st.paused ? "primary" : "secondary"}`;
  $("#btn-check").disabled = !!st.busy;
  if (view === "activity") renderNow(st);
  const c = st.engine && st.engine.changed;
  if (c !== changed) {          // something happened: refresh the lists on this page
    changed = c;
    loaders[view]?.(false);
  }
}
function setPill(state, text) {
  const p = $("#pill");
  p.className = `pill ${state || ""}`;
  p.lastElementChild.textContent = text || "";
  p.title = text || "";
}
$("#btn-pause").onclick = () => act("toggle_pause");
$("#btn-feeds").onclick = () => {
  const u = status.feeds_url || status.feeds_url_local;
  if (u) window.open(u, "_blank"); else toast("Phone feeds are off", "Turn on the feed server in Settings.");
};
$("#btn-folder").onclick = () => openOnPC("output");
$("#btn-check").onclick = () => act("check_now", { force: true });

/* ================= Activity ================= */
function renderNow(st) {
  const a = st.activity || {}, cur = a.current;
  $("#now-actions").hidden = !cur;
  $("#now-bar").hidden = !cur;
  if (cur) {
    $("#now-title").textContent = cur.title || "";
    const secs = cur.started ? Date.now() / 1000 - cur.started : 0;
    $("#now-sub").textContent = `${cur.podcast} · ${cur.step || "starting"}${cur.pct != null ? ` · ${cur.pct}%` : ""} · ${clock(secs)}`;
    $("#now-bar").firstElementChild.style.width = `${cur.pct || 0}%`;
    $("#now-also").innerHTML = (a.also || []).map((j) =>
      `<li>Also: ${esc(j.podcast)} — ${esc(j.title)} · ${esc(j.step || "")}</li>`).join("");
    const sa = $("#btn-stop-after");
    sa.classList.toggle("on", !!st.stop_after_current);
    sa.textContent = st.stop_after_current ? "Will pause after this episode" : "Stop after this episode";
  } else {
    $("#now-title").textContent = st.paused ? "Paused" : a.checking ? "Checking feeds" : a.cycle_running ? "Getting ready" : "Idle";
    let sub = a.checking ? `Looking at ${a.checking}…` : "";
    if (!sub && st.next_check && !st.paused) sub = `Next check at ${new Date(st.next_check).toTimeString().slice(0, 5)}`;
    if (!sub && st.paused) sub = "Nothing new starts until you press Resume (or Start now on an episode).";
    if (!st.config_ok) sub = st.status;
    $("#now-sub").textContent = sub;
    $("#now-also").innerHTML = "";
  }
  renderMachines(a.machines || [], st.paused_computers || []);
  const tr = a.transcribers || [];
  $("#transcribers-card").hidden = tr.length < 2;
  if (tr.length >= 2) $("#transcribers").innerHTML = tr.map((m) => `<li><div class="grow"><div class="t">${esc(m.name)}</div>
    <div class="s">${esc(m.state)}${m.done ? ` · ${m.done} episode(s)` : ""}${m.speed ? ` · ${m.speed}× real time` : ""}</div></div></li>`).join("");
}
$("#btn-stop-after").onclick = () => act("toggle_stop_after");
$("#btn-skip").onclick = () => { if (confirm("Skip this episode? It won't be downloaded again.")) act("cancel_current"); };
$("#btn-stop-now").onclick = () => act("stop_now");

function renderMachines(ms, paused) {
  $("#machines-card").hidden = ms.length < 2;
  if (ms.length < 2) return;
  $("#machines").innerHTML = ms.map((m) => {
    const p = paused.includes(m.url);
    return `<li><div class="grow"><div class="t">${esc(m.name)}</div>
      <div class="s">${esc(p ? "paused" : m.state || "")}${m.done ? ` · ${m.done} sections done` : ""}</div></div>
      <button class="btn small secondary" data-url="${esc(m.url)}" data-p="${p ? 1 : 0}">${p ? "Resume" : "Pause"}</button></li>`;
  }).join("");
}
$("#machines").onclick = (e) => {
  const b = e.target.closest("button[data-url]");
  if (b) act("set_computer_paused", { url: b.dataset.url, paused: b.dataset.p !== "1" });
};

let upnext = [], selected = new Set(), anchor = null;
async function loadActivity() {
  const [rows, recent] = await Promise.all([get("upnext"), get("history?limit=80")]);
  recentRows = recent;
  upnext = rows;
  const keys = new Set(rows.map((r) => r.key));
  selected = new Set([...selected].filter((k) => keys.has(k)));
  renderUpnext();
  renderRecent();
  renderNow(status);
}
let recentRows = [];
function renderRecent() {
  const q = $("#recent-search").value.trim().toLowerCase();
  const recent = recentRows.filter((r) => !q || `${r.podcast} ${r.title}`.toLowerCase().includes(q));
  $("#recent").innerHTML = recent.length ? recent.map((r) => `
    <li><div class="grow"><div class="t">${esc(r.title)}</div>
      <div class="s">${esc(r.podcast)} · ${esc(ago(r.time))}${r.detail ? " · " + esc(r.detail) : ""}${r.seconds ? " · took " + clock(r.seconds) : ""}</div></div>
      <span class="chip ${esc(r.result)}">${esc(RESULT[r.result] || r.result)}</span>
      ${r.result === "done" && r.guid ? `<button class="btn small ghost" data-re="${esc(r.folder)}|${esc(r.guid)}" data-title="${esc(r.title)}" title="Find the ads again with your current model and rules">Redo</button>` : ""}
    </li>`).join("") : `<li class="empty">${recentRows.length ? "No match" : "Nothing finished yet"}</li>`;
}
$("#recent-search").oninput = renderRecent;
function renderUpnext() {
  $("#upnext-count").textContent = upnext.length ? `${upnext.length} episode${upnext.length > 1 ? "s" : ""}` : "";
  $("#upnext").innerHTML = upnext.length ? upnext.map((r) => `
    <li data-key="${esc(r.key)}" class="${selected.has(r.key) ? "sel" : ""} ${r.tag === "muted" ? "muted-row" : ""}">
      <div class="grow"><div class="t">${esc(r.title)}</div>
      <div class="s">${esc(r.podcast)}${r.published ? " · " + esc(r.published) : ""} · ${esc(r.why)}</div></div>
      ${r.tag === "high" ? `<span class="chip high">Priority</span>` : ""}
    </li>`).join("") : `<li class="empty">Nothing waiting</li>`;
  const n = selected.size;
  for (const id of ["#btn-start", "#btn-top", "#btn-up", "#btn-down", "#btn-bottom", "#btn-skip-sel"]) $(id).disabled = !n;
  $("#btn-start").textContent = n > 1 ? `Start these ${n}` : "Start now";
}
$("#upnext").onclick = (e) => {
  const li = e.target.closest("li[data-key]");
  if (!li) return;
  const k = li.dataset.key, order = upnext.map((r) => r.key);
  if (e.shiftKey && anchor && order.includes(anchor)) {
    const [a, b] = [order.indexOf(anchor), order.indexOf(k)].sort((x, y) => x - y);
    selected = new Set(order.slice(a, b + 1));
  } else {
    selected.has(k) ? selected.delete(k) : selected.add(k);     // tap to pick several (phone friendly)
    anchor = k;
  }
  renderUpnext();
};
function reorder(fn) {
  let keys = upnext.map((r) => r.key);
  keys = fn(keys);
  upnext.sort((a, b) => keys.indexOf(a.key) - keys.indexOf(b.key));
  renderUpnext();
  return act("set_queue_order", { keys });
}
$("#btn-top").onclick = () => reorder((keys) => [...keys.filter((k) => selected.has(k)), ...keys.filter((k) => !selected.has(k))]);
$("#btn-up").onclick = () => reorder((keys) => {
  for (let i = 1; i < keys.length; i++) if (selected.has(keys[i]) && !selected.has(keys[i - 1])) [keys[i - 1], keys[i]] = [keys[i], keys[i - 1]];
  return keys;
});
$("#btn-down").onclick = () => reorder((keys) => {
  for (let i = keys.length - 2; i >= 0; i--) if (selected.has(keys[i]) && !selected.has(keys[i + 1])) [keys[i + 1], keys[i]] = [keys[i], keys[i + 1]];
  return keys;
});
$("#btn-bottom").onclick = () => reorder((keys) => [...keys.filter((k) => !selected.has(k)), ...keys.filter((k) => selected.has(k))]);
$("#btn-skip-sel").onclick = async () => {
  const rows = upnext.filter((r) => selected.has(r.key) && !r.key.startsWith("Ad-free|"));
  if (!rows.length || !confirm(`Skip ${rows.length} episode${rows.length > 1 ? "s" : ""}? They won't be downloaded.`)) return;
  for (const r of rows) {
    const i = r.key.indexOf("|");
    await act("skip_episode", { folder: r.key.slice(0, i), guid: r.key.slice(i + 1), title: r.title });
    await post("action/drop_queued", { folder: r.key.slice(0, i), guid: r.key.slice(i + 1) }).catch(() => {});
  }
  selected.clear();
  loadActivity();
};
$("#btn-start").onclick = async () => {
  const sel = upnext.map((r) => r.key).filter((k) => selected.has(k));
  const wasPaused = status.paused, busy = !!(status.activity && status.activity.current);
  await reorder((keys) => [...sel, ...keys.filter((k) => !selected.has(k))]);
  await act("start_now", { keys: sel });
  $("#upnext-hint").textContent = busy ? "Next, as soon as the episode being processed finishes."
    : wasPaused ? `Starting ${sel.length > 1 ? `just these ${sel.length}` : "just this one"} — then it pauses again.` : "Starting…";
  selected.clear();
};
$("#recent").onclick = (e) => {
  const b = e.target.closest("button[data-re]");
  if (!b) return;
  const [folder, guid] = [b.dataset.re.slice(0, b.dataset.re.indexOf("|")), b.dataset.re.slice(b.dataset.re.indexOf("|") + 1)];
  act("reanalyze_episode", { folder, guid, title: b.dataset.title }).then(() => toast("Queued", "It will be cleaned again with your current settings."));
};

/* ================= Podcasts ================= */
let pods = [];
async function loadPodcasts() {
  pods = await get("podcasts");
  renderPods();
}
function renderPods() {
  const q = $("#pod-search").value.trim().toLowerCase();
  const list = pods.filter((p) => !q || p.name.toLowerCase().includes(q)).sort((a, b) => a.name.localeCompare(b.name));
  $("#pods").innerHTML = list.length ? list.map((p) => `
    <li data-folder="${esc(p.folder)}" data-name="${esc(p.name)}" class="${p.enabled ? "" : "muted-row"}">
      ${p.image ? `<img class="art" loading="lazy" alt="" src="${esc(p.image)}">` : `<div class="art"></div>`}
      <div class="grow pick" data-a="episodes" style="cursor:pointer"><div class="t">${esc(p.name)}</div>
        <div class="s">${p.done} cleaned${p.queued ? ` · ${p.queued} waiting` : ""}${p.available ? ` · ${p.available} to choose` : ""}${p.failed ? ` · ${p.failed} failed` : ""}${p.last_cleaned ? ` · last ${esc(ago(p.last_cleaned))}` : ""}
        ${p.feed_error ? `<br><span class="bad">Feed problem: ${esc(p.feed_error.slice(0, 120))}</span>` : ""}</div></div>
      ${!p.enabled ? `<span class="chip">Off</span>` : !p.auto_download ? `<span class="chip">You choose</span>` : ""}
      ${p.priority && p.priority !== "medium" ? `<span class="chip ${p.priority.startsWith("high") ? "high" : ""}">${esc(p.priority)}</span>` : ""}
      <details class="menu"><summary class="btn ghost small" aria-label="More for ${esc(p.name)}">⋯</summary>
        <div class="menu-body" style="left:auto;right:0">
          <button class="btn ghost small" data-a="episodes">Episodes…</button>
          <button class="btn ghost small" data-a="edit">Edit…</button>
          <button class="btn ghost small" data-a="auto">${p.auto_download ? "Let me choose episodes" : "Download automatically"}</button>
          <button class="btn ghost small" data-a="enabled">${p.enabled ? "Turn off" : "Turn on"}</button>
          ${system.local ? `<button class="btn ghost small" data-a="folder">Open folder</button>` : ""}
          <button class="btn ghost small bad" data-a="remove">Remove…</button>
        </div></details>
    </li>`).join("") : `<li class="empty">${pods.length ? "No match" : "No podcasts yet — press Add podcast"}</li>`;
}
$("#pod-search").oninput = renderPods;
$("#pods").onclick = async (e) => {
  const b = e.target.closest("[data-a]"), li = e.target.closest("li[data-folder]");
  if (!b || !li) return;
  b.closest("details")?.removeAttribute("open");
  const name = li.dataset.name, folder = li.dataset.folder;
  const run = (path, args) => post(path, args).then((o) => { loadPodcasts(); return o; }).catch((err) => toast("Didn't work", err.message));
  switch (b.dataset.a) {
    case "episodes": return openShow(folder, name);
    case "edit": return podcastDialog(name);
    case "auto": return run("do/podcast_toggle", { name, field: "auto_download" });
    case "enabled": return run("do/podcast_toggle", { name, field: "enabled" });
    case "folder": return openOnPC("show", { name });
    case "remove":
      if (confirm(`Remove ${name}? Its downloaded episodes stay in the folder; only the subscription goes.`))
        return run("do/podcast_remove", { name });
  }
};
$("#pod-add").onclick = () => podcastDialog(null);
$("#opml-adfree").href = "../api/opml?which=adfree";
$("#opml-orig").href = "../api/opml?which=original";
$("#opml-file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const choose = confirm("Let you choose their episodes? (Cancel = download new episodes automatically)");
  try {
    const r = await post("do/opml_import", { content: await f.text(), choose });
    toast("Imported", `${r.added} podcast(s) added${r.skipped ? `, ${r.skipped} already there` : ""}.`);
    loadPodcasts();
  } catch (err) { toast("Couldn't import", err.message); }
};
$("#pod-clean").onclick = async () => {
  let paths = [];
  if (desktop()) paths = (await window.pywebview.api.pick_audio()) || [];
  else {
    const t = prompt("Full paths of the audio files to clean, separated by |");
    paths = (t || "").split("|").map((x) => x.trim()).filter(Boolean);
  }
  if (!paths.length) return;
  try {
    const r = await post("do/clean_files", { paths });
    toast("Added to Up next", `${r.added} file(s). The cleaned copies go to ${r.where} — your files are left as they are.`);
  } catch (err) { toast("Didn't work", err.message); }
};
const EP_LABEL = { done: "Cleaned", failed: "Failed", queued: "Waiting", available: "Not downloaded", skipped: "Skipped", downloading: "Working" };
async function openShow(folder, name) {
  $("#dlg-title").textContent = name;
  $("#dlg-body").innerHTML = `<p class="muted">Loading…</p>`;
  $("#dlg").showModal();
  const eps = await get(`episodes?show=${encodeURIComponent(folder)}`);
  $("#dlg-body").innerHTML = eps.length ? `<ul class="list">${eps.map((e) => `
    <li><div class="grow"><div class="t">${esc(e.display_title || e.title)}</div>
      <div class="s">${esc((e.published || "").slice(0, 10))}${e.ads_removed != null ? ` · ${e.ads_removed} ad(s), ${clock(e.seconds_removed)} removed` : ""}${e.error ? ` · ${esc(String(e.error).slice(0, 100))}` : ""}</div></div>
      <span class="chip ${esc(e.status)}">${esc(EP_LABEL[e.status] || e.status)}</span>
      ${["available", "failed", "skipped"].includes(e.status) ? `<button class="btn small primary" data-a="request_episode" data-g="${esc(e.guid)}">Get</button>` : ""}
      ${e.status === "available" ? `<button class="btn small ghost" data-a="skip_episode" data-g="${esc(e.guid)}">Skip</button>` : ""}
      ${e.status === "done" ? `<button class="btn small ghost" data-a="reanalyze_episode" data-g="${esc(e.guid)}">Redo</button>` : ""}
    </li>`).join("")}</ul>` : `<p class="muted">No episodes yet — they appear after the first check.</p>`;
  $("#dlg-body").onclick = async (ev) => {
    const b = ev.target.closest("button[data-a]");
    if (!b) return;
    const ep = eps.find((x) => x.guid === b.dataset.g);
    b.disabled = true;
    await act(b.dataset.a, { folder, guid: b.dataset.g, title: ep ? ep.title : "" });
    b.textContent = b.dataset.a === "skip_episode" ? "Skipped" : "Queued";
  };
}

/* ================= Found ads ================= */
let ads = [], held = [];
async function loadAds() {
  const r = await get("found-ads");
  ads = r.recent || [];
  held = r.held || [];
  renderHeld();
  renderAds();
  const [sp, learned] = await Promise.all([get("sponsors"), get("learned")]);
  $("#sponsors").innerHTML = sp.sponsors.length ? sp.sponsors.slice(0, 60).map((x) => `
    <li><div class="grow"><div class="t">${esc(x.name)}</div>
      <div class="s">${x.ads} ad(s) · ${x.shows} show(s) · ${hours(x.seconds)}${x.not_ads ? ` · ${x.not_ads} not an ad` : ""}</div></div></li>`).join("")
    : `<li class="empty">None yet</li>`;
  renderWatch(sp.watch);
  $("#learned-info").textContent = `${learned.ads} ad read(s) and ${learned.not_ads} "not an ad" learned. Share them with another PC running Podcast Cleaner.`;
}
function renderWatch(list) {
  $("#watch").innerHTML = list.length ? list.map((n) => `<li><div class="grow t">${esc(n)}</div>
    <button class="btn ghost small" data-rm="${esc(n)}" aria-label="Remove ${esc(n)}">✕</button></li>`).join("")
    : `<li class="empty">Named in every show's prompt, even if the AI never found them.</li>`;
}
$("#watch").onclick = async (e) => {
  const b = e.target.closest("button[data-rm]");
  if (b) renderWatch((await post("do/watch", { remove: [b.dataset.rm] }).catch((err) => toast("Didn't work", err.message)) || {}).watch || []);
};
$("#watch-add").onclick = async () => {
  const v = $("#watch-new").value.trim();
  if (!v) return;
  const r = await post("do/watch", { add: v }).catch((err) => toast("Didn't work", err.message));
  if (r) { $("#watch-new").value = ""; renderWatch(r.watch); }
};
$("#learned-file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  try {
    const r = await post("do/learned_import", { content: await f.text() });
    toast("Added", `${r.new} new read(s); ${r.known} were already known.`);
    loadAds();
  } catch (err) { toast("Couldn't add them", err.message); }
};
function renderHeld() {
  $("#held-card").hidden = !held.length;
  $("#held-count").textContent = held.length ? `${held.length} waiting` : "";
  $("#held").innerHTML = held.map((h, i) => `
    <li><div class="grow"><div class="t">${esc(h.label || "Possible ad")} <span class="muted">· ${clock(h.start)}–${clock(h.end)}</span></div>
      <div class="s">${esc(h.podcast)} · ${esc(h.episode)}${h.confidence != null ? ` · ${Math.round(h.confidence * 100)}% sure` : ""}</div>
      ${h.text ? `<div class="s" style="margin-top:4px">“${esc(String(h.text).slice(0, 220))}${String(h.text).length > 220 ? "…" : ""}”</div>` : ""}</div>
      <button class="btn small ghost" data-i="${i}" data-listen="1" title="Hear it">▶</button>
      <button class="btn small primary" data-i="${i}" data-v="approved" ${h.has_original ? "" : 'title="The original was not kept: it will be learned for next time"'}>Cut it</button>
      <button class="btn small secondary" data-i="${i}" data-v="dismissed">Not an ad</button>
    </li>`).join("");
}
function play(url) {
  const p = $("#player");
  p.src = url;
  p.play().catch((err) => toast("Couldn't play it", err.message));
}
$("#held").onclick = async (e) => {
  const l = e.target.closest("button[data-listen]");
  if (l) {
    const h = held[+l.dataset.i];
    return play(`../api/clip?kind=held&folder=${encodeURIComponent(h.folder)}&guid=${encodeURIComponent(h.guid)}&start=${h.start}&end=${h.end}`);
  }
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  const item = held[+b.dataset.i];
  const out = await act("review_held", { items: [item], verdict: b.dataset.v });
  held.splice(+b.dataset.i, 1);
  renderHeld();
  if (b.dataset.v === "approved") toast("Thanks", out && out[0] ? "The episode will be cut again without it." : "Learned for next time.");
};
function renderAds() {
  const q = $("#ads-search").value.trim().toLowerCase();
  const list = ads.filter((a) => !q || `${a.podcast} ${a.episode} ${a.sponsor} ${a.excerpt}`.toLowerCase().includes(q)).slice(0, 150);
  $("#ads").innerHTML = list.length ? list.map((a) => `
    <li><div class="grow"><div class="t">${esc(a.sponsor || "Ad")} <span class="muted">· ${clock(a.seconds)}</span></div>
      <div class="s">${esc(a.podcast)} · ${esc(a.episode)} · at ${clock(a.start)} · ${esc(a.found_by || "")}</div>
      ${a.excerpt ? `<div class="s" style="margin-top:4px">“${esc(String(a.excerpt).slice(0, 200))}…”</div>` : ""}</div>
      <button class="btn small ghost" data-play="${esc(a.id)}" title="Hear the join where it was cut">▶</button>
      ${a.status === "not_ad"
        ? `<span class="chip">Not an ad</span><button class="btn small ghost" data-id="${esc(a.id)}" data-s="ad">Undo</button>`
        : `<button class="btn small ghost" data-id="${esc(a.id)}" data-s="not_ad" title="Teach it: this part wasn't an ad">Not an ad</button>`}
    </li>`).join("") : `<li class="empty">${ads.length ? "No match" : "No ads found yet"}</li>`;
}
$("#ads-search").oninput = renderAds;
$("#ads").onclick = async (e) => {
  const pl = e.target.closest("button[data-play]");
  if (pl) return play(`../api/clip?kind=ad&id=${encodeURIComponent(pl.dataset.play)}`);
  const b = e.target.closest("button[data-id]");
  if (!b) return;
  await act("mark_ads", { ids: [b.dataset.id], status: b.dataset.s });
  const a = ads.find((x) => String(x.id) === b.dataset.id);
  if (a) a.status = b.dataset.s;
  renderAds();
};

/* ================= Stats ================= */
async function loadStats() {
  const s = await get("stats");
  const shows = s.shows || [];
  const eps = shows.reduce((n, x) => n + x.episodes, 0), secs = shows.reduce((n, x) => n + x.seconds, 0);
  const nads = shows.reduce((n, x) => n + x.ads, 0);
  $("#tiles").innerHTML = [["Episodes cleaned", eps], ["Ads removed", nads], ["Time saved", hours(secs)],
    ["Per episode", eps ? clock(secs / eps) : "–"]].map(([k, v]) =>
    `<div class="card tile"><div class="label">${k}</div><div class="big">${esc(v)}</div></div>`).join("");
  const weeks = s.weeks || [], max = Math.max(1, ...weeks.map((w) => w.seconds));
  $("#chart").innerHTML = weeks.length ? weeks.map((w) => `
    <div class="col" title="${esc(w.week)}: ${w.episodes} episodes, ${hours(w.seconds)} removed">
      <div class="b" style="height:${Math.round((w.seconds / max) * 100)}%"></div><div class="x">${esc(w.week.slice(5))}</div></div>`).join("")
    : `<p class="muted">Nothing cleaned in the last 12 weeks.</p>`;
  $("#by-show tbody").innerHTML = shows.map((x) => `<tr><td>${esc((pods.find((p) => p.folder === x.show) || {}).name || x.show)}</td>
    <td class="num">${x.episodes}</td><td class="num">${x.ads}</td><td class="num">${hours(x.seconds)}</td></tr>`).join("");
}

/* ================= Log ================= */
let logLines = [];
async function loadLog() {
  logLines = await get("log?lines=400");
  renderLog();
}
function renderLog() {
  const q = $("#log-search").value.trim().toLowerCase(), problems = $("#log-problems").checked;
  const pre = $("#log"), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.innerHTML = logLines.filter((l) => (!q || l.toLowerCase().includes(q)) && (!problems || / (WARNING|ERROR) /.test(l)))
    .map((l) => / ERROR /.test(l) ? `<span class="e">${esc(l)}</span>` : / WARNING /.test(l) ? `<span class="w">${esc(l)}</span>` : esc(l)).join("\n");
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}
$("#log-search").oninput = renderLog;
$("#log-problems").onchange = renderLog;

/* ---------- refresh ---------- */
const loaders = {
  activity: (first) => loadActivity().catch(() => {}),
  podcasts: (first) => loadPodcasts().catch(() => {}),
  ads: (first) => loadAds().catch(() => {}),
  stats: (first) => (pods.length ? Promise.resolve() : loadPodcasts()).then(loadStats).catch(() => {}),
  log: (first) => loadLog().catch(() => {}),
};
$("#log-open").onclick = () => openOnPC("log");
$("#ads-csv-open").onclick = () => openOnPC("ads_csv");
window.addEventListener("load", async () => {
  try { system = await get("system"); } catch (e) { /* engine not ready yet */ }
  document.querySelectorAll(".local").forEach((el) => { el.hidden = !system.local; });
  $("#btn-folder").hidden = !system.local;
  show();
  poll();
});
setInterval(() => { if (!document.hidden) poll(); }, 1000);
setInterval(() => { if (!document.hidden && (view === "log" || view === "activity")) loaders[view](false); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
