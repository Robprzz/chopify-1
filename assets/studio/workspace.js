"use strict";

const workspaceViews = {
  "home-view": "show-home", "live-view": "show-live", "clips-view": "show-clips-library",
  "new-view": "show-projects", "project-view": "show-projects", "projects-view": "show-projects",
  "clip-editor-view": "show-clips-library", "automation-view": "show-settings", "ai-settings-view": "show-settings",
  "trash-view": "show-settings",
  "calendar-view": "show-publications", "publications-view": "show-publications", "statistics-view": "show-statistics",
  "settings-view": "show-settings", "system-view": "show-settings", "watermark-view": "show-settings", "telegram-view": "show-settings",
};
const workspaceEntry = {...workspaceViews, "automation-view": "show-automation", "ai-settings-view": "show-ai-settings", "trash-view": "show-trash", "new-view": "new-project", "publications-view": "calendar-tiktok",
  "system-view": "show-system", "watermark-view": "show-watermark", "telegram-view": "show-telegram"};
const workspaceStates = {generated:"Nuevo clip detectado", ready:"Lista para revisión", review:"Revisión pendiente",
  rejected:"Rechazado", failed:"Fallido", exported:"Exportado", cancelled:"Cancelado",
  analyzing:"En evaluación", validating:"Control técnico", repairing:"Corrigiendo"};
let workspaceData = null;
let workspaceLoading = null;
let workspaceView = "clips-view";
let workspaceLast = 0;
const sessionPrefs = ["live-aspect","live-camera-mode","live-duration","live-max-height","live-threshold","live-per-hour","live-per-session","live-whisper-device"];

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
  return `<button type="button" class="thumb" data-workspace="preview-clip" data-key="${escapeHTML(clip.url)}" aria-label="Reproducir ${escapeHTML(clip.title)}">${clip.thumbnail ?
    `<img src="${escapeHTML(clip.thumbnail)}" loading="lazy" alt="" />` : `<video src="${escapeHTML(clip.url)}#t=0.1" muted playsinline preload="metadata" tabindex="-1" aria-hidden="true"></video>`}
    ${clip.duration == null ? "" : `<time>${mediaDuration(clip.duration)}</time>`}</button>`;
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
  $("#home-greeting").textContent = profile ? "Hola, " + profile : "Centro de operaciones";
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
  patchWorkspace("#home-recent", data.clips.slice(0, 5).map(clip => `<article class="home-clip">${clipThumb(clip)}
    <div><strong>${escapeHTML(clip.title)}</strong><p>${escapeHTML(clip.source)}</p></div>
    <div class="priority-actions">${clipActions(clip)}</div></article>`).join("") || '<p class="empty-card">Los clips nuevos aparecerán aquí.</p>');
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
  if (["clips-view","project-view"].includes(workspaceView)) renderLibrary();
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
function clipActions(clip) {
  const id = escapeHTML(clip.project + "/" + clip.filename);
  return `<button data-workspace="edit-clip" data-key="${id}">Editar clip</button>
    <button data-workspace="publish-clip" data-key="${id}">Publicar ahora</button>
    <button data-workspace="plan-clip" data-key="${id}">Programar</button>
    ${!clip.project.startsWith("live/") ? `<button data-workspace="open-project" data-key="${escapeHTML(clip.project)}">Abrir proyecto</button>` : ""}
    <button data-workspace="trash-clip" data-key="${id}">Enviar a la papelera</button>`;
}
function showClipPreview(url) {
  const dialog = $("#clip-preview-dialog");
  $("#clip-preview-video").src = url;
  if (!dialog.open) dialog.showModal();
}
$("#clip-preview-dialog").addEventListener("close", () => {
  const video = $("#clip-preview-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
});

let trashItems = [];
async function refreshTrash() {
  const {items} = await api("/api/trash");
  trashItems = items;
  $("#trash-empty").disabled = !items.length || items.some(item => item.blocked);
  $("#trash-summary").textContent = `${items.length} elementos · ${bytesLabel(items.reduce((n, item) => n + (item.bytes || 0),0))}`;
  $("#asset-trash-list").innerHTML = items.length ? [...items].reverse().map(item =>
    `<article class="workspace-card"><strong>${escapeHTML(item.title)}</strong>
    <p>${item.file ? "Clip" : "Proyecto"} · ${escapeHTML(item.project)} · ${bytesLabel(item.bytes)}</p>
    ${item.blocked ? `<p>${escapeHTML(item.blocked)}</p>` :
      `<details><summary>Archivos locales</summary><ul>${item.files.map(file => `<li>${escapeHTML(file)}</li>`).join("")}</ul></details>`}
    <div class="publication-actions"><button data-workspace="restore-asset" data-key="${escapeHTML(item.id)}">Restaurar</button>
    <button data-workspace="purge-asset" data-key="${escapeHTML(item.id)}" ${item.blocked ? "disabled" : ""}>Eliminar definitivamente</button></div></article>`
  ).join("") : '<p class="empty-card">La papelera está vacía.</p>';
}
function bytesLabel(value) {
  return value == null ? "Tamaño sin verificar" : new Intl.NumberFormat("es",{maximumFractionDigits:2}).format(value/1048576) + " MiB";
}
async function purgeTrash(items) {
  if (!items.length) return;
  const expected = items.length === 1 && !items[0].file ? items[0].title :
    ["ELIMINAR", ...items.filter(item => !item.file).map(item => item.title).sort()].join(" / ");
  const size = bytesLabel(items.reduce((total,item) => total + item.bytes,0));
  const files = items.flatMap(item => item.files);
  const summary = files.length > 12 ? `${files.slice(0,12).join("\n")}\n… y ${files.length - 12} más (consulta el listado en la papelera).` : files.join("\n");
  const confirmation = prompt(`Eliminación irreversible · ${size}\n${items.map(item => item.title).join(", ")}\n${summary}
Escribe ${expected} para continuar.`);
  if (confirmation === null) return;
  const result = await api("/api/trash/purge",{ids:items.map(item => item.id),confirmation});
  await refreshTrash();
  await refreshWorkspace(true);
  notice(result.message || "Archivos eliminados definitivamente.");
}
$("#trash-empty").addEventListener("click",safe(() => purgeTrash(trashItems)));
async function manageProject(id, action) {
  const request = {action: action.replace("-project", "")};
  if (request.action === "rename") {
    const detail = await api(`/api/projects/${id}`);
    request.name = prompt("Nombre del proyecto", detail.project.name || projectName(detail.project.source));
    if (request.name === null) return;
  }
  if (request.action === "trash" && !confirm("¿Enviar el proyecto a la papelera? Puedes restaurarlo después.")) return;
  const result = await api(`/api/projects/${id}/manage`, request);
  if (state.id === id) {
    if (request.action === "trash") {
      state.id = state.detail = null;
      setPreference("project", "");
      showWorkspace("projects-view");
    } else if (state.detail) {
      Object.assign(state.detail.project, result);
      $("#project-title").textContent = result.name || projectName(result.source);
    }
  }
  await refreshProjects();
  notice(request.action === "trash" ? "Proyecto enviado a la papelera." : "Proyecto actualizado.");
}
const libraryFilters = ["query","filter","sort","mode","project","origin","format","date"];
function libraryCard(clip) {
  const plans = workspaceData.plans.filter(row => row.project === clip.project && row.file === clip.filename);
  const providers = plans.map(row => `${calendarPlatforms[row.platform]}: ${planLabel(row)}`).join(" · ");
  return `<article class="workspace-card library-item">${clipThumb(clip)}
    <div class="library-info"><strong>${escapeHTML(clip.title)}</strong><p>${escapeHTML(clip.source)}
    · ${clip.project.startsWith("live/") ? "Live" : "Proyecto"} · ${escapeHTML(new Date(clip.created*1000).toLocaleDateString())}
    · ${escapeHTML(clip.format || "Formato sin verificar")} · ${mediaDuration(clip.duration)}</p>
    <p>Detección ${clip.score ?? "—"} · Prioridad ${clip.priority?.toFixed(1) ?? "—"} · ${workspaceStates[clip.status] || "En producción"}</p>
    <p>${escapeHTML(providers || "Sin programación registrada")}</p></div>
    <div class="publication-actions">${clipActions(clip)}
    <a href="${escapeHTML(clip.url)}" download>Descargar MP4</a></div></article>`;
}
$("#project-continue").addEventListener("click", () => $("#prepare").click());
$("#project-new-clip").addEventListener("click", () => {
  $("#show-clips").click();
  $("#clip-panel").scrollIntoView({block:"start"});
});
$(".project-heading").addEventListener("click",safe(event => {
  const action = event.target.closest("[data-project-manage]")?.dataset.projectManage;
  if (action && state.id) return manageProject(state.id,action);
}));
function renderLibrary() {
  const all = workspaceData?.clips || [];
  const projectSelect = $("#clip-library-project");
  const projects = new Map(all.filter(clip => !clip.project.startsWith("live/")).map(clip => [clip.project,clip.source]));
  const selected = projectSelect.value || getPreference(projectSelect.id);
  const options = JSON.stringify([...projects]);
  if (projectSelect.dataset.options !== options) {
    projectSelect.replaceChildren(new Option("Todos los proyectos",""), ...[...projects].map(([key,title]) => new Option(title,key)));
    projectSelect.value = projects.has(selected) ? selected : "";
    projectSelect.dataset.options = options;
  }
  const fields = Object.fromEntries(libraryFilters.map(key => [key,$("#clip-library-" + key).value]));
  const clips = all.filter(clip => (!fields.filter || fields.filter === clip.status || fields.filter === clip.level) &&
    (!fields.project || clip.project === fields.project) && (!fields.format || clip.format === fields.format) &&
    (!fields.origin || (clip.project.startsWith("live/") ? "live" : "project") === fields.origin) &&
    (!fields.date || calendarLocal(clip.created,workspaceData.timezone,false) >= fields.date) &&
    (clip.title + clip.source + clip.filename).toLocaleLowerCase().includes(fields.query.toLocaleLowerCase()));
  if (fields.sort === "priority") clips.sort((a,b) => (b.priority ?? -1) - (a.priority ?? -1));
  if (fields.sort === "title") clips.sort((a,b) => a.title.localeCompare(b.title));
  $("#clip-library").className = fields.mode === "list" ? "library-list" : "library-grid";
  patchWorkspace("#clip-library", clips.map(libraryCard).join("") || '<p class="empty-card">No hay exportaciones que coincidan con la búsqueda.</p>');
  if (workspaceView === "project-view") patchWorkspace("#project-exported-clips",
    all.filter(clip => clip.project === state.id).map(libraryCard).join("") || '<p class="empty-card">Este proyecto aún no tiene exportaciones.</p>');
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
  switchView(view);
  if (view === "clips-view") renderLibrary();
  if (view === "projects-view") refreshProjects().catch(error => notice(error.message, true));
  if (view === "trash-view") refreshTrash().catch(error => notice(error.message, true));
  if (view === "settings-view") settingsRoute(getPreference("settings-category","general"));
}
async function workspaceAction(action, key, job) {
  if (action === "open-project") return selectProject(key);
  if (action === "preview-clip") return showClipPreview(key);
  if (["rename-project","archive","unarchive","trash-project"].includes(action)) return manageProject(key, action);
  if (action === "purge-asset") return purgeTrash(trashItems.filter(item => item.id === key));
  if (action === "restore-asset") {
    await api("/api/trash/restore",{id:key});
    await refreshTrash();
    notice("Elemento restaurado.");
    return;
  }
  if (action === "trash-clip") {
    const clip = workspaceData?.clips.find(item => item.project + "/" + item.filename === key);
    if (!clip || !confirm("¿Enviar el clip a la papelera? Puedes restaurarlo después.")) return;
    await api("/api/trash/clip", {project:clip.project, filename:clip.filename});
    document.activeElement?.blur();
    await refreshWorkspace(true);
    notice("Clip enviado a la papelera.");
    return;
  }
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
  if (["edit-clip","publish-clip","plan-clip"].includes(action)) {
    const clip = workspaceData?.clips.find(item => item.project + "/" + item.filename === key);
    if (!clip) return;
    if (await showClipEditor(clip) === false) return;
    if (action !== "edit-clip") await publishFromClip(action === "publish-clip");
    return;
  }
  if (action === "open-plan") {
    await openCalendar();
    return openCalendarDetail(key);
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
  if (action === "queue" || action === "job-detail") {
    await settingsRoute("diagnostics");
    $("#technical-jobs").open = true;
    if (job) {
      const detail = await api("/api/production/job?id=" + encodeURIComponent(job));
      showProductionDetail(detail);
    }
    $(job ? "#production-detail" : "#production-heading").scrollIntoView({block:"start"});
    return;
  }
  if (["review", "pending", "recommended"].includes(action)) {
    const clip = workspaceData?.clips.find(item => item.job === Number(job));
    if (clip) return showClipEditor(clip);
    $("#clip-library-filter").value = action === "pending" ? "review" : "";
    $("#clip-library-sort").value = "priority";
    showWorkspace("clips-view");
  }
}

document.addEventListener("click", safe(async event => {
  const target = event.target.closest("[data-workspace]");
  if (!target) return;
  if ($("#workspace-dialog").open) $("#workspace-dialog").close();
  await workspaceAction(target.dataset.workspace, target.dataset.key, target.dataset.job);
}));
for (const [button, view] of [["show-home","home-view"],["show-clips-library","clips-view"],["show-projects","projects-view"],["show-trash","trash-view"],
  ["show-statistics","statistics-view"],["show-settings","settings-view"]]) {
  $("#" + button).addEventListener("click", () => showWorkspace(view));
}
$("#projects-filter").addEventListener("change", safe(refreshProjects));
$("#brand-home").addEventListener("click", () => showWorkspace("clips-view"));
$("#rail-toggle").addEventListener("click", () => {
  setPreference("collapsed", getPreference("collapsed") === "1" ? "0" : "1"); updateCollapsedRail();
});
$("#statistics-period").addEventListener("change", () => refreshWorkspace(true));
for (const key of libraryFilters) {
  const id = "clip-library-" + key;
  const field = $("#" + id);
  const saved = getPreference(id);
  if (saved && key !== "project") field.value = saved;
  field.addEventListener(key === "query" ? "input" : "change", () => {
    setPreference(id,field.value);
    renderLibrary();
  });
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
const settingsViews = {"automation-view":"automation","ai-settings-view":"ai","watermark-view":"appearance",
  "telegram-view":"integrations","trash-view":"storage","system-view":"diagnostics"};
const settingsTriggers = {automation:"show-automation",ai:"show-ai-settings",appearance:"show-watermark",
  integrations:"show-telegram",storage:"show-trash",diagnostics:"show-system"};
const settingsNav = [...document.querySelectorAll("[data-setting]")];
const settingsVideoKeys = {"new-aspect":"Formato de proyectos nuevos","live-aspect":"Formato Live","live-camera-mode":"Encuadre Live",
  "live-max-height":"Resolución máxima Live","new-style":"Estilo de subtítulos","new-model":"Modelo de transcripción"};
const settingsRows = [...$("#settings-categories").children];
function settingsDisplay(category, view) {
  setPreference("settings-category",category);
  settingsNav.forEach(item => {
    if (item.dataset.setting === category) item.setAttribute("aria-current","page");
    else item.removeAttribute("aria-current");
  });
  $("#settings-breadcrumb").textContent = "Configuración / " + settingsNav.find(item => item.dataset.setting === category).textContent;
  settingsRows.forEach(item => { item.hidden = item.dataset.settingPanel !== category; });
  for (const item of settingsRows.filter(item => item.tagName === "DETAILS" && !item.hidden)) item.open = true;
  for (const [id,key] of Object.entries({"settings-brand":"appearance","settings-tiktok":"integrations",
    "settings-disk":"storage","settings-video":"video"})) $("#" + id).hidden = category !== key;
  for (const section of $("#settings-embedded").children) section.hidden = section.id !== view;
  if (category === "integrations") {
    $("#settings-tiktok-controls").append($("#tiktok-account-controls"));
    refreshPublications().catch(error => notice(error.message,true));
  }
  if (category === "storage") api("/api/system/status").then(data => {
    $("#settings-disk-path").textContent = data.projects_dir || "Detalles de ocupación por elemento en la papelera.";
  }).catch(() => {});
}
async function settingsRoute(category) {
  if (settingsTriggers[category]) return $("#" + settingsTriggers[category]).click();
  switchView("settings-view");
  settingsDisplay(category);
}
function initSettings() {
  for (const id of Object.keys(settingsViews)) $("#settings-embedded").append($("#" + id));
  for (const item of settingsNav.filter(item => !settingsTriggers[item.dataset.setting])) item.addEventListener("click", () => settingsRoute(item.dataset.setting));
  for (const [id,label] of Object.entries(settingsVideoKeys)) {
    const input = $("#" + id);
    const saved = getPreference(id);
    if (saved && [...input.options].some(option => option.value === saved)) input.value = saved;
    const copy = input.cloneNode(true);
    copy.id = "config-" + id;
    const node = document.createElement("label");
    node.textContent = label;
    node.append(copy);
    $("#settings-video-fields").append(node);
  }
  $("#settings-video").addEventListener("submit",event => {
    event.preventDefault();
    for (const id of Object.keys(settingsVideoKeys)) {
      const saved = $("#config-" + id).value;
      $("#" + id).value = saved;
      setPreference(id,saved);
    }
    notice("Preferencias de video guardadas.");
  });
}
$("#settings-query").addEventListener("input",event => {
  const normalize = text => text.normalize("NFD").replace(/\p{M}/gu,"").toLocaleLowerCase().trim();
  const query = normalize(event.target.value);
  for (const item of settingsNav) item.hidden = !normalize(item.textContent + " " + item.dataset.keywords).includes(query);
  $("#settings-empty").hidden = settingsNav.some(item => !item.hidden);
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
const workspaceHashes = {"clips-view":"clips","projects-view":"proyectos","new-view":"proyecto-nuevo",
  "live-view":"live","calendar-view":"calendario","publications-view":"publicaciones",
  "home-view":"operaciones","statistics-view":"estadisticas","settings-view":"configuracion"};
let restoringRoute = false;
function recordStudioRoute(view) {
  let hash = workspaceHashes[view];
  if (settingsViews[view]) hash = "configuracion";
  if (view === "project-view") hash = "proyecto/" + state.id;
  if (view === "clip-editor-view" && clipEditor.clip) hash = "clip?" + new URLSearchParams({
    project:clipEditor.clip.project, file:clipEditor.clip.filename, origin:clipEditor.origin
  });
  if (hash && location.hash !== "#" + hash && !restoringRoute) history.pushState(null,"","#" + hash);
}
async function restoreStudioRoute() {
  const hash = location.hash.slice(1);
  if (hash.startsWith("clip?")) {
    const key = new URLSearchParams(hash.slice(5));
    try {
      if (key.get("origin") === "project-view" && /^[a-f0-9]{12}$/.test(key.get("project"))) await selectProject(key.get("project"));
      await showClipEditor({project:key.get("project"),filename:key.get("file")},key.get("origin") || "clips-view");
    } catch { showWorkspace("clips-view"); }
  } else if (/^proyecto\/[a-f0-9]{12}$/.test(hash)) {
    try { await selectProject(hash.slice(9)); } catch { showWorkspace("projects-view"); }
  } else {
    const view = Object.keys(workspaceHashes).find(id => workspaceHashes[id] === hash);
    $("#" + (workspaceEntry[view] || "show-clips-library")).click();
  }
}
window.addEventListener("popstate", safe(async () => {
  restoringRoute = true;
  try { await restoreStudioRoute(); } finally { restoringRoute = false; }
}));
window.addEventListener("studio-view", event => {
  workspaceView = event.detail;
  if (workspaceView === "publications-view") $("#tiktok-local-account").append($("#tiktok-account-controls"));
  setPreference("view", workspaceView);
  if (workspaceView === "project-view" && state.id) {setPreference("project", state.id); renderLibrary();}
  for (const button of document.querySelectorAll(".main-navigation button")) {
    if (button.id === workspaceViews[workspaceView]) button.setAttribute("aria-current","page");
    else button.removeAttribute("aria-current");
  }
  if (["home-view","clips-view","project-view","calendar-view","statistics-view"].includes(workspaceView)) refreshWorkspace(true);
});
document.addEventListener("DOMContentLoaded", () => {
  initSettings();
  updateWorkspaceProfile(); updateCollapsedRail();
  $("#workspace-profile-name").value = getPreference("profile");
  if (studioNavigation || ["#live","#publicaciones"].includes(location.hash)) return;
  const project = getPreference("project");
  if (/^[a-f0-9]{12}$/.test(project)) state.id = project;
  if (!location.hash) showWorkspace("clips-view");
  else studioConfig.then(restoreStudioRoute).catch(() => showWorkspace("clips-view"));
});
setInterval(() => {
  if (!document.hidden && ["home-view","clips-view","calendar-view","statistics-view"].includes(workspaceView)) refreshWorkspace();
}, 9000);
