"use strict";

const renderStages = {queued:"En cola", preparation:"Preparación", inspection:"Inspección de fuente",
  camera:"Análisis de cámara", filters:"Preparación de filtros", encode:"Codificación",
  verification:"Verificación", write:"Escritura final", metadata:"Metadatos y miniatura",
  promotion:"Traslado a Clips", reuse:"Reutilización de archivo", ready:"Listo"};

function renderProgressSummary(data, includeTimes = true) {
  const stage = renderStages[data.stage] || data.stage;
  const fraction = Number.isFinite(data.fraction) ? " · " + Math.round(data.fraction * 100) + "%" : "";
  const eta = data.eta > 0 ? " · " + data.eta + " s restantes aprox." : "";
  const times = includeTimes ? Object.entries(data.times || {}).map(([key, time]) =>
    (renderStages[key] || key) + " " + time + " s") : [];
  return stage + fraction + eta + (times.length ? " · " + times.join(" / ") : "");
}

function exportSummary(value) {
  if (!value) return "";
  return [value.inherited ? "Plantilla global" : "Configuración del proyecto", value.aspect,
    value.signature ? "Anqora Signature 1.0" : "Sin franjas", value.style, value.layout,
    value.export_profile === "fast" ? "Rápido" : "Alta calidad",
    value.brand_name, value.camera_mode, value.fit, value.watermark_enabled === true ? "Marca de agua activa" : "Sin marca de agua", value.warning].filter(Boolean).join(" · ");
}

const exportTemplateForm = $("#export-template-form");
function templateWarning() {
  const {aspect, signature, layout} = exportTemplateForm.elements;
  $("#export-template-warning").textContent = aspect.value === "9:16" ? "" :
    [signature.value === "signature" ? "Signature requiere 9:16. En este formato se omitirán sus franjas." : "",
      layout.value !== "single" ? "Se utilizará un solo encuadre." : ""].filter(Boolean).join(" ");
}

async function refreshExportTemplate() {
  try {
    const [data] = await Promise.all([api("/api/export-template"), studioConfig]);
    const config = state.config;
    const {style, brand_name} = exportTemplateForm.elements;
    style.replaceChildren(...Object.keys(config.caption_presets).map(name => new Option(name, name)));
    brand_name.replaceChildren(new Option("Sin marca guardada",""),
      ...config.brands.map(name => new Option(name, name)));
    for (const [key, value] of Object.entries(data)) {
      const field = exportTemplateForm.elements.namedItem(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = value;
      else field.value = key === "signature" ? (value ? "signature" : "none") : value;
    }
    templateWarning();
  } catch (error) {
    $("#export-template-status").textContent = error.message;
  }
}
exportTemplateForm.addEventListener("change", templateWarning);
exportTemplateForm.addEventListener("submit", safe(async event => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(exportTemplateForm));
  data.enabled = exportTemplateForm.elements.enabled.checked;
  data.watermark = exportTemplateForm.elements.watermark.checked;
  data.signature = data.signature === "signature";
  const status = $("#export-template-status");
  try {
    const result = await api("/api/export-template", data);
    status.textContent = result.enabled ? "Plantilla global guardada." : "Plantilla global desactivada.";
    if (state.id) state.detail = await api("/api/projects/" + state.id);
  } catch (error) { status.textContent = error.message; }
}));
$("#export-test-nvenc").addEventListener("click", safe(async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const report = await api("/api/export-template/nvenc", {});
    $("#export-template-status").textContent = report.message;
  } finally { button.disabled = false; }
}));

const channelForm = $("#youtube-monitor-form");
let channelData = [];
let channelEditing = null;
let channelBusy = false;
const channelStates = {reference:"Referencia inicial", unapproved:"Sin autorización de descarga",
  pending:"Pendiente", queued:"En producción", ready:"Por revisar", restricted:"Restringido",
  failed:"Error de procesamiento", deferred:"Límite de cola alcanzado"};
const channelDate = date => date == null ? "Pendiente" : new Date(date * 1000).toLocaleString();

function channelsRender(data) {
  channelData = data.channels;
  const markup = channelData.map(row => `<article class="workspace-card">
    <h2><a href="${escapeHTML(row.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(row.name)}</a></h2>
    <p>${row.active ? "Activo" : "En pausa"} · ${row.authorized ? "Autorización registrada" : "Detección sin descarga"}
      · ${row.projects} proyectos · Cada ${row.interval} min · Última revisión ${escapeHTML(channelDate(row.checked))}</p>
    ${row.error ? `<p class="muted">${escapeHTML(row.error)} Próximo intento ${escapeHTML(channelDate(row.next_check))}.</p>` : ""}
    <div class="publication-actions">
      <button type="button" data-channel-action="settings" data-channel="${row.id}">Editar</button>
      <button type="button" data-channel-action="pause" data-channel="${row.id}">${row.active ? "Pausar" : "Activar"}</button>
      <button type="button" data-channel-action="check" data-channel="${row.id}">Revisar ahora</button>
      <button type="button" data-channel-action="import" data-channel="${row.id}" ${row.active && row.authorized && row.videos.length ? "" : "disabled"}>Importar último video</button>
    </div>
    <ul>${row.videos.map(video => `<li>${escapeHTML(video.title)} · ${escapeHTML(channelDate(video.published))} ·
      ${escapeHTML(channelStates[video.state] || video.state)}${video.error ? " · " + escapeHTML(video.error) : ""}
      ${video.project && /^[a-f0-9]{12}$/.test(video.project) ? `<button type="button" data-monitor-project="${video.project}">Abrir proyecto</button>` : ""}</li>`).join("")}</ul>
  </article>`).join("");
  patchWorkspace("#youtube-monitor-list", markup || "<p>Agrega un canal para comenzar.</p>");
}

function setChannel(row) {
  channelEditing = row?.id || null;
  channelForm.reset();
  $("#youtube-monitor-heading").textContent = row ? "Editar " + row.name : "Agregar canal";
  $("#youtube-monitor-source").hidden = Boolean(row);
  channelForm.elements.source.required = !row;
  if (row) for (const key of ["interval","maximum","preferred","template","category","render","active","authorized","reason"]) {
    const field = channelForm.elements[key];
    if (field.type === "checkbox") field.checked = Boolean(row[key]);
    else field.value = row[key];
  }
}

async function channelRequest(action, data, message) {
  if (channelBusy) return;
  channelBusy = true;
  try {
    channelsRender(await api("/api/youtube-monitor/" + action, data));
    $("#youtube-monitor-feedback").textContent = message;
    return true;
  } catch (error) { $("#youtube-monitor-feedback").textContent = error.message; }
  finally { channelBusy = false; }
}
$("#show-youtube-monitor").addEventListener("click", safe(async () => {
  switchView("youtube-monitor-view");
  await studioConfig;
  const config = state.config;
  const select = channelForm.elements.category;
  if (select.options.length === 1) for (const key of config.video_types || []) {
    if (key !== "auto") select.add(new Option(key, key));
  }
  channelsRender(await api("/api/youtube-monitor"));
}));
$("#youtube-monitor-reset").addEventListener("click", () => setChannel());
channelForm.addEventListener("submit", safe(async event => {
  event.preventDefault();
  const {elements} = channelForm;
  const data = Object.fromEntries(new FormData(channelForm));
  for (const key of ["active","authorized","render"]) data[key] = elements[key].checked;
  for (const key of ["interval","maximum","preferred"]) data[key] = elements[key].valueAsNumber;
  if (channelEditing) data.id = channelEditing;
  if (await channelRequest(channelEditing ? "settings" : "add", data, "Canal guardado.")) setChannel();
}));
$("#youtube-monitor-list").addEventListener("click", safe(async event => {
  const project = event.target.closest("[data-monitor-project]");
  if (project) return selectProject(project.dataset.monitorProject);
  const action = event.target.closest("[data-channel-action]");
  if (!action) return;
  const row = channelData.find(item => item.id === action.dataset.channel);
  if (!row) return;
  action.blur();
  const id = row.id;
  if (action.dataset.channelAction === "settings") return setChannel(row);
  if (action.dataset.channelAction === "pause") return channelRequest("settings", {id, active:!row.active},
    row.active ? "Canal en pausa." : "Canal activado.");
  if (action.dataset.channelAction === "check") return channelRequest("check", {id}, "Consulta solicitada.");
  await channelRequest("import", {id}, "Importación solicitada si el video aún no está en producción.");
}));
setInterval(() => {
  if (!$("#youtube-monitor-view").hidden && !channelBusy) api("/api/youtube-monitor").then(channelsRender).catch(() => {});
}, 5000);
