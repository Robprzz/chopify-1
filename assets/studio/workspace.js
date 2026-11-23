"use strict";

const workspaceViews = {
  "home-view": "show-home", "live-view": "show-live", "clips-view": "show-clips-library",
  "new-view": "show-clips-library", "project-view": "show-clips-library", "automation-view": "show-automation",
  "calendar-view": "show-publications", "publications-view": "show-settings", "statistics-view": "show-statistics",
  "settings-view": "show-settings", "system-view": "show-settings", "watermark-view": "show-settings", "telegram-view": "show-settings",
};
const workspaceEntry = {...workspaceViews, "new-view": "new-project", "publications-view": "show-tiktok",
  "system-view": "show-system", "watermark-view": "show-watermark", "telegram-view": "show-telegram"};
const workspaceStates = {generated:"Nuevo clip detectado", ready:"Lista para revisión", review:"Revisión pendiente",
  rejected:"Rechazado", failed:"Fallido", exported:"Exportado", cancelled:"Cancelado",
  analyzing:"En evaluación", validating:"Control técnico", repairing:"Corrigiendo"};
let workspaceData = null;
let workspaceLoading = null;
let workspaceView = "home-view";
let workspaceLast = 0;
const sessionPrefs = ["live-aspect","live-duration","live-max-height","live-threshold","live-per-hour","live-per-session","live-whisper-device"];

function getPreference(key, fallback = "") {
  try { return localStorage.getItem("anqora." + key) ?? fallback; } catch { return fallback; }
}
function setPreference(key, value) {
  try { localStorage.setItem("anqora." + key, value); } catch {}
}
function workspaceIcon(glyph) {
  return `<svg aria-hidden="true" class="ani-icon" viewBox="0 0 24 24"><use href="#icon-${glyph}"/></svg>`;
}
function mediaDuration(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const seconds = Math.max(0, Math.floor(value));
  const part = n => String(n).padStart(2, "0");
  return (seconds >= 3600 ? part(Math.floor(seconds / 3600)) + ":" : "") +
    part(Math.floor(seconds / 60) % 60) + ":" + part(seconds % 60);
}
function clipThumb(clip) {
  return `<a class="thumb" href="${escapeHTML(clip.url)}" target="_blank" rel="noopener" aria-label="Reproducir ${escapeHTML(clip.title)}">${clip.thumbnail ?
    `<img src="${escapeHTML(clip.thumbnail)}" loading="lazy" alt="" />` : workspaceIcon("clip")}
    ${clip.duration == null ? "" : `<time>${mediaDuration(clip.duration)}</time>`}</a>`;
}
function patchWorkspace(selector, markup) {
  const node = $(selector);
  if (node.dataset.markup !== markup && !node.contains(document.activeElement)) {
    node.innerHTML = markup;
    node.dataset.markup = markup;
  }
}
function updateWorkspaceProfile() {
  const profile = getPreference("profile");
  $("#home-greeting").textContent = profile ? "Hola, " + profile : "Inicio";
  $("#workspace-profile").textContent = profile;
  $("#workspace-profile").hidden = !profile;
}
function updateCollapsedRail() {
  const collapsed = getPreference("collapsed") === "1";
  document.body.classList.toggle("rail-collapsed", collapsed);
  $("#rail-toggle").setAttribute("aria-expanded", String(!collapsed));
}

function renderWorkspace(data) {
  const today = data.today;
  const counts = [
    ["live", data.active.length, "Capturas activas", data.active.length ? "En ejecución" : "Sin actividad", "live"],
    ["clip", today.clips, "Clips de hoy", "Exportaciones terminadas", "clips"],
    ["stack", today.review, "Por revisar", today.urgent + " urgentes", "pending"],
    ["star", today.recommended + "/" + today.limit, "Recomendados", "Selección diaria", "recommended"],
  ];
  patchWorkspace("#home-kpis", counts.map(([icon, value, label, desc, action]) =>
    `<button class="kpi-card" data-workspace="${action}"><span class="kpi-icon">${workspaceIcon(icon)}</span><strong>${value}</strong>
      <span class="kpi-label">${label}</span><small>${desc}</small></button>`).join(""));
  const percent = Math.round(today.recommended * 100 / today.limit);
  $("#progress-percent").textContent = percent + "%";
  $("#progress-count").textContent = today.recommended + " de " + today.limit;
  $("#progress-arc").setAttribute("stroke-dasharray", percent + " 100");
  $("#progress-arc").hidden = percent === 0;
  const session = data.active[0];
  const thumbnail = session && data.clips.find(clip => clip.project === "live/" + session.id && clip.thumbnail)?.thumbnail;
  const visual = thumbnail ? `<img src="${escapeHTML(thumbnail)}" alt="" />` : workspaceIcon("live");
  const liveText = session
    ? `<h2>${session.status === "starting" ? "Preparando captura" : "Captura en ejecución"}</h2>
      <p>${escapeHTML(session.source?.label || session.source?.platform || "Live")}${session.height ? " · Máx. " + session.height + "p" : ""}
      ${session.aspect ? " · " + escapeHTML(session.aspect) : ""} · ${mediaDuration(session.stream_time)}</p>`
    : "<h2>Sin captura activa</h2><p>Inicia una sesión para detectar y producir clips en vivo.</p>";
  patchWorkspace("#home-live", `<div class="live-visual">${visual}</div><div>${liveText}</div>` + (session
    ? `<button data-workspace="session" data-key="${escapeHTML(session.id)}">Ver sesión</button>`
    : '<button data-workspace="new-live">Iniciar captura</button>'));
  patchWorkspace("#home-clips", data.priority.slice(0,5).map(clip => `<article class="home-clip">${clipThumb(clip)}
    <div><strong>${escapeHTML(clip.title)}</strong><p>${clip.age == null ? "Antigüedad sin verificar" : "Hace " + Math.round(clip.age) + " min"}
      · Calidad verificada${clip.level === "urgent" ? " · Urgente" : ""}</p></div><span class="score-pill">★ ${clip.priority?.toFixed(1) ?? "—"}</span>
    <div class="priority-actions"><button class="primary" data-workspace="review" data-job="${clip.job}">Revisar</button>
    ${clip.level === "urgent" ? `<button data-workspace="plan-clip" data-key="${escapeHTML(clip.project + "/" + clip.filename)}">Preparar ahora</button>` : ""}</div></article>`).join("") ||
    '<p class="empty-card">Los clips con calidad y evaluación verificadas aparecerán aquí cuando estén disponibles.</p>');
  patchWorkspace("#home-activity", data.events.map(item => `<li><strong>${workspaceStates[item.state] || "Producción"}</strong>
    <small>${escapeHTML(item.filename)} · ${escapeHTML(new Date(item.created * 1000).toLocaleString())}</small></li>`).join("") ||
    "<li>Sin actividad registrada.</li>");
  $("#notification-count").textContent = data.notifications.length;
  $("#notification-count").hidden = !data.notifications.length;
  if (workspaceView === "clips-view") renderLibrary();
  if (workspaceView === "statistics-view") renderStatistics(data);
  window.dispatchEvent(new Event("workspace-data"));
}

async function refreshWorkspace(force = false) {
  if (!force && Date.now() - workspaceLast < 3500) return workspaceData;
  if (workspaceLoading) return workspaceLoading;
  const period = workspaceView === "statistics-view" ? $("#statistics-period").value : 1;
  workspaceLoading = api("/api/workspace?days=" + period).then(data => {
    workspaceData = data;
    workspaceLast = Date.now();
    $("#rail-health").textContent = "Disponible";
    $(".system-indicator").classList.remove("offline");
    renderWorkspace(data);
    return data;
  }).catch(error => {
    $("#rail-health").textContent = "Sin conexión";
    $(".system-indicator").classList.add("offline");
    if (force) notice(error.message, true);
    return workspaceData;
  }).finally(() => { workspaceLoading = null; });
  return workspaceLoading;
}
function renderLibrary() {
  const query = $("#clip-library-query").value.toLocaleLowerCase();
  const filter = $("#clip-library-filter").value;
  const sort = $("#clip-library-sort").value;
  const clips = (workspaceData?.clips || []).filter(clip =>
    (!filter || filter === clip.status || filter === clip.level) &&
    (clip.title + clip.source + clip.filename).toLocaleLowerCase().includes(query));
  if (sort === "priority") clips.sort((a,b) => (b.priority ?? -1) - (a.priority ?? -1));
  if (sort === "title") clips.sort((a,b) => a.title.localeCompare(b.title));
  $("#clip-library").className = $("#clip-library-mode").value === "list" ? "library-list" : "library-grid";
  patchWorkspace("#clip-library", clips.map(clip => {
    const key = escapeHTML(clip.project + "/" + clip.filename);
    const platform = workspaceData.plans.find(row => row.project === clip.project && row.file === clip.filename)?.platform;
    return `<article class="workspace-card library-item">${clipThumb(clip)}
    <div class="library-info"><strong>${escapeHTML(clip.title)}</strong><p>${escapeHTML(clip.source)}
    · ${escapeHTML(new Date(clip.created * 1000).toLocaleDateString())} · ${workspaceStates[clip.status] || "En producción"}</p>
    <p>Detección ${clip.score ?? "—"} · Prioridad ${clip.priority?.toFixed(1) ?? "—"} · ${escapeHTML(platform || "Sin plataforma")}</p></div>
    <div class="publication-actions">${clip.job ? `<button data-workspace="review" data-job="${clip.job}">Revisar e historial</button>` : ""}
    ${clip.level && !clip.review ? `<button data-workspace="correction" data-job="${clip.job}">Solicitar corrección</button>` : ""}
    <button data-workspace="edit-clip" data-key="${key}">Editar metadatos</button>
    <button data-workspace="plan-clip" data-key="${key}">Programar</button>
    <a href="${escapeHTML(clip.url)}" download>Exportar MP4</a></div></article>`;
  }).join("") || '<p class="empty-card">No hay exportaciones que coincidan con la búsqueda.</p>');
}
function renderStatistics(data) {
  const labels = {detected:"Detectados en Live",finished:"Exportaciones terminadas",rejected:"Rechazados",
    recommended:"Recomendados hoy",pending:"Pendientes ahora",scheduled:"Programaciones",confirmed:"Publicaciones confirmadas",
    manual:"Confirmaciones manuales",api:"Confirmaciones por TikTok",errors:"Fallos"};
  patchWorkspace("#statistics-cards", Object.entries(data.statistics).map(([key, n]) =>
    `<article class="kpi-card"><span class="kpi-icon">${workspaceIcon("stats")}</span><strong>${n}</strong><span class="kpi-label">${labels[key]}</span></article>`).join(""));
  const timings = data.processing, lag = data.detection_latency;
  $("#statistics-latency").textContent = timings.samples
    ? `Automation · ${timings.samples} muestras · media ${timings.average} s · mediana ${timings.median} s · P95 ${timings.p95} s` +
      (lag?.samples ? ` · Detección a revisión · ${lag.samples} muestras · mediana ${lag.median} s · P95 ${lag.p95} s` : "")
    : "Todavía no hay muestras de procesamiento en este período.";
}
function showWorkspace(view) {
  state.id = null; switchView(view);
  if (view === "clips-view") { renderLibrary(); refreshProjects().catch(() => {}); }
  if (view === "settings-view") {
    api("/api/telegram/status").then(data => {
      $("#workspace-telegram-state").textContent = data.connection === "online" ? "Telegram conectado" :
        data.settings.enabled ? "Telegram no disponible" : "Telegram desactivado";
    }).catch(() => { $("#workspace-telegram-state").textContent = "Telegram no disponible"; });
  }
}
async function workspaceAction(action, key, job) {
  if (action === "clips" || action === "editing") return showWorkspace("clips-view");
  if (["live","new-live","session","live-settings"].includes(action)) {
    if (key) selectLive(key);
    showLive();
    if (action === "new-live" || action === "live-settings") {
      await refreshLive(); showLiveForm(true);
      if (action === "live-settings") $("#live-advanced").open = true;
      else $("#live-source").focus();
    }
    return;
  }
  if (action === "system") return $("#show-system").click();
  if (action === "edit-clip") {
    const clip = workspaceData?.clips.find(item => item.project + "/" + item.filename === key);
    if (clip) await openEditorial({project: clip.project, filename: clip.filename});
    return;
  }
  if (action === "plan-clip" || action === "open-plan") {
    await openCalendar();
    return action === "plan-clip" ? openCalendarDetail(null, key) : openCalendarDetail(key);
  }
  if (action === "correction" && job) {
    if (!confirm("¿Solicitar una corrección para esta versión?")) return;
    const data = await api("/api/production/realtime");
    const clip = data.clips.find(item => item.id === Number(job));
    if (clip?.revision == null || clip.remote_review) throw new Error("La revisión ya no está disponible. Abre el historial del clip.");
    await applyRealtimeDecision(clip, "correction");
    await refreshWorkspace(true);
    return;
  }
  $("#show-automation").click();
  if (action === "ai") $("#editorial-heading").scrollIntoView({block:"start"});
  if (action === "queue") $("#production-heading").scrollIntoView({block:"start"});
  if (action === "pending" || action === "recommended") {
    $("#realtime-filter").value = action === "recommended" ? "recommended" : "all";
    renderRealtime();
    $("#realtime-heading").scrollIntoView({block:"start"});
  }
  if (action === "review" && job) {
    const detail = await api("/api/production/job?id=" + encodeURIComponent(job));
    showProductionDetail(detail);
    $("#production-detail").scrollIntoView({block:"start"});
  }
}

document.addEventListener("click", safe(async event => {
  const target = event.target.closest("[data-workspace]");
  if (!target) return;
  if ($("#workspace-dialog").open) $("#workspace-dialog").close();
  await workspaceAction(target.dataset.workspace, target.dataset.key, target.dataset.job);
}));
for (const [button, view] of [["show-home","home-view"],["show-clips-library","clips-view"],
  ["show-statistics","statistics-view"],["show-settings","settings-view"]]) {
  $("#" + button).addEventListener("click", () => showWorkspace(view));
}
$("#brand-home").addEventListener("click", () => showWorkspace("home-view"));
$("#rail-toggle").addEventListener("click", () => {
  setPreference("collapsed", getPreference("collapsed") === "1" ? "0" : "1"); updateCollapsedRail();
});
$("#statistics-period").addEventListener("change", () => refreshWorkspace(true));
for (const id of ["clip-library-query","clip-library-filter","clip-library-sort","clip-library-mode"]) {
  $("#" + id).addEventListener(id.endsWith("query") ? "input" : "change", renderLibrary);
}
$("#workspace-profile-form").addEventListener("submit", event => {
  event.preventDefault(); setPreference("profile", $("#workspace-profile-name").value.trim());
  updateWorkspaceProfile(); notice("Perfil guardado.");
});
$("#workspace-reset-interface").addEventListener("click", () => {
  if (!confirm("¿Restaurar formato Live, aspecto del menú y nombre de perfil?")) return;
  for (const key of ["profile", "collapsed", ...sessionPrefs]) setPreference(key, "");
  location.reload();
});
$("#settings-query").addEventListener("input", event => {
  const query = event.target.value.toLocaleLowerCase();
  for (const card of $("#settings-categories").children) card.hidden = !card.textContent.toLocaleLowerCase().includes(query);
});
for (const id of sessionPrefs) {
  const field = $("#" + id);
  const previous = getPreference(id);
  if (previous) field.value = previous;
  field.addEventListener("change", () => setPreference(id, field.value));
}

function dialogResults(alerts = false) {
  const query = $("#workspace-query").value.toLocaleLowerCase();
  const data = workspaceData || {clips: [], sessions: [], plans: [], notifications: []};
  const clips = (alerts ? data.notifications.filter(row => row.job && !row.platform) : data.clips).map(clip =>
    [clip.title, clip.job ? "review" : "plan-clip", clip.project + "/" + clip.filename, clip.job]);
  const sessions = alerts ? [] : data.sessions.map(session => [session.source?.label || session.source?.platform || session.id, "session", session.id]);
  const plans = (alerts ? data.notifications.filter(row => row.platform) : data.plans).map(plan => [plan.title, "open-plan", plan.id]);
  $("#workspace-results").innerHTML = [...clips, ...sessions, ...plans].filter(([title]) => title.toLocaleLowerCase().includes(query))
    .slice(0,25).map(([title, action, id, job]) => `<button data-workspace="${action}" data-key="${escapeHTML(id)}" data-job="${job ?? ""}">
      ${escapeHTML(title)}</button>`).join("") || "<p>Sin resultados.</p>";
}
async function workspaceDialog(alerts = false) {
  const dialog = $("#workspace-dialog");
  $("#workspace-dialog-title").textContent = alerts ? "Notificaciones" : "Buscar";
  $("#workspace-query").hidden = alerts;
  $("#workspace-query").value = "";
  if (!dialog.open) dialog.showModal();
  if (!alerts) $("#workspace-query").focus();
  await refreshWorkspace();
  dialogResults(alerts);
}
$("#workspace-search").addEventListener("click", () => workspaceDialog());
$("#workspace-notifications").addEventListener("click", () => workspaceDialog(true));
$("#workspace-query").addEventListener("input", () => dialogResults());
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("#workspace-dialog").open) {
    event.preventDefault(); $("#workspace-dialog").close();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault(); workspaceDialog();
  }
});
window.addEventListener("studio-view", event => {
  workspaceView = event.detail;
  setPreference("view", workspaceView);
  if (workspaceView === "project-view" && state.id) setPreference("project", state.id);
  for (const button of document.querySelectorAll(".main-navigation button")) {
    if (button.id === workspaceViews[workspaceView]) button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  }
  if (["home-view","clips-view","calendar-view","statistics-view"].includes(workspaceView)) refreshWorkspace(true);
});
window.addEventListener("studio-ready", () => {
  updateWorkspaceProfile(); updateCollapsedRail();
  $("#workspace-profile-name").value = getPreference("profile");
  if (["#live","#publicaciones"].includes(location.hash)) return;
  const saved = getPreference("view","home-view");
  const project = getPreference("project");
  if (saved === "project-view" && /^[a-f0-9]{12}$/.test(project)) {
    selectProject(project).catch(() => showWorkspace("clips-view"));
  } else $("#" + (workspaceEntry[saved] || "show-home")).click();
});
setInterval(() => {
  if (!document.hidden && ["home-view","clips-view","calendar-view","statistics-view"].includes(workspaceView)) refreshWorkspace();
}, 9000);
