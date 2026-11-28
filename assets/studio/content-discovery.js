"use strict";

const discoverySection = $("#content-discovery-view");
const discoveryBase = "/api/content-discovery";
const discoveryFilters = $("#discovery-filters");
const discoverySourceForm = $("#discovery-source-form");
const discoveryRulesForm = $("#discovery-rules-form");
const discoveryReviewForm = $("#discovery-review-form");
const discoveryDialog = $("#discovery-review");
const discoveryTabs = [...discoverySection.querySelectorAll("[role=tab]")];
const discoveryLabels = {affinity:"Afinidad temática", freshness:"Actualidad", conversation:"Conversación",
  narrative:"Narrativa", verifiable:"Información verificable"};
const discoveryStates = {inbox:"Pendiente", saved:"Guardada", approved:"Aprobada", discarded:"Descartada"};
const discoveryRights = {pending:"Derechos pendientes", cleared:"Derechos confirmados", restricted:"Derechos restringidos"};
const discovery = {tab:"browse", page:1, data:null, sources:[], rules:null, source:null,
  sequence:0, selected:null, mutation:false, request:null, reviewStart:null, reviewTime:0};
const discoveryDate = (stamp, time=false) => stamp > 0 ? new Intl.DateTimeFormat("es", {
  dateStyle:"medium", ...(time ? {timeStyle:"short"} : {})
}).format(new Date(stamp * 1000)) : "Fecha desconocida";
const discoveryFeedback = (text, error=false) => {
  const node = $("#discovery-feedback");
  node.textContent = text;
  node.classList.toggle("is-error", error);
};
const discoveryNewRequest = () => [...crypto.getRandomValues(new Uint8Array(16))]
  .map(value => value.toString(16).padStart(2, "0")).join("");

function discoveryCategory(value) {
  return {auto:"Sin categoría", gaming:"Gaming", interview:"Entrevistas", podcast:"Podcasts", movie:"Cine",
    livestream:"En directo", tutorial:"Tutoriales", music:"Música", unknown:"Desconocida"}[value] || value;
}

function discoverySourceCard(source) {
  return `<article class="workspace-card discovery-source-card">
    <div><h3>${escapeHTML(source.name)}</h3><p>${source.active ? "Activa" : "En pausa"}
      · ${source.mode === "discover" ? "Solo descubrir" : "Descubrir y producir"}
      · ${escapeHTML(discoveryCategory(source.category))}</p></div>
    <p>${source.videos} videos · Cada ${source.interval} min${source.checked ? " · " + escapeHTML(discoveryDate(source.checked, true)) : ""}</p>
    ${source.error ? `<p class="is-error">${escapeHTML(source.error)} Próximo intento ${escapeHTML(discoveryDate(source.next_check, true))}.</p>` : ""}
    <div class="publication-actions"><button type="button" data-discovery-source="${escapeHTML(source.id)}">Editar canal</button>
      <a href="${escapeHTML(source.url)}" target="_blank" rel="noopener noreferrer">Ver canal</a></div>
  </article>`;
}

async function discoveryLoadSources() {
  const {sources} = await api(discoveryBase + "/sources");
  discovery.sources = sources;
  const select = discoveryFilters.elements.channel;
  const previous = select.value;
  select.replaceChildren(new Option("Todos", ""), ...sources.map(source => new Option(source.name, source.id)));
  if (sources.some(source => source.id === previous)) select.value = previous;
  patchWorkspace("#discovery-source-list", sources.map(discoverySourceCard).join("") ||
    '<p class="workspace-card discovery-empty">Agrega tu primer canal para empezar a descubrir oportunidades.</p>');
  const errors = sources.filter(source => source.error);
  const banner = $("#discovery-connectivity");
  banner.hidden = !errors.length;
  banner.textContent = errors.length ? "YouTube pendiente en " + errors.length + " fuente(s). Se muestran los datos guardados. Consulta Fuentes para ver el error y el próximo intento." : "";
}

function discoveryVideoCard(item, index) {
  const score = item.score == null ? "—" : item.score.toFixed(1);
  const review = discoveryStates[item.state];
  return `<article class="workspace-card discovery-video">
    <div class="discovery-video-main"><p class="discovery-byline">${escapeHTML(item.channel_name)}
      · ${escapeHTML(discoveryDate(item.published))} · ${escapeHTML(discoveryCategory(item.category))}</p>
      <h2>${escapeHTML(item.title)}</h2>
      <div class="discovery-status"><span>${review}</span><span>${discoveryRights[item.rights]}</span></div>
    </div>
    <div class="discovery-score"><strong>${score}</strong><small>${item.coverage}% de peso evaluado</small></div>
    <details class="discovery-why"><summary>Ver justificación</summary>${discoveryExplanation(item)}</details>
    <div class="publication-actions discovery-actions">
      <button type="button" data-discovery-action="open" data-discovery-index="${index}">Vista previa y revisión</button>
      ${["inbox","discarded"].includes(item.state) ? `<button type="button" data-discovery-action="save" data-discovery-index="${index}">Guardar</button>` : ""}
      ${item.state !== "discarded" ? `<button type="button" data-discovery-action="discard" data-discovery-index="${index}">Descartar</button>` : ""}
    </div>
  </article>`;
}

function discoveryExplanation(item) {
  return `<dl class="discovery-dimensions">${Object.entries(discoveryLabels).map(([key, label]) => {
    const dimension = item.dimensions[key];
    return `<div><dt>${label}</dt><dd><strong>${dimension.score == null ? "Sin evaluar" : Math.round(dimension.score) + "/100"}</strong>
      <span>${escapeHTML(dimension.reason)}</span></dd></div>`;
  }).join("")}</dl>`;
}

async function discoveryBrowse() {
  const sequence = ++discovery.sequence;
  const query = new URLSearchParams(new FormData(discoveryFilters));
  for (const [key,value] of [...query]) if (!value) query.delete(key);
  query.set("page", discovery.page);
  if (discovery.tab === "library") query.set("library", "1");
  if (discovery.data && discovery.page > 1) query.set("at", discovery.data.at);
  const list = $("#discovery-list");
  list.setAttribute("aria-busy", "true");
  $("#discovery-count").textContent = "Cargando oportunidades…";
  try {
    const data = await api(discoveryBase + "?" + query);
    if (sequence !== discovery.sequence) return;
    discovery.data = data;
    const total = Math.max(1, Math.ceil(data.total / data.limit));
    if (discovery.page > total) {
      discovery.page = total;
      return discoveryBrowse();
    }
    $("#discovery-count").textContent = data.total + " " + (data.total === 1 ? "oportunidad" : "oportunidades");
    $("#discovery-page").textContent = "Página " + data.page + " de " + total;
    $("#discovery-prev").disabled = data.page <= 1;
    $("#discovery-next").disabled = data.page >= total;
    list.innerHTML = data.items.map(discoveryVideoCard).join("") || `<p class="workspace-card discovery-empty">${discovery.tab === "library"
      ? "Todavía no hay oportunidades guardadas para estos filtros."
      : "No se encontraron oportunidades. Comprueba tus fuentes o ajusta los filtros."}</p>`;
  } catch (error) {
    if (sequence === discovery.sequence) {
      $("#discovery-count").textContent = "Oportunidades pendientes de actualizar.";
      discoveryFeedback(error instanceof TypeError ? "Sin conexión con Studio. Comprueba que siga abierto." : error.message, true);
    }
  } finally {
    if (sequence === discovery.sequence) list.removeAttribute("aria-busy");
  }
}

function discoveryRenderRules(rules) {
  discovery.rules = rules;
  document.querySelectorAll("#discovery-weights input").forEach(field => { field.value = rules.weights[field.name]; });
  discoveryRulesForm.elements.topics.value = rules.topics.map(topic => topic.name + ": " + topic.keywords.join(", ")).join("\n");
  discoveryWeightsTotal();
}

async function discoveryMetrics() {
  const value = await api(discoveryBase + "/metrics");
  const metrics = [[value.detected,"Videos detectados"], [value.evaluated,"Oportunidades evaluadas"],
    [value.saved,"Guardadas"], [value.discarded,"Descartadas"], [value.approved,"Aprobadas"],
    [value.produced,"Proyectos vinculados"], [mediaDuration(value.review_seconds),"Tiempo de revisión registrado"]];
  $("#discovery-metric-list").innerHTML = metrics.map(([count,label]) =>
    `<article class="workspace-card"><strong>${escapeHTML(count)}</strong><span>${label}</span></article>`).join("");
}

async function discoveryTab(tab) {
  discovery.tab = tab;
  discovery.page = 1;
  discovery.sequence++;
  for (const button of discoveryTabs) {
    const current = button.id === "discovery-tab-" + tab;
    button.setAttribute("aria-selected", current);
    button.tabIndex = current ? 0 : -1;
    $("#" + button.getAttribute("aria-controls")).hidden = !current;
  }
  $("#discovery-list").removeAttribute("aria-busy");
  discoveryFeedback("");
  if (tab === "browse" || tab === "library") {
    const target = $(tab === "library" ? "#discovery-library-slot" : "#discovery-browse");
    target.append(discoveryFilters, $("#discovery-results"));
    discoveryFilters.elements.state.value = tab === "library" ? "" : "inbox";
    discoveryFilters.elements.state.disabled = tab === "library";
    await discoveryBrowse();
  } else if (tab === "metrics") await discoveryMetrics();
  else {
    await discoveryLoadSources();
    discoveryRenderRules(await api(discoveryBase + "/rules"));
  }
}

$("#discovery-weights").innerHTML = Object.entries(discoveryLabels).map(([key,label]) =>
  `<label>${label} (%)<input name="${key}" type="number" min="0" max="100" required /></label>`).join("");

function discoveryWeightsTotal() {
  const inputs = [...$("#discovery-weights").querySelectorAll("input")];
  const total = inputs.reduce((sum, field) => sum + (field.valueAsNumber || 0), 0);
  $("#discovery-weight-total").textContent = "Total " + total + "% · debe sumar 100%";
  discoveryRulesForm.querySelector("button[type=submit]").disabled = total !== 100;
}

async function discoveryRun(action) {
  try {
    await action();
  } catch (error) {
    discoveryFeedback(error instanceof TypeError ? "Sin conexión con Studio. Comprueba que siga abierto." : error.message, true);
  }
}

$("#show-content-discovery").addEventListener("click", () => discoveryRun(async () => {
  switchView("content-discovery-view");
  await studioConfig;
  const categories = ["auto", ...state.config.video_types.filter(key => key !== "auto")];
  const selects = [discoveryFilters.elements.category, discoverySourceForm.elements.category];
  if (selects[1].options.length === 0) {
    selects[0].replaceChildren(new Option("Todas", ""), ...categories.map(key => new Option(discoveryCategory(key), key)));
    selects[1].replaceChildren(...categories.map(key => new Option(discoveryCategory(key), key)));
  }
  await Promise.all([discoveryLoadSources(), discoveryTab(discovery.tab)]);
}));
for (const button of discoveryTabs) {
  button.addEventListener("click", () => discoveryRun(() => discoveryTab(button.id.replace("discovery-tab-", ""))));
  button.addEventListener("keydown", event => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = discoveryTabs.indexOf(button), count = discoveryTabs.length;
    const next = event.key === "Home" ? 0 : event.key === "End" ? count - 1 :
      (current + (event.key === "ArrowRight" ? 1 : -1) + count) % count;
    discoveryTabs[next].focus();
    discoveryTabs[next].click();
  });
}

discoveryFilters.addEventListener("submit", event => {
  event.preventDefault();
  discovery.page = 1;
  discoveryFeedback("");
  discoveryBrowse();
});
$("#discovery-prev").addEventListener("click", () => { if (discovery.page > 1) { discovery.page--; discoveryBrowse(); } });
$("#discovery-next").addEventListener("click", () => { discovery.page++; discoveryBrowse(); });

function discoveryEditSource(row=null) {
  discovery.source = row?.id || null;
  discoverySourceForm.reset();
  $("#discovery-source-title").textContent = row ? "Editar " + row.name : "Agregar canal";
  $("#discovery-source-location").hidden = Boolean(row);
  discoverySourceForm.elements.source.required = !row;
  if (row) {
    for (const key of ["category","interval","mode"]) discoverySourceForm.elements[key].value = row[key];
    discoverySourceForm.elements.active.checked = Boolean(row.active);
  }
  $("#discovery-production-hint").hidden = discoverySourceForm.elements.mode.value !== "produce";
}

$("#discovery-source-list").addEventListener("click", event => {
  const button = event.target.closest("[data-discovery-source]");
  if (!button) return;
  const row = discovery.sources.find(item => item.id === button.dataset.discoverySource);
  if (row) discoveryEditSource(row);
});
$("#discovery-source-reset").addEventListener("click", () => discoveryEditSource());
$("#discovery-open-monitor").addEventListener("click", () => $("#show-youtube-monitor").click());
discoverySourceForm.elements.mode.addEventListener("change", () => {
  $("#discovery-production-hint").hidden = discoverySourceForm.elements.mode.value !== "produce";
});
discoverySourceForm.addEventListener("submit", event => {
  event.preventDefault();
  const button = event.submitter;
  if (button.disabled) return;
  discoveryRun(async () => {
    button.disabled = true;
    try {
      const body = Object.fromEntries(new FormData(discoverySourceForm));
      body.interval = discoverySourceForm.elements.interval.valueAsNumber;
      body.active = discoverySourceForm.elements.active.checked;
      if (discovery.source) body.id = discovery.source;
      await api(discoveryBase + "/source", body);
      discoveryEditSource();
      await discoveryLoadSources();
      discoveryFeedback("Fuente guardada.");
    } finally { button.disabled = false; }
  });
});

discoveryRulesForm.addEventListener("input", discoveryWeightsTotal);
discoveryRulesForm.addEventListener("submit", event => {
  event.preventDefault();
  const button = event.submitter;
  if (!discovery.rules || button.disabled) return;
  discoveryRun(async () => {
    button.disabled = true;
    try {
      const weights = Object.fromEntries(Object.keys(discoveryLabels).map(key => [key, discoveryRulesForm.elements[key].valueAsNumber]));
      const topics = discoveryRulesForm.elements.topics.value.split("\n").map(line => line.trim()).filter(Boolean).map(line => {
        const colon = line.indexOf(":");
        return {name:colon === -1 ? line : line.slice(0,colon),
          keywords:colon === -1 ? [] : line.slice(colon + 1).split(",").map(word => word.trim()).filter(Boolean)};
      });
      discoveryRenderRules(await api(discoveryBase + "/rules", {revision:discovery.rules.revision, weights, topics}));
      discoveryFeedback("Reglas guardadas.");
    } finally { discoveryWeightsTotal(); }
  });
});

function discoveryReviewClock() {
  const now = performance.now();
  if (discovery.reviewStart != null) discovery.reviewTime += now - discovery.reviewStart;
  discovery.reviewStart = discoveryDialog.open && !document.hidden ? now : null;
  return Math.min(3600, Math.floor(discovery.reviewTime / 1000));
}
document.addEventListener("visibilitychange", discoveryReviewClock);

function discoveryOpen(item) {
  discovery.selected = item;
  discovery.request = null;
  $("#discovery-review-title").textContent = item.title;
  $("#discovery-review-meta").textContent = item.channel_name + " · " + discoveryDate(item.published) + " · " + discoveryRights[item.rights];
  $("#discovery-description").textContent = item.description || "La fuente no proporcionó una descripción.";
  $("#discovery-original").href = item.url;
  $("#discovery-explanation").innerHTML = discoveryExplanation(item);
  for (const key of ["notes","rights","rights_note"]) discoveryReviewForm.elements[key].value = item[key];
  discoveryReviewForm.elements.action.value = "edit";
  $("#discovery-review-feedback").textContent = "";
  discovery.reviewTime = 0;
  discovery.reviewStart = null;
  discoveryDialog.showModal();
  discoveryReviewClock();
}

async function discoveryReview(item, action, details={}) {
  if (discovery.mutation) return false;
  discovery.mutation = true;
  const params = {channel:item.channel, video:item.id, revision:item.revision, action, ...details};
  // Retry the same submitted payload with its original id after connection loss.
  const stable = JSON.stringify({...params, seconds:0});
  if (discovery.request?.signature !== stable) {
    discovery.request = {signature:stable, body:{...params, request_id:discoveryNewRequest()}};
  }
  try {
    Object.assign(item, await api(discoveryBase + "/review", discovery.request.body));
    discovery.request = null;
    return true;
  } finally { discovery.mutation = false; }
}

$("#discovery-list").addEventListener("click", event => {
  const button = event.target.closest("[data-discovery-action]");
  if (!button || discovery.mutation) return;
  const item = discovery.data?.items[Number(button.dataset.discoveryIndex)];
  if (!item) return;
  const action = button.dataset.discoveryAction;
  if (action === "open") return discoveryOpen(item);
  discoveryRun(async () => {
    button.disabled = true;
    try {
      if (await discoveryReview(item, action)) {
        discoveryFeedback(action === "save" ? "Oportunidad guardada." : "Oportunidad descartada.");
        await discoveryBrowse();
      }
    } finally { button.disabled = false; }
  });
});

$("#discovery-close-review").addEventListener("click", () => discoveryDialog.close());
discoveryDialog.addEventListener("close", () => { discovery.reviewStart = null; discovery.selected = null; });
discoveryReviewForm.addEventListener("submit", async event => {
  event.preventDefault();
  const item = discovery.selected;
  if (!item || discovery.mutation) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const {action, ...fields} = Object.fromEntries(new FormData(discoveryReviewForm));
    if (await discoveryReview(item, action, {...fields, seconds:discoveryReviewClock()})) {
      discoveryDialog.close();
      discoveryFeedback("Revisión guardada.");
      await discoveryBrowse();
    }
  } catch (error) {
    $("#discovery-review-feedback").textContent = error.message;
  } finally { button.disabled = false; }
});

$("#discovery-refresh").addEventListener("click", event => {
  const button = event.currentTarget;
  if (button.disabled) return;
  discoveryRun(async () => {
    button.disabled = true;
    try {
      const {scheduled} = await api(discoveryBase + "/refresh", {});
      discoveryFeedback(scheduled ? "Consultas programadas para " + scheduled + " fuente(s)." :
        "Las fuentes respetan el intervalo mínimo y los reintentos pendientes.");
      await discoveryLoadSources();
      if (discovery.tab === "browse" || discovery.tab === "library") { discovery.page = 1; await discoveryBrowse(); }
      if (discovery.tab === "metrics") await discoveryMetrics();
    } finally { button.disabled = false; }
  });
});

setInterval(async () => {
  if (document.hidden || discoverySection.hidden || discoveryDialog.open || discovery.mutation) return;
  const before = JSON.stringify(discovery.sources.map(row => [row.id,row.checked]));
  try {
    await discoveryLoadSources();
    const after = JSON.stringify(discovery.sources.map(row => [row.id,row.checked]));
    if (before === after || discovery.page !== 1 || discoveryFilters.contains(document.activeElement)) return;
    if (discovery.tab === "browse" || discovery.tab === "library") await discoveryBrowse();
    if (discovery.tab === "metrics") await discoveryMetrics();
  } catch (error) {
    discoveryFeedback(error instanceof TypeError ? "Sin conexión con Studio. Se conservan los datos visibles." : error.message, true);
  }
}, 6000);
