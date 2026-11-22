"use strict";

const editorialLabels = {
  hook: "Atención", emotion: "Emoción", relevance: "Relevancia",
  curiosity: "Curiosidad", context: "Claridad y contexto", engagement: "Interacción",
  fidelity: "Fidelidad", originality: "Originalidad", writing: "Redacción", platform: "Adecuación al perfil",
};
const editorialStyles = { curiosity: "Curiosidad", interaction: "Interacción", contextual: "Contextual" };
const editorialStates = { queued: "En espera", running: "Procesando", done: "Completado", failed: "Fallido", cancelled: "Cancelado" };
const editorialOutcomes = {
  unassessed: "Pendiente de evaluación", understanding: "Comprendiendo el clip",
  generating: "Redactando propuestas", validating_proposals: "Validando evidencia y calidad",
  deterministic: "Control de reglas editoriales", autocorrecting: "Corrigiendo campos", valid: "Propuesta válida",
  valid_warnings: "Propuesta válida con advertencias", review: "Revisión requerida",
  rejected_evidence: "Evidencia insuficiente", extractive: "Extractos para revisión",
};
const editorialIssues = {
  structure: "Formato de respuesta", missing_evidence: "Evidencia ausente",
  insufficient_evidence: "Transcripción insuficiente", unsupported_entity: "Entidad sin respaldo",
  unsupported_name: "Nombre sin respaldo", unsupported_number: "Cifra sin respaldo",
  generic: "Texto genérico", repetition: "Frases repetidas", duplicate_copy: "Texto repetido", duplicate_variants: "Variantes similares",
  invalid_tags: "Etiquetas inválidas", unrelated_tags: "Etiquetas sin relación",
  missing: "Campo obligatorio", format: "Formato", length: "Longitud",
  review: "Rechazo de la revisión", grounding: "Texto sin vínculo con la evidencia",
  timeout: "Tiempo agotado", offline: "Ollama sin conexión", model_missing: "Modelo ausente",
  sensational: "Tono sensacionalista", attribution: "Atribución pendiente",
  fidelity: "Fidelidad pendiente", uncertainty: "Incertidumbre",
};
const editorialIssueLabel = (code) => editorialIssues[code] || code;
const editorialEvidenceLabels = { attributed_fact: "Hecho atribuido", opinion: "Opinión", question: "Pregunta", interpretation: "Interpretación" };
const editorialContextLabels = { before: "Fragmento anterior", after: "Fragmento posterior", title: "Título",
  channel: "Canal", description: "Descripción" };
const editorialValueLabels = { educational: "Educativo", informational: "Informativo", entertainment: "Entretenimiento",
  mixed: "Mixto", unknown: "Sin determinar" };
const editorialPlatforms = { tiktok: "TikTok", facebook: "Facebook Reels", youtube: "YouTube Shorts" };
const editorialTextFields = {
  title: ["Título", 160], caption: ["Caption", 650], description: ["Descripción", 1300],
  call_to_action: ["Llamada a la acción", 180],
};
const editorialView = {
  config: null, job: null, selected: null, clips: [], before: null, next: null,
  profile: "tiktok", polling: false, models: [],
};
const editorialApi = (path, data) => api("/api/editorial/" + path, data);
const editorialClipKey = (clip) => clip.project + "/" + clip.filename;
const editorialProvider = (provider) => provider === "rules" ? "Generado mediante reglas locales"
  : provider === "ollama" ? "Ollama" : "—";

function editorialError(message = "") {
  $("#editorial-error").textContent = message;
  $("#editorial-error").hidden = !message;
}

function editorialSafe(handler) {
  return async (event) => {
    editorialError();
    try {
      await handler(event);
    } catch (error) {
      editorialError(error.message);
    }
  };
}

function renderEditorialModels(models) {
  const menu = $("#editorial-model");
  const previous = menu.value || editorialView.config?.settings.model || "";
  editorialView.models = models;
  menu.replaceChildren(new Option("Automático · Qwen3:4b o reglas locales", ""));
  for (const model of models) {
    if (model.digest) menu.add(new Option(model.name, model.name));
  }
  if (previous && ![...menu.options].some((option) => option.value === previous)) {
    menu.add(new Option(`${previous} · no disponible`, previous));
  }
  menu.value = previous;
}

function renderEditorialService(service, address) {
  const availability = service.available === true
    ? (service.models.some((model) => model.digest) ? "Ollama disponible" : "Ollama sin modelos locales")
    : service.available === false ? "Ollama no disponible. Respaldo local activo." : "Comprobando Ollama…";
  const hint = service.available === false
    ? (service.executable_found ? " · El ejecutable está en PATH. Comprueba que el servicio esté iniciado."
      : " · Ejecutable fuera de PATH o sin instalar.")
    : "";
  $("#editorial-ollama").textContent = availability + " · " + address + hint;
  $("#editorial-install").hidden = service.available !== false
    && !(service.available && !service.models.some((model) => model.digest));
}

function renderEditorialProfile() {
  const profile = editorialView.config.settings.profiles[editorialView.profile];
  $("#editorial-title-limit").value = profile.title_limit;
  $("#editorial-caption-limit").value = profile.caption_limit;
  $("#editorial-tag-limit").value = profile.max_tags;
  $("#editorial-instructions").value = profile.instructions;
}

function collectEditorialProfile() {
  editorialView.config.settings.profiles[editorialView.profile] = {
    title_limit: $("#editorial-title-limit").valueAsNumber,
    caption_limit: $("#editorial-caption-limit").valueAsNumber,
    max_tags: $("#editorial-tag-limit").valueAsNumber,
    instructions: $("#editorial-instructions").value,
  };
}

function applyEditorialConfig(config) {
  editorialView.config = config;
  if (config.settings.model && ![...$("#editorial-model").options].some(o => o.value === config.settings.model)) {
    $("#editorial-model").add(new Option(config.settings.model + " · no disponible", config.settings.model));
  }
  $("#editorial-model").value = config.settings.model;
  $("#editorial-timeout").value = config.settings.timeout;
  $("#editorial-repair-attempts").value = config.settings.repair_attempts;
  $("#editorial-default-platform").value = config.settings.platform;
  $("#editorial-platform").value = config.settings.platform;
  const weights = $("#editorial-weights");
  weights.replaceChildren();
  for (const [criterion, value] of Object.entries(config.settings.weights)) {
    const label = document.createElement("label");
    label.textContent = editorialLabels[criterion] + " (%)";
    const input = document.createElement("input");
    input.type = "number";
    input.name = criterion;
    input.min = "0";
    input.max = "100";
    input.required = true;
    input.value = value;
    label.append(input);
    weights.append(label);
  }
  editorialView.profile = $("#editorial-profile").value;
  renderEditorialProfile();
}

function renderEditorialResult(job) {
  editorialView.job = job;
  const output = $("#editorial-result");
  $("#editorial-cancel").hidden = !job || !["queued", "running"].includes(job.state);
  output.replaceChildren();
  if (!job) return;
  const status = document.createElement("p");
  status.textContent = (editorialStates[job.state] || job.state)
    + " · " + (editorialOutcomes[job.editorial_state] || job.editorial_state)
    + (job.finished ? " · " + new Date(job.finished * 1000).toLocaleString() : "")
    + (job.elapsed !== null ? " · " + job.elapsed.toFixed(2) + " s" : "")
    + (job.error ? " · " + job.error : "");
  output.append(status);
  const diagnostic = job.diagnostics || {};
  const formatIssue = (item) => escapeHTML([editorialStyles[item.style || item.variant], editorialTextFields[item.field]?.[0] || item.field, editorialIssueLabel(item.code)].filter(Boolean).join(" · "));
  const timeline = Object.entries(diagnostic.stages || {}).map(([stage, time]) =>
    `<li>${escapeHTML(editorialOutcomes[stage] || stage)} · ${time.toFixed(2)} s</li>`).join("");
  const reasons = (diagnostic.issues || []).map(item => `<li>${formatIssue(item)}</li>`).join("");
  const fields = (items) => (items || []).map(item =>
    escapeHTML([editorialStyles[item.style || item.variant], editorialTextFields[item.field]?.[0] || item.field].filter(Boolean).join(" / "))).join(", ");
  const digest = job.settings?.model_digest;
  output.insertAdjacentHTML("beforeend", `
    <p>${job.settings?.semantic ? "Redacción semántica" : "Modo extractivo"} · ${escapeHTML(job.settings?.model || "Reglas locales")} · Prompt ${escapeHTML(job.settings?.prompt_version || "—")}</p>
    ${job.previous_id ? `<p>Versión anterior #${job.previous_id} · Editorial ${job.previous?.score ?? "—"}/10 · Prompt ${escapeHTML(job.previous?.version || "—")}</p>` : ""}
    ${Object.keys(diagnostic).length ? `<details class="editorial-details"><summary>Diagnóstico de ejecución</summary>
      <p>Llamadas al modelo ${diagnostic.model_calls ?? 0} · Intentos de autocorrección ${diagnostic.retries ?? 0} · Variantes válidas ${diagnostic.valid_variants ?? 0}</p>
      ${digest ? `<p>Identificador del modelo <code>${escapeHTML(digest)}</code></p>` : ""}
      ${timeline ? `<ul>${timeline}</ul>` : ""}${reasons ? `<ul>${reasons}</ul>` : ""}
      <p>Campos corregidos · ${fields(diagnostic.corrected_fields) || "—"}</p>
      <p>Campos pendientes de revisión · ${fields(diagnostic.rejected_fields) || "—"}</p>
    </details>` : ""}`);
  if (!job.result) {
    if (job.error?.startsWith("Subtítulos bloqueados.")) $("#subtitle-quality").open = true;
    return;
  }
  const caseData = job.result;
  const recommended = caseData.variants.find(v => v.style === caseData.recommended_variant
    && caseData.mode === "semantic" && v.valid !== false && v.score > 0 && caseData.editorial_score > 0);
  const chosen = job.selected || recommended?.style;
  const active = caseData.variants.find(v => v.style === chosen);
  const activeText = job.edits[chosen] || active;
  const canReview = caseData.clip_id !== "test" && activeText && (active?.valid === true || job.edits[chosen]);
  const secondary = caseData.secondary_topics.map(escapeHTML).join(" · ") || "—";
  const coverage = caseData.transcript_coverage || {};
  const analysis = caseData.analysis || {};
  const evidence = (analysis.ledger || []).map(item => {
    const citation = item.evidence || item;
    return `<li>${escapeHTML(editorialEvidenceLabels[item.kind] || "")} · ${citation.start ?? "—"}–${citation.end ?? "—"} s · ${escapeHTML(item.quote || citation.text || "")}</li>`;
  }).join("");
  const criteria = Object.entries(caseData.criteria).map(([name, item]) =>
    `<li>${escapeHTML(editorialLabels[name])} · ${item.score}/10 · ${escapeHTML(item.justification)}</li>`).join("");
  const variants = caseData.variants.map(variant => {
    const fields = job.edits[variant.style] || variant;
    const inputs = Object.entries(editorialTextFields).map(([key, [label, max]]) =>
      `<label>${label}<textarea name="${key}" maxlength="${max}" rows="2" ${key === "call_to_action" ? "" : "required"}>${escapeHTML(fields[key])}</textarea></label>`).join("");
    const issues = (diagnostic.rejected_fields || []).filter(item => item.variant === variant.style)
      .map(item => `<li>${formatIssue(item)}</li>`).join("");
    const scores = Object.entries(variant.review || {}).filter(([, value]) => typeof value === "number")
      .map(([key, value]) => `${editorialLabels[key] || key} ${value}/10`).map(escapeHTML).join(" · ");
    return `<details class="editorial-variant" data-variant="${variant.style}" ${variant.style === chosen ? "open" : ""}>
      <summary>${editorialStyles[variant.style]} · ${variant.score}/10${variant.style === chosen ? " · Seleccionada" : ""}${variant.valid === false ? " · Revisión requerida" : ""}</summary>
      <p>${escapeHTML(variant.reason)}</p>
      ${scores ? `<p>${scores}</p>` : ""}${issues ? `<ul>${issues}</ul>` : ""}
      <form data-editorial-edit="${variant.style}">
        ${inputs}
        <label>Hashtags separados por espacios<input name="hashtags" maxlength="255" value="${escapeHTML(fields.hashtags.join(" "))}" /></label>
        <div class="publication-actions">
          <button type="submit">Guardar edición</button>
          <button type="button" data-editorial-action="select">Elegir variante</button>
          <button type="button" data-editorial-action="copy" ${variant.valid === false && !job.edits[variant.style] ? "disabled" : ""}>Copiar</button>
          ${analysis.ledger && caseData.drafts?.[variant.style] ? `
            <button type="button" data-editorial-action="repair">Corregir variante</button>
            <button type="button" data-editorial-action="regenerate">Regenerar variante</button>` : ""}
          <button type="button" data-editorial-action="restore">Restaurar original</button>
        </div>
      </form>
    </details>`;
  }).join("");
  output.insertAdjacentHTML("beforeend", `
    ${activeText ? `<h3>${escapeHTML(activeText.title)}</h3>
    <p>${escapeHTML(activeText.caption)} · ${escapeHTML(activeText.hashtags.join(" "))}</p>` : ""}
    ${canReview ? '<button type="button" data-editorial-publication>Revisar en Publicaciones</button>' : ""}
    <p>Puntuación de detección ${caseData.original_score ?? "—"} · Editorial ${caseData.editorial_score}/10${caseData.mode === "semantic" ? "" : ` · Confianza ${Math.round(caseData.confidence * 100)}%`}</p>
    <p>${editorialProvider(caseData.provider)}${caseData.model ? " · " + escapeHTML(caseData.model) : ""}
      · ${editorialPlatforms[caseData.platform]} · Prompt ${escapeHTML(caseData.prompt_version)}</p>
    <p>Tema · ${escapeHTML(caseData.main_topic)}</p>
    <p>${recommended ? `Recomendada · ${editorialStyles[recommended.style]}. ${escapeHTML(caseData.recommendation_reason)}` : "No hay una propuesta editorial válida para recomendar."}</p>
    <details class="editorial-details"><summary>Evaluación y evidencia</summary>
      <p>Temas secundarios · ${secondary}</p>
      ${coverage.cues !== undefined ? `<p>Cobertura · ${coverage.safe_cues}/${coverage.cues} subtítulos · ${coverage.passages} ${coverage.passages === 1 ? "pasaje" : "pasajes"}${Number.isFinite(coverage.speech_coverage) ? ` · ${Math.round(coverage.speech_coverage * 100)}% del habla` : ""}</p>` : ""}
      ${analysis.provenance?.project_context?.length ? `<p>Contexto auxiliar · ${analysis.provenance.project_context.map(key => escapeHTML(editorialContextLabels[key] || key)).join(", ")}</p>` : ""}
      ${analysis.context_needed ? `<p>Contexto pendiente · ${escapeHTML(analysis.context_needed)}</p>` : ""}
      ${analysis.uncertainty ? `<p>Incertidumbre · ${escapeHTML(({low:"Baja", medium:"Media", high:"Alta"})[analysis.uncertainty] || analysis.uncertainty)}</p>` : ""}
      ${evidence ? `<h4>Registro de evidencia</h4><ul>${evidence}</ul>` : ""}
      ${caseData.mode === "semantic" ? [
        ["Mensaje central", caseData.central_message], ["Audiencia", caseData.audience],
        [analysis.value ? "Valor del fragmento" : "Punto de interés", editorialValueLabels[caseData.interest] || caseData.interest],
        ["Personas mencionadas", caseData.people.join(" · ")], ["Entidades", (analysis.entities || [])
          .filter(entity => !["person", "event"].includes(entity.kind)).map(entity => entity.name).join(" · ")],
        ["Ambigüedades", analysis.ambiguities?.join(" · ")],
        ["Acontecimientos", caseData.events.join(" · ")],
      ].filter(([, value]) => value).map(([label, value]) => `<p>${label} · ${escapeHTML(value)}</p>`).join("") : ""}
      <p>Intención · ${escapeHTML(({explain:"Explicar", tell:"Narrar", react:"Reaccionar", ask:"Preguntar"})[caseData.intent] || "Sin determinar")}</p>
      <p>Emoción · ${escapeHTML(({neutral:"Neutral", joy:"Alegría", surprise:"Sorpresa", concern:"Preocupación", frustration:"Frustración"})[caseData.emotion] || "Sin determinar")}</p>
      <p>${caseData.mode === "semantic" ? "" : `Valor educativo · ${caseData.educational_value}/10 · `}Contexto independiente · ${caseData.standalone ? "Suficiente" : "Limitado"}</p>
      <ul>${criteria}</ul>
      <ul>${caseData.highlights.map(e => `<li>${e.start.toFixed(2)}–${e.end.toFixed(2)} s · ${escapeHTML(e.text)}</li>`).join("")}</ul>
    </details>
    ${caseData.warnings.map(w => `<p class="muted">${escapeHTML(w)}</p>`).join("")}
    ${variants}`);
}

async function refreshEditorialHistory() {
  if (editorialView.polling) return;
  editorialView.polling = true;
  try {
    const query = new URLSearchParams();
    const clip = $("#editorial-clip").value;
    if (clip) query.set("clip", clip);
    if (editorialView.before) query.set("before", editorialView.before);
    const status = await editorialApi("status?" + query);
    if (clip !== $("#editorial-clip").value) return;
    if (!editorialView.config) applyEditorialConfig(status.config);
    renderEditorialService(status.ollama, status.address);
    if (JSON.stringify(status.ollama.models) !== JSON.stringify(editorialView.models)) {
      renderEditorialModels(status.ollama.models);
    }
    const { counts, average_seconds } = status;
    const measurement = status.metrics;
    const timing = (key, label) => measurement?.[key] != null ? `${label} ${measurement[key].toFixed(2)} s` : "";
    const issues = (measurement?.top_issues || []).map(([code, count]) => `${editorialIssueLabel(code)} (${count})`).join(", ");
    const percentage = (key, label) => measurement?.[key] != null ? `${label} ${Math.round(measurement[key] * 100)}%` : "";
    const active = status.active;
    const pending = (counts.queued || 0) + (counts.running || 0) + status.auto_pending;
    const metrics = [counts.running ? "Motor ocupado" : "Motor listo", `${pending} pendientes`,
      `${counts.done || 0} completados`, `${counts.failed || 0} fallidos`,
      `${measurement?.valid || 0} trabajos con propuestas válidas`,
      `${measurement?.editorial_states.review || 0} en revisión`,
      active ? `Trabajo #${active.id} · ${editorialOutcomes[active.editorial_state] || "Procesando"} · ${active.model || "Reglas locales"} · Prompt ${active.version || "—"}` : "",
      percentage("completion_ratio", "Finalización técnica"), percentage("usable_ratio", "Clips evaluados con propuestas válidas"),
      `${measurement?.repaired || 0} recuperados por autocorrección`, timing("median_seconds", "Mediana"), timing("p95_seconds", "P95"),
      issues ? "Incidencias frecuentes · " + issues : "",
      average_seconds === null ? "Sin tiempos medidos" : `Promedio ${average_seconds.toFixed(2)} s`,
      `Último proveedor · ${editorialProvider(status.last_provider)}`,
      status.last_error ? "Último error · " + status.last_error : "",
      status.auto_error || ""];
    $("#editorial-metrics").textContent = metrics.filter(Boolean).join(" · ");
    editorialView.next = status.next;
    $("#editorial-older").hidden = !status.next;
    const history = $("#editorial-history");
    const rows = JSON.stringify(status.jobs);
    if (history.dataset.rows !== rows) {
      history.dataset.rows = rows;
      history.replaceChildren(new Option("Elige una versión", ""));
      for (const job of status.jobs) {
        history.add(new Option(`#${job.id} · ${editorialStates[job.state]} · ${editorialPlatforms[job.platform]} · ${job.clip_id.split("/").pop()}`, job.id));
      }
    }
    const first = status.jobs[0]?.id ?? null;
    const id = editorialView.selected || first;
    const job = status.jobs.find(row => row.id === id);
    history.value = id === null ? "" : String(id);
    if (id === null) {
      renderEditorialResult(null);
    } else if (!editorialView.job || editorialView.job.id !== id
      || (["queued", "running"].includes(editorialView.job.state)
        && (!job || job.revision !== editorialView.job.revision))) {
      renderEditorialResult(await editorialApi("result?id=" + id));
    }
  } finally {
    editorialView.polling = false;
  }
}

async function refreshEditorialClips(clip) {
  const list = $("#editorial-clip");
  const previous = clip ? editorialClipKey(clip) : list.value;
  editorialView.clips = (await editorialApi("clips")).clips;
  list.replaceChildren(new Option("Todos los clips", ""));
  for (const item of editorialView.clips) list.add(new Option(item.project_name + " · " + item.filename, editorialClipKey(item)));
  if ([...list.options].some(option => option.value === previous)) list.value = previous;
}

async function refreshEditorialConnection() {
  await studioConfig;
  const connection = await editorialApi("models", {});
  renderEditorialModels(connection.models);
  await refreshEditorialHistory();
}

async function openEditorial(clip) {
  await studioConfig;
  if (clip) {
    state.id = null;
    switchView("automation-view");
    loadAutomationSettings();
    editorialView.selected = editorialView.before = null;
    renderEditorialResult(null);
  }
  await refreshEditorialClips(clip);
  resetSubtitlePanel();
  await refreshEditorialHistory();
  await refreshEditorialConnection();
  if (clip) $("#editorial-heading").scrollIntoView({ block: "start" });
}

async function generateEditorial(force, test = false, semantic = null) {
  await studioConfig;
  const clip = editorialView.clips.find(item => editorialClipKey(item) === $("#editorial-clip").value);
  if (!test && !clip) throw new Error("Selecciona un clip terminado.");
  let response;
  try {
    response = await editorialApi(test ? "test" : "generate", {
      project: clip?.project, filename: clip?.filename, platform: $("#editorial-platform").value, force, semantic,
    });
  } catch (error) {
    if (/subtítulo|\bSRT\b|transcripci[oó]n/i.test(error.message)) $("#subtitle-quality").open = true;
    throw error;
  }
  if (test) {
    $("#editorial-clip").value = "";
    resetSubtitlePanel();
  }
  editorialView.before = null;
  editorialView.selected = response.job_id;
  renderEditorialResult(await editorialApi("result?id=" + response.job_id));
  await refreshEditorialHistory();
}

function readEditorialText(form) {
  const data = new FormData(form);
  const text = Object.fromEntries(Object.keys(editorialTextFields).map(key => [key, data.get(key).trim()]));
  text.hashtags = data.get("hashtags").trim().split(/\s+/).filter(Boolean);
  return text;
}

async function editEditorial(form, action) {
  const style = form.dataset.editorialEdit;
  if (action === "copy") {
    const text = readEditorialText(form);
    await navigator.clipboard.writeText([
      text.title, text.caption, text.description, text.call_to_action, text.hashtags.join(" "),
    ].filter(Boolean).join("\n\n"));
    notice("Texto editorial copiado.");
    return;
  }
  const job = editorialView.job;
  if (["repair", "regenerate"].includes(action)) {
    const { job_id } = await editorialApi("variant", { job_id: job.id, style, regenerate: action === "regenerate" });
    editorialView.before = null;
    editorialView.selected = job_id;
    renderEditorialResult(await editorialApi("result?id=" + job_id));
    await refreshEditorialHistory();
    return;
  }
  renderEditorialResult(await editorialApi("edit", {
    job_id: job.id, style, revision: job.revision, select: action === "select",
    fields: action === "save" ? readEditorialText(form) : null,
  }));
}

$("#show-automation").addEventListener("click", editorialSafe(() => openEditorial()));
$("#editorial-connect").addEventListener("click", editorialSafe(refreshEditorialConnection));
$("#editorial-test").addEventListener("click", editorialSafe(() => generateEditorial(true, true)));
$("#editorial-generate").addEventListener("click", editorialSafe(() => generateEditorial(false)));
$("#editorial-regenerate").addEventListener("click", editorialSafe(() => generateEditorial(true)));
$("#editorial-semantic").addEventListener("click", editorialSafe(() => generateEditorial(true, false, true)));
$("#editorial-extractive").addEventListener("click", editorialSafe(() => generateEditorial(false, false, false)));
for (const button of document.querySelectorAll("[data-editorial-reprocess]")) {
  button.addEventListener("click", editorialSafe(async () => {
    const result = await editorialApi("reprocess", {
      mode: button.dataset.editorialReprocess, clip_id: $("#editorial-clip").value || null,
    });
    editorialView.before = null;
    editorialView.selected = result.queued[0] || null;
    await refreshEditorialHistory();
    notice(`${result.queued.length} en espera · ${result.skipped.length} sin reprocesar · ${result.deferred} aplazados`);
  }));
}
$("#editorial-cancel").addEventListener("click", editorialSafe(async () => {
  renderEditorialResult(await editorialApi("cancel", { job_id: editorialView.job.id }));
}));
$("#editorial-clip").addEventListener("change", editorialSafe(async () => {
  resetSubtitlePanel();
  editorialView.selected = editorialView.before = null;
  renderEditorialResult(null);
  await refreshEditorialHistory();
}));
$("#editorial-history").addEventListener("change", editorialSafe(async (event) => {
  editorialView.selected = +event.target.value || null;
  if (editorialView.selected) renderEditorialResult(await editorialApi("result?id=" + editorialView.selected));
}));
$("#editorial-older").addEventListener("click", editorialSafe(async () => {
  editorialView.before = editorialView.next;
  editorialView.selected = null;
  await refreshEditorialHistory();
}));
$("#editorial-profile").addEventListener("change", editorialSafe(() => {
  if (!editorialView.config) return;
  collectEditorialProfile();
  editorialView.profile = $("#editorial-profile").value;
  renderEditorialProfile();
}));
$("#editorial-config").addEventListener("submit", editorialSafe(async (event) => {
  event.preventDefault();
  if (!editorialView.config) return;
  collectEditorialProfile();
  const settings = {
    ...editorialView.config.settings,
    model: $("#editorial-model").value, timeout: $("#editorial-timeout").valueAsNumber,
    platform: $("#editorial-default-platform").value, repair_attempts: $("#editorial-repair-attempts").valueAsNumber,
    weights: Object.fromEntries([...$("#editorial-weights").querySelectorAll("input")]
      .map(input => [input.name, input.valueAsNumber])),
  };
  applyEditorialConfig(await editorialApi("config", { settings, revision: editorialView.config.revision }));
  notice("Ajustes de AI Engine guardados.");
}));
$("#editorial-result").addEventListener("submit", editorialSafe(async (event) => {
  event.preventDefault();
  await editEditorial(event.target, "save");
}));
$("#editorial-result").addEventListener("click", editorialSafe(async (event) => {
  if (event.target.closest("[data-editorial-publication]")) {
    const job = editorialView.job;
    const result = job.result;
    const style = job.selected || result.recommended_variant;
    const variant = job.edits[style] || result.variants.find(item => item.style === style && item.valid === true);
    if (variant) {
      try {
        await reviewPublicationClip(job.clip_id, [variant.description, variant.hashtags.join(" ")].filter(Boolean).join("\n\n"));
      } catch (error) {
        notice(error.message, true);
      }
    }
    return;
  }
  const button = event.target.closest("[data-editorial-action]");
  if (button) await editEditorial(button.closest("form"), button.dataset.editorialAction);
}));
$("#tiktok-editorial").addEventListener("click", editorialSafe(async () => {
  const select = $("#tiktok-clip");
  if (!select.value) {
    notice("Selecciona un clip en Publicaciones.", true);
    return;
  }
  await openEditorial(publication.clips[Number(select.value)]);
}));
$("#outputs").addEventListener("click", editorialSafe(async (event) => {
  const button = event.target.closest("[data-editorial-file]");
  if (button) await openEditorial({ project: state.id, filename: button.dataset.editorialFile });
}));
$("#live-clips").addEventListener("click", editorialSafe(async (event) => {
  const button = event.target.closest("[data-editorial-file]");
  if (button) await openEditorial({ project: "live/" + state.liveId, filename: button.dataset.editorialFile });
}));
setInterval(() => {
  if (!$("#automation-view").hidden && editorialView.config) {
    refreshEditorialHistory().catch(error => editorialError(error.message));
  }
}, 2000);
