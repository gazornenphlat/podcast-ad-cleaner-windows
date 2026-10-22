/* The rest of the pages: podcast editing, Choose Episodes, Browse Feeds, Ad Rules, AI, Settings. Uses app.js helpers. */
"use strict";

/* ---------------- a small form builder ----------------
   field: {key, label, type: text|number|check|select|time|password|textarea|choice, hint, options:[[value,label]],
           step, min, max, text (for checks), width} */
const pathGet = (obj, key) => key.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
function buildForm(el, fields, values) {
  el.innerHTML = "";
  for (const f of fields) {
    const v = pathGet(values, f.key);
    let input;
    if (f.type === "check") {
      input = `<label class="check"><input type="checkbox" data-k="${f.key}" ${v ? "checked" : ""}> ${esc(f.text || "")}</label>`;
    } else if (f.type === "select") {
      input = `<select data-k="${f.key}">${f.options.map(([o, l]) => `<option value="${esc(o)}" ${String(v) === String(o) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    } else if (f.type === "textarea") {
      input = `<textarea data-k="${f.key}" rows="${f.rows || 4}">${esc(v ?? "")}</textarea>`;
    } else {
      const list = f.datalist ? ` list="dl-${f.key.replace(/\W/g, "")}"` : "";
      input = `<input type="${f.type || "text"}" data-k="${f.key}" value="${esc(v ?? "")}"${f.step ? ` step="${f.step}"` : ""}${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""}${f.width ? ` style="width:${f.width}"` : ""}${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}${list}>`
        + (f.datalist ? `<datalist id="dl-${f.key.replace(/\W/g, "")}">${f.datalist.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>` : "");
    }
    el.insertAdjacentHTML("beforeend", `<label class="k">${esc(f.label || "")}</label><div class="v">${input}${f.after ? `<span class="muted small">${esc(f.after)}</span>` : ""}</div>`
      + (f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""));
  }
}
function readForm(el, fields) {
  const out = {};
  for (const f of fields) {
    const i = el.querySelector(`[data-k="${f.key}"]`);
    if (!i) continue;
    if (f.type === "check") out[f.key] = i.checked;
    else if (f.type === "number") {
      if (i.value.trim() === "") continue;
      const n = Number(i.value);
      if (Number.isNaN(n)) throw new Error(`${f.label || f.key}: must be a number`);
      out[f.key] = n;
    } else out[f.key] = f.type === "textarea" ? i.value : i.value.trim();
  }
  return out;
}
async function saveSettings(changes, okMsg) {
  try {
    const r = await post("settings", { set: changes });
    toast("Saved", okMsg || "They apply from the next episode.");
    return r;
  } catch (e) { toast("Not saved", e.message); throw e; }
}
let settingsCache = null;
async function settings(force) {
  if (!settingsCache || force) settingsCache = await get("settings");
  return settingsCache;
}

/* ================= Podcasts: add / edit ================= */
async function podcastDialog(name) {
  $("#dlg-title").textContent = name ? `Edit ${name}` : "Add podcast";
  $("#dlg-body").innerHTML = `<p class="muted">Loading…</p>`;
  $("#dlg").showModal();
  const p = await get(`podcast?name=${encodeURIComponent(name || "")}`);
  const removeBoxes = Object.entries(p.remove_labels).map(([k, l]) =>
    `<label class="check"><input type="checkbox" data-r="${k}" ${p.remove[k] ? "checked" : ""}> ${esc(l)}</label>`).join("");
  $("#dlg-body").innerHTML = `
    ${name ? "" : `<div class="label small-label">Find a show</div>
      <div class="inline"><input id="pd-q" placeholder="Name of the podcast" style="flex:1;min-width:200px"><button class="btn secondary" id="pd-search" type="button">Search</button></div>
      <ul class="list compact" id="pd-results"></ul><p class="muted small">…or paste the RSS feed address below.</p>`}
    <div class="form">
      <label class="k">Name</label><div class="v"><input id="pd-name" value="${esc(p.name)}" style="width:100%"></div>
      <label class="k">RSS feed</label><div class="v"><input id="pd-rss" value="${esc(p.rss)}" style="width:100%" placeholder="https://…"></div>
      <label class="k">New episodes</label><div class="v">
        <label class="check"><input type="radio" name="pd-auto" value="1" ${p.auto_download ? "checked" : ""}> download automatically</label>
        <label class="check"><input type="radio" name="pd-auto" value="0" ${p.auto_download ? "" : "checked"}> let me choose</label></div>
      <label class="k">Enabled</label><div class="v"><input type="checkbox" id="pd-enabled" ${p.enabled ? "checked" : ""}></div>
      <label class="k">Priority</label><div class="v"><select id="pd-prio">${["highest", "high", "medium", "low"].map((x) =>
        `<option value="${x}" ${p.priority === x ? "selected" : ""}>${x[0].toUpperCase() + x.slice(1)}</option>`).join("")}</select></div>
      <div class="hint">Highest/High: new episodes go to the top of Up next. Low: to the bottom (and Low Priority Hours apply).</div>
      <label class="k">Check feed</label><div class="v"><label class="check"><input type="checkbox" id="pd-smart" ${p.smart_schedule ? "checked" : ""}> only around its usual release times</label></div>
      <label class="k">First check takes</label><div class="v"><input type="number" id="pd-initial" min="0" max="50" value="${esc(p.initial_episodes)}" placeholder="${p.default_initial}"><span class="muted small">newest episodes</span></div>
      <label class="k">Keep at most</label><div class="v"><input type="number" id="pd-keep" min="0" value="${esc(p.keep_episodes)}" placeholder="all"><span class="muted small">episodes</span></div>
      <label class="k">Delete after</label><div class="v"><input type="number" id="pd-days" min="0" value="${esc(p.keep_days)}" placeholder="never"><span class="muted small">days</span></div>
      <label class="k">Known sponsors</label><div class="v"><input id="pd-sponsors" value="${esc(p.sponsors)}" style="width:100%" placeholder="comma-separated"></div>
      <label class="k">Also cut</label><div class="v">${removeBoxes}</div>
      <div class="hint">Sponsor ads are always cut. The defaults come from the Ad Rules tab.</div>
    </div>
    <p class="bad small" id="pd-error"></p>
    <div class="row-actions"><button class="btn primary" id="pd-save" type="button">Save</button>
      <button class="btn secondary" type="button" onclick="this.closest('dialog').close()">Cancel</button></div>`;
  let search = p.search;
  if (!name) {
    const go = async () => {
      const q = $("#pd-q").value.trim();
      if (!q) return;
      $("#pd-results").innerHTML = `<li class="empty">Searching…</li>`;
      try {
        const res = await get(`search?term=${encodeURIComponent(q)}`);
        $("#pd-results").innerHTML = res.length ? res.map((r, i) => `<li class="pick" data-i="${i}" style="cursor:pointer">
          <div class="grow"><div class="t">${esc(r.name)}</div><div class="s">${esc(r.author)}</div></div></li>`).join("")
          : `<li class="empty">No podcasts found</li>`;
        $("#pd-results").onclick = (e) => {
          const li = e.target.closest("li[data-i]");
          if (!li) return;
          const r = res[+li.dataset.i];
          $("#pd-name").value = r.name;
          $("#pd-rss").value = r.feed_url;
          [...$("#pd-results").children].forEach((x) => x.classList.toggle("sel", x === li));
        };
      } catch (e) { $("#pd-results").innerHTML = `<li class="empty">Search failed: ${esc(e.message)}</li>`; }
    };
    $("#pd-search").onclick = go;
    $("#pd-q").onkeydown = (e) => { if (e.key === "Enter") go(); };
    $("#pd-q").focus();
  }
  $("#pd-save").onclick = async () => {
    const remove = {};
    document.querySelectorAll("#dlg-body [data-r]").forEach((c) => { remove[c.dataset.r] = c.checked; });
    const args = {
      orig_name: name, name: $("#pd-name").value, rss: $("#pd-rss").value, search,
      auto_download: document.querySelector("input[name=pd-auto]:checked").value === "1",
      enabled: $("#pd-enabled").checked, smart_schedule: $("#pd-smart").checked, priority: $("#pd-prio").value,
      initial_episodes: $("#pd-initial").value, keep_episodes: $("#pd-keep").value, keep_days: $("#pd-days").value,
      sponsors: $("#pd-sponsors").value, remove,
    };
    try {
      const r = await post("do/podcast_save", args);
      $("#dlg").close();
      if (r.note) toast("Saved", r.note);
      loadPodcasts();
      if (!name && confirm(`Added ${args.name}. Check for its episodes now?`)) act("check_now", { force: true });
    } catch (e) { $("#pd-error").textContent = e.message; }
  };
}

/* ================= Choose episodes ================= */
let chooseData = [];
async function loadChoose() {
  chooseData = await get("choose");
  const picked = new Set([...document.querySelectorAll("#choose input[data-g]:checked")].map((c) => c.dataset.folder + "|" + c.dataset.g));
  let total = 0;
  $("#choose").innerHTML = chooseData.map((s) => {
    const fresh = s.episodes.filter((e) => e.status !== "queued").length;
    total += fresh;
    return `<div class="show-head"><input type="checkbox" data-show="${esc(s.folder)}" aria-label="All of ${esc(s.name)}">
      ${esc(s.name)} <span class="muted small">${fresh ? `${fresh} new` : s.episodes.length ? "" : "nothing new"}</span></div>
      <ul class="list">${s.episodes.map((e) => `<li class="${e.status === "queued" ? "off" : "pick"}">
        <input type="checkbox" data-folder="${esc(s.folder)}" data-g="${esc(e.guid)}" data-title="${esc(e.title)}" ${e.status === "queued" ? "disabled" : ""} ${picked.has(s.folder + "|" + e.guid) ? "checked" : ""}>
        <div class="grow"><div class="t">${esc(e.title)}</div><div class="s">${esc(e.published)}${e.error ? ` · failed: ${esc(e.error)}` : ""}</div></div>
        <span class="chip ${e.status === "queued" ? "queued" : e.status === "failed" ? "failed" : ""}">${e.status === "queued" ? "Queued" : e.status === "failed" ? "Failed" : "New"}</span>
      </li>`).join("")}</ul>`;
  }).join("");
  $("#choose-hint").textContent = chooseData.length
    ? "Tick episodes (or a whole show) and press Download or Skip. Episodes you don't decide on stay here."
    : "Nothing new to choose right now. Shows set to \"let me choose\" appear here when they have new episodes.";
  $("#choose-count").textContent = total || "";
  chooseButtons();
}
function chooseButtons() {
  const n = document.querySelectorAll("#choose input[data-g]:checked").length;
  $("#choose-get").disabled = $("#choose-skip").disabled = !n;
  $("#choose-get").textContent = n ? `Download ${n}` : "Download";
}
$("#choose").onclick = (e) => {
  const all = e.target.closest("input[data-show]");
  if (all) document.querySelectorAll(`#choose input[data-folder="${CSS.escape(all.dataset.show)}"]:not(:disabled)`).forEach((c) => { c.checked = all.checked; });
  const li = e.target.closest("li.pick");
  if (li && e.target.tagName !== "INPUT") { const c = li.querySelector("input"); c.checked = !c.checked; }
  chooseButtons();
};
async function chooseDo(skip) {
  const boxes = [...document.querySelectorAll("#choose input[data-g]:checked")];
  if (skip && boxes.length > 3 && !confirm(`Skip ${boxes.length} episodes? They won't be downloaded.`)) return;
  const by = {};
  boxes.forEach((c) => (by[c.dataset.folder] ||= []).push({ guid: c.dataset.g, title: c.dataset.title }));
  for (const [folder, episodes] of Object.entries(by)) await post("do/get_episodes", { folder, episodes, skip }).catch((e) => toast("Didn't work", e.message));
  boxes.forEach((c) => { c.checked = false; });
  toast(skip ? "Skipped" : "Queued", `${boxes.length} episode(s).`);
  loadChoose();
}
$("#choose-get").onclick = () => chooseDo(false);
$("#choose-skip").onclick = () => chooseDo(true);

/* ================= Browse feeds ================= */
let browseData = null;
const browsePicked = new Set();
async function loadBrowse(first, refresh) {
  if (first || !$("#browse-show").options.length) {
    if (!pods.length) pods = await get("podcasts");
    const cur = $("#browse-show").value;
    const names = pods.map((p) => p.name).sort((a, b) => a.localeCompare(b));
    $("#browse-show").innerHTML = `<option value="">Pick a podcast…</option>` + names.map((n) => `<option ${n === cur ? "selected" : ""}>${esc(n)}</option>`).join("");
  }
  const show = $("#browse-show").value;
  if (!show) { $("#browse").innerHTML = ""; $("#browse-info").textContent = "Every episode in a show's feed, including old ones — tick any to download."; return; }
  if (!browseData || browseData.name !== show || refresh) {
    $("#browse-info").textContent = `Loading ${show}…`;
    if (!browseData || browseData.name !== show) browsePicked.clear();
  }
  try { browseData = await get(`browse?show=${encodeURIComponent(show)}${refresh ? "&refresh=1" : ""}`); }
  catch (e) { $("#browse-info").textContent = `Couldn't load ${show}: ${e.message}`; return; }
  renderBrowse();
}
function renderBrowse() {
  if (!browseData) return;
  const q = $("#browse-search").value.trim().toLowerCase(), hide = $("#browse-hide").checked;
  let downloaded = 0;
  const rows = browseData.episodes.filter((e) => {
    if (e.status.startsWith("Downloaded")) { downloaded++; if (hide) return false; }
    if (!e.can) browsePicked.delete(e.guid);
    return !q || e.title.toLowerCase().includes(q);
  });
  $("#browse").innerHTML = rows.map((e) => `<li class="${e.can ? "pick" : "off"}" data-g="${esc(e.guid)}">
    <input type="checkbox" ${e.can ? "" : "disabled"} ${browsePicked.has(e.guid) ? "checked" : ""} aria-label="Pick">
    <div class="grow"><div class="t">${esc(e.title)}</div><div class="s">${esc(e.published)}${e.duration ? ` · ${clock(e.duration)}` : ""}</div></div>
    <span class="chip ${e.status.startsWith("Downloaded") ? "done" : e.status.startsWith("Queued") ? "queued" : e.status.startsWith("Failed") ? "failed" : ""}">${esc(e.status)}</span></li>`).join("")
    || `<li class="empty">No episodes</li>`;
  const total = browseData.episodes.length;
  $("#browse-info").textContent = `${browseData.name}: ${total} episode${total !== 1 ? "s" : ""} in the feed, ${downloaded} downloaded${rows.length !== total ? ` — showing ${rows.length}` : ""}`;
  const n = browsePicked.size;
  $("#browse-get").disabled = !n;
  $("#browse-get").textContent = n ? `Download selected (${n})` : "Download selected";
}
$("#browse").onclick = (e) => {
  const li = e.target.closest("li.pick");
  if (!li) return;
  const g = li.dataset.g;
  browsePicked.has(g) ? browsePicked.delete(g) : browsePicked.add(g);
  renderBrowse();
};
$("#browse-show").onchange = () => loadBrowse(false, false);
$("#browse-refresh").onclick = () => loadBrowse(false, true);
$("#browse-search").oninput = renderBrowse;
$("#browse-hide").onchange = renderBrowse;
$("#browse-all").onclick = () => { browseData && [...document.querySelectorAll("#browse li.pick")].forEach((li) => browsePicked.add(li.dataset.g)); renderBrowse(); };
$("#browse-none").onclick = () => { browsePicked.clear(); renderBrowse(); };
$("#browse-get").onclick = async () => {
  const eps = browseData.episodes.filter((e) => browsePicked.has(e.guid)).map((e) => ({ guid: e.guid, title: e.title }));
  if (eps.length > 1 && !confirm(`Download ${eps.length} episodes of ${browseData.name}? Each one is downloaded, transcribed and cleaned in turn.`)) return;
  await post("do/get_episodes", { show: browseData.name, episodes: eps }).catch((e) => toast("Didn't work", e.message));
  browsePicked.clear();
  toast("Queued", `${eps.length} episode(s).`);
  loadBrowse(false, false);
};

/* ================= Ad rules ================= */
const RULES_EXAMPLE = "Treat plugs for the hosts' Patreon, live tour dates and merch as ads.\nAds for other shows on the same network are ads.\n"
  + "Do NOT treat the hosts talking about a product they personally like as an ad unless they give a promo code or URL.\n";
const REMOVE_LABELS = { cross_promo: "promos for other shows", self_promo: "the hosts' own promos", intro: "generic intros", outro: "generic outros", jingle: "ad jingles, dead air" };
const RULES_CUT = [
  { key: "ad_removal.min_ad_seconds", label: "Ignore ads shorter than", type: "number", min: 0, after: "seconds" },
  { key: "ad_removal.max_ad_seconds", label: "Ignore ads longer than", type: "number", min: 30, after: "seconds (protects content)" },
  { key: "ad_removal.padding_seconds", label: "Trim extra around each ad", type: "number", step: 0.5, min: 0, after: "seconds (if bits are left)" },
  ...Object.entries(REMOVE_LABELS).map(([k, l], i) => ({ key: `ad_removal.remove.${k}`, label: i ? "" : "Also cut", type: "check", text: l })),
];
const RULES_DETECT = [
  { key: "ad_removal.min_confidence", label: "Cut only when at least", type: "number", step: 0.05, min: 0.3, max: 0.95, after: "sure (0.6 = 60%)" },
  { key: "chapters.publisher_ads", label: "Publisher's ad chapters", type: "select", options: [["combine", "use them and the AI"], ["skip_ai", "trust them, skip the AI"], ["skip_all", "trust them, skip transcription too"]] },
  { key: "ollama.highlight_cues", type: "check", text: "point out web addresses, promo codes… to the AI" },
  { key: "ad_removal.extend_with_cues", type: "check", text: "add the lead-in and 'back to the show' lines to each ad" },
  { key: "ad_removal.refine_boundaries", type: "check", text: "check each ad's edges with a second AI pass (slower)" },
  { key: "output.cut_tones", type: "check", text: "play a soft tone where something was cut" },
  { key: "output.artwork_badge", type: "check", text: "put an 'AD-FREE' badge on the artwork" },
];
const RULES_MORE = [
  { key: "ad_removal.learn_ads", type: "check", text: "recognise ad reads cut before (no AI needed)" },
  { key: "ad_removal.complete_tails", type: "check", text: "cut an ad's last lines if they still name the sponsor" },
  { key: "ad_removal.use_publisher_transcript", type: "check", text: "compare with the publisher's transcript, if any" },
  { key: "ad_removal.ad_break_gap_seconds", label: "Join ads with at most", type: "number", min: 0, max: 60, after: "s of talk between" },
  { key: "second_download.mode", label: "Download twice", type: "select", options: [["auto", "for ad-inserting hosts"], ["on", "every episode"], ["off", "never"]], after: "to spot inserted ads" },
  { key: "review.enabled", label: "Hold for review", type: "check", text: "when the AI is only somewhat sure" },
  { key: "review.min_confidence", label: "…from", type: "number", step: 0.05, min: 0.1, max: 0.9, after: "sure (less sure than 'Cut only when')" },
];
async function loadRules(first) {
  if (!first && $("#view-rules").contains(document.activeElement)) return;     // don't undo what you're typing
  const s = await settings(true);
  const mode = s.second_download?.mode;
  const vals = structuredClone(s);
  vals.second_download.mode = mode === true ? "on" : mode === false ? "off" : mode === "always" ? "on" : mode === "never" ? "off" : (mode || "auto");
  $("#rules-text").value = s.ollama?.extra_instructions || "";
  buildForm($("#rules-cut"), RULES_CUT, vals);
  buildForm($("#rules-detect"), RULES_DETECT, vals);
  buildForm($("#rules-more"), RULES_MORE, vals);
}
$("#rules-example").onclick = () => { const t = $("#rules-text"); t.value = (t.value.trim() ? t.value.trim() + "\n" : "") + RULES_EXAMPLE; t.focus(); };
$("#rules-undo").onclick = () => loadRules(true);
$("#rules-save").onclick = async () => {
  let ch;
  try { ch = { ...readForm($("#rules-cut"), RULES_CUT), ...readForm($("#rules-detect"), RULES_DETECT), ...readForm($("#rules-more"), RULES_MORE) }; }
  catch (e) { return toast("Not saved", e.message); }
  const mn = ch["ad_removal.min_ad_seconds"], mx = ch["ad_removal.max_ad_seconds"];
  if (mn < 0 || ch["ad_removal.padding_seconds"] < 0 || mx <= mn) return toast("Not saved", "'Longer than' must be bigger than 'shorter than', and nothing can be negative.");
  ch["ad_removal.min_confidence"] = Math.round(Math.min(1, Math.max(0, ch["ad_removal.min_confidence"])) * 100) / 100;
  ch["review.min_confidence"] = Math.round(Math.min(ch["ad_removal.min_confidence"], ch["review.min_confidence"]) * 100) / 100;
  ch["ollama.extra_instructions"] = $("#rules-text").value.trim();
  await saveSettings(ch, "They apply to the next episode processed.").catch(() => {});
};

/* ================= AI ================= */
let aiData = null, aiModels = {};
async function loadAI(first) {
  if (first || !aiData) {
    aiData = await get("ai");
    renderAIForm(aiData.service);
    loadBench();
  }
  loadPCs();
  if (benchState.running) loadBench();
}
function renderAIForm(svc) {
  const d = aiData, cloud = d.cloud[svc], lms = d.lmstudio;
  const opts = Object.entries(d.services).map(([k, l]) => `<option value="${k}" ${k === svc ? "selected" : ""}>${esc(l)}</option>`).join("");
  const models = (aiModels[svc] || []).map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  let rows = `<label class="k">Service</label><div class="v"><select id="ai-svc">${opts}</select></div>`;
  if (svc === "ollama") {
    rows += `<label class="k">This PC</label><div class="v"><input id="ai-oll-url" value="${esc(d.ollama.url || "")}" style="width:260px"><span class="muted small">Ollama's address on this PC</span></div>
      <label class="k">Model</label><div class="v"><input id="ai-oll-model" value="${esc(d.ollama.model || "")}" list="ai-dl" style="width:260px"><datalist id="ai-dl">${models}</datalist>
      <button class="btn ghost small" id="ai-models" type="button" title="List installed models">↻</button></div>
      <div class="hint">The same model is used on every Ollama PC.</div>`;
  } else if (svc === "lmstudio") {
    const ld = lms.load || {};
    rows += `<label class="k">This PC</label><div class="v"><input id="ai-lms-url" value="${esc(lms.url || "http://localhost:1234")}" style="width:220px">
        <span class="muted small">API token</span><input type="password" id="ai-lms-token" value="${lms.has_token ? "********" : ""}" style="width:120px"></div>
      <div class="hint">LM Studio › Developer › Start server</div>
      <label class="k">Model</label><div class="v"><input id="ai-lms-model" value="${esc(lms.model || "")}" list="ai-dl" style="width:300px"><datalist id="ai-dl">${models}</datalist>
        <button class="btn ghost small" id="ai-models" type="button" title="List LM Studio's models">↻</button></div>
      <label class="k">GPU offload</label><div class="v"><select id="ai-lms-gpu">${["max", "0.75", "0.5", "0.25", "off", "auto"].map((x) => `<option ${String(ld.gpu ?? "max") === x ? "selected" : ""}>${x}</option>`).join("")}</select>
        <span class="muted small">Context</span><input type="number" id="ai-lms-ctx" step="2048" min="2048" value="${esc(ld.context_length ?? 8192)}">
        <label class="check"><input type="checkbox" id="ai-lms-flash" ${ld.flash_attention !== false ? "checked" : ""}> flash attention</label>
        <label class="check"><input type="checkbox" id="ai-lms-kv" ${ld.offload_kv_cache_to_gpu !== false ? "checked" : ""}> cache on GPU</label></div>
      <label class="k">Sections at once</label><div class="v"><input type="number" id="ai-par" min="1" max="16" value="${esc(ld.parallel ?? 4)}"></div>`;
  } else {
    rows += (svc === "compatible" ? `<label class="k">API address</label><div class="v"><input id="ai-url" value="${esc(cloud.base_url)}" style="width:100%" placeholder="https://api.groq.com/openai/v1"></div>` : "")
      + `<label class="k">API key</label><div class="v"><input type="password" id="ai-key" value="${cloud.has_key ? "********" : ""}" style="width:300px"></div>
      <div class="hint">${cloud.env && !cloud.has_key ? `Using ${esc(cloud.env)} from the environment.` : cloud.keys ? `Get one at ${esc(cloud.keys)}` : "Leave blank if the service doesn't need one."}</div>
      <label class="k">Model</label><div class="v"><input id="ai-model" value="${esc(cloud.model)}" list="ai-dl" style="width:300px"><datalist id="ai-dl">${models}</datalist>
        <button class="btn ghost small" id="ai-models" type="button" title="List the models your key can use">↻</button></div>
      <label class="k">Options</label><div class="v"><label class="check"><input type="checkbox" id="ai-fallback" ${d.fallback ? "checked" : ""}> if it fails or runs out of credit, use local instead</label>
        <span class="muted small">Sections at once</span><input type="number" id="ai-par" min="1" max="16" value="${esc(d.parallel)}"></div>`;
  }
  $("#ai-form").innerHTML = rows;
  $("#ai-note").textContent = svc === "ollama" ? "Uses Ollama on this PC and the computers below. Nothing leaves your network."
    : svc === "lmstudio" ? "Runs on your own PCs' GPUs through LM Studio — free, and nothing leaves your network."
    : "Episode transcripts are sent to this service, and it bills you per use; a 2-hour episode is about 40,000 words of input. The key is saved in config.yaml on the PC running Podcast Cleaner.";
  $("#ai-svc").onchange = () => { renderAIForm($("#ai-svc").value); loadPCs(); };
  $("#ai-models").onclick = async () => {
    aiStatus("Getting the model list…");
    try {
      const r = await post("do/ai_models", aiArgs());
      aiModels[svc] = r.models;
      $("#ai-dl").innerHTML = r.models.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
      aiStatus(`${r.models.length} model(s) — click the Model box to pick one`, true);
    } catch (e) { aiStatus(e.message, false); }
  };
}
function aiStatus(text, ok) { const el = $("#ai-status"); el.textContent = text; el.className = `small ${ok === true ? "ok" : ok === false ? "bad" : "muted"}`; }
function aiArgs() {
  const svc = $("#ai-svc").value, v = (id) => $(id) ? $(id).value : undefined;
  if (svc === "ollama") return { service: svc, url: v("#ai-oll-url"), ollama: { url: v("#ai-oll-url"), model: v("#ai-oll-model") } };
  if (svc === "lmstudio") {
    const lm = { url: v("#ai-lms-url"), api_token: v("#ai-lms-token"), model: v("#ai-lms-model"),
      load: { gpu: v("#ai-lms-gpu"), context_length: Number(v("#ai-lms-ctx") || 8192), flash_attention: $("#ai-lms-flash").checked, offload_kv_cache_to_gpu: $("#ai-lms-kv").checked } };
    return { service: svc, ...lm, lmstudio: lm, parallel: Number(v("#ai-par") || 4) };
  }
  return { service: svc, api_key: v("#ai-key"), model: v("#ai-model"), base_url: v("#ai-url"), fallback: $("#ai-fallback").checked, parallel: Number(v("#ai-par") || 4) };
}
$("#ai-save").onclick = async () => {
  try { await post("do/ai_save", aiArgs()); aiStatus("Saved — used from the next episode", true); aiData = await get("ai"); settingsCache = null; }
  catch (e) { aiStatus(e.message, false); }
};
$("#ai-test").onclick = async () => {
  aiStatus($("#ai-svc").value === "lmstudio" ? "Testing (loads the model if needed — can take a minute)…" : "Testing…");
  try { const r = await post("do/ai_test", aiArgs()); aiStatus(r.message, r.ok); loadPCs(); }
  catch (e) { aiStatus(e.message, false); }
};
const PC_TEXT = {
  ollama: ["Sections are shared out among these PCs, fastest first. On each other PC: install Ollama and run  ollama pull <the same model>,  set OLLAMA_HOST=0.0.0.0 and restart Ollama, and allow TCP port 11434 in Windows Firewall (home network only).",
    "This PC's Ollama — when_idle: only while Whisper isn't transcribing (they share the CPU). always: if this PC has a GPU. never: only if no other PC is reachable."],
  lmstudio: ["Sections are shared out among these PCs, several at once on each. On each other PC: in LM Studio download the model, then Developer › Start server with 'Serve on Local Network' on, and allow TCP port 1234 in Windows Firewall (home network only).",
    "This PC's LM Studio — always: works while Whisper transcribes. when_idle: if GPU memory runs short. never: only if no other PC is reachable."],
};
let pcTimer = null;
async function loadPCs() {
  const svc = $("#ai-svc") ? $("#ai-svc").value : "ollama";
  const card = $("#pcs").closest(".card");
  if (svc !== "ollama" && svc !== "lmstudio") { card.hidden = true; return; }
  card.hidden = false;
  const r = await get(`machines?kind=${svc}`);
  $("#pcs-title").textContent = `Computers running ${r.name}`;
  [$("#pcs-desc").textContent, $("#pcs-hint").textContent] = PC_TEXT[r.kind];
  $("#pc-addr").placeholder = `Address, e.g. 192.168.1.20 (port ${r.port})`;
  $("#pc-this").value = r.this_pc;
  $("#pcs").innerHTML = r.machines.map((m) => `<li><div class="grow"><div class="t">${esc(m.name)} <span class="muted small">${esc(m.url)}</span></div>
    <div class="s">${esc(m.paused ? "paused" : m.state || "")}${m.done != null ? ` · ${m.done} sections${m.failed ? ` (${m.failed} failed)` : ""}` : ""}${m.avg ? ` · ${clock(m.avg)} each` : ""}</div></div>
    <button class="btn small secondary" data-pause="${esc(m.url)}" data-p="${m.paused ? 1 : 0}">${m.paused ? "Resume" : "Pause"}</button>
    ${m.this ? "" : `<button class="btn small ghost" data-rm="${esc(m.url)}" aria-label="Remove">✕</button>`}</li>`).join("");
  clearTimeout(pcTimer);
  if (r.machines.some((m) => (m.state || "").startsWith("checking")) && view === "ai") pcTimer = setTimeout(loadPCs, 1500);
}
$("#pcs").onclick = async (e) => {
  const p = e.target.closest("button[data-pause]"), rm = e.target.closest("button[data-rm]");
  if (p) { await act("set_computer_paused", { url: p.dataset.pause, paused: p.dataset.p !== "1" }); loadPCs(); }
  if (rm && confirm("Remove this PC?")) { await post("do/machine_remove", { url: rm.dataset.rm }).catch((err) => toast("Didn't work", err.message)); loadPCs(); }
};
$("#pc-add").onclick = async () => {
  try { await post("do/machine_add", { address: $("#pc-addr").value, name: $("#pc-name").value }); $("#pc-addr").value = $("#pc-name").value = ""; loadPCs(); }
  catch (e) { toast("Not added", e.message); }
};
$("#pc-test").onclick = async () => { await post("do/machines_test", {}); loadPCs(); };
$("#pc-this").onchange = () => post("do/this_pc", { value: $("#pc-this").value }).then(() => toast("Saved", "")).catch((e) => toast("Not saved", e.message));

/* model test */
let benchState = { running: false }, benchLists = null, benchTimer = null;
async function loadBench() {
  const b = await get(`bench${benchLists ? "" : "?lists=1"}`);
  if (!benchLists) {
    benchLists = b;
    $("#bench-eps").innerHTML = b.episodes.length ? b.episodes.map((e, i) => `<li><label class="check"><input type="checkbox" data-e="${i}" ${i < 3 ? "checked" : ""}>
      <span>${esc(e.podcast)} — ${esc(e.episode)} <span class="muted small">${clock(e.duration)} · ${e.ads} ads${e.corrected ? " · corrected" : ""}</span></span></label></li>`).join("")
      : `<li class="empty">No episodes yet: the transcript this test needs is saved for episodes processed from now on.</li>`;
    let first = true;
    $("#bench-setups").innerHTML = b.setups.map((s) => {
      const on = s.current && first; if (on) first = false;
      return `<li><label class="check"><input type="checkbox" data-s="${esc(s.key)}" ${on ? "checked" : ""}> ${esc(s.name)}</label><span class="muted small" data-st="${esc(s.key)}"></span></li>`;
    }).join("");
  }
  benchState = b;
  $("#bench-run").disabled = !!b.running;
  $("#bench-stop").hidden = !b.running;
  $("#bench-bar").hidden = b.total == null;
  if (b.total != null) {
    const pct = b.total ? (100 * b.done) / b.total : 0;
    $("#bench-bar").firstElementChild.style.width = `${b.running ? pct : b.stopped ? pct : 100}%`;
    $("#bench-progress").textContent = b.running ? `${Math.round(pct)}% — ${b.done} of ${b.total} sections${b.eta != null ? ` — about ${clock(b.eta)} left` : ""}`
      : `${b.stopped ? "Stopped" : "Done"} after ${clock(b.elapsed)}.`;
    for (const [k, v] of Object.entries(b.status || {})) { const el = document.querySelector(`[data-st="${CSS.escape(k)}"]`); if (el) el.textContent = v; }
  }
  $("#bench-report").hidden = !b.report;
  $("#bench-report").textContent = b.report || "";
  clearTimeout(benchTimer);
  if (b.running) benchTimer = setTimeout(loadBench, 1000);
}
$("#bench-run").onclick = async () => {
  const episodes = [...document.querySelectorAll("#bench-eps input:checked")].map((c) => +c.dataset.e);
  const setups = [...document.querySelectorAll("#bench-setups input:checked")].map((c) => c.dataset.s);
  try { await post("do/bench_start", { episodes, setups }); loadBench(); } catch (e) { toast("Can't start", e.message); }
};
$("#bench-stop").onclick = () => post("do/bench_stop", {}).then(loadBench);

/* ================= Transcription ================= */
const WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo", "distil-small.en", "distil-medium.en", "distil-large-v3", "distil-large-v3.5", "small-q5_1", "medium-q5_0", "large-v3-turbo-q5_0"];
const TR = {
  whisper: [
    { key: "whisper.engine", label: "Engine", type: "select", options: [["faster-whisper", "faster-whisper"], ["whisper.cpp", "whisper.cpp"], ["openai-whisper", "openai-whisper"]], hint: "whisper.cpp: uses AMD/Intel graphics (run build-whisper-vulkan.ps1 once)" },
    { key: "whisper.model", label: "Model", datalist: WHISPER_MODELS, hint: "small / distil-*: CPU.  turbo, *-q5_0: GPU" },
    { key: "whisper.device", label: "Device", type: "select", options: [["auto", "auto"], ["cpu", "cpu"], ["cuda", "cuda"]] },
    { key: "whisper.beam_size", label: "Speed", type: "select", options: [["1", "1 (fastest)"], ["2", "2"], ["5", "5 (most careful)"]] },
    { key: "whisper.batch_size", label: "Batch", type: "select", options: [["0", "off"], ["4", "4"], ["8", "8 (~2x faster)"], ["16", "16"]], hint: "Bigger batches are faster but use more memory." },
  ],
};
async function loadTranscription(first) {
  if (first) {
    const s = await settings(true);
    buildForm($("#tr-whisper"), TR.whisper, s);
  }
  const r = await get("transcribers");
  $("#tr-this").value = r.this_pc;
  $("#trs").innerHTML = r.machines.map((m) => `<li><div class="grow">
      <div class="t">${esc(m.name)} <span class="muted small">${m.this ? "" : esc(m.url)} ${m.this ? "" : "· " + esc(r.kinds[m.kind] || m.kind)}</span></div>
      <div class="s">${esc(m.paused ? "paused" : m.state || (m.this ? "uses the settings above" : ""))}${m.done ? ` · ${m.done} episode(s)` : ""}${m.failed ? ` (${m.failed} failed)` : ""}${m.speed ? ` · ${m.speed}× real time` : ""}${m.model_used ? ` · model ${esc(m.model_used)}` : ""}</div></div>
    <button class="btn small secondary" data-pause="${esc(m.url)}" data-p="${m.paused ? 1 : 0}">${m.paused ? "Resume" : "Pause"}</button>
    ${m.this ? "" : `<button class="btn small ghost" data-rm="${esc(m.url)}" aria-label="Remove">✕</button>`}</li>`).join("");
  clearTimeout(trTimer);
  if (view === "transcription" && r.machines.some((m) => (m.state || "").startsWith("checking"))) trTimer = setTimeout(() => loadTranscription(false), 1500);
}
let trTimer = null;
$("#tr-kind").onchange = () => {
  const cpp = $("#tr-kind").value === "whispercpp";
  $("#tr-model").hidden = cpp;
  $("#tr-addr").placeholder = `Address, e.g. 192.168.1.20 (port ${cpp ? 8080 : 8000})`;
};
$("#tr-kind").onchange();
$("#trs").onclick = async (e) => {
  const p = e.target.closest("button[data-pause]"), rm = e.target.closest("button[data-rm]");
  if (p) { await act("set_computer_paused", { url: p.dataset.pause, paused: p.dataset.p !== "1" }); loadTranscription(false); }
  if (rm && confirm("Remove this PC?")) { await post("do/transcriber_remove", { url: rm.dataset.rm }).catch((err) => toast("Didn't work", err.message)); loadTranscription(false); }
};
$("#tr-add").onclick = async () => {
  try {
    await post("do/transcriber_add", { kind: $("#tr-kind").value, address: $("#tr-addr").value, name: $("#tr-name").value, model: $("#tr-model").value });
    $("#tr-addr").value = $("#tr-name").value = $("#tr-model").value = "";
    loadTranscription(false);
  } catch (e) { toast("Not added", e.message); }
};
$("#tr-test").onclick = async () => { await post("do/transcribers_test", {}); loadTranscription(false); };
$("#tr-this").onchange = () => post("do/transcriber_this_pc", { value: $("#tr-this").value }).then(() => toast("Saved", "")).catch((e) => toast("Not saved", e.message));
$("#tr-undo").onclick = () => loadTranscription(true);
$("#tr-save").onclick = async () => {
  let ch;
  try { ch = readForm($("#tr-whisper"), TR.whisper); } catch (e) { return toast("Not saved", e.message); }
  for (const k of ["whisper.beam_size", "whisper.batch_size"]) ch[k] = Number(ch[k]);
  await saveSettings(ch).catch(() => {});
};

/* ================= Settings ================= */
const SET = {
  general: [
    { key: "output_dir", label: "Save podcasts to", width: "100%" },
    { key: "check_interval_minutes", label: "Check every", type: "number", min: 5, max: 1440, after: "minutes" },
    { key: "notifications", label: "Notifications", type: "check", text: "show a message when episodes finish" },
  ],
  other: [
    { key: "output.crossfade_ms", label: "Crossfade at cuts", type: "number", min: 0, max: 500, after: "milliseconds, 0 = hard cut" },
    { key: "output.archive_transcripts", label: "Transcripts", type: "check", text: "keep one for every episode" },
    { key: "processing.overlap_stages", label: "Overlap", type: "check", text: "transcribe the next episode meanwhile" },
    { key: "output.normalize.enabled", label: "Volume", type: "check", text: "even out loudness (podcast standard, -16 LUFS)" },
  ],
  feeds: [
    { key: "server.enabled", label: "Feed server", type: "check", text: "serve cleaned episodes as RSS" },
    { key: "server.port", label: "Port", type: "number", min: 1024, max: 65535 },
    { key: "server.token", label: "Secret token", type: "password", hint: "optional; restart the app after changes here" },
  ],
  low: [
    { key: "processing.low_priority_hours.enabled", label: "", type: "check", text: "only process Low-priority shows between" },
    { key: "processing.low_priority_hours.start", label: "From", type: "time" },
    { key: "processing.low_priority_hours.end", label: "To", type: "time", hint: "Up next › Start now overrides it." },
  ],
  phone: [
    { key: "phone_notifications.ntfy_topic", label: "Topic", placeholder: "e.g. podcasts-k7q2x9" },
    { key: "phone_notifications.ntfy_server", label: "Server", hint: "only change if you run your own ntfy" },
    { key: "phone_notifications.episodes", label: "Tell me", type: "check", text: "when an episode is ready" },
    { key: "phone_notifications.held", type: "check", text: "when something is held for review" },
    { key: "phone_notifications.problems", type: "check", text: "about problems (failed episodes, failing feeds)" },
  ],
};
async function loadSettings(first) {
  if (!first && $("#view-settings").contains(document.activeElement)) return;
  const s = await settings(true);
  for (const [k, fields] of Object.entries(SET)) buildForm($(`#set-${k}`), fields, s);
  if (system.local && system.windows) {
    $("#set-general").insertAdjacentHTML("beforeend", `<label class="k">Windows startup</label><div class="v"><label class="check"><input type="checkbox" id="set-startup" ${system.startup ? "checked" : ""}> start with Windows, in the tray</label></div>`);
  }
  if (desktop()) {
    const v = $('#set-general [data-k="output_dir"]').parentElement;
    v.insertAdjacentHTML("beforeend", `<button class="btn secondary small" type="button" id="set-browse">Browse…</button>`);
    $("#set-browse").onclick = async () => { const d = await window.pywebview.api.pick_folder(); if (d) $('#set-general [data-k="output_dir"]').value = d; };
  }
}
$("#set-undo").onclick = () => loadSettings(true);
$("#set-config").onclick = () => openOnPC("config");
$("#set-save").onclick = async () => {
  let ch = {};
  try { for (const [k, fields] of Object.entries(SET)) Object.assign(ch, readForm($(`#set-${k}`), fields)); }
  catch (e) { return toast("Not saved", e.message); }
  for (const k of ["processing.low_priority_hours.start", "processing.low_priority_hours.end"]) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(ch[k] || "");
    if (!m || +m[1] > 23 || +m[2] > 59) return toast("Not saved", "Low Priority Hours: enter times like 01:00 and 06:00.");
    ch[k] = `${m[1].padStart(2, "0")}:${m[2]}`;
  }
  if (ch["server.token"] === "********") delete ch["server.token"];
  if (!ch["phone_notifications.ntfy_server"]) ch["phone_notifications.ntfy_server"] = "https://ntfy.sh";
  await saveSettings(ch).catch(() => {});
  if ($("#set-startup") && $("#set-startup").checked !== system.startup) {
    await post("do/startup", { on: $("#set-startup").checked }).catch((e) => toast("Windows startup not changed", e.message));
    system.startup = $("#set-startup").checked;
  }
};
$("#phone-test").onclick = async () => {
  $("#phone-status").textContent = "Sending…";
  try {
    const r = await post("do/phone_test", { topic: $('[data-k="phone_notifications.ntfy_topic"]').value, server: $('[data-k="phone_notifications.ntfy_server"]').value });
    $("#phone-status").textContent = r.message;
    $("#phone-status").className = `small ${r.ok ? "ok" : "bad"}`;
  } catch (e) { $("#phone-status").textContent = e.message; $("#phone-status").className = "small bad"; }
};

Object.assign(loaders, {
  choose: () => loadChoose().catch(() => {}),
  browse: (first) => loadBrowse(first, false).catch(() => {}),
  rules: (first) => first && loadRules(true).catch(() => {}),
  ai: (first) => loadAI(first).catch((e) => aiStatus(e.message, false)),
  settings: (first) => first && loadSettings(true).catch(() => {}),
  transcription: (first) => loadTranscription(first).catch(() => {}),
});
/* the Choose Episodes count in the tab, whatever page you're on */
function chooseCount() {
  if (document.hidden || view === "choose") return;
  get("choose").then((d) => {
    const n = d.reduce((t, s) => t + s.episodes.filter((e) => e.status !== "queued").length, 0);
    $("#choose-count").textContent = n || "";
  }).catch(() => {});
}
window.addEventListener("load", () => setTimeout(chooseCount, 1500));
setInterval(chooseCount, 15000);
