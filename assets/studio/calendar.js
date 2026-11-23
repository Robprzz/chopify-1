"use strict";

const calendarPlatforms = {tiktok:"TikTok", youtube:"YouTube Shorts", facebook:"Facebook Reels", instagram:"Instagram Reels"};
const calendarStatuses = {draft:"Borrador", pending_review:"Pendiente de revisión", editorial_approved:"Aprobado editorialmente",
  ready:"Listo para programar", scheduled:"Programado", requires_action:"Requiere acción", processing:"En proceso",
  published:"Publicado", failed:"Fallido", cancelled:"Cancelado"};
const calendarPriorities = {urgent:"Urgente", high:"Alta", normal:"Normal", low:"Baja"};
let calendarData = null;
let calendarDetail = null;
let calendarDraftId = null;
let calendarLoading = null;
let calendarBusy = false;
let calendarDirty = false;
let calendarPrefsInitialized = false;
let calendarDetailRequest = 0;

const calendarApi = (action, body) => api("/api/calendar/" + action, body);
function calendarLocal(seconds, zone, includeTime = true) {
  const format = new Intl.DateTimeFormat("en-CA", {timeZone:zone, year:"numeric", month:"2-digit", day:"2-digit",
    ...(includeTime ? {hour:"2-digit", minute:"2-digit", hourCycle:"h23"} : {})});
  const parts = Object.fromEntries(format.formatToParts(new Date(seconds * 1000)).map(item => [item.type,item.value]));
  return `${parts.year}-${parts.month}-${parts.day}` + (includeTime ? `T${parts.hour}:${parts.minute}` : "");
}
function calendarDate(seconds) {
  return calendarLocal(seconds, calendarData.preferences.timezone, false);
}
function planLabel(item) {
  return calendarStatuses[item.status] + (item.confirmation === "manual" ? " · confirmación manual" :
    item.confirmation === "api" ? " · confirmado por TikTok" : "");
}
function calendarEntry(item, grid = false) {
  const preview = workspaceData?.clips.find(clip => clip.project === item.project && clip.filename === item.file);
  const time = item.scheduled == null ? "Sin fecha" : calendarLocal(item.scheduled, calendarData.preferences.timezone);
  return `<button type="button" class="calendar-entry" data-workspace="open-plan" data-key="${escapeHTML(item.id)}">
    ${preview?.thumbnail ? `<img src="${escapeHTML(preview.thumbnail)}" loading="lazy" alt="" />` : ""}
    <strong>${escapeHTML(item.title)}</strong><small>${grid ? time.slice(11) : time.replace("T", " · ")} · ${calendarPlatforms[item.platform]}</small>
    <small>${calendarPriorities[item.priority]} · ${mediaDuration(item.duration)} · ${escapeHTML(planLabel(item))}</small></button>`;
}
function renderCalendar() {
  if (!calendarData) return;
  const {preferences, plans} = calendarData;
  const mode = $("#calendar-view-mode").value;
  const chosen = $("#calendar-date").value || calendarDate(Date.now()/1000);
  const target = new Date(chosen + "T12:00:00Z");
  if (!Number.isFinite(target.getTime())) return;
  const first = new Date(target);
  if (mode === "month") first.setUTCDate(1);
  if (mode !== "day") first.setUTCDate(first.getUTCDate() - (first.getUTCDay() + 6) % 7);
  const platform = $("#calendar-platform-filter").value, status = $("#calendar-status-filter").value;
  const filtered = plans.filter(item => (!platform || item.platform === platform) && (!status || item.status === status));
  const grid = $("#calendar-grid");
  grid.dataset.mode = mode;
  const cells = Array.from({length:mode === "month" ? 42 : mode === "week" ? 7 : 1}, (_, index) => {
    const day = new Date(first);
    day.setUTCDate(day.getUTCDate() + index);
    const key = day.toISOString().slice(0,10);
    const outside = mode === "month" && day.getUTCMonth() !== target.getUTCMonth();
    const label = new Intl.DateTimeFormat("es", {weekday:"short", day:"numeric", month:"short",timeZone:"UTC"}).format(day);
    return `<section class="calendar-cell${outside ? " outside" : ""}" aria-label="${escapeHTML(label)}"><header>${escapeHTML(label)}</header>
      ${filtered.filter(item => item.scheduled != null && calendarDate(item.scheduled) === key).map(item => calendarEntry(item,true)).join("")}</section>`;
  }).join("");
  patchWorkspace("#calendar-grid",cells);
  $("#calendar-zone").textContent = preferences.timezone;
  patchWorkspace("#calendar-upcoming",filtered.filter(row => row.scheduled > Date.now()/1000 &&
    !["cancelled","published"].includes(row.status)).sort((a,b) => a.scheduled - b.scheduled).slice(0,8).map(item => calendarEntry(item)).join("") || "<p>Sin próximas programaciones.</p>");
  patchWorkspace("#calendar-drafts",filtered.filter(row => row.scheduled == null && row.status !== "cancelled").slice(0,12)
    .map(item => calendarEntry(item)).join("") || "<p>Sin tareas sin fecha.</p>");
  const candidates = workspaceData?.priority || [];
  const planned = new Set(plans.filter(item => item.status !== "cancelled").map(item => item.job));
  const shortcuts = items => items.slice(0,8).map(clip => `<button type="button" class="calendar-entry" data-workspace="plan-clip" data-key="${escapeHTML(clip.project + "/" + clip.filename)}">
      <strong>${escapeHTML(clip.title)}</strong><small>${clip.level === "urgent" ? "Urgente · " : ""}${mediaDuration(clip.duration)} · ${clip.review === "approved" ? "Aprobado" : "Revisión editorial"}</small></button>`).join("");
  patchWorkspace("#calendar-urgent",shortcuts(candidates.filter(clip => clip.level === "urgent" && !planned.has(clip.job))) || "<p>Sin clips urgentes por programar.</p>");
  patchWorkspace("#calendar-unplanned",shortcuts(candidates.filter(clip => !planned.has(clip.job))) || "<p>Sin clips elegibles por programar.</p>");
}
async function refreshCalendar() {
  const date = $("#calendar-date").value;
  if (calendarLoading?.date === date) return calendarLoading.pending;
  const pending = api("/api/calendar" + (date ? "?date=" + encodeURIComponent(date) : "")).then(data => {
    if (calendarLoading?.pending !== pending) return;
    calendarData = data;
    if (!$("#calendar-date").value) $("#calendar-date").value = calendarDate(Date.now()/1000);
    renderCalendar();
  }).finally(() => { if (calendarLoading?.pending === pending) calendarLoading = null; });
  calendarLoading = {date, pending};
  return pending;
}
async function openCalendar() {
  state.id = null;
  switchView("calendar-view");
  await Promise.all([refreshCalendar(), refreshWorkspace()]);
}
function selectedCalendarClip(key) {
  return workspaceData?.clips.find(clip => clip.project + "/" + clip.filename === key);
}
function setCalendarPreview(url, poster) {
  const video = $("#calendar-preview");
  if (url && !video.src.endsWith(url)) video.src = url;
  if (!url && video.hasAttribute("src")) {video.removeAttribute("src"); video.load();}
  if (poster) video.poster = poster;
  else video.removeAttribute("poster");
  video.hidden = !url;
}
async function fillCalendarClip(clip) {
  const serial = ++calendarDetailRequest;
  setCalendarPreview(clip?.url, clip?.thumbnail);
  if (!clip) return;
  $("#calendar-title").value = String(clip.title).slice(0,160);
  $("#calendar-description").value = "";
  $("#calendar-tags").value = "";
  if (clip.level) $("#calendar-priority").value = clip.level;
  if (clip.job) {
    const copy = await api("/api/calendar-clip?job=" + clip.job);
    if (serial !== calendarDetailRequest) return;
    if (!$("#calendar-title").matches(":focus") && $("#calendar-title").value === String(clip.title).slice(0,160)) $("#calendar-title").value = copy.title;
    if (!$("#calendar-description").matches(":focus") && !$("#calendar-description").value) $("#calendar-description").value = copy.description;
    if (!$("#calendar-tags").matches(":focus") && !$("#calendar-tags").value) $("#calendar-tags").value = copy.hashtags.join(" ");
  }
}
function confirmCalendarEdits() {
  return !calendarDirty || confirm("¿Descartar los cambios sin guardar de esta programación?");
}
async function openCalendarDetail(id = null, key = null) {
  if (!confirmCalendarEdits()) return;
  calendarDirty = false;
  if (!calendarData) await refreshCalendar();
  const serial = ++calendarDetailRequest;
  const detail = id ? await api("/api/calendar?id=" + encodeURIComponent(id)) : null;
  if (serial !== calendarDetailRequest) return;
  calendarDetail = detail;
  calendarDraftId = detail?.id || crypto.randomUUID();
  const zone = detail?.zone || calendarData.preferences.timezone;
  const form = $("#calendar-form");
  form.reset();
  const select = $("#calendar-clip");
  select.replaceChildren(new Option("Elige un clip terminado",""), ...(workspaceData?.clips || []).map(clip =>
    new Option(clip.title,clip.project + "/" + clip.filename)));
  const currentKey = detail ? detail.project + "/" + detail.file : key;
  if (currentKey && ![...select.options].some(option => option.value === currentKey)) {
    select.add(new Option(detail?.title || currentKey, currentKey));
  }
  select.disabled = Boolean(detail);
  select.value = currentKey || "";
  $("#calendar-local-zone").value = zone;
  $("#calendar-local").value = detail?.scheduled == null ? "" : calendarLocal(detail.scheduled, zone);
  $("#calendar-refresh-line").hidden = !(detail?.stale || detail?.can_link_production);
  $("#calendar-refresh-version").checked = false;
  $("#calendar-detail-title").textContent = detail ? detail.title : "Nueva programación";
  $("#calendar-detail-status").textContent = detail ? planLabel(detail) +
    (detail.stale ? " · Versión obsoleta. Actualiza la referencia." : "") + (detail.error ? " · " + detail.error : "") : "";
  $("#calendar-events").innerHTML = (detail?.history || []).map(item => `<li>${escapeHTML(new Date(item.at*1000).toLocaleString())} ·
    ${escapeHTML(calendarStatuses[item.action] || ({created:"Creada",updated:"Actualizada"}[item.action]) || item.action)}${item.detail ? " · " + escapeHTML(calendarStatuses[item.detail] || item.detail) : ""}</li>`).join("");
  const terminal = detail && ["cancelled","published"].includes(detail.status);
  const readonly = Boolean(terminal || detail?.external_id);
  $("#calendar-edit-tools").hidden = readonly;
  for (const field of form.querySelectorAll("input, select, textarea")) field.disabled = readonly || (field === select && !!detail);
  const actions = [];
  if (detail && !terminal) {
    if (!detail.external_id) {
      actions.push(["cancel", "Cancelar programación"]);
      if (detail.job && !detail.current_approval) actions.push(["review", "Revisar en Automation"]);
      if (detail.current_approval) {
        if (detail.platform === "tiktok") actions.push(["prepare", "Abrir revisión de TikTok"]);
        else actions.push(["prepare", "Preparar paquete manual"],["confirm", "Confirmar publicación manual"]);
      }
    } else if (detail.status === "failed" && !detail.publish_id) actions.push(["retry", "Preparar un nuevo intento"]);
    if (detail.external_id) actions.push(["history", "Ver historial de TikTok"]);
  }
  $("#calendar-actions").innerHTML = actions.map(([action,label]) =>
    `<button type="button" data-calendar-action="${action}">${label}</button>`).join("");
  $("#calendar-detail").hidden = false;
  const clip = selectedCalendarClip(currentKey);
  if (detail) {
    $("#calendar-platform").value = detail.platform;
    $("#calendar-priority").value = detail.priority;
    $("#calendar-title").value = detail.title;
    $("#calendar-description").value = detail.description;
    $("#calendar-tags").value = detail.hashtags.join(" ");
    $("#calendar-ready").checked = detail.status === "ready";
    const fallback = clip?.url || "/media/" + detail.project + "/clips/" + encodeURIComponent(detail.file);
    setCalendarPreview(detail.stale ? null : fallback, clip?.thumbnail);
  } else await fillCalendarClip(clip);
}
async function calendarMutation(operation) {
  if (calendarBusy) return;
  calendarBusy = true;
  try {
    await operation();
  } catch (error) {
    $("#calendar-feedback").textContent = error.message;
    notice(error.message, true);
  } finally {
    calendarBusy = false;
  }
}
function dateStep(step) {
  const value = $("#calendar-date");
  const date = new Date(value.value + "T12:00Z");
  if (!Number.isFinite(date.getTime())) return;
  const mode = $("#calendar-view-mode").value;
  if (mode === "month") {date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth()+step);}
  else date.setUTCDate(date.getUTCDate() + step * (mode === "week" ? 7 : 1));
  value.value = date.toISOString().slice(0,10);
  refreshCalendar().catch(error => notice(error.message, true));
}
function copyPackage(item) {
  const clip = selectedCalendarClip(item.project + "/" + item.file);
  const name = item.file;
  const payload = {platform:item.platform, title:item.title, description:item.description, hashtags:item.hashtags,
    scheduled_utc:item.scheduled == null ? null : new Date(item.scheduled*1000).toISOString(), clip:name};
  const metadata = document.createElement("a");
  metadata.href = URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json"}));
  metadata.download = "anqora-" + item.id.slice(0,8) + ".json";
  metadata.click();
  setTimeout(() => URL.revokeObjectURL(metadata.href),2000);
  const media = document.createElement("a");
  media.href = clip?.url || "/media/" + item.project + "/clips/" + encodeURIComponent(name);
  media.download = name;
  media.textContent = "Descargar MP4 original";
  $("#calendar-actions").append(media);
}
for (const [key,label] of Object.entries(calendarPlatforms)) {
  $("#calendar-platform").add(new Option(label,key));
  $("#calendar-platform-filter").add(new Option(label,key));
}
for (const [key,label] of Object.entries(calendarStatuses)) $("#calendar-status-filter").add(new Option(label,key));
$("#show-publications").addEventListener("click", safe(openCalendar));
$("#calendar-create").addEventListener("click", safe(() => openCalendarDetail()));
$("#calendar-close-detail").addEventListener("click", () => {
  if (!confirmCalendarEdits()) return;
  calendarDirty = false;
  ++calendarDetailRequest; $("#calendar-detail").hidden = true; setCalendarPreview(null);
});
$("#calendar-clip").addEventListener("change", safe(() => fillCalendarClip(selectedCalendarClip($("#calendar-clip").value))));
$("#calendar-date").addEventListener("change",safe(refreshCalendar));
for (const id of ["calendar-view-mode","calendar-platform-filter","calendar-status-filter"]) {
  $("#" + id).addEventListener("change",renderCalendar);
}
$("#calendar-previous").addEventListener("click",() => dateStep(-1));
$("#calendar-next").addEventListener("click",() => dateStep(1));
$("#calendar-form").addEventListener("input", () => { calendarDirty = true; });
$("#calendar-form").addEventListener("submit", event => {
  event.preventDefault();
  calendarMutation(async () => {
    const clip = selectedCalendarClip($("#calendar-clip").value);
    const when = $("#calendar-local").value;
    if (calendarDetail?.scheduled && when !== calendarLocal(calendarDetail.scheduled, calendarDetail.zone)
        && !confirm("¿Cambiar la hora de esta programación?")) return;
    const saved = await calendarApi("save",{
      id:calendarDraftId, ...(calendarDetail ? {revision:calendarDetail.revision} :
        {job:clip?.job || null, project:clip?.project, file:clip?.filename}),
      platform:$("#calendar-platform").value, priority:$("#calendar-priority").value,
      title:$("#calendar-title").value, description:$("#calendar-description").value,
      hashtags:$("#calendar-tags").value.trim().split(/\s+/).filter(Boolean), local:when, zone:$("#calendar-local-zone").value,
      ready:$("#calendar-ready").checked, refresh_version:$("#calendar-refresh-version").checked,
    });
    calendarDirty = false;
    if (saved.scheduled != null) $("#calendar-date").value = calendarDate(saved.scheduled);
    $("#calendar-feedback").textContent = "Programación guardada.";
    await refreshCalendar();
    await openCalendarDetail(saved.id);
  });
});
$("#calendar-suggest").addEventListener("click",() => calendarMutation(async () => {
  const slot = await calendarApi("suggest",{id:calendarDraftId,platform:$("#calendar-platform").value,priority:$("#calendar-priority").value});
  calendarDirty = true;
  $("#calendar-local").value = slot.local;
  $("#calendar-local-zone").value = slot.zone;
  $("#calendar-feedback").textContent = "Horario sugerido según preferencias y huecos de la agenda.";
}));
$("#calendar-actions").addEventListener("click",event => {
  const button = event.target.closest("[data-calendar-action]");
  if (!button || !calendarDetail) return;
  calendarMutation(async () => {
    const action = button.dataset.calendarAction;
    if (action === "history") return showPublications();
    if (action === "review") return workspaceAction("review", null, calendarDetail.job);
    if (!confirmCalendarEdits()) return;
    calendarDirty = false;
    if (action === "cancel" && !confirm("¿Cancelar esta programación?")) return;
    if (action === "confirm" && !confirm("¿Confirmas que este clip ya está publicado en la plataforma indicada?")) return;
    const result = await calendarApi(action,{id:calendarDetail.id,revision:calendarDetail.revision,confirmed:action === "confirm"});
    if (action === "prepare" && result.platform === "tiktok") {
      return reviewPublicationClip(result.project + "/" + result.file,
        [result.description,result.hashtags.join(" ")].filter(Boolean).join("\n\n"),result);
    }
    await refreshCalendar();
    await openCalendarDetail(result.id);
    if (action === "prepare") copyPackage(result);
  });
});
function fillCalendarPreferences(prefs) {
  calendarPrefsInitialized = true;
  $("#schedule-timezone").value = prefs.timezone;
  $("#schedule-daily").value = prefs.daily;
  $("#schedule-interval").value = prefs.interval;
  $("#schedule-hours").value = prefs.hours.join(", ");
}
$("#show-settings").addEventListener("click",safe(async () => {
  if (calendarPrefsInitialized) return;
  if (calendarData) fillCalendarPreferences(calendarData.preferences);
  else {await refreshCalendar(); fillCalendarPreferences(calendarData.preferences);}
}));
$("#calendar-preferences").addEventListener("submit",event => {
  event.preventDefault();
  calendarMutation(async () => {
    const prefs = await calendarApi("preferences",{timezone:$("#schedule-timezone").value,
      daily:$("#schedule-daily").valueAsNumber, interval:$("#schedule-interval").valueAsNumber,
      hours:$("#schedule-hours").value.split(",").map(value => value.trim()).filter(Boolean)});
    fillCalendarPreferences(prefs); await refreshCalendar(); notice("Preferencias guardadas.");
  });
});
$("#schedule-reset").addEventListener("click",() => calendarMutation(async () => {
  if (!confirm("¿Restaurar los horarios del calendario? Las programaciones guardadas conservarán sus fechas.")) return;
  const prefs = await calendarApi("preferences",{timezone:"America/Santo_Domingo",daily:3,interval:60,hours:["09:00","13:00","19:00"]});
  fillCalendarPreferences(prefs); await refreshCalendar();
}));
window.addEventListener("workspace-data",() => {
  if (!$("#calendar-view").hidden) renderCalendar();
});
window.addEventListener("calendar-evidence",() => refreshCalendar().catch(() => {}));
setInterval(() => {
  if (!document.hidden && !$("#calendar-view").hidden && !calendarBusy) refreshCalendar().catch(() => {});
}, 12000);
