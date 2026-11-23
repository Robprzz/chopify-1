"use strict";

const realtimeLabels = {
  context: "Preservación del contexto", live_queue: "Espera en cola Live", transcription: "Transcripción Live",
  source_transcription: "Transcripción de la fuente convencional", render: "Renderizado", live_technical: "Control técnico Live",
  automation_queue: "Espera en cola Automation", technical: "Control técnico Automation",
  editorial_queue: "Espera editorial", editorial: "Evaluación editorial", detection_to_review: "Detección a revisión",
  queue_to_review: "Ingreso a Automation a revisión", event_to_review: "Acontecimiento a revisión",
  review_to_approval: "Disponibilidad a aprobación", consent_to_confirmation: "Consentimiento a confirmación de TikTok",
};
const realtimeStages = {collecting: "Preservando contexto", queued: "En cola", transcribing: "Transcribiendo",
  editing: "Editando", quality: "Control Live", generated: "En cola", ...productionNames};
const realtimeLevels = {urgent: "Urgente", high: "Alta", normal: "Normal", low: "Baja"};
const realtimeKinds = {live: "Live", conventional: "Convencional", evergreen: "Atemporal"};
const realtimeFactors = {quality: "Calidad y potencial general", opportunity: "Oportunidad",
  interaction: "Potencial de interacción", diversity: "Diferenciación"};
const realtimeMarks = {detected: "Detectado", context_preserved: "Contexto preservado",
  media_available: "Archivo disponible", automation_claimed: "Inicio de Automation", ready: "Disponible para revisión",
  approved: "Aprobado editorialmente", consent: "Consentimiento TikTok", publication_observed: "Confirmación observada"};
const realtimeSeconds = value => value == null ? "—" : value >= 60 ? `${(value / 60).toFixed(1)} min` : `${value.toFixed(1)} s`;
let realtimeData = null;
let realtimeBusy = false;
let realtimeSelection = null;
let realtimeDetailId = null;

function renderRealtime() {
  if (!realtimeData) return;
  const data = realtimeData;
  const ranks = new Map(data.recommendations.map(item => [item.id, item.rank]));
  $("#realtime-summary").textContent = `Recomendaciones diarias ${ranks.size}/${data.daily_limit} · ${data.day} · ${data.urgent_count} urgentes · ${data.eligible_count} elegibles`;
  const filter = $("#realtime-filter").value;
  const clips = data.clips.filter(row => filter === "all" || (filter === "urgent" && row.level === "urgent")
    || (filter === "recommended" && ranks.has(row.id)));
  const markup = clips.map(row => {
    const age = row.age_minutes == null ? (row.event_kind === "evergreen" ? "Sin caducidad" : "Sin verificar")
      : `${row.age_minutes.toFixed(1)} min <small>Verificado por el operador</small>`;
    const url = "/media/" + row.project + "/clips/" + encodeURIComponent(row.file);
    const approved = row.remote_review?.decision === "approved";
    const review = row.revision != null && !approved ? `
      <button type="button" data-realtime="approved">Aprobar editorialmente</button>
      <button type="button" data-realtime="rejected">Rechazar</button>
      <button type="button" data-realtime="correction">Solicitar corrección</button>` : "";
    return `<tr data-realtime-id="${row.id}">
      <td>${escapeHTML(row.filename)}<small>${escapeHTML(realtimeKinds[row.event_kind])} · Lista para revisión${ranks.has(row.id) ? " · #" + ranks.get(row.id) : ""}${approved ? " · Aprobado editorialmente" : ""}</small></td>
      <td>${age}</td><td>${productionScore(row.detection)}</td><td>${productionScore(row.editorial)}</td><td>${productionScore(row.priority)}</td>
      <td>${productionScore(row.temporal_score)} · ${escapeHTML(realtimeLevels[row.level] || "Sin puntuación")}<small>${escapeHTML(row.reason)}</small></td>
      <td><a href="${escapeHTML(url)}" target="_blank" rel="noopener">Reproducir</a>
      <button type="button" data-realtime="detail">Detalles</button>
      <button type="button" data-realtime="event">Hora del acontecimiento</button>${review}</td></tr>`;
  }).join("") || '<tr><td colspan="7">Sin clips en esta vista.</td></tr>';
  const ranking = $("#realtime-ranking");
  if (ranking.dataset.markup !== markup) {
    const button = document.activeElement.closest("[data-realtime]");
    const focusId = button?.closest("[data-realtime-id]")?.dataset.realtimeId;
    const focusAction = button?.dataset.realtime;
    ranking.innerHTML = markup;
    ranking.dataset.markup = markup;
    if (focusId && focusAction) ranking.querySelector(`[data-realtime-id="${focusId}"] [data-realtime="${focusAction}"]`)?.focus();
  }
  const queue = data.live_production;
  $("#realtime-queue").textContent = `Live ${queue.jobs.length}/${queue.capacity}${queue.saturated ? " · cola saturada" : ""}
    · Automation ${data.queue_count} en espera${data.paused ? " · pausada" : ""}`;
  const showQueue = row => `<li>${escapeHTML(row.filename || row.id)} · ${escapeHTML(realtimeStages[row.state] || row.state)}
    ${row.eta_seconds == null ? "" : " · espera estimada " + realtimeSeconds(row.eta_seconds)}</li>`;
  $("#realtime-pending").innerHTML = [...queue.jobs, ...data.production].map(showQueue).join("");
  $("#realtime-bottlenecks").textContent = data.bottlenecks.length
    ? "Mayores medianas por etapa · " + data.bottlenecks.map(key => realtimeLabels[key]).join(" · ")
    : "Sin muestras suficientes para describir cuellos de botella.";
  $("#realtime-metrics").innerHTML = Object.entries(data.metrics).filter(([, value]) => value.samples).map(([key, value]) =>
    `<tr><td>${escapeHTML(realtimeLabels[key] || key)}</td><td>${value.samples}</td>
    <td>${realtimeSeconds(value.median)}</td><td>${realtimeSeconds(value.p95)}</td></tr>`).join("");
  if (realtimeDetailId != null) {
    const clip = data.clips.find(row => row.id === realtimeDetailId);
    if (clip) renderRealtimeDetail(clip);
    else {
      realtimeDetailId = null;
      $("#realtime-detail").hidden = true;
    }
  }
}

function renderRealtimeDetail(clip) {
  const box = $("#realtime-detail");
  const values = Object.entries(clip.timing?.seconds || {}).filter(([, value]) => value != null);
  const marks = Object.entries(clip.timing?.timestamps || {}).filter(([, value]) => value != null);
  box.innerHTML = `<h3>${escapeHTML(clip.filename)}</h3>
    <p>${escapeHTML(clip.reason)}</p><dl class="production-metrics">${Object.entries(clip.factors).map(([key, value]) =>
      `<div><dt>${escapeHTML(realtimeFactors[key])}</dt><dd>${productionScore(value)}</dd></div>`).join("")}</dl>
    ${marks.length ? `<ul>${marks.map(([key, value]) =>
      `<li>${escapeHTML(realtimeMarks[key])} · ${escapeHTML(new Date(value * 1000).toLocaleString())}</li>`).join("")}</ul>` : ""}
    ${values.length ? `<dl class="production-metrics">${values.map(([key, value]) =>
      `<div><dt>${escapeHTML(realtimeLabels[key])}</dt><dd>${realtimeSeconds(value)}</dd></div>`).join("")}</dl>`
    : "<p>Esta exportación carece de mediciones por etapa.</p>"}`;
  box.hidden = false;
}

async function refreshRealtime() {
  if (realtimeBusy || $("#automation-view").hidden) return;
  realtimeBusy = true;
  const error = $("#realtime-error");
  try {
    realtimeData = await api("/api/production/realtime");
    renderRealtime();
    error.hidden = true;
  } catch (exc) {
    error.textContent = exc.message;
    error.hidden = false;
  } finally {
    realtimeBusy = false;
  }
}

$("#show-automation").addEventListener("click", refreshRealtime);
$("#realtime-filter").addEventListener("change", renderRealtime);
$("#realtime-ranking").addEventListener("click", safe(async event => {
  const button = event.target.closest("button[data-realtime]");
  if (!button || !realtimeData) return;
  const job = realtimeData.clips.find(row => row.id === Number(button.closest("tr").dataset.realtimeId));
  if (!job) return;
  const action = button.dataset.realtime;
  if (action === "event") {
    realtimeSelection = {id: job.id, run: job.run};
    $("#realtime-event-title").textContent = "Hora del acontecimiento · " + job.filename;
    $("#realtime-evergreen").checked = job.event_kind === "evergreen";
    const date = job.event_at == null ? null : new Date(job.event_at * 1000);
    $("#realtime-event-at").value = date ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
    $("#realtime-event-at").disabled = $("#realtime-evergreen").checked;
    $("#realtime-event").hidden = false;
    return;
  }
  if (action === "detail") {
    realtimeDetailId = job.id;
    renderRealtimeDetail(job);
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/api/production/review", {id: job.id, run: job.run, revision: job.revision, decision: action});
    notice(result.changed ? "Decisión editorial registrada." : "La versión ya tiene una decisión registrada.");
    await refreshRealtime();
  } finally {
    button.disabled = false;
  }
}));
$("#realtime-evergreen").addEventListener("change", () => {
  $("#realtime-event-at").disabled = $("#realtime-evergreen").checked;
});
$("#realtime-event-close").addEventListener("click", () => { $("#realtime-event").hidden = true; });
$("#realtime-event").addEventListener("submit", safe(async event => {
  event.preventDefault();
  if (!realtimeSelection) return;
  const evergreen = $("#realtime-evergreen").checked;
  const raw = $("#realtime-event-at").value;
  const event_at = evergreen || !raw ? null : new Date(raw).getTime() / 1000;
  await api("/api/production/event", {...realtimeSelection, evergreen, event_at});
  $("#realtime-event").hidden = true;
  await refreshRealtime();
}));
setInterval(refreshRealtime, 4000);
