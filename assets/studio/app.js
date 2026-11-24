"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {
  token: "",
  config: null,
  id: null,
  liveId: null,
  detail: null,
  words: [],
  page: 0,
  candidatePage: 0,
  busy: false,
  tab: "clips",
  selected: new Set(),
  noticeTimer: null,
  captionDraft: null,
  captionKey: "",
  captionSerial: 0,
  captionPreset: "default",
  previewFile: null,
  pendingPreview: null,
};
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const basename = (source) =>
  source.split(/[\\/]/).filter(Boolean).pop() || source;
const projectName = (source) =>
  /^https?:/.test(source)
    ? source.replace(/^https?:\/\//, "").slice(0, 65)
    : basename(source);
let studioNavigation = 0;
let projectRequest = 0;
function switchView(id) {
  studioNavigation += 1;
  const alias = typeof settingsViews !== "undefined" ? settingsViews[id] : null;
  const top = alias ? "settings-view" : id;
  for (const view of document.querySelectorAll("main > section.view")) view.hidden = view.id !== top;
  if (alias) settingsDisplay(alias,id);
  if (typeof recordStudioRoute === "function") recordStudioRoute(id);
  window.dispatchEvent(new CustomEvent("studio-view", {detail: id}));
}
function notice(message, error = false) {
  clearTimeout(state.noticeTimer);
  const box = $("#notice");
  box.hidden = false;
  box.textContent = message;
  box.classList.toggle("error", error);
  state.noticeTimer = setTimeout(
    () => {
      box.hidden = true;
    },
    error ? 15000 : 6000,
  );
}
async function api(path, data) {
  const response = await fetch(
    path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Chopify-Token": state.token,
          },
          body: JSON.stringify(data),
        },
  );
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.data = result;
    throw error;
  }
  return result;
}
function safe(handler) {
  return async (event) => {
    try {
      await handler(event);
    } catch (error) {
      notice(error.message, true);
    }
  };
}
async function config() {
  state.config = await api("/api/config");
  state.token = state.config.token;
  updatePresets();
  updateBrands();
  renderCaptionGallery();
}
function presetNames() {
  return Object.keys(state.config.caption_presets || {});
}
function updatePresets() {
  const markup = presetNames()
    .map((name) => `<option>${escapeHTML(name)}</option>`)
    .join("");
  for (const select of [
    $("#new-style"),
    $("#caption-preset"),
    $("#settings-form").elements.style,
  ]) {
    if (!select) continue;
    const current = select.value;
    select.innerHTML = markup;
    if (presetNames().includes(current)) select.value = current;
  }
}
function aspectCaptionBase(aspect) {
  return (
    state.config.caption_aspects[aspect] ||
    state.config.caption_aspects["16:9"] || { font_size: 72, margin: 95 }
  );
}
function brandCaption(name) {
  return (state.config.caption_brands || {})[name] || {};
}
function savedCaptionStyle() {
  const options = state.detail.project.options;
  const brand = options.brand
    ? basename(options.brand).replace(/\.json$/, "")
    : "";
  const base = aspectCaptionBase(options.aspect);
  const merged = {
    ...state.config.caption_defaults,
    ...(state.config.caption_presets[options.style] || {}),
    ...brandCaption(brand),
    ...(options.caption || {}),
  };
  if (merged.font_size == null) merged.font_size = base.font_size;
  if (merged.margin == null) merged.margin = base.margin;
  return merged;
}
function captionFromForm() {
  const form = $("#caption-form");
  const value = (name) => form.elements[name].value;
  return {
    font: value("font").trim() || "Arial Black",
    font_size: Number(value("font_size")),
    color: value("color"),
    highlight: value("highlight"),
    emphasis_color: value("emphasis_color"),
    outline_color: value("outline_color"),
    uppercase: form.elements.uppercase.checked,
    words_per_cue: Number(value("words_per_cue")),
    cue_min_ms: Number(value("cue_min_ms")),
    cue_max_ms: Number(value("cue_max_ms")),
    cue_gap_ms: Number(value("cue_gap_ms")),
    cue_lines: Number(value("cue_lines")),
    position: value("position"),
    margin: Number(value("margin")),
    outline: Number(value("outline")),
    shadow: Number(value("shadow")),
    background: value("background"),
    background_color: value("background_color"),
    background_opacity: Number(value("background_opacity")),
    animation: value("animation"),
    highlight_mode: value("highlight_mode"),
    emoji: form.elements.emoji.checked,
    emphasis: form.elements.emphasis.checked,
  };
}
function fillCaptionForm(style, preset) {
  const form = $("#caption-form");
  for (const [key, value] of Object.entries(style)) {
    const input = form.elements.namedItem(key);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!value;
    else input.value = value ?? "";
  }
  $("#caption-preset").value = preset;
  state.captionDraft = captionFromForm();
  state.captionSerial++;
  state.captionKey = "";
  updateCaptionSummary();
  updateCaptionOverlay();
}
function syncCaption() {
  if (!state.detail) return;
  state.captionDraft = captionFromForm();
  state.captionSerial++;
  state.captionKey = "";
  updateCaptionSummary();
  updateCaptionOverlay();
}
function updateCaptionSummary() {
  const style = state.captionDraft;
  if (!style) return;
  $("#caption-summary").textContent =
    `${style.words_per_cue} words · ${style.position} · ` +
    `${style.animation === "none" ? "no motion" : style.animation}`;
}
function renderCaptionGallery() {
  const wrap = $("#caption-presets");
  if (!wrap) return;
  wrap.innerHTML = presetNames()
    .map(
      (name) =>
        `<button type="button" class="caption-preset" data-caption-preset="${escapeHTML(name)}" aria-pressed="false">${escapeHTML(name)}</button>`,
    )
    .join("");
  for (const button of wrap.querySelectorAll("[data-caption-preset]")) {
    const preset = state.config.caption_presets[button.dataset.captionPreset];
    button.style.setProperty("--preset-font", preset.font);
    button.style.setProperty("--preset-color", preset.color);
    button.style.setProperty("--preset-highlight", preset.highlight);
  }
  markCaptionPreset(state.captionPreset);
}
function markCaptionPreset(name) {
  state.captionPreset = name;
  for (const button of document.querySelectorAll("[data-caption-preset]"))
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.captionPreset === name),
    );
}
function applyCaptionPreset(name) {
  if (!state.detail || !state.config.caption_presets[name]) return;
  const base = aspectCaptionBase(state.detail.project.options.aspect);
  const style = { ...state.config.caption_defaults, ...state.config.caption_presets[name] };
  if (style.font_size == null) style.font_size = base.font_size;
  if (style.margin == null) style.margin = base.margin;
  fillCaptionForm(style, name);
  $("#settings-form").elements.style.value = name;
  markCaptionPreset(name);
}
function hexToRgba(hex, opacity) {
  const match = /^#([0-9a-f]{6})$/i.exec(String(hex));
  if (!match) return "rgba(0, 0, 0, 0.7)";
  const value = parseInt(match[1], 16);
  const alpha = Math.max(0, Math.min(100, Number(opacity) || 0)) / 100;
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}
function isEmphasizedWord(text) {
  const plain = String(text)
    .trim()
    .replace(/^[^\w$€£¥]+|[^\w%$€£¥]+$/g, "");
  if (!plain) return false;
  if (/[\d$€£¥]/.test(plain)) return true;
  if (plain.length > 2 && plain === plain.toUpperCase() && /[A-Z]/.test(plain))
    return true;
  return state.config.caption_power_words.includes(
    plain.toLowerCase().replace(/[.,!?;:'"]+$/, ""),
  );
}
function emojiForWord(text) {
  const token = String(text)
    .toLowerCase()
    .replace(/[^a-z0-9']/g, "");
  if (!token) return "";
  for (const rule of state.config.caption_emoji_rules || [])
    if (rule.words.some((stem) => token.startsWith(stem))) return rule.emoji;
  return "";
}
function updateCaptionOverlay(force = false) {
  const overlay = $("#caption-overlay"),
    player = $("#source-player"),
    style = state.captionDraft;
  if (
    !state.detail ||
    !state.detail.transcript ||
    state.detail.playable_source === false ||
    state.previewFile ||
    state.tab !== "captions" ||
    !style
  ) {
    overlay.hidden = true;
    return;
  }
  const words = state.words.filter((item) => !item.hidden);
  const per = Math.max(1, Math.min(8, Number(style.words_per_cue) || 3));
  const current = player.currentTime || 0;
  let cue = null,
    cueIndex = -1;
  const minimum = style.cue_min_ms / 1000;
  const maximum = style.cue_max_ms / 1000;
  const gap = style.cue_gap_ms / 1000;
  for (let i = 0; i < words.length;) {
    let end = i + 1;
    while (end < Math.min(i + per, words.length)) {
      const previous = words[end - 1], next = words[end];
      if (next.start - previous.end >= gap || next.end - words[i].start > maximum
          || (previous.end - words[i].start >= minimum && /[.!?…]$/.test(previous.word.trim()))) break;
      end++;
    }
    if (current >= words[i].start && current < words[end - 1].end) {
      cue = words.slice(i, end);
      cueIndex = i;
      break;
    }
    if (current < words[i].start) break;
    i = end;
  }
  const active = cue?.findIndex((word, i) => {
    const next = cue[i + 1];
    const end = next && next.start - word.end <= .15 ? next.start : word.end;
    return current >= word.start && current < end;
  }) ?? -1;
  if (active < 0) {
    overlay.hidden = true;
    return;
  }
  const marked = [];
  for (let i = 0; i < cue.length; i++)
    if (cue[i].emphasis === true) marked.push(i);
  if (style.emphasis)
    for (let i = 0; i < cue.length; i++)
      if (!marked.includes(i) && isEmphasizedWord(cue[i].word)) marked.push(i);
  const emphasised = new Set(marked.slice(0, 2));
  let emojiIndex = -1,
    emoji = "";
  if (style.emoji) {
    for (let i = 0; i < cue.length; i++) {
      emoji = emojiForWord(cue[i].word);
      if (emoji) {
        emojiIndex = i;
        break;
      }
    }
  }
  const key = `${cueIndex}:${active}:${state.captionSerial}`;
  if (state.captionKey !== key || force) {
    const parts = [];
    cue.forEach((item, i) => {
      if (style.animation === "typewriter" && i > active) return;
      let token = escapeHTML(
        style.uppercase ? item.word.toUpperCase() : item.word,
      );
      if (i === emojiIndex) token += ` <span class="caption-emoji">${emoji}</span>`;
      let className = "caption-word";
      if (style.highlight_mode !== "none" && i === active)
        className += " is-active";
      else if (style.highlight_mode === "karaoke" && i < active)
        className += " is-karaoke";
      else if (style.highlight_mode !== "none" && emphasised.has(i))
        className += " is-emphasis";
      if (
        i === active &&
        style.animation !== "none" &&
        style.animation !== "typewriter"
      )
        className += ` is-${style.animation}`;
      parts.push(`<span class="${className}">${token}</span>`);
    });
    $("#caption-cue").innerHTML = parts.join(" ");
    state.captionKey = key;
  }
  const wrap = $(".player-wrap");
  const scale = Math.max(0.04, (wrap.clientHeight || 1) / 1080);
  const set = (name, value) => overlay.style.setProperty(name, value);
  overlay.className =
    `caption-overlay caption-${style.position}` +
    (style.background === "box" ? " caption-boxed" : "");
  set("--caption-font", `${style.font}, "Segoe UI Emoji", sans-serif`);
  set("--caption-font-size", `${Math.max(8, style.font_size * scale)}px`);
  set("--caption-margin", `${Math.max(0, style.margin * scale)}px`);
  set("--caption-outline", `${Math.max(0, style.outline * scale)}px`);
  set("--caption-shadow", `${Math.max(0, style.shadow * scale)}px`);
  set("--caption-color", style.color);
  set("--caption-highlight", style.highlight);
  set("--caption-emphasis", style.emphasis_color);
  set("--caption-outline-color", style.outline_color);
  set(
    "--caption-box-bg",
    hexToRgba(style.background_color, style.background_opacity),
  );
  set("--caption-box-pad", `${Math.max(2, style.outline * scale + 2)}px`);
  overlay.hidden = false;
}
function updateBrands(selected = "") {
  $("#brand-select").innerHTML =
    '<option value="">Use caption style</option>' +
    state.config.brands
      .map(
        (name) =>
          `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`,
      )
      .join("");
  $("#brand-select").value = selected;
}
const projectLabels = {
  new: "Nuevo", waiting: "En espera", queued: "En cola", running: "En proceso",
  complete: "Completado", "render recovery": "Alternativa disponible", interrupted: "Interrumpido", failed: "Fallido",
  error: "Error", ready: "Listo", prepare: "Preparación", render: "Exportación",
  "render incomplete": "Exportación parcial", "rendered": "Exportado", review: "En revisión",
};
const projectLabel = value => projectLabels[value] || value;
async function refreshProjects() {
  const result = await api("/api/projects");
  const filter = $("#projects-filter").value;
  const visible = result.projects.filter(project => filter === "all" || Boolean(project.archived) === (filter === "archived"));
  $("#project-count").textContent = `${visible.length} ${visible.length === 1 ? "proyecto" : "proyectos"}`;
  const markup = visible.length
    ? visible
        .map(
          (p) =>
            `<article class="workspace-card project-entry">
              <div><strong>${escapeHTML(p.name || projectName(p.source))}</strong><p class="muted">${escapeHTML(projectLabel(p.status))} · ${escapeHTML(projectLabel(p.stage))}</p></div>
              <div class="publication-actions">
                <button class="${p.id === state.id ? "primary" : ""}" data-id="${p.id}">${p.id === state.id ? "Continuar edición" : "Abrir"}</button>
                <button data-workspace="rename-project" data-key="${p.id}">Renombrar</button>
                <button data-workspace="${p.archived ? "unarchive" : "archive"}" data-key="${p.id}">${p.archived ? "Desarchivar" : "Archivar"}</button>
                <button data-workspace="trash-project" data-key="${p.id}">Enviar a la papelera</button>
              </div></article>`,
        )
        .join("")
    : '<p class="muted">Todavía no hay proyectos en esta vista.</p>';
  // Preserve keyboard focus while polling unchanged projects.
  if (state.projectMarkup !== markup) {
    $("#projects").innerHTML = markup;
    state.projectMarkup = markup;
  }
}
async function selectProject(id) {
  if (state.id === id && state.detail?.project.id === id) {
    switchView("project-view");
    return;
  }
  const navigation = studioNavigation;
  const serial = ++projectRequest;
  const detail = await api(`/api/projects/${id}`);
  if (serial !== projectRequest || navigation !== studioNavigation) return;
  state.id = id;
  state.page = 0;
  state.candidatePage = 0;
  state.tab = "clips";
  state.captionDraft = null;
  state.captionKey = "";
  state.previewFile = null;
  state.pendingPreview = null;
  switchView("project-view");
  state.detail = detail;
  state.words = structuredClone(state.detail.transcript?.words || []);
  state.selected = new Set(state.detail.candidates.map((_, i) => i + 1));
  renderDetail();
  await refreshProjects();
}
const recoveryLabels = {stable:"Exportar con encuadre vertical estable", subtitles:"Exportar sin subtítulos incrustados",
  watermark:"Exportar sin marca de agua"};
$("#render-recovery").addEventListener("click",safe(async () => {
  const alternative = state.detail.project.render_recovery?.alternative;
  if (!recoveryLabels[alternative] || !confirm(recoveryLabels[alternative] + " para este intento. ¿Continuar?")) return;
  await api(`/api/projects/${state.id}/render-recovery`,{alternative,confirmed:true});
  const detail = await api(`/api/projects/${state.id}`);
  if (detail.project.id === state.id) { state.detail = detail; updateStatus(detail); }
}));
function updateStatus(detail) {
  const progress = detail.progress || {};
  const total = Number(progress.total);
  const progressText =
    detail.busy && progress.stage && Number.isFinite(total) && total > 0
      ? ` · ${progress.stage} ${Number(progress.done) || 0}/${total}`
      : "";
  const statusEl = $("#status");
  statusEl.textContent =
    `${projectLabel(detail.project.status)} · ${projectLabel(detail.project.stage)}${progressText}`;
  statusEl.dataset.state = detail.project.status || "";
  const errors = detail.render_errors || [];
  const error =
    detail.project.error ||
    (errors.length ? `${errors.length} clip(s) failed to render.` : "");
  $("#job-error").hidden = !error;
  $("#job-error").textContent = error;
  const recovery = recoveryLabels[detail.project.render_recovery?.alternative];
  $("#render-recovery").hidden = detail.busy || !recovery || detail.project.stage !== "render recovery";
  $("#render-recovery").textContent = recovery || "";
  $("#log").textContent = detail.log || "Aún no se ha ejecutado ningún trabajo.";
  state.busy = detail.busy;
  for (const selector of [
    "#prepare",
    "#preview",
    "#render",
    "#save-candidates",
    "#save-transcript",
  ])
    $(selector).disabled = detail.busy;
  for (const el of document.querySelectorAll(
    "#settings-form button, #caption-form button, #caption-preview",
  ))
    el.disabled = detail.busy;
  for (const el of document.querySelectorAll(
    "#settings-form input, #settings-form select, #candidates input, #transcript-words input, #caption-form input, #caption-form select, #caption-presets button",
  ))
    el.disabled = detail.busy;
}
const CANDIDATES_PER_PAGE = 25;
const CONTENT_PROFILE_LABELS = {
  talk: "podcast/talk",
  mixed: "mixed footage",
  "gameplay/action": "gameplay/action",
};
function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "0s";
  if (total < 10) return `${Math.round(total * 10) / 10}s`;
  if (total < 60) return `${Math.round(total * 10) / 10}s`;
  const whole = Math.round(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  if (hours)
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
}
function renderSelectionSummary(detail) {
  const box = $("#selection-summary");
  const selection = detail.selection || {};
  const candidates = detail.candidates || [];
  const scores = candidates
    .map((item) => Number(item.overall))
    .filter(Number.isFinite);
  const minScore = Number(
    selection.min_score ?? detail.project.options.min_score ?? 8,
  );
  const passing = Number.isFinite(Number(selection.passing))
    ? Number(selection.passing)
    : scores.filter((score) => score >= minScore).length;
  const scoreLow = Number.isFinite(Number(selection.score_min))
    ? Number(selection.score_min)
    : scores.length
      ? Math.min(...scores)
      : null;
  const scoreHigh = Number.isFinite(Number(selection.score_max))
    ? Number(selection.score_max)
    : scores.length
      ? Math.max(...scores)
      : null;
  const passingScores = scores.filter((score) => score >= minScore);
  const passLow = Number.isFinite(Number(selection.passing_score_min))
    ? Number(selection.passing_score_min)
    : passingScores.length
      ? Math.min(...passingScores)
      : null;
  const passHigh = Number.isFinite(Number(selection.passing_score_max))
    ? Number(selection.passing_score_max)
    : passingScores.length
      ? Math.max(...passingScores)
      : null;
  const durations = candidates
    .map((item) => Number(item.duration ?? item.end - item.start))
    .filter((value) => Number.isFinite(value) && value >= 0);
  const durationLow = Number.isFinite(Number(selection.duration_min))
    ? Number(selection.duration_min)
    : durations.length
      ? Math.min(...durations)
      : null;
  const durationHigh = Number.isFinite(Number(selection.duration_max))
    ? Number(selection.duration_max)
    : durations.length
      ? Math.max(...durations)
      : null;
  const lines = [];
  if (!passing) {
    lines.push(
      scoreLow === null || scoreHigh === null
        ? "No scored candidates yet."
        : `Candidate scores ${scoreLow.toFixed(1)}-${scoreHigh.toFixed(1)}.`,
    );
    lines.push(
      `No clips at ${minScore.toFixed(1)}+. Lower the minimum score to review more.`,
    );
  } else {
    const range =
      passLow === null || passHigh === null
        ? ""
        : ` · passing scores ${passLow.toFixed(1)}-${passHigh.toFixed(1)}`;
    const length =
      durationLow === null || durationHigh === null
        ? ""
        : ` · ${formatDuration(durationLow)}-${formatDuration(durationHigh)}`;
    lines.push(`${passing} clips at ${minScore.toFixed(1)}+${range}${length}`);
    const profile =
      CONTENT_PROFILE_LABELS[selection.content_profile] ||
      selection.content_profile;
    if (profile) lines.push(`Footage: ${profile}`);
  }
  box.textContent = lines.join("\n");
  box.hidden = false;
}
function renderDetection(detail) {
  const banner = $("#detection-banner");
  if (!banner) return;
  const detection = detail.detection;
  if (!detection) { banner.hidden = true; return; }
  banner.hidden = false;
  const confidence = Math.round((detection.confidence || 0) * 100);
  const formatName = detail.format?.name || "unknown";
  banner.textContent = `Detected: ${detection.type} (${confidence}%) · Using format: ${formatName}`;
  const change = document.createElement("select");
  change.setAttribute("aria-label", "Clipping format");
  const formats = Object.keys((state.config && state.config.formats) || {});
  for (const name of formats) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    option.selected = name === formatName;
    change.appendChild(option);
  }
  change.addEventListener("change", async () => {
    await api(`/api/projects/${state.id}/settings`, { format: change.value });
    notice("Format saved. Prepare again to apply it.");
  });
  banner.appendChild(change);
  const build = document.createElement("button");
  build.type = "button";
  build.textContent = "Build new format";
  build.addEventListener("click", () => buildFormat(detail));
  banner.appendChild(build);
}

async function buildFormat(detail) {
  const name = window.prompt("New format name (lowercase):");
  if (!name) return;
  const base = detail.format_recipe || {};
  const min = window.prompt("Minimum clip seconds", String(base.min_len || 30));
  const max = window.prompt("Maximum clip seconds", String(base.max_len || 120));
  try {
    await api("/api/formats", {
      ...base, name,
      min_len: Number(min), max_len: Number(max),
    });
    notice("Format saved. Reload Studio to see it in the list.");
    await config();
  } catch (error) {
    notice(error.message, true);
  }
}

function renderDetail() {
  const detail = state.detail,
    p = detail.project,
    options = p.options;
  renderDetection(detail);
  $("#project-id").textContent = `FAC ${String(p.id || "").slice(0, 6).toUpperCase()}`;
  $("#project-title").textContent = p.name || projectName(p.source);
  $("#project-source").textContent = p.source;
  $("#project-created").textContent = p.created ? "Creado " + new Date(p.created * 1000).toLocaleDateString("es") : "";
  const archive = $("[data-project-manage=archive],[data-project-manage=unarchive]");
  archive.dataset.projectManage = p.archived ? "unarchive" : "archive";
  archive.textContent = p.archived ? "Desarchivar" : "Archivar";
  const player = $("#source-player");
  const hint = $("#player-hint");
  if (detail.playable_source) {
    player.hidden = !detail.transcript;
    hint.hidden = !!detail.transcript;
    hint.textContent = "Prepara el proyecto para previsualizar la fuente.";
    const source = `/media/${p.id}/source`;
    if (detail.transcript && player.getAttribute("src") !== source)
      player.src = source;
  } else if (state.previewFile) {
    player.hidden = false;
    hint.hidden = true;
    if (player.getAttribute("src") !== state.previewFile.url)
      player.src = state.previewFile.url;
  } else {
    player.hidden = true;
    hint.hidden = false;
    hint.textContent = detail.transcript
      ? "Long source: press Watch on a clip to render a 480p preview from a downloaded section."
      : "Prepara el proyecto para previsualizar la fuente.";
  }
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(options)) {
    const input = form.elements.namedItem(key);
    if (input) {
      if (input.type === "checkbox") input.checked = value;
      else input.value = value ?? "";
    }
  }
  form.elements.center1.value = options.centers[0];
  form.elements.center2.value = options.centers[1];
  $("#facecam-box").value = options.facecam_box
    ? options.facecam_box.join(" ")
    : "";
  $("#facecam-height").value = options.facecam_height ?? 0.34;
  const brand = options.brand
    ? basename(options.brand).replace(/\.json$/, "")
    : "";
  updateBrands(brand);
  $("#search-mode").value = options.semantic
    ? "semantic"
    : options.search
      ? "search"
      : "auto";
  $("#query").value = options.semantic || options.search || "";
  fillCaptionForm(savedCaptionStyle(), options.style);
  markCaptionPreset(options.style);
  $("#candidate-count").textContent = detail.candidates.length;
  const total = detail.candidates.length;
  const pageCount = Math.max(1, Math.ceil(total / CANDIDATES_PER_PAGE));
  state.candidatePage = Math.min(Math.max(0, state.candidatePage), pageCount - 1);
  const start = state.candidatePage * CANDIDATES_PER_PAGE;
  const visible = detail.candidates.slice(start, start + CANDIDATES_PER_PAGE);
  $("#candidates").innerHTML = visible.length
    ? visible
        .map((c, offset) => {
          const i = start + offset;
          const rank = Number.isFinite(Number(c.rank)) ? Number(c.rank) : i + 1;
          const duration = Number(c.duration ?? c.end - c.start);
          return `<article class="candidate" data-index="${i}"><input type="checkbox" aria-label="Select clip ${i + 1}" data-select="${i + 1}" ${state.selected.has(i + 1) ? "checked" : ""}><div><div class="candidate-title"><input aria-label="Clip ${i + 1} title" data-field="hook" value="${escapeHTML(c.hook)}"></div><div class="candidate-meta">#${rank} · ${Number(c.overall).toFixed(1)}/10 · ${formatDuration(duration)}${c.similarity !== undefined ? " · SEMANTIC " + Number(c.similarity).toFixed(2) : ""}</div><div class="candidate-time"><label>Start (seconds)<input data-field="start" type="number" min="0" step="0.01" value="${c.start}"></label><label>End (seconds)<input data-field="end" type="number" min="0" step="0.01" value="${c.end}"></label></div><details class="candidate-reason"><summary>Why this clip</summary><p>${escapeHTML(c.reason || "")}</p><pre>${escapeHTML(JSON.stringify(c.score_parts || {}, null, 2))}</pre>${c.evidence ? `<pre>${escapeHTML(JSON.stringify(c.evidence, null, 2))}</pre>` : ""}</details></div><button data-play="${i}">▶ Watch</button></article>`;
        })
        .join("")
    : '<p class="muted">Find clips to see candidates here. You can adjust selection and timing before export.</p>';
  $("#candidate-page").textContent =
    `${total ? start + 1 : 0}–${Math.min(start + CANDIDATES_PER_PAGE, total)} of ${total}`;
  $("#candidates-prev").disabled = state.candidatePage === 0;
  $("#candidates-next").disabled = start + CANDIDATES_PER_PAGE >= total;
  renderSelectionSummary(detail);
  renderWords();
  updateSelection();
  renderOutputs(detail);
  updateStatus(detail);
  setTab(state.tab);
}
function setTab(tab) {
  state.tab = tab;
  $("#clip-panel").hidden = tab !== "clips";
  $("#transcript-panel").hidden = tab !== "transcript";
  $("#caption-panel").hidden = tab !== "captions";
  $("#show-clips").setAttribute("aria-pressed", String(tab === "clips"));
  $("#show-transcript").setAttribute(
    "aria-pressed",
    String(tab === "transcript"),
  );
  $("#show-captions").setAttribute(
    "aria-pressed",
    String(tab === "captions"),
  );
  state.captionKey = "";
  updateCaptionOverlay(true);
}
function updateSelection() {
  $("#selection-count").textContent = `${state.selected.size} selected`;
}
function renderWords() {
  const start = state.page * 100,
    visible = state.words.slice(start, start + 100);
  $("#transcript-words").innerHTML = visible.length
    ? '<div class="word-header"><span></span><span>WORD</span><span>START (S)</span><span>END (S)</span></div>' +
      visible
        .map(
          (word, i) =>
            `<div class="word-row" data-word="${start + i}"><button data-seek="${start + i}" aria-label="Play word ${start + i + 1}">${start + i + 1}</button><input aria-label="Word ${start + i + 1}" data-word-field="word" value="${escapeHTML(word.word)}"><input aria-label="Word ${start + i + 1} start" data-word-field="start" type="number" min="0" step="0.01" value="${word.start}"><input aria-label="Word ${start + i + 1} end" data-word-field="end" type="number" min="0" step="0.01" value="${word.end}"></div>`,
        )
        .join("")
    : '<p class="muted">Prepare the project to get an editable transcript.</p>';
  $("#word-page").textContent =
    `${state.words.length ? start + 1 : 0}–${Math.min(start + 100, state.words.length)} of ${state.words.length} words`;
  $("#words-prev").disabled = state.page === 0;
  $("#words-next").disabled = start + 100 >= state.words.length;
  for (const el of document.querySelectorAll("#transcript-words input"))
    el.disabled = state.busy;
}
function renderOutputs(detail) {
  const videos = detail.files.filter((f) => f.kind === "previews" && f.name.endsWith(".mp4"));
  $("#output-count").textContent = `${videos.length} CLIPS`;
  $("#outputs").className = videos.length ? "output-grid" : "";
  $("#outputs").innerHTML = videos.length
    ? videos
        .map((file) => {
          const stem = file.name.slice(0, -4),
            related = detail.files.filter(
              (f) =>
                f.kind === file.kind &&
                [stem + ".srt", stem + ".png", stem + ".meta.json"].includes(
                  f.name,
                ),
            );
          return `<article class="output-card"><video controls preload="none" src="${encodeURI(file.url)}"></video><div class="output-info"><p>${escapeHTML(file.kind === "previews" ? "DRAFT · " : "EXPORT · ")}${escapeHTML(stem)}</p><div class="output-links"><a href="${encodeURI(file.url)}" download>MP4 ↓</a>${related.map((f) => `<a href="${encodeURI(f.url)}" download>${f.name.endsWith(".srt") ? "SRT" : f.name.endsWith(".png") ? "POSTER" : "METADATA"} ↓</a>`).join("")}${file.kind === "clips" ? `<button type="button" data-editorial-file="${escapeHTML(file.name)}">Editar clip</button>` : ""}</div></div></article>`;
        })
        .join("")
    : '<p class="muted">Choose candidates and make a preview or export.</p>';
}
async function saveCandidates(showNotice = true) {
  if (!state.detail.candidates.length)
    throw new Error("Find clips before exporting.");
  await api(`/api/projects/${state.id}/candidates`, {
    candidates: state.detail.candidates,
  });
  if (showNotice) notice("Clip edits saved.");
}
function projectOutput() {
  const form = $("#settings-form").elements;
  const aspect = form.aspect.value;
  return {aspect, layout: aspect === "9:16" ? form.layout.value : "single", camera_mode: form.camera_mode.value};
}
async function run(action, preview = false) {
  if (action === "render") {
    if (!state.selected.size) throw new Error("Selecciona al menos un clip.");
    await saveCandidates(false);
  }
  await api(`/api/projects/${state.id}/run`, {
    action,
    preview,
    ...(action === "render" ? {output: projectOutput()} : {}),
    pick:
      action === "render"
        ? [...state.selected].sort((a, b) => a - b).join(",")
        : null,
  });
  state.detail.busy = true;
  state.detail.project.status = "queued";
  state.detail.project.stage = action;
  updateStatus(state.detail);
  notice(
    action === "prepare"
      ? "Queued. Saved stages will be reused when their inputs match."
      : "Exportación en cola.",
  );
  await refreshProjects();
}
$("#new-project").addEventListener("click", () => {
  switchView("new-view");
  $("#sources").focus();
  refreshProjects().catch((e) => notice(e.message, true));
});
$("#projects").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-id]");
    if (button) await selectProject(button.dataset.id);
  }),
);
$("#create-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const sources = $("#sources")
      .value.split(/\r?\n/)
      .map((s) => s.trim().replace(/^"|"$/g, ""))
      .filter(Boolean);
    if (!sources.length) throw new Error("Add a video path or URL.");
    const button = event.target.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const options = {
        aspect: $("#new-aspect").value,
        whisper_model: $("#new-model").value,
        style: $("#new-style").value,
      };
      const brief = ($("#brief-text")?.value || "").trim();
      const result = await api("/api/batch", { sources, options, brief });
      await selectProject(result.projects[0].id);
      notice(
        `${sources.length} project${sources.length === 1 ? "" : "s"} queued for review.`,
      );
      $("#sources").value = "";
    } finally {
      button.disabled = false;
    }
  }),
);
$("#settings-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const form = event.target,
      values = Object.fromEntries(new FormData(form));
    for (const key of ["min_len", "max_len"]) values[key] = Number(values[key]);
    values.max_clips =
      values.max_clips === "" ? null : Number(values.max_clips);
    values.min_score =
      values.min_score === "" ? null : Number(values.min_score);
    values.preferred_len = (values.preferred_len || "").trim() || null;
    values.tighten = form.elements.tighten.checked;
    values.loudnorm = form.elements.loudnorm.checked;
    values.centers = [Number(values.center1), Number(values.center2)];
    delete values.center1;
    delete values.center2;
    const facecamText = ($("#facecam-box")?.value || "").trim();
    let facecamBox = null;
    if (facecamText) {
      const parts = facecamText.split(/[\s,]+/).map(Number);
      if (parts.length === 4 && parts.every(Number.isFinite)) facecamBox = parts;
    }
    values.facecam_box = facecamBox;
    const facecamHeight = Number($("#facecam-height")?.value);
    if (Number.isFinite(facecamHeight))
      values.facecam_height = facecamHeight;
    values.llm = values.llm.trim() || null;
    values.search = null;
    values.semantic = null;
    const mode = $("#search-mode").value;
    if (mode !== "auto") {
      if (!$("#query").value.trim())
        throw new Error("Enter a topic or keywords.");
      values[mode] = $("#query").value.trim();
    }
    await api(`/api/projects/${state.id}/settings`, values);
    notice(
      "Settings saved. Find clips again for search changes, or export with your new style.",
    );
  }),
);
$("#brand-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.target));
    const name = values.name;
    delete values.name;
    for (const key of ["font_size", "margin", "logo_width"])
      values[key] = Number(values[key]);
    values.logo = values.logo.trim() || null;
    await api("/api/brands", { name, preset: values });
    await config();
    $("#brand-select").value = name;
    notice("Brand preset saved. Save settings to apply it to this project.");
  }),
);
$("#candidates").addEventListener("input", (event) => {
  const selected = event.target.dataset.select;
  if (selected) {
    event.target.checked
      ? state.selected.add(Number(selected))
      : state.selected.delete(Number(selected));
    updateSelection();
  }
  const field = event.target.dataset.field;
  if (field) {
    const i = Number(event.target.closest("[data-index]").dataset.index);
    state.detail.candidates[i][field] =
      field === "hook" ? event.target.value : Number(event.target.value);
  }
});
let stopAt = null;
async function play(start, end = null) {
  const player = $("#source-player");
  stopAt = end;
  player.currentTime = start;
  await player.play();
}
async function watchCandidate(index) {
  const candidate = state.detail.candidates[index];
  if (state.detail.playable_source) {
    state.previewFile = null;
    await play(candidate.start, candidate.end);
    return;
  }
  if (state.busy || state.pendingPreview)
    throw new Error("Wait for the current job to finish.");
  const before = new Set(
    state.detail.files
      .filter((file) => file.kind === "previews" && file.name.endsWith(".mp4"))
      .map((file) => file.name),
  );
  state.pendingPreview = { index, before };
  state.previewFile = null;
  renderDetail();
  $("#player-hint").textContent =
    "Rendering this clip from its downloaded source section…";
  try {
    await saveCandidates(false);
    await api(`/api/projects/${state.id}/run`, {
      action: "render",
      preview: true,
      output: projectOutput(),
      pick: String(index + 1),
    });
  } catch (error) {
    state.pendingPreview = null;
    renderDetail();
    throw error;
  }
  state.detail.busy = true;
  state.detail.project.status = "queued";
  state.detail.project.stage = "render";
  updateStatus(state.detail);
  notice("Rendering a 480p preview from a downloaded section…");
  await refreshProjects();
}
function loadPreview(file) {
  state.previewFile = file;
  const player = $("#source-player");
  player.src = file.url;
  player.hidden = false;
  $("#player-hint").hidden = true;
  player.currentTime = 0;
  player.play().catch(() => {});
}
$("#source-player").addEventListener("timeupdate", () => {
  if (stopAt !== null && $("#source-player").currentTime >= stopAt) {
    $("#source-player").pause();
    stopAt = null;
  }
  updateCaptionOverlay();
});
$("#source-player").addEventListener("seeked", () =>
  updateCaptionOverlay(true),
);
$("#source-player").addEventListener("loadedmetadata", () =>
  updateCaptionOverlay(true),
);
window.addEventListener("resize", () => {
  state.captionKey = "";
  updateCaptionOverlay(true);
});
$("#candidates").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-play]");
    if (button) {
      await watchCandidate(Number(button.dataset.play));
    }
  }),
);
$("#transcript-words").addEventListener("input", (event) => {
  const field = event.target.dataset.wordField;
  if (field) {
    const i = Number(event.target.closest("[data-word]").dataset.word);
    state.words[i][field] =
      field === "word" ? event.target.value : Number(event.target.value);
  }
});
$("#transcript-words").addEventListener(
  "click",
  safe(async (event) => {
    const button = event.target.closest("[data-seek]");
    if (button) await play(state.words[Number(button.dataset.seek)].start);
  }),
);
$("#words-prev").addEventListener("click", () => {
  state.page--;
  renderWords();
});
$("#words-next").addEventListener("click", () => {
  state.page++;
  renderWords();
});
$("#candidates-prev").addEventListener("click", () => {
  state.candidatePage = Math.max(0, state.candidatePage - 1);
  renderDetail();
});
$("#candidates-next").addEventListener("click", () => {
  state.candidatePage++;
  renderDetail();
});
$("#select-all").addEventListener("click", () => {
  state.selected = new Set(state.detail.candidates.map((_, i) => i + 1));
  renderDetail();
});
$("#select-none").addEventListener("click", () => {
  state.selected.clear();
  renderDetail();
});
$("#show-clips").addEventListener("click", () => setTab("clips"));
$("#show-transcript").addEventListener("click", () => setTab("transcript"));
$("#show-captions").addEventListener("click", () => setTab("captions"));

function showLive() {
  switchView("live-view");
  refreshLive().catch((e) => notice(e.message, true));
}
$("#show-live").addEventListener("click", showLive);

const LIVE_SELECTION = "chopify.live.selected";
const liveActive = (session) => ["starting", "running"].includes(session?.status);
let liveRefreshSerial = 0;
let liveWanted = null;
let liveClipSignature = "";
try { liveWanted = localStorage.getItem(LIVE_SELECTION); } catch {}

function selectLive(id) {
  state.liveId = liveWanted = id;
  try {
    if (id) localStorage.setItem(LIVE_SELECTION, id);
    else localStorage.removeItem(LIVE_SELECTION);
  } catch {}
}

function showLiveForm(show) {
  $("#live-form").hidden = !show;
  $("#live-new").setAttribute("aria-expanded", String(show));
}

$("#live-new").addEventListener("click", () => showLiveForm($("#live-form").hidden));
$("#live-sessions").addEventListener("click", safe(async (event) => {
  const link = event.target.closest("[data-live-session]");
  if (!link) return;
  liveWanted = link.dataset.liveSession;
  await refreshLive();
}));

$("#live-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const source = $("#live-source").value.trim();
    const brief = ($("#live-brief")?.value || "").trim();
    const start = $("#live-start");
    if (start.disabled) return;
    start.disabled = true;
    const previousStatus = $("#live-status").textContent;
    $("#live-status").textContent = "Resolviendo la transmisión…";
    $("#live-start-error").hidden = true;
    try {
      const options = { aspect: $("#live-aspect").value, live_max_height: Number($("#live-max-height").value),
        live_duration: $("#live-duration").value, camera_mode: $("#live-camera-mode").value, live_max_per_hour: $("#live-per-hour").valueAsNumber,
        live_max_clips: $("#live-per-session").valueAsNumber, live_whisper_device: $("#live-whisper-device").value };
      const threshold = $("#live-threshold");
      if (threshold.value !== "") options.live_threshold = threshold.valueAsNumber;
      const result = await api("/api/live", { source, options, brief });
      liveWanted = result.id;
      showLiveForm(false);
      await refreshLive();
    } catch (error) {
      $("#live-status").textContent = previousStatus;
      if (error.status === 409 && error.data.session_id) {
        liveWanted = error.data.session_id;
        showLiveForm(false);
        notice(error.message);
        await refreshLive();
        return;
      }
      $("#live-start-error").textContent = error.message;
      $("#live-start-error").hidden = false;
    } finally {
      start.disabled = false;
    }
  }),
);

$("#live-stop").addEventListener(
  "click",
  safe(async () => {
    await api(`/api/live/${state.liveId}/stop`, {});
    await refreshLive();
  }),
);

$("#live-export").addEventListener("click", safe(async () => {
  const button = $("#live-export");
  button.disabled = true;
  try {
    await api("/api/live/" + state.liveId + "/export", { seconds: Number($("#live-export-seconds").value) });
    $("#live-export-status").textContent = "Exportación en cola.";
  } catch (error) {
    $("#live-export-status").textContent = error.message;
    button.disabled = false;
  }
}));

const LIVE_NEXT_ACTION = {
  ok: "Watching",
  degraded: "Waiting for segments",
  failed: "Check the URL or restart Studio",
};

async function refreshLive() {
  const serial = ++liveRefreshSerial;
  const data = await api("/api/live");
  if (serial !== liveRefreshSerial) return;
  const sessions = data.sessions || [];
  let detail = null;
  const candidates = [sessions.find((item) => item.id === liveWanted),
    ...sessions.filter(liveActive), ...sessions].filter(Boolean);
  for (const session of new Set(candidates)) {
    try {
      detail = await api(`/api/live/${encodeURIComponent(session.id)}`);
      break;
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  }
  if (serial !== liveRefreshSerial) return;
  const firstDetail = $("#live-new").hidden;
  selectLive(detail?.id || null);
  $("#live-new").hidden = !detail;
  if (!detail || firstDetail) showLiveForm(!detail);
  renderLiveSessions(sessions);
  $("#live-stop").hidden = !liveActive(detail);
  $("#live-error").textContent = detail?.errors?.at(-1) || detail?.health?.warnings?.at(-1) || "";
  $("#live-error").hidden = !$("#live-error").textContent;
  const diagnostics = [...new Set([...(detail?.errors || []), ...(detail?.health?.warnings || [])])];
  $("#live-errors").hidden = diagnostics.length === 0;
  $("#live-errors ul").innerHTML = diagnostics.map((message) => `<li>${escapeHTML(message)}</li>`).join("");
  $("#live-status").textContent = detail
    ? `${detail.status} · ${Math.round(detail.stream_time || 0)}s · ${detail.export?.completed ?? detail.triggers.filter((clip) => clip.files?.mp4 && !clip.error).length} clips`
    : "Sin captura activa.";
  const health = detail?.health;
  $("#live-health").textContent = health
    ? `${health.state} · segment ${health.last_segment_age}s · ${health.restarts} restarts · `
      + `${liveActive(detail) ? LIVE_NEXT_ACTION[health.state] || "Watching" : "Último diagnóstico guardado"} · brief `
      + `${health.brief_source || "none"}/${health.brief_provider || "none"}`
    : "";
  const score = health?.detection?.last_score;
  const recent = (detail?.recent_scores || []).slice(-10);
  $("#live-score").textContent = detail
    ? `Detección ${score == null ? "sin señales recientes" : Number(score).toFixed(2)} / 10 · Umbral ${detail.threshold ?? "—"}`
      + (recent.length ? ` · Últimos scores: ${recent.map((value) => Number(value).toFixed(2)).join(", ")}` : "")
    : "";
  $("#live-manual").hidden = detail?.status !== "running";
  $("#live-export").disabled = ["queued", "exporting"].includes(detail?.export?.state);
  $("#live-export-status").textContent = detail?.export?.message || "";
  const tech = (name, track) => {
    const item = health?.analyzers?.[name];
    if (!item) return track && health?.tracks?.[track] === false ? "Pista ausente" : "En espera";
    const diagnostic = item.last_error;
    return `${item.state} · ${item.processed} procesados · ${item.errors} errores · ${item.retries} reintentos · ${item.dropped} omitidos`
      + (item.lag_seconds == null ? "" : ` · retraso ${item.lag_seconds}s`)
      + (diagnostic ? ` · ${diagnostic.segment}: ${diagnostic.message}`
        + (diagnostic.exit_code == null ? "" : ` (${diagnostic.exit_code})`)
        + (diagnostic.timeout_seconds == null ? "" : ` (${diagnostic.timeout_seconds}s)`)
        + (diagnostic.stderr ? ` ${diagnostic.stderr}` : "") : "");
  };
  const capture = health?.capture;
  const components = detail ? [
    ["Captura", capture ? `${capture.state} · ${capture.closed_segments} cerrados · ${capture.available} disponibles · ${capture.errors} errores`
      + (capture.exit_code == null ? "" : ` · FFmpeg ${capture.exit_code}`)
      + (capture.last_error ? ` · ${capture.last_error.segment}: ${capture.last_error.message}` : "")
      + (capture.message ? ` · ${capture.message}` : "")
      + (capture.stderr ? ` · ${capture.stderr}` : "") : "En espera"],
    ["Audio", tech("audio", "audio")], ["Video", tech("visual", "video")], ["Transcripción", tech("transcript")],
    ["Detección", score == null ? "En espera" : Object.keys(health.detection.signals).join(", ")],
    ["Exportación", `${detail.export?.completed || 0} clips · ${detail.export?.errors || 0} errores`],
  ] : [];
  $("#live-components").innerHTML = components.map(([label, value]) =>
    `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`).join("");
  renderLiveProduction(detail);
  const signature = JSON.stringify([detail?.id, detail?.triggers]);
  if (signature === liveClipSignature) return;
  liveClipSignature = signature;
  const clips = $("#live-clips");
  clips.replaceChildren();
  for (const record of detail?.triggers || []) {
    const row = document.createElement("div");
    row.className = "live-clip";
    row.textContent = `#${record.id} · ${Number(record.start).toFixed(1)}s-${Number(record.end).toFixed(1)}s · ${record.manual ? "Manual" : (record.score ?? "—")}`
      + (record.error ? ` · ${record.error}` : "");
    const video = record.files?.mp4;
    if (video && !record.error) {
      const link = document.createElement("a");
      const name = basename(video);
      link.href = `/media/live/${encodeURIComponent(detail.id)}/clips/${encodeURIComponent(name)}`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = " Reproducir";
      link.title = "Abrir clip en una pestaña nueva";
      const preview = document.createElement("button");
      preview.type = "button";
      preview.textContent = "Vista previa";
      preview.addEventListener("click", () => showClipPreview(link.getAttribute("href")));
      row.append(preview, link);
      const editor = document.createElement("button");
      editor.type = "button";
      editor.dataset.editorialFile = name;
      editor.textContent = "Editar clip";
      row.append(editor);
    }
    clips.append(row);
  }
}

function renderLiveProduction(detail) {
  const queue = detail?.production;
  $("#live-production").hidden = !queue;
  if (!queue) return;
  const counts = queue.counts;
  const statuses = {collecting: "Preservando fuente", queued: "Pendiente de transcripción",
    transcribing: "Transcribiendo", editing: "En edición", quality: "Control de calidad",
    done: "Terminado", rejected: "Rechazado", error: "Error"};
  const active = ["collecting", "queued", "transcribing", "editing", "quality"].reduce((sum, key) => sum + (counts[key] || 0), 0);
  const profile = detail.production_profile || queue.recent[0]?.profile || "—";
  $("#live-queue-summary").textContent = `Duración ${profile} · ${queue.detected} detecciones admitidas · ${active}/${queue.capacity} en cola para esta sesión · `
    + Object.entries(statuses).map(([key, label]) => `${label} ${counts[key] || 0}`).join(" · ");
  $("#live-queue-message").textContent = detail.health?.production_message || "";
  const formatWhisper = (name, metrics) => metrics
    ? `${name} ${metrics.model}/${metrics.device}: ${metrics.inference_seconds} s · RTF ${metrics.rtf}`
      + (metrics.time_since_detection_seconds == null ? "" : ` · ${metrics.time_since_detection_seconds} s desde la detección`)
      + (metrics.fallback ? ` · ${metrics.fallback}` : "")
      + (metrics.gpu_after ? ` · GPU del sistema ${metrics.gpu_after.utilization_percent}% · ${metrics.gpu_after.used_mib} MiB` : "")
    : `${name}: sin mediciones`;
  $("#live-whisper-status").textContent = [
    formatWhisper("Whisper Live", detail.health?.analyzers?.transcript?.performance),
    formatWhisper("Whisper de producción", queue.whisper),
  ].join(" | ");
  $("#live-queue-jobs").innerHTML = queue.recent.map((job) =>
    `<li>${escapeHTML(job.id)} · ${escapeHTML(statuses[job.state])} · ${escapeHTML(job.score)}/10${job.error ? " · " + escapeHTML(job.error) : ""}</li>`).join("");
}

function renderLiveSessions(sessions) {
  const list = $("#live-sessions");
  const focused = list.contains(document.activeElement) && document.activeElement.dataset.liveSession;
  const source = (item) => item.source?.label || [item.source?.platform, item.source?.kind].filter(Boolean).join(" · ") || "—";
  const rows = sessions.map((item) => `<tr>
    <td><button type="button" data-live-session="${escapeHTML(item.id)}" aria-pressed="${item.id === state.liveId}">${escapeHTML(source(item))} · ${escapeHTML(item.id)}</button></td>
    <td>${escapeHTML({starting: "Iniciando", running: "Activa", stopped: "Finalizada", failed: "Con error", interrupted: "Interrumpida"}[item.status])}</td>
    <td>${Math.round(item.stream_time || 0)} s</td><td>${escapeHTML(item.clips)}</td>
    <td>${item.score == null ? "—" : Number(item.score).toFixed(2)}</td><td>${escapeHTML(item.errors)}</td>
    <td>${item.started_at == null ? "—" : escapeHTML(new Date(item.started_at * 1000).toLocaleString())}</td>
    </tr>`).join("");
  const contents = rows ? `<table><thead><tr><th>Fuente y sesión</th><th>Estado</th><th>Duración</th><th>Clips</th><th>Último score</th><th>Errores</th><th>Inicio</th></tr></thead><tbody>${rows}</tbody></table>` : "";
  if (contents !== list.dataset.contents) {
    list.innerHTML = contents;
    list.dataset.contents = contents;
    if (focused) [...list.querySelectorAll("[data-live-session]")].find((button) => button.dataset.liveSession === focused)?.focus();
  }
  $("#live-sessions-empty").hidden = sessions.length > 0;
}
$("#caption-form").addEventListener("input", () => syncCaption());
$("#caption-preset").addEventListener("change", (event) =>
  applyCaptionPreset(event.target.value),
);
$("#caption-presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-caption-preset]");
  if (button) applyCaptionPreset(button.dataset.captionPreset);
});
$("#caption-form").addEventListener(
  "submit",
  safe(async (event) => {
    event.preventDefault();
    const caption = captionFromForm();
    const style = $("#caption-preset").value;
    await api(`/api/projects/${state.id}/settings`, { caption, style });
    state.detail.project.options.caption = caption;
    state.detail.project.options.style = style;
    state.captionDraft = caption;
    markCaptionPreset(style);
    notice("Caption style saved for this project.");
  }),
);
$("#save-caption-preset").addEventListener(
  "click",
  safe(async () => {
    if (!state.detail) throw new Error("Select a project first.");
    const name = $("#caption-preset-name").value.trim();
    if (!name) throw new Error("Name the preset first.");
    await api("/api/brands", { name, preset: captionFromForm() });
    await config();
    $("#brand-select").value = name;
    notice(
      "Caption preset saved. Choose it under Brand preset, then Save settings.",
    );
  }),
);
$("#caption-preview").addEventListener(
  "click",
  safe(async () => {
    if (!state.detail?.transcript) throw new Error("Prepare the project first.");
    if (state.detail.playable_source === false)
      throw new Error(
        "Long sources have no full player. Use Watch on a clip to render and play its preview.",
      );
    const candidates = state.detail.candidates;
    const selected = [...state.selected].sort((a, b) => a - b);
    const clip = candidates[(selected[0] || 1) - 1] || candidates[0];
    setTab("captions");
    const start = clip ? Number(clip.start) : state.words[0]?.start || 0;
    await play(start, start + 5);
  }),
);
$("#prepare").addEventListener(
  "click",
  safe(() => run("prepare")),
);
$("#preview").addEventListener(
  "click",
  safe(() => run("render", true)),
);
$("#render").addEventListener(
  "click",
  safe(() => run("render")),
);
$("#save-candidates").addEventListener(
  "click",
  safe(() => saveCandidates()),
);
$("#save-transcript").addEventListener(
  "click",
  safe(async () => {
    await api(`/api/projects/${state.id}/transcript`, { words: state.words });
    notice("Caption edits saved. Export clips to apply them.");
  }),
);
let polling = false;
setInterval(async () => {
  if (polling) return;
  polling = true;
  try {
    await refreshProjects();
    if (!$("#live-view").hidden) await refreshLive().catch(() => {});
    if (!$("#project-view").hidden && state.id && state.busy) {
      const id = state.id,
        detail = await api(`/api/projects/${id}`);
      if (id !== state.id) return;
      const finished = !detail.busy;
      updateStatus(detail);
      if (finished) {
        const pending = state.pendingPreview;
        state.detail = detail;
        state.words = structuredClone(detail.transcript?.words || []);
        if (!pending) {
          state.selected = new Set(detail.candidates.map((_, i) => i + 1));
          state.candidatePage = 0;
        }
        let match = null;
        if (pending) {
          const previews = detail.files.filter(
            (file) => file.kind === "previews" && file.name.endsWith(".mp4"),
          );
          const fresh = previews.filter(
            (file) => !pending.before.has(file.name),
          );
          const prefix = `${String(pending.index + 1).padStart(3, "0")}-`;
          const failed =
            !!detail.project.error || (detail.render_errors || []).length > 0;
          match =
            fresh.find((file) => file.name.startsWith(prefix)) ||
            fresh[0] ||
            (!failed
              ? previews.find((file) => file.name.startsWith(prefix))
              : null) ||
            null;
          state.pendingPreview = null;
          state.previewFile = match;
        }
        renderDetail();
        if (pending) {
          if (match) {
            loadPreview(match);
            notice("Preview rendered from a downloaded source section.");
          } else {
            notice(
              detail.project.error ||
                "Preview finished, but no clip was produced.",
              true,
            );
          }
        } else {
          notice(
            detail.project.error || "Project ready for review.",
            !!detail.project.error,
          );
        }
      }
    }
    $("#connection").textContent = "Estudio local";
  } catch (error) {
    $("#connection").textContent = "Sin conexión local";
  } finally {
    polling = false;
  }
}, 2000);
const studioConfig = config();
safe(async () => {
  await studioConfig;
  if (location.hash === "#live") showLive();
  else refreshLive().catch(() => {});
  refreshProjects().catch(error => notice(error.message, true));
  window.dispatchEvent(new Event("studio-ready"));
})();
$("#tiktok-manage-account").addEventListener("click", () => settingsRoute("integrations"));
$("#calendar-tiktok").addEventListener("click", safe(async () => {
  if (typeof showPublications !== "function") {
    throw new Error("Publicaciones no pudo cargar. Cierra el servidor anterior e inicia dashboard.py desde la copia actualizada.");
  }
  await studioConfig;
  await showPublications();
}));

/* Signal Desk: one authored motion — a hairline pulse for the current job. */
(() => {
  const canvas = $("#job-pulse");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let phase = 0;
  function scores() {
    const detail = state.detail;
    if (!detail) return [];
    const history = detail.recent_scores || [];
    if (history.length) return history.slice(-60).map(Number);
    const progress = detail.progress || {};
    const total = Number(progress.total) || 0;
    return total ? Array.from({ length: 24 }, (_, i) => i < Number(progress.done) || 0 ? 8 : 2) : [];
  }
  function draw() {
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const values = scores();
    const count = Math.max(32, Math.min(120, values.length || 48));
    const step = w / count;
    for (let i = 0; i < count; i += 1) {
      const v = values.length ? values[i % values.length] : 2 + Math.sin((i + phase) * 0.35) * 1.6;
      const amp = Math.max(2, Math.min(1, v / 10) * (h - 16));
      const active = !values.length && Math.abs(i - (phase % count)) < 1;
      ctx.strokeStyle = active ? "#d4ef83" : "#3a4547";
      ctx.lineWidth = active ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(i * step + step / 2, h / 2 - amp / 2);
      ctx.lineTo(i * step + step / 2, h / 2 + amp / 2);
      ctx.stroke();
    }
    phase += 0.12;
    if (!reduced) requestAnimationFrame(draw);
  }
  draw();
})();
