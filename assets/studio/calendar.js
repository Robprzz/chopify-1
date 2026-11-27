"use strict";

const calendarPlatforms = {tiktok:"TikTok", youtube:"YouTube Shorts", facebook:"Facebook Reels", instagram:"Instagram Reels"};
const calendarStatuses = {draft:"Borrador", pending_review:"Borrador anterior", editorial_approved:"Borrador anterior",
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
let calendarImmediate = false;
let calendarAccount = "";
const draftStatuses = new Set(["draft", "pending_review", "editorial_approved", "ready"]);
const checkedDrafts = new Set();
let visibleDrafts = [];

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
    item.confirmation === "api" ? " · confirmado por TikTok" : item.platform === "tiktok" && item.publish_id ? " · buzón de TikTok" : "");
}
function calendarEntry(item, grid = false) {
  const preview = workspaceData?.clips.find(clip => clip.project === item.project && clip.filename === item.file);
  const time = item.scheduled == null ? "Sin fecha" : calendarLocal(item.scheduled, calendarData.preferences.timezone);
  return `<button type="button" class="calendar-entry" data-workspace="open-plan" data-key="${escapeHTML(item.id)}">
    ${preview?.thumbnail ? `<img src="${escapeHTML(preview.thumbnail)}" loading="lazy" alt="" />` : ""}
    <strong>${escapeHTML(item.title)}</strong><small>${grid ? time.slice(11) : time.replace("T", " · ")} · ${calendarPlatforms[item.platform]}</small>
    <small>${calendarPriorities[item.priority]} · ${mediaDuration(item.duration)} · ${escapeHTML(planLabel(item))}</small></button>`;
}
function draftCard(item) {
  return `<article class="calendar-draft-card">
    ${calendarEntry(item)}
    <div class="publication-actions">
      ${item.deletable ? `<label class="check"><input type="checkbox" data-draft-check="${escapeHTML(item.id)}"
        ${checkedDrafts.has(item.id) ? "checked" : ""}/> Seleccionar</label>
        <button type="button" data-draft-remove="${escapeHTML(item.id)}">Eliminar borrador</button>`
        : "<small>Plan protegido por su historial de entrega.</small>"}
    </div></article>`;
}
function draftSelection() {
  for (const checkbox of document.querySelectorAll("[data-draft-check]")) {
    checkbox.checked = checkedDrafts.has(checkbox.dataset.draftCheck);
  }
  $("#calendar-draft-count").textContent = checkedDrafts.size + " seleccionados";
  $("#calendar-remove-drafts").disabled = !checkedDrafts.size;
  $("#calendar-clear-drafts").disabled = !checkedDrafts.size;
  $("#calendar-select-drafts").disabled = !visibleDrafts.some(item => item.deletable);
}
function filterCalendar(plan) {
  const platform = $("#calendar-platform-filter").value, status = $("#calendar-status-filter").value;
  return (!platform || plan.platform === platform) && (!status ||
    (status === "draft-only" ? draftStatuses.has(plan.status) && plan.scheduled == null : plan.status === status));
}
async function removeDrafts(items) {
  if (!items.length || !await confirmDeletion("Eliminar " + items.length + " borradores del calendario. Los MP4 se conservarán.")) return;
  document.activeElement?.blur();
  const result = await calendarApi("delete-drafts", {confirmed:true, items:items.map(({id, revision}) => ({id, revision}))});
  for (const id of result.ids) checkedDrafts.delete(id);
  if (result.ids.includes(calendarDetail?.id)) {
    calendarDetail = null;
    $("#calendar-detail").hidden = true;
  }
  await refreshCalendar(true);
  await refreshWorkspace(true);
  $("#calendar-draft-message").textContent = result.deleted + " borradores eliminados."
    + (result.excluded.length ? " Excluidos: " + result.excluded.map(item => (item.title || item.id) + " — " + item.reason).join(" / ") : "");
}

function renderCalendar() {
  if (!calendarData) return;
  const batch = $("#batch-clips");
  const selected = new Set([...batch.selectedOptions].map(item => item.value));
  batch.replaceChildren(...(workspaceData?.clips || []).map(clip => {
    const key = clip.project + "/" + clip.filename;
    return new Option(clip.title,key,false,selected.has(key));
  }));
  const {preferences, plans} = calendarData;
  const modeFilter = $("#publishing-view-mode").value;
  const statusGroups = {draft:["draft","pending_review","editorial_approved","ready"],scheduled:["scheduled"],
    attention:["requires_action","failed"],history:["published","cancelled","failed"]};
  $("#calendar-grid").hidden = Boolean(modeFilter);
  $("#publishing-list").hidden = !modeFilter;
  if (modeFilter) {
    const visible = plans.filter(plan => modeFilter === "sent" ? Boolean(plan.publish_id || plan.confirmation) :
      statusGroups[modeFilter].includes(plan.status));
    patchWorkspace("#publishing-list", visible.filter(filterCalendar)
      .map(plan => modeFilter === "draft" && plan.scheduled == null ? draftCard(plan) : calendarEntry(plan)).join("") || "<p>Sin planes en esta vista.</p>");
  }
  const mode = $("#calendar-view-mode").value;
  const chosen = $("#calendar-date").value || calendarDate(Date.now()/1000);
  const target = new Date(chosen + "T12:00:00Z");
  if (!Number.isFinite(target.getTime())) return;
  const first = new Date(target);
  if (mode === "month") first.setUTCDate(1);
  if (mode !== "day") first.setUTCDate(first.getUTCDate() - (first.getUTCDay() + 6) % 7);
  const filtered = plans.filter(filterCalendar);
  const deleteList = $("#calendar-delete-selection");
  const selectedForDelete = new Set([...deleteList.selectedOptions].map(option => option.value));
  const deletable = filtered.filter(item => item.deletable);
  patchWorkspace("#calendar-delete-selection", deletable.map(item => `<option value="${escapeHTML(item.id)}"
    ${selectedForDelete.has(item.id) ? "selected" : ""}>${escapeHTML(item.title)} · ${escapeHTML(planLabel(item))}</option>`).join(""));
  $("#calendar-delete-batch").disabled = !deletable.length;
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
  visibleDrafts = filtered.filter(row => row.scheduled == null && draftStatuses.has(row.status));
  const eligible = new Set(visibleDrafts.filter(item => item.deletable).map(item => item.id));
  for (const id of checkedDrafts) if (!eligible.has(id)) checkedDrafts.delete(id);
  patchWorkspace("#calendar-drafts", visibleDrafts.map(draftCard).join("") || "<p>No hay borradores con estos filtros.</p>");
  draftSelection();
  const candidates = workspaceData?.clips || [];
  const planned = new Set(plans.filter(item => item.status !== "cancelled").map(item => item.project + "/" + item.file));
  const shortcuts = items => items.slice(0,8).map(clip => `<button type="button" class="calendar-entry" data-workspace="plan-clip" data-key="${escapeHTML(clip.project + "/" + clip.filename)}">
      <strong>${escapeHTML(clip.title)}</strong><small>${clip.level === "urgent" ? "Urgente · " : ""}${mediaDuration(clip.duration)} · </small></button>`).join("");
  patchWorkspace("#calendar-urgent",shortcuts(candidates.filter(clip => clip.level === "urgent" && !planned.has(clip.project + "/" + clip.filename))) || "<p>Sin clips urgentes por programar.</p>");
  patchWorkspace("#calendar-unplanned",shortcuts(candidates.filter(clip => !planned.has(clip.project + "/" + clip.filename))) || "<p>Sin clips elegibles por programar.</p>");
}
async function refreshCalendar(force = false) {
  const date = $("#calendar-date").value;
  if (!force && calendarLoading?.date === date) return calendarLoading.pending;
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
  switchView("calendar-view");
  api("/api/publishing-capabilities").then(data => {
    $("#platform-capabilities").textContent = (data.tiktok.inbox ? "TikTok: buzón disponible. " : "TikTok: conecta una cuenta con permiso de subida. ")
      + "Publicación directa no disponible. Shorts y Reels: paquete manual.";
  }).catch(() => { $("#platform-capabilities").textContent = "No se pudo comprobar la disponibilidad de las plataformas."; });
  await Promise.all([refreshCalendar(), refreshWorkspace(), api("/api/tiktok/state", {}).then(response => {
    calendarAccount = response.account || "";
  }).catch(() => { calendarAccount = ""; })]);
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
  $("#calendar-title").value = String(clip.title).slice(0,100);
  $("#calendar-description").value = "";
  $("#calendar-tags").value = "";
  if (clip.level) $("#calendar-priority").value = clip.level;
  $("#calendar-caption").value = "";
  const platform = $("#calendar-platform").value;
  if (!platform) return;
  const result = await api(clipQuery(clip.project,clip.filename,platform));
  if (serial === calendarDetailRequest) fillPublishCopy(result.copy, calendarImmediate);
}
function fillPublishCopy(copy, now) {
  $("#calendar-title").value = copy.title;
  $("#calendar-description").value = copy.description;
  $("#calendar-caption").value = copy.caption;
  $("#calendar-tags").value = copy.hashtags.join(" ");
  calendarMode(now);
}
function calendarMode(now) {
  calendarImmediate = now;
  const platform = $("#calendar-platform").value;
  $("#calendar-platform-status").textContent = platform === "tiktok"
    ? (calendarAccount ? "Cuenta: " + calendarAccount + ". " : "Sin cuenta conectada. ") + "TikTok permite enviar al buzón. La publicación directa no está habilitada."
    : "Flujo manual: descarga el clip y copia el texto. Studio no puede publicar en esta plataforma.";
  for (const id of ["calendar-local","calendar-local-zone"]) $("#" + id).closest("label").hidden = now;
  $("#calendar-local").required = !now;
  $("#calendar-suggest").hidden = now;
  $("#calendar-inbox-consent-line").hidden = now || platform !== "tiktok" || !calendarAccount;
  if (now || platform !== "tiktok" || !calendarAccount) $("#calendar-inbox-consent").checked = false;
  $("#calendar-edit-tools button[type=submit]").textContent = now
    ? platform === "tiktok" ? "Revisar envío al buzón" : "Preparar paquete manual" : "Guardar programación";
  $("#calendar-detail-title").textContent = calendarDetail?.title || (now ? "Publicar ahora" : "Nueva programación");
}
function confirmCalendarEdits() {
  return !calendarDirty || confirm("¿Descartar los cambios sin guardar de esta programación?");
}
async function openCalendarDetail(id = null, key = null, immediate = false) {
  if (!confirmCalendarEdits()) return false;
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
  $("#calendar-refresh-line").hidden = !detail?.stale;
  $("#calendar-refresh-version").checked = false;
  $("#calendar-detail-title").textContent = detail ? detail.title : "Nueva programación";
  $("#calendar-detail-status").textContent = detail ? planLabel(detail) +
    (detail.stale ? " · Versión obsoleta. Actualiza la referencia." : "") + (detail.error ? " · " + detail.error : "") : "";
  $("#calendar-events").innerHTML = (detail?.history || []).map(item => `<li>${escapeHTML(new Date(item.at*1000).toLocaleString())} ·
    ${escapeHTML(calendarStatuses[item.action] || ({created:"Creada",updated:"Actualizada"}[item.action]) || item.action)}${item.detail ? " · " + escapeHTML(calendarStatuses[item.detail] || item.detail) : ""}</li>`).join("");
  const deliveryLabels = {dispatching:"En proceso",delivered_to_inbox:"Entregado al buzón",
    published:"Publicación confirmada",failed:"Falló",uncertain:"Pendiente de verificación"};
  $("#calendar-deliveries").replaceChildren(...(detail?.deliveries || []).map(attempt => {
    const item = document.createElement("li");
    item.textContent = new Date(attempt.started*1000).toLocaleString() + " · " + deliveryLabels[attempt.state] +
      (attempt.error ? " · " + attempt.error : "");
    return item;
  }));
  const terminal = detail && ["cancelled","published"].includes(detail.status);
  const readonly = Boolean(terminal || detail?.external_id);
  $("#calendar-edit-tools").hidden = readonly;
  for (const field of form.querySelectorAll("input, select, textarea")) field.disabled = readonly || (field === select && !!detail);
  const actions = [];
  if (detail?.inbox_state === "claimed") $("#calendar-edit-tools").hidden = true;
  if (detail && !terminal && detail.inbox_state !== "claimed") {
    if (!detail.external_id) {
      actions.push(["cancel", "Cancelar programación"]);
      if (detail.scheduled == null) actions.push(["schedule", "Asignar fecha"]);
      if (detail.platform === "tiktok") actions.push(["prepare", "Revisar envío al buzón"]);
      else actions.push(["prepare", "Preparar paquete manual"],["confirm", "Confirmar publicación manual"]);
    } else if (detail.status === "failed" && !detail.publish_id) actions.push(["retry", "Preparar un nuevo intento"]);
    if (detail.external_id) actions.push(["history", "Ver historial de TikTok"]);
  }
  if (detail?.deletable) {
    actions.push(["delete", detail.status === "scheduled" ? "Cancelar y eliminar" : "Eliminar plan"]);
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
    $("#calendar-caption").value = detail.caption || "";
    $("#calendar-inbox-consent").checked = detail.inbox_state === "pending";
    $("#calendar-tags").value = detail.hashtags.join(" ");
    const fallback = clip?.url || "/media/" + detail.project + "/clips/" + encodeURIComponent(detail.file);
    setCalendarPreview(detail.stale ? null : fallback, clip?.thumbnail);
  }
  calendarMode(immediate || Boolean(detail && !detail.scheduled));
  if (!detail) await fillCalendarClip(clip);
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
  const payload = {platform:item.platform, title:item.title, caption:item.caption, description:item.description, hashtags:item.hashtags,
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
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Copiar texto";
  copy.addEventListener("click",safe(async () => {
    const text = [...new Set([item.title,item.caption,item.description,item.hashtags.join(" ")].filter(Boolean))].join("\n\n");
    await navigator.clipboard.writeText(text);
    notice("Texto copiado para la publicación manual.");
  }));
  $("#calendar-actions").append(media,copy);
}
for (const [key,label] of Object.entries(calendarPlatforms)) {
  $("#calendar-platform").add(new Option(label,key));
  $("#calendar-platform-filter").add(new Option(label,key));
}
$("#calendar-status-filter").add(new Option("Solo borradores sin programar","draft-only"));
for (const [key,label] of Object.entries(calendarStatuses)) $("#calendar-status-filter").add(new Option(label,key));
$("#calendar-view").addEventListener("change", event => {
  const input = event.target.closest("[data-draft-check]");
  if (!input) return;
  if (input.checked) checkedDrafts.add(input.dataset.draftCheck);
  else checkedDrafts.delete(input.dataset.draftCheck);
  draftSelection();
});
$("#calendar-view").addEventListener("click", event => {
  const button = event.target.closest("[data-draft-remove]");
  if (button) calendarMutation(() => removeDrafts(visibleDrafts.filter(item => item.id === button.dataset.draftRemove)));
});
$("#calendar-select-drafts").addEventListener("click", () => {
  for (const item of visibleDrafts) if (item.deletable) checkedDrafts.add(item.id);
  draftSelection();
});
$("#calendar-clear-drafts").addEventListener("click", () => { checkedDrafts.clear(); draftSelection(); });
$("#calendar-remove-drafts").addEventListener("click", () => calendarMutation(
  () => removeDrafts(visibleDrafts.filter(item => checkedDrafts.has(item.id)))
));
$("#show-publications").addEventListener("click", safe(openCalendar));
$("#tiktok-back").addEventListener("click",safe(openCalendar));
$("#calendar-now").addEventListener("click", safe(() => openCalendarDetail(null,null,true)));
$("#calendar-create").addEventListener("click", safe(() => openCalendarDetail()));
$("#calendar-close-detail").addEventListener("click", () => {
  if (!confirmCalendarEdits()) return;
  calendarDirty = false;
  ++calendarDetailRequest; $("#calendar-detail").hidden = true; setCalendarPreview(null);
});
$("#calendar-clip").addEventListener("change", safe(() => fillCalendarClip(selectedCalendarClip($("#calendar-clip").value))));
$("#calendar-platform").addEventListener("change",safe(async () => {
  calendarMode(calendarImmediate);
  if (!calendarDirty) await fillCalendarClip(selectedCalendarClip($("#calendar-clip").value));
}));
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
    const when = calendarImmediate ? "" : $("#calendar-local").value;
    if (calendarDetail?.scheduled && when !== calendarLocal(calendarDetail.scheduled, calendarDetail.zone)
        && !confirm("¿Cambiar la hora de esta programación?")) return;
    const saved = await calendarApi("save",{
      id:calendarDraftId, ...(calendarDetail ? {revision:calendarDetail.revision} :
        {project:clip?.project, file:clip?.filename}),
      platform:$("#calendar-platform").value, priority:$("#calendar-priority").value,
      title:$("#calendar-title").value, caption:$("#calendar-caption").value, description:$("#calendar-description").value,
      hashtags:$("#calendar-tags").value.trim().split(/\s+/).filter(Boolean), local:when, zone:$("#calendar-local-zone").value,
      inbox_consent:!calendarImmediate && $("#calendar-inbox-consent").checked, refresh_version:$("#calendar-refresh-version").checked,
    });
    calendarDirty = false;
    if (calendarImmediate) {
      const result = await calendarApi("prepare",{id:saved.id,revision:saved.revision});
      return prepareCalendarCopy(result);
    }
    if (saved.scheduled != null) $("#calendar-date").value = calendarDate(saved.scheduled);
    $("#calendar-feedback").textContent = "Programación guardada.";
    await refreshCalendar();
    await openCalendarDetail(saved.id);
  });
});
$("#calendar-suggest").addEventListener("click",() => calendarMutation(async () => {
  const slot = await calendarApi("suggest",{id:calendarDraftId,platform:$("#calendar-platform").value,account:$("#calendar-growth-account").value,priority:$("#calendar-priority").value});
  calendarDirty = true;
  $("#calendar-local").value = slot.local;
  $("#calendar-local-zone").value = slot.zone;
  $("#calendar-feedback").textContent = slot.basis === "imported_csv" ? `Horario sugerido con ${slot.history_samples} publicaciones importadas de esa franja. Es una comparación histórica.` : "Horario sugerido según preferencias y huecos de la agenda.";
}));
async function deleteCalendarPlans(plans) {
  if (!plans.length || !await confirmDeletion(`Eliminar definitivamente ${plans.length} planes locales y retirar sus programaciones de la cola.`)) return;
  await calendarApi("delete", {items:plans.map(({id, revision}) => ({id, revision})), confirmed:true});
  if (plans.some(plan => plan.id === calendarDetail?.id)) {
    calendarDetail = null;
    $("#calendar-detail").hidden = true;
  }
  await refreshCalendar(true);
  await refreshWorkspace(true);
}
$("#calendar-delete-batch").addEventListener("click", () => calendarMutation(async () => {
  if (!confirmCalendarEdits()) return;
  const ids = new Set([...$("#calendar-delete-selection").selectedOptions].map(option => option.value));
  return deleteCalendarPlans(calendarData.plans.filter(plan => ids.has(plan.id)));
}));
$("#calendar-actions").addEventListener("click",event => {
  const button = event.target.closest("[data-calendar-action]");
  if (!button || !calendarDetail) return;
  calendarMutation(async () => {
    const action = button.dataset.calendarAction;
    if (action === "history") return showPublications();
    if (action === "schedule") {
      calendarMode(false);
      $("#calendar-local").focus();
      return;
    }
    if (!confirmCalendarEdits()) return;
    calendarDirty = false;
    if (action === "delete") return deleteCalendarPlans([calendarDetail]);
    if (action === "cancel" && !confirm("¿Cancelar esta programación?")) return;
    if (action === "confirm" && !confirm("¿Confirmas que este clip ya está publicado en la plataforma indicada?")) return;
    const result = await calendarApi(action,{id:calendarDetail.id,revision:calendarDetail.revision,confirmed:action === "confirm"});
    if (action === "prepare") return prepareCalendarCopy(result);
    await refreshCalendar();
    await openCalendarDetail(result.id);
  });
});
async function prepareCalendarCopy(result) {
  if (result.platform === "tiktok") return reviewPublicationClip(result.project + "/" + result.file,
    [...new Set([result.caption,result.description,result.hashtags.join(" ")].filter(Boolean))].join("\n\n"),result);
  await refreshCalendar();
  await openCalendarDetail(result.id);
  copyPackage(result);
}
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


$("#batch-drafts").addEventListener("submit",event => {
  event.preventDefault();
  calendarMutation(async () => {
    const values = id => [...$(id).selectedOptions].map(option => option.value);
    const data = await calendarApi("drafts",{clips:values("#batch-clips"),platforms:values("#batch-platforms")});
    $("#calendar-feedback").textContent = `${data.plans.length} borradores guardados.`;
    await refreshCalendar();
  });
});

$("#publishing-view-mode").addEventListener("change",renderCalendar);

const assistForm = $("#publishing-assist-form");
function showAssist(settings) {
  assistForm.elements.mode.value = settings.mode;
  for (const option of assistForm.elements.platforms.options) option.selected = settings.platforms.includes(option.value);
  const label = assistForm.elements.mode.selectedOptions[0].textContent;
  $("#publishing-assist-mode").textContent = "Modo activo: " + label;
  const attention = (settings.history || []).filter(item => item.result === "needs_attention");
  $("#publishing-assist-result").textContent = attention.length ?
    attention.map(item => item.file + " · " + calendarPlatforms[item.platform]).join(", ") : "";
}
for (const id of ["#show-settings","#show-publications"]) $(id).addEventListener("click",safe(async () =>
  showAssist(await api("/api/publishing-assist"))));
assistForm.addEventListener("submit",safe(async event => {
  event.preventDefault();
  showAssist(await api("/api/publishing-assist", {mode:assistForm.elements.mode.value,
    platforms:[...assistForm.elements.platforms.selectedOptions].map(option => option.value)}));
  notice("Preferencias de preparación guardadas.");
}));
