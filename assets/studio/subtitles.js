"use strict";

const subtitleFields = ["cue_min_ms", "cue_max_ms", "cue_gap_ms", "cue_lines"];
const subtitleActions = ["diagnose", "transcribe", "preview", "export"];
const subtitlePanel = { clip: null, draft: null, pending: false };
const subtitleApi = (route, data) => api("/api/subtitle-repair/" + route, data);
const subtitleAsset = (id, filename) => `/subtitle-repair/${id}/${encodeURIComponent(filename)}`;

function updateSubtitleActions() {
  for (const action of subtitleActions) {
    $("#subtitle-" + action).disabled = !subtitlePanel.clip || subtitlePanel.pending
      || (["preview", "export"].includes(action) && !subtitlePanel.draft);
  }
}

function resetSubtitlePanel() {
  const clip = $("#editorial-clip").value;
  if (clip === subtitlePanel.clip) return;
  subtitlePanel.clip = clip;
  subtitlePanel.draft = null;
  $("#subtitle-status").textContent = clip ? "" : "Selecciona un clip terminado.";
  $("#subtitle-diagnostics").replaceChildren();
  $("#subtitle-draft").hidden = true;
  $("#subtitle-output").hidden = true;
  const video = $("#subtitle-video");
  video.hidden = true;
  video.pause();
  if (video.hasAttribute("src")) {
    video.removeAttribute("src");
    video.load();
  }
  updateSubtitleActions();
  const selected = editorialView.clips.find(item => editorialClipKey(item) === clip);
  if (selected) restoreSubtitleDraft(selected);
}

async function restoreSubtitleDraft(clip) {
  const query = new URLSearchParams({ project: clip.project, filename: clip.filename });
  try {
    const response = await subtitleApi("latest?" + query);
    if (response.draft && editorialClipKey(clip) === subtitlePanel.clip && !subtitlePanel.draft) {
      showSubtitleDraft(response.draft);
      updateSubtitleActions();
    }
  } catch (error) {
    if (editorialClipKey(clip) === subtitlePanel.clip && !subtitlePanel.pending) {
      $("#subtitle-status").textContent = error.message;
    }
  }
}

function showSubtitleDraft(draft) {
  subtitlePanel.draft = draft.id;
  $("#subtitle-draft").hidden = false;
  $("#subtitle-draft-text").textContent = draft.text;
  $("#subtitle-srt").href = subtitleAsset(draft.id, "draft.srt");
}

function renderSubtitleDiagnosis(data) {
  const state = { blocked: "Bloqueado", warning: "Revisar", valid: "Válido" };
  $("#subtitle-status").textContent = `Diagnóstico · ${state[data.state]}`;
  const fields = [
    ["Modelo", data.model], ["Origen", data.origin],
    ["Versión de transcripción", data.version], ["Versión de subtítulos", data.timeline_version],
    ["Duración", data.duration == null ? null : data.duration + " s"],
    ["Segmentos SRT", data.segments], ["Palabras", data.words],
    ["Cobertura de voz", data.speech_coverage == null ? null : Math.round(data.speech_coverage * 100) + "%"],
    ["Subtítulo más largo", data.max_cue_seconds == null ? null : data.max_cue_seconds + " s"],
    ["Tiempo de diagnóstico", data.elapsed + " s"],
  ];
  const list = $("#subtitle-diagnostics");
  list.replaceChildren();
  const messages = fields.map(([label, value]) => `${label} · ${value ?? "Sin verificar"}`)
    .concat(data.errors, data.warnings);
  for (const message of messages) {
    const item = document.createElement("li");
    item.textContent = message;
    list.append(item);
  }
  if (data.model && [...$("#subtitle-model").options].some(option => option.value === data.model)) {
    $("#subtitle-model").value = data.model;
  }
}

async function runSubtitleAction(action) {
  const key = subtitlePanel.clip;
  const clip = editorialView.clips.find(item => editorialClipKey(item) === key);
  if (!clip) return;
  subtitlePanel.pending = true;
  updateSubtitleActions();
  $("#subtitle-status").textContent = "Procesando localmente…";
  try {
    const timing = {};
    if (action === "transcribe") {
      for (const field of subtitleFields) {
        const control = $("#subtitle-" + field);
        if (!control.reportValidity()) throw new Error("Comprueba los tiempos de subtítulos.");
        timing[field] = Number(control.value);
      }
    }
    let job = await subtitleApi(action, {
      project: clip.project, filename: clip.filename, draft: subtitlePanel.draft,
      model: $("#subtitle-model").value, timing,
    });
    while (job.state === "running") {
      await new Promise(resolve => setTimeout(resolve, 700));
      job = await subtitleApi(job.id);
    }
    if (key !== subtitlePanel.clip) return;
    if (job.state === "failed") throw new Error(job.error);
    if (job.diagnosis) renderSubtitleDiagnosis(job.diagnosis);
    if (job.draft) {
      showSubtitleDraft(job.draft);
      $("#subtitle-output").hidden = true;
      $("#subtitle-video").hidden = true;
      $("#subtitle-status").textContent = `Borrador listo · ${job.draft.segments} segmentos · ${job.draft.words} palabras · ${job.elapsed} s`;
    }
    if (job.preview) {
      const video = $("#subtitle-video");
      video.src = subtitleAsset(job.id, job.preview);
      video.hidden = false;
      $("#subtitle-status").textContent = "Vista previa del subtítulo corregido.";
    }
    if (job.export) {
      const output = $("#subtitle-output");
      output.href = job.in_repair_folder ? subtitleAsset(job.id, job.export)
        : `/media/${clip.project}/clips/${encodeURIComponent(job.export)}`;
      output.download = job.export;
      output.hidden = false;
      $("#subtitle-status").textContent = job.in_repair_folder
        ? "Nueva versión Live lista para descargar."
        : "Nueva versión disponible en Publicaciones y AI Engine.";
      await refreshEditorialClips();
    }
  } catch (error) {
    if (key === subtitlePanel.clip) $("#subtitle-status").textContent = error.message;
  } finally {
    subtitlePanel.pending = false;
    updateSubtitleActions();
  }
}

for (const action of subtitleActions) {
  $("#subtitle-" + action).addEventListener("click", () => runSubtitleAction(action));
}
subtitlePanel.clip = null;
updateSubtitleActions();
