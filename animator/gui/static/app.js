"use strict";

const $ = (id) => document.getElementById(id);
const S = { presets: {}, preset: "middle", keySet: false, job: null, es: null, logLines: [], selected: null, timer: null, voicesFor: null };
const STEP_ORDER = ["storyboard", "scenes", "render", "done"];

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (!r.ok) throw new Error((data && data.detail && (typeof data.detail === "string" ? data.detail : "Invalid input")) || `Request failed (${r.status})`);
  return data;
}

const money = (n) => "$" + n.toFixed(n < 1 ? 3 : 2).replace(/0$/, "");
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const running = () => S.job && S.job.status === "running";

// ---------- form ----------

function fillScenes() {
  const sel = $("scenes");
  sel.innerHTML = '<option value="">Auto (3-6)</option>' + [1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option value="${n}">${n}</option>`).join("");
}

function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  for (const [key, p] of Object.entries(S.presets)) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = p.label; b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(key === S.preset));
    b.onclick = () => selectPreset(key);
    box.appendChild(b);
  }
}

function selectPreset(key) {
  S.preset = key;
  const p = S.presets[key];
  if (key !== "custom") {
    for (const f of ["text", "region", "image", "tts"]) $("m-" + f).value = p.models[f];
    $("voice").value = p.voice;
    loadVoices();
  } else {
    $("advanced").open = true;
  }
  renderPresets(); updateCost(); updateGenerate();
}

function updateCost() {
  const p = S.presets[S.preset];
  const el = $("cost");
  if (!p || p.per_scene == null) { el.textContent = "Cost depends on the models you pick."; return; }
  const n = $("scenes").value;
  el.textContent = n
    ? `Rough estimate: ${money(p.per_scene * n)} for ${n} scene${n > 1 ? "s" : ""}`
    : `Rough estimate: ${money(p.per_scene * 3)} - ${money(p.per_scene * 6)} (3-6 scenes)`;
}

async function loadVoices() {
  const model = $("m-tts").value.trim();
  if (!model || model === S.voicesFor) return;
  S.voicesFor = model;
  try {
    const { voices } = await api("/api/voices?model=" + encodeURIComponent(model));
    if (S.voicesFor !== model) return;
    $("voice-list").innerHTML = voices.map((v) => `<option value="${v.replace(/"/g, "&quot;")}">`).join("");
  } catch (_) { $("voice-list").innerHTML = ""; }
}

function updateGenerate() {
  let reason = "";
  if (!S.keySet) reason = "Add your OpenRouter key in settings first.";
  else if (running()) reason = "A video is being generated.";
  else if (!$("prompt").value.trim()) reason = "Describe what the video should explain.";
  else if (["m-text", "m-region", "m-image", "m-tts", "voice"].some((id) => !$(id).value.trim())) reason = "Fill in every model and the voice.";
  $("generate").disabled = !!reason;
  $("reason").textContent = reason;
}

async function submit(ev) {
  ev.preventDefault();
  if ($("generate").disabled) return;
  showError("form-error", "");
  const body = {
    prompt: $("prompt").value.trim(),
    scenes: $("scenes").value ? Number($("scenes").value) : null,
    quality: $("quality").value, preset: S.preset,
    models: { text: $("m-text").value.trim(), region: $("m-region").value.trim(), image: $("m-image").value.trim(), tts: $("m-tts").value.trim() },
    voice: $("voice").value.trim(),
  };
  try {
    S.job = await api("/api/jobs", { method: "POST", body });
    attachJob(true);
  } catch (e) { showError("form-error", e.message); }
}

function showError(id, msg) { const el = $(id); el.textContent = msg; el.hidden = !msg; }

// ---------- job progress ----------

function attachJob(fresh) {
  S.logLines = []; $("log").textContent = "";
  $("result").hidden = true;
  if (fresh) $("log-wrap").open = false;
  renderJob();
  if (S.es) S.es.close();
  const es = (S.es = new EventSource("/api/jobs/current/events"));
  es.addEventListener("log", (e) => {
    const d = JSON.parse(e.data);
    if (d.start === 0) S.logLines = [];
    S.logLines.push(...d.lines);
    const log = $("log"), stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = S.logLines.join("\n");
    if (stick) log.scrollTop = log.scrollHeight;
  });
  es.addEventListener("state", (e) => { S.job = JSON.parse(e.data); renderJob(); });
  es.addEventListener("end", (e) => { es.close(); S.es = null; S.job = JSON.parse(e.data); renderJob(); jobFinished(); });
}

function renderJob() {
  const j = S.job;
  $("progress").hidden = !j || (j.status === "done");
  updateGenerate();
  if (!j) return;
  const idx = STEP_ORDER.indexOf(j.step);
  document.querySelectorAll("#steps li").forEach((li, i) => {
    li.classList.toggle("done", i < idx || j.step === "done");
    li.classList.toggle("active", i === idx && j.status === "running");
  });
  $("scene-count").textContent = j.total ? `${j.ready}/${j.total}` : "";
  let pct = 3;
  if (j.step === "scenes") pct = 10 + 70 * (j.total ? j.ready / j.total : 0);
  else if (j.step === "render") pct = 90;
  else if (j.step === "done") pct = 100;
  $("bar-fill").style.width = pct + "%";
  $("progress").querySelector(".bar").classList.toggle("failed", j.status === "failed");
  const titles = { running: "Making your video…", failed: "Something went wrong", cancelled: "Cancelled", done: "Done" };
  $("progress-title").textContent = titles[j.status] || "";
  $("cancel").hidden = j.status !== "running";
  showError("job-error", j.status === "failed" ? (j.error || "Failed") : "");
  tickElapsed();
}

function tickElapsed() {
  clearInterval(S.timer);
  const j = S.job;
  if (!j) return;
  const draw = () => { $("elapsed").textContent = fmtTime(((j.finished || Date.now() / 1000) - j.started)); };
  draw();
  if (j.status === "running") S.timer = setInterval(draw, 1000);
}

async function jobFinished() {
  await loadHistory();
  loadBalance(true);
  if (S.job && S.job.status === "done") showVideo(S.job.id);
}

async function cancelJob() {
  try { await api("/api/jobs/current/cancel", { method: "POST" }); } catch (e) { showError("job-error", e.message); }
}

// ---------- result + history ----------

function showVideo(id) {
  S.selected = id;
  const item = (S.history || []).find((h) => h.id === id);
  $("result").hidden = false;
  $("result-title").textContent = item ? item.prompt : "Result";
  const player = $("player");
  player.src = `/api/history/${id}/video`;
  player.load();
  $("thumbs").innerHTML = "";
  for (let i = 0; i < (item ? item.images : 0); i++) {
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = `Scene ${i + 1}`; img.src = `/api/history/${id}/image/${i}`;
    $("thumbs").appendChild(img);
  }
  markSelected();
  $("result").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function markSelected() {
  document.querySelectorAll(".item").forEach((el) => el.classList.toggle("selected", el.dataset.id === S.selected));
}

async function loadHistory() {
  try { S.history = await api("/api/history"); } catch (_) { S.history = []; }
  const box = $("history");
  box.innerHTML = "";
  $("history-empty").hidden = S.history.length > 0;
  for (const h of S.history) box.appendChild(historyItem(h));
  markSelected();
}

function historyItem(h) {
  const el = document.createElement("div");
  el.className = "item"; el.dataset.id = h.id; el.tabIndex = 0; el.setAttribute("role", "button");
  const thumb = document.createElement(h.images ? "img" : "div");
  thumb.className = "thumb";
  if (h.images) { thumb.src = `/api/history/${h.id}/image/0`; thumb.loading = "lazy"; thumb.alt = ""; }
  const info = document.createElement("div");
  const title = document.createElement("div");
  title.className = "title"; title.textContent = h.prompt;
  const meta = document.createElement("div");
  meta.className = "meta";
  const badge = `<span class="badge ${h.status}">${h.status}</span>`;
  const when = h.started ? new Date(h.started).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  const dur = h.duration ? ` · ${fmtTime(h.duration)}` : "";
  meta.innerHTML = `${badge}${when}${dur} · ${h.preset}`;
  if (h.status === "failed" && h.error) { meta.title = h.error; }
  info.append(title, meta);
  const del = document.createElement("button");
  del.className = "del"; del.type = "button"; del.textContent = "×"; del.setAttribute("aria-label", "Delete this video"); del.title = "Delete";
  del.onclick = (e) => { e.stopPropagation(); deleteItem(h); };
  el.append(thumb, info, del);
  const open = () => { if (h.has_video) showVideo(h.id); else if (h.error) alert(h.error); };
  el.onclick = open;
  el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  return el;
}

async function deleteItem(h) {
  if (!confirm(`Delete "${h.prompt}" and its files?`)) return;
  try {
    await api("/api/history/" + h.id, { method: "DELETE" });
    if (S.selected === h.id) { $("result").hidden = true; $("player").removeAttribute("src"); S.selected = null; }
    loadHistory();
  } catch (e) { alert(e.message); }
}

async function openFolder() {
  if (S.selected) try { await api(`/api/history/${S.selected}/open`, { method: "POST" }); } catch (e) { alert(e.message); }
}

// ---------- balance + settings ----------

async function loadBalance(force) {
  const pill = $("balance");
  if (!S.keySet) { pill.textContent = "no API key"; return; }
  pill.textContent = "balance…";
  try {
    const b = await api("/api/balance");
    pill.textContent = b.remaining == null ? `$${b.used.toFixed(2)} used` : `${money(b.remaining)} left`;
  } catch (e) { pill.textContent = "balance unavailable"; pill.title = e.message; }
}

function renderKeyStatus(key) {
  S.keySet = key.set;
  $("key-status").textContent = key.set ? `Key set ${key.tail}` : "No key set. Get one at openrouter.ai/keys";
  updateGenerate();
}

async function saveKey(ev) {
  ev.preventDefault();
  showError("key-error", "");
  try {
    const key = await api("/api/settings/key", { method: "POST", body: { key: $("key-input").value } });
    $("key-input").value = "";
    renderKeyStatus(key);
    loadBalance(true);
    $("settings").close();
  } catch (e) { showError("key-error", e.message); }
}

// ---------- init ----------

async function init() {
  fillScenes();
  const st = await api("/api/state");
  S.presets = st.presets;
  renderKeyStatus(st.key);
  selectPreset(st.default_preset);
  S.job = st.job;
  loadBalance();
  await loadHistory();
  if (running()) attachJob(false);
  else if (S.job) renderJob();
  if (!st.key.set) $("settings").showModal();
}

$("form").addEventListener("submit", submit);
$("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("form").requestSubmit(); });
$("prompt").addEventListener("input", updateGenerate);
$("scenes").addEventListener("change", updateCost);
for (const f of ["text", "region", "image", "tts"]) {
  $("m-" + f).addEventListener("input", () => {
    if (S.preset !== "custom") { S.preset = "custom"; renderPresets(); updateCost(); }
    updateGenerate();
  });
}
$("m-tts").addEventListener("change", loadVoices);
$("voice").addEventListener("input", () => {
  if (S.preset !== "custom" && $("voice").value !== S.presets[S.preset].voice) { S.preset = "custom"; renderPresets(); updateCost(); }
  updateGenerate();
});
$("cancel").addEventListener("click", cancelJob);
$("open-folder").addEventListener("click", openFolder);
$("balance").addEventListener("click", () => loadBalance(true));
$("settings-btn").addEventListener("click", () => { showError("key-error", ""); $("settings").showModal(); });
$("key-close").addEventListener("click", () => $("settings").close());
$("key-form").addEventListener("submit", saveKey);

init().catch((e) => showError("form-error", e.message));
