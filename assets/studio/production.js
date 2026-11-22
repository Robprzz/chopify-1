"use strict";

const productionNames = {
  generated: "Generado", validating: "Validando", repairing: "Reparando", revalidating: "Revalidando",
  analyzing: "Analizando contenido", generating: "Generando propuestas", validating_proposals: "Validando propuestas",
  ready: "Listo para aprobación", review: "Revisión requerida", rejected: "Rechazado",
  failed: "Fallido", cancelled: "Cancelado",
};
const productionCodes = {
  interrupted: "Recuperación tras interrupción", media_unreadable: "MP4 ilegible", no_audio: "Audio ausente",
  av_sync: "Desfase audiovisual", av_unverified: "Relojes de pista sin verificar",
  duration_limit: "Duración fuera del límite", decode_timeout: "Tiempo de decodificación agotado",
  ffmpeg_missing: "FFmpeg no disponible", ffprobe_missing: "FFprobe no disponible", media_decode: "Error decodificando MP4",
  voice_coverage: "Voz sin subtítulos", subtitles: "Subtítulos defectuosos", vad_unavailable: "Voz sin verificar",
  voice_unconfirmed: "Voz no confirmada",
  transcript_reuse: "Reutilización de transcripción", whisper: "Retranscripción local",
  repair_exhausted: "Reparación sin resolver", work_unavailable: "Archivos no disponibles",
  subtitle_evidence: "Evidencia de subtítulos no utilizable", editorial_review: "Propuestas pendientes de revisión",
  ai_disabled: "Generación automática desactivada", live_disabled: "Automation para Live desactivado",
  editorial_cancelled: "Análisis editorial cancelado",
};
const productionView = { before: null, next: null, paused: false, busy: false, details: null, signature: "" };
const productionAPI = (route, data) => api("/api/production/" + route, data);
const timing = value => value == null ? "—" : `${Number(value).toFixed(1)} s`;

function productionFail(message) {
  const error = $("#production-error");
  error.textContent = message;
  error.hidden = !message;
}

function renderProduction(data) {
  productionView.next = data.next;
  productionView.paused = data.config.paused;
  $("#production-pause").textContent = data.config.paused ? "Reanudar cola" : "Pausar cola";
  $("#production-pause").disabled = false;
  $("#production-status").textContent = data.config.paused ? "Pausada para nuevos trabajos" : "En producción";
  const limit = $("#production-limit");
  if (document.activeElement !== limit && !limit.dataset.dirty) limit.value = data.config.max_repairs;

  const m = data.metrics;
  const completed = m.total - m.pending;
  const stats = [
    ["Procesados", completed], ["Pendientes", m.pending], ["Validación técnica aprobada", m.technical],
    ["Reparados automáticamente", m.repaired], ["Listos para aprobación", data.counts.ready || 0],
    ["Requieren intervención", (data.counts.review || 0) + (data.counts.failed || 0)],
    ["Rechazados", data.counts.rejected || 0],
    ["Tasa de éxito", completed ? `${Math.round((data.counts.ready || 0) / completed * 100)} %` : "—"],
    ["Reparaciones manuales evitadas", m.manual_repairs_avoided],
    ["Procesamiento promedio", timing(m.processing)], ["Reparación promedio", timing(m.repairs)],
    ["Análisis editorial promedio", timing(m.editorial)],
  ];
  $("#production-metrics").innerHTML = stats.map(([label, value]) =>
    `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("");
  $("#production-empty").hidden = !!data.jobs.length;
  $("#production-older").hidden = data.next == null;
  $("#production-newest").hidden = productionView.before == null;
  const signature = JSON.stringify(data.jobs);
  if (productionView.signature !== signature) {
    productionView.signature = signature;
    $("#production-jobs").innerHTML = data.jobs.map(job => {
      const terminal = job.finished != null;
      const buttons = [
        ["detail", "Ver detalle", true],
        ["editorial", "Ver propuestas", job.editorial_id && terminal],
        ["publication", "Revisar en Publicaciones", job.state === "ready"],
        ["cancel", "Cancelar", !terminal && !job.cancel_requested],
        ["retry", "Reintentar", terminal && job.state !== "ready"],
      ];
      return `<tr data-production-job="${job.id}"><td>${escapeHTML(job.filename)}${job.active_filename
        ? `<small>Versión corregida disponible</small>` : ""}</td>
        <td>${job.cancel_requested && !terminal ? "Cancelación solicitada" : productionNames[job.state]}</td>
        <td>${job.technical === "passed" ? "Aprobada" : job.technical === "failed" ? "Con defectos" : "Pendiente"}</td>
        <td>${job.attempts}</td><td>${buttons.filter(([, , show]) => show).map(([action, label]) =>
          `<button type="button" data-production-action="${action}">${label}</button>`).join(" ")}</td></tr>`;
    }).join("");
  }
  const problems = $("#production-problems");
  problems.hidden = !data.problems.length;
  problems.querySelector("ul").innerHTML = data.problems.map(row =>
    `<li>${escapeHTML(productionCodes[row.code] || row.code)} · ${row.count}</li>`).join("");
}

async function refreshProduction() {
  if ($("#automation-view").hidden || productionView.busy) return;
  productionView.busy = true;
  try {
    renderProduction(await productionAPI("status" +
      (productionView.before == null ? "" : "?before=" + productionView.before)));
  } catch (error) {
    productionFail(error.message);
  } finally {
    productionView.busy = false;
  }
}

async function productionAction(handler) {
  productionFail("");
  try {
    await studioConfig;
    await handler();
    await refreshProduction();
  } catch (error) {
    productionFail(error.message);
  }
}

function showProductionDetail(job) {
  const detail = $("#production-detail");
  detail.hidden = false;
  const file = job.active_filename || job.filename;
  const media = "/media/" + job.project + "/clips/" + encodeURIComponent(file);
  const diag = job.diagnosis || {};
  detail.innerHTML = `<h3>${escapeHTML(job.filename)}</h3>
    <p>${productionNames[job.state]} · ${timing(job.seconds)} · ${job.attempts} intentos</p>
    ${job.error ? `<p>${escapeHTML(job.error)}</p>` : ""}
    ${diag.integrity ? `<p>Multimedia decodificado · Desfase A/V ${Number(diag.av_end_skew).toFixed(3)} s
      · Cobertura de voz ${diag.speech_coverage == null ? "sin verificar" : Math.round(diag.speech_coverage * 100) + "%"}</p>` : ""}
    <p><a href="${escapeHTML(media)}" target="_blank" rel="noopener">Abrir clip</a></p>
    <details open><summary>Historial del trabajo</summary><ul>${job.events.map(event =>
      `<li>${new Date(event.created * 1000).toLocaleString()} · ${productionNames[event.state]}
      ${event.message ? " · " + escapeHTML(event.message) : ""}</li>`).join("")}</ul></details>`;
}

$("#show-automation").addEventListener("click", refreshProduction);
$("#production-limit").addEventListener("input", event => { event.target.dataset.dirty = "true"; });
$("#production-pause").addEventListener("click", () => productionAction(async () => {
  await productionAPI(productionView.paused ? "resume" : "pause", {});
}));
$("#production-save").addEventListener("click", () => productionAction(async () => {
  const limit = $("#production-limit");
  if (!limit.reportValidity()) return;
  await productionAPI("settings", { max_repairs: limit.valueAsNumber });
  delete limit.dataset.dirty;
}));
$("#production-older").addEventListener("click", () => {
  productionView.before = productionView.next;
  refreshProduction();
});
$("#production-newest").addEventListener("click", () => {
  productionView.before = null;
  refreshProduction();
});
$("#production-jobs").addEventListener("click", event => {
  const action = event.target.closest("[data-production-action]")?.dataset.productionAction;
  if (!action) return;
  const id = Number(event.target.closest("[data-production-job]").dataset.productionJob);
  productionAction(async () => {
    if (action === "cancel" || action === "retry") {
      showProductionDetail(await productionAPI(action, { id }));
      return;
    }
    const job = await productionAPI("job?id=" + id);
    if (action === "detail") {
      showProductionDetail(job);
      return;
    }
    if (action === "publication") {
      await showPublications();
      const menu = $("#tiktok-clip");
      const key = job.project + "/" + (job.active_filename || job.filename);
      const option = [...menu.options].find(item => item.dataset.key === key);
      if (option) {
        menu.value = option.value;
        menu.dispatchEvent(new Event("change"));
      }
      return;
    }
    await openEditorial({ project: job.project, filename: job.active_filename || job.filename });
    editorialView.selected = job.editorial_id;
    renderEditorialResult(await editorialApi("result?id=" + job.editorial_id));
  });
});
$("#production-enqueue").addEventListener("click", () => productionAction(async () => {
  const clip = editorialView.clips.find(item => editorialClipKey(item) === $("#editorial-clip").value);
  if (!clip) throw new Error("Selecciona un clip terminado.");
  await productionAPI("enqueue", { project: clip.project, filename: clip.filename });
  $("#production-heading").scrollIntoView({ block: "start" });
}));
setInterval(refreshProduction, 1800);
