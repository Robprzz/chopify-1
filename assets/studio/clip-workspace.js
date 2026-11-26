"use strict";

const clipEditor = {clip:null, origin:"clips-view", serial:0, dirty:false};
const clipForm = $("#clip-copy-form");
const clipGrid = $(".clip-editor-grid");
const clipSubtitles = $("#clip-subtitles");
const clipFeedbackNode = $("#clip-editor-feedback");
const clipVideo = $("#clip-editor-video");
function dockClip(inProject) {
  const target = inProject ? $("#project-clip-detail") : $("#clip-editor-view");
  target.append(clipFeedbackNode,clipGrid,clipSubtitles);
}
const copyKeys = ["title","caption","description","hashtags"];
const copyLimits = {tiktok:[150,2200], instagram:[150,2200], facebook:[150,2200], youtube:[100,5000]};
const clipPlatform = $("#clip-platform");
const clipFields = () => ({
  project:clipEditor.clip.project, file:clipEditor.clip.filename, platform:clipPlatform.value,
});
function clipQuery(project, file, platform) {
  return "/api/clip?" + new URLSearchParams({project,file,platform});
}
function clipFeedback(value) {
  $("#clip-editor-feedback").textContent = value;
}
function clipCopyForm() {
  return Object.fromEntries(copyKeys.map(key => [key, key === "hashtags"
    ? clipForm.elements[key].value.trim().split(/\s+/).filter(Boolean) : clipForm.elements[key].value]));
}
function showCopy(data) {
  const copy = data.copy;
  for (const key of copyKeys) clipForm.elements[key].value = key === "hashtags" ? copy.hashtags.join(" ") : copy[key];
  const [title,text] = copyLimits[data.platform];
  clipForm.elements.title.maxLength = title;
  for (const field of ["caption","description"]) clipForm.elements[field].maxLength = text;
  $("#clip-copy-history").replaceChildren(...data.versions.map(item =>
    new Option(new Date(item.created * 1000).toLocaleString() + " · " + item.title, item.id)));
  $("#clip-copy-restore").disabled = !data.versions.length;
  clipEditor.dirty = false;
}
async function loadClipCopy() {
  const serial = clipEditor.serial;
  const fields = clipFields();
  const pending = api(clipQuery(fields.project, fields.file, fields.platform));
  clipEditor.loading = pending;
  let data;
  try {
    data = await pending;
  } finally {
    if (clipEditor.loading === pending) clipEditor.loading = null;
  }
  if (serial !== clipEditor.serial || clipPlatform.value !== fields.platform) return;
  showCopy(data);
  $("#clip-editor-media").textContent = `${data.width} × ${data.height} · ${data.format} · ${mediaDuration(data.duration)}`;
  clipVideo.poster = data.cover || clipEditor.clip.thumbnail || "";
  clipEditor.mark = data.mark;
  previewClipMark(data.mark);
  return data;
}
async function showClipEditor(input, origin = workspaceView) {
  const existing = workspaceData?.clips.find(row => row.project === input.project && row.filename === input.filename);
  const url = input.url || existing?.url || "/media/" + input.project + "/clips/" + encodeURIComponent(input.filename);
  const id = input.project + "/" + input.filename;
  if (clipEditor.clip && clipEditor.dirty && !confirm("¿Descartar los cambios sin guardar del caption?")) return false;
  if (!/^(?:live\/)?[a-f0-9]{12}$/.test(input.project) || !/^[^/\\:]+\.mp4$/.test(input.filename)) {
    throw new Error("No se encontró el clip.");
  }
  rememberClipPosition();
  const serial = ++clipEditor.serial;
  clipEditor.clip = {...existing,...input,url,id};
  const ranking = workspaceData?.priority.find(item => item.project === input.project && item.filename === input.filename);
  $("#clip-publishing-priority").textContent = ranking?.priority != null ?
    "Prioridad " + ranking.priority.toFixed(1) + "/10 · Detección " + (ranking.score ?? "sin datos") +
    " · " + (ranking.age == null ? "Antigüedad sin verificar" : "Hace " + Math.round(ranking.age) + " min") :
    "El clip todavía no tiene una recomendación de Automation. Puedes prepararlo manualmente.";

  clipEditor.origin = origin === "clip-editor-view" ? clipEditor.origin : origin;
  setPreference("last-clip", id);
  clipPlatform.value = getPreference("clip-platform","tiktok");
  if (!clipPlatform.value) clipPlatform.value = "tiktok";
  $("#clip-subtitles").open = false;
  $("#editorial-view").hidden = true;
  $("#clip-art-form").reset();
  $("#clip-download").href = url;
  clipVideo.src = url;
  clipFeedback("Cargando clip…");
  const inProject = clipEditor.origin === "project-view" && workspaceView === "project-view";
  dockClip(inProject);
  if (!inProject) switchView("clip-editor-view");
  else {
    recordStudioRoute("clip-editor-view");
    $("#project-clip-detail").scrollIntoView({block:"start"});
  }
  const data = await loadClipCopy();
  if (!data || serial !== clipEditor.serial) return;
  $("#clip-render-aspect").value = data.format;
  const controls = $("#clip-art-form").elements;
  $("#clip-mark-mode").value = data.mark?.mode || "global";
  $("#clip-mark-controls").hidden = data.mark?.mode !== "custom";
  if (data.mark?.mode === "custom") {
    for (const key of ["position","size","opacity","margin"]) controls[key].value = data.mark[key];
  }
  clipFeedback("");
}
clipVideo.addEventListener("loadedmetadata", () => {
  const seconds = Number(getPreference("clip-position:" + clipEditor.clip.id, 0));
  if (Number.isFinite(seconds) && seconds > 0 && seconds < clipVideo.duration) clipVideo.currentTime = seconds;
});
function rememberClipPosition() {
  if (clipEditor.clip) setPreference("clip-position:" + clipEditor.clip.id, String(Math.round(clipVideo.currentTime || 0)));
}
clipVideo.addEventListener("seeked", rememberClipPosition);
clipVideo.addEventListener("pause", rememberClipPosition);
window.addEventListener("pagehide", rememberClipPosition);
window.addEventListener("studio-view", event => {
  if (event.detail !== "clip-editor-view" && event.detail !== "project-view") clipVideo.pause();
  if (event.detail === "project-view" && clipEditor.clip?.project !== state.id) dockClip(false);
});
$("#clip-editor-back").addEventListener("click", safe(async () => {
  if (clipEditor.dirty && !confirm("¿Descartar los cambios sin guardar?")) return;
  clipEditor.dirty = false;
  if (clipEditor.origin === "project-view" && !clipEditor.clip.project.startsWith("live/")) await selectProject(clipEditor.clip.project);
  else if (clipEditor.origin === "live-view") showLive();
  else showWorkspace("clips-view");
}));
clipForm.addEventListener("input", event => { if (event.target !== clipPlatform) clipEditor.dirty = true; });
clipPlatform.addEventListener("change",safe(async () => {
  if (clipEditor.dirty && !confirm("¿Descartar los cambios sin guardar de esta plataforma?")) {
    clipPlatform.value = getPreference("clip-platform","tiktok");
    return;
  }
  setPreference("clip-platform",clipPlatform.value);
  await loadClipCopy();
}));
async function saveClipCopy(other = {}) {
  if (clipEditor.loading) await clipEditor.loading;
  const data = await api("/api/clip/copy",{...clipFields(), ...clipCopyForm(), ...other});
  showCopy(data);
  clipFeedback("Caption guardado.");
  return data.copy;
}
clipForm.addEventListener("submit",safe(async event => { event.preventDefault(); await saveClipCopy(); }));
$("#clip-copy-restore").addEventListener("click",safe(async () => {
  if (confirm("¿Restaurar esta versión como caption actual?")) await saveClipCopy({restore:Number($("#clip-copy-history").value)});
}));
$("#clip-generate").addEventListener("click",safe(async () => {
  if (clipEditor.loading) await clipEditor.loading;
  const button = $("#clip-generate");
  const fields = clipFields();
  const serial = clipEditor.serial;
  button.disabled = true;
  try {
    clipFeedback("Generando caption…");
    const {job_id} = await api("/api/clip/generate",fields);
    for (let i=0; i<180; i++) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      if (clipEditor.serial !== serial || clipPlatform.value !== fields.platform) {
        clipFeedback("");
        return;
      }
      const result = await api("/api/editorial/result?id=" + job_id);
      if (["failed","cancelled","rejected"].includes(result.state)) throw new Error(result.error || "No se generó un caption válido.");
      if (result.state !== "done") continue;
      const version = result.result?.variants?.find(item =>
        item.style === result.result.recommended_variant && item.valid !== false);
      if (!version) throw new Error("La IA no encontró un caption verificable. Puedes editarlo manualmente.");
      const copy = {};
      for (const key of copyKeys) copy[key] = version[key] ?? (key === "hashtags" ? [] : "");
      showCopy({platform:fields.platform,copy,versions:[]});
      clipEditor.dirty = true;
      clipFeedback("Caption generado. Revisa el texto y pulsa Guardar.");
      return;
    }
    throw new Error("La generación continúa en segundo plano. Revisa el historial del clip.");
  } catch (error) {
    clipFeedback(error.message);
  } finally {
    button.disabled = false;
  }
}));
function previewClipMark(mark) {
  const image = $("#clip-mark-preview");
  image.hidden = !mark?.url;
  if (image.hidden) return;
  image.src = mark.url;
  image.style.width = mark.size + "%";
  image.style.opacity = mark.opacity / 100;
  const margin = mark.margin + "%";
  const [v,h] = mark.position.split("-");
  Object.assign(image.style,{left:h === "left" ? margin : "",right:h === "right" ? margin : "",
    top:v === "top" ? margin : "",bottom:v === "bottom" ? margin : "", transform:""});
  if (mark.position === "center") Object.assign(image.style,{top:"50%",left:"50%",transform:"translate(-50%, -50%)"});
}
function previewArtworkControls() {
  const form = $("#clip-art-form");
  const mode = $("#clip-mark-mode").value;
  if (mode !== "custom") return previewClipMark(mode === "global" && clipEditor.mark?.mode === "global" ? clipEditor.mark : null);
  const image = $("#clip-mark-input").files[0];
  if (clipEditor.previewURL) URL.revokeObjectURL(clipEditor.previewURL);
  clipEditor.previewURL = image ? URL.createObjectURL(image) : "";
  previewClipMark({url:clipEditor.previewURL || clipEditor.mark?.url,
    ...Object.fromEntries(["position","size","margin","opacity"].map(key => [key,form.elements[key].value]))});
}
$("#clip-art-form").addEventListener("input",previewArtworkControls);
$("#clip-mark-mode").addEventListener("change", () => {
  $("#clip-mark-controls").hidden = $("#clip-mark-mode").value !== "custom";
});
$("#clip-art-form").addEventListener("submit",safe(async event => {
  event.preventDefault();
  const cover = $("#clip-cover-input").files[0], image = $("#clip-mark-input").files[0];
  const markControls = event.target.elements;
  const mark = $("#clip-mark-mode").value;
  const fields = {...clipFields(),mark,settings:{position:markControls.position.value}};
  for (const key of ["size","opacity","margin"]) fields.settings[key] = markControls[key].valueAsNumber;
  if (cover) fields.cover = await readWatermarkPNG(cover);
  if (image && mark === "custom") fields.image = await readWatermarkPNG(image);
  const result = await api("/api/clip/artwork",fields);
  clipVideo.poster = result.cover || clipEditor.clip.thumbnail || "";
  clipEditor.mark = result.mark;
  previewClipMark(result.mark);
  $("#clip-cover-input").value = $("#clip-mark-input").value = "";
  clipFeedback("Portada y marca guardadas. Exporta una versión para aplicar la marca al MP4.");
}));
$("#clip-cover-remove").addEventListener("click",safe(async () => {
  await api("/api/clip/artwork",{...clipFields(),cover:null});
  clipVideo.poster = mediaPreviewPath(clipEditor.clip);
  clipFeedback("Miniatura automática restaurada.");
}));
function mediaPreviewPath(clip) { return clip.url.replace(/\.mp4$/,".png"); }
$("#clip-reexport").addEventListener("click",safe(async () => {
  const button = $("#clip-reexport");
  button.disabled = true;
  try {
    const fields = clipFields(), serial = clipEditor.serial;
    const {job} = await api("/api/clip/render",{...fields,aspect:$("#clip-render-aspect").value});
    clipFeedback("Generando una nueva exportación…");
    for (let i=0; i<330; i++) {
      await new Promise(resolve => setTimeout(resolve,2000));
      const result = await api("/api/clip-render?id=" + job);
      if (result.state === "error") throw new Error(result.error);
      if (result.state !== "ready") continue;
      await refreshWorkspace(true);
      if (serial === clipEditor.serial && workspaceView === "clip-editor-view") {
        clipFeedback("Nueva versión disponible en Clips: " + result.file);
      }
      notice("Nueva versión exportada.");
      return;
    }
    clipFeedback("La exportación sigue en segundo plano. Consulta Clips cuando termine.");
  } finally { button.disabled = false; }
}));
async function publishFromClip(now) {
  const copy = await saveClipCopy();
  await openCalendar();
  if (await openCalendarDetail(null,clipEditor.clip.id,now) === false) return;
  $("#calendar-platform").value = clipPlatform.value;
  fillPublishCopy(copy, now);
}
$("#clip-publish").addEventListener("click",safe(() => publishFromClip(true)));
$("#clip-schedule").addEventListener("click",safe(() => publishFromClip(false)));
$("#clip-trash").addEventListener("click",safe(async () => {
  if (!await confirmDeletion("Enviar el clip a la papelera. Podrás restaurarlo después.")) return;
  const {project,file} = clipFields();
  await api("/api/trash/clip",{project,filename:file});
  clipEditor.dirty = false;
  showWorkspace("clips-view");
  await refreshWorkspace(true);
}));
