"use strict";

const watermarkForm = $("#watermark-form");
const watermarkFields = $("#watermark-fields");
const watermarkEndpoint = "/api/watermark";
const watermarkField = (name) => watermarkForm.elements.namedItem(name);
let watermarkId = null;
let watermarkUpload = null;
let watermarkBusy = false;
let watermarkLoaded = false;
let watermarkRemoved = false;

function watermarkError(message = "") {
  $("#watermark-error").textContent = message;
  $("#watermark-error").hidden = !message;
}

function previewWatermark() {
  const [width, height] = $("#watermark-aspect").value.split(":").map(Number);
  $("#watermark-stage").dataset.aspect = $("#watermark-aspect").value;
  $("#watermark-stage").style.aspectRatio = `${width} / ${height}`;
  const overlay = $("#watermark-overlay");
  const thumbnail = $("#watermark-thumbnail");
  overlay.hidden = !watermarkField("enabled").checked || !thumbnail.naturalWidth || !thumbnail.complete;
  if (overlay.hidden) return;
  const margin = Math.min(width, height) * Number(watermarkField("margin").value) / 100;
  const ratio = Math.min(width * Number(watermarkField("size").value) / 100 / thumbnail.naturalWidth,
                         (height - 2 * margin) / thumbnail.naturalHeight);
  const w = thumbnail.naturalWidth * ratio;
  const h = thumbnail.naturalHeight * ratio;
  const position = watermarkField("position").value;
  const x = position === "center" ? (width - w) / 2 : position.endsWith("left") ? margin : width - w - margin;
  const y = position === "center" ? (height - h) / 2 : position.startsWith("top") ? margin : height - h - margin;
  Object.assign(overlay.style, { width: `${w / width * 100}%`, height: `${h / height * 100}%`,
    left: `${x / width * 100}%`, top: `${y / height * 100}%`, opacity: Number(watermarkField("opacity").value) / 100 });
}

function setWatermarkImage() {
  const src = watermarkUpload ? `data:image/png;base64,${watermarkUpload}`
    : watermarkId && !watermarkRemoved ? `/watermark-image/${watermarkId}` : "";
  $("#watermark-remove").disabled = !src;
  $("#watermark-placeholder").hidden = Boolean(src);
  for (const id of ["#watermark-thumbnail", "#watermark-overlay"]) {
    const image = $(id);
    image.hidden = true;
    if (src) image.src = src;
    else image.removeAttribute("src");
  }
  const thumbnail = $("#watermark-thumbnail");
  if (src && thumbnail.complete && thumbnail.naturalWidth) {
    thumbnail.hidden = false;
    previewWatermark();
  }
}

function applyWatermarkSettings(settings) {
  watermarkId = settings.image_id;
  watermarkRemoved = false;
  watermarkUpload = null;
  $("#watermark-file").value = "";
  watermarkField("enabled").checked = settings.enabled;
  for (const name of ["size", "opacity", "margin", "position"]) watermarkField(name).value = settings[name];
  setWatermarkImage();
}

async function updateWatermark(loading, operation, onError) {
  if (watermarkBusy) return;
  watermarkBusy = true;
  watermarkFields.disabled = true;
  watermarkForm.setAttribute("aria-busy", "true");
  watermarkError();
  $("#watermark-status").textContent = loading;
  try {
    $("#watermark-status").textContent = await operation();
  } catch (error) {
    onError?.();
    $("#watermark-status").textContent = "";
    watermarkError(error.message);
  } finally {
    watermarkBusy = false;
    watermarkFields.disabled = !watermarkLoaded;
    watermarkForm.removeAttribute("aria-busy");
  }
}

function loadWatermark() {
  if (watermarkBusy) return;
  $("#watermark-retry").hidden = true;
  watermarkLoaded = false;
  return updateWatermark("Cargando los ajustes…", async () => {
    applyWatermarkSettings(await api(watermarkEndpoint));
    watermarkLoaded = true;
    return "";
  }, () => { $("#watermark-retry").hidden = false; });
}

$("#show-watermark").addEventListener("click", () => {
  switchView("watermark-view");
  loadWatermark();
});
$("#watermark-back").addEventListener("click", () => $("#show-settings").click());
$("#watermark-retry").addEventListener("click", loadWatermark);
$("#watermark-thumbnail").addEventListener("load", () => {
  $("#watermark-thumbnail").hidden = false;
  previewWatermark();
});
$("#watermark-thumbnail").addEventListener("error", () => {
  watermarkError("La imagen guardada no está disponible. Selecciona otro PNG.");
});
watermarkFields.addEventListener("input", previewWatermark);
$("#watermark-change").addEventListener("click", () => $("#watermark-file").click());

async function readWatermarkPNG(file) {
  if (file.size > 2 * 1024 * 1024) throw new Error("La imagen PNG debe pesar como máximo 2 MiB.");
  const header = new DataView(await file.slice(0, 24).arrayBuffer());
  if (header.byteLength !== 24 || header.getUint32(0) !== 0x89504e47 ||
      header.getUint32(4) !== 0x0d0a1a0a || header.getUint32(8) !== 13 ||
      header.getUint32(12) !== 0x49484452) {
    throw new Error("Selecciona una imagen PNG válida.");
  }
  const width = header.getUint32(16), height = header.getUint32(20);
  if (!width || !height || width > 2048 || height > 2048) {
    throw new Error("La imagen PNG debe medir como máximo 2048 × 2048 píxeles.");
  }
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("No se pudo leer el PNG seleccionado."));
    reader.readAsDataURL(file);
  });
  const decoded = new Image();
  decoded.src = dataUrl;
  try {
    await decoded.decode();
  } catch {
    throw new Error("La imagen PNG seleccionada está dañada.");
  }
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

$("#watermark-file").addEventListener("change", (event) => {
  const input = event.target;
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  updateWatermark("Comprobando el PNG…", async () => {
    watermarkUpload = await readWatermarkPNG(file);
    watermarkRemoved = false;
    setWatermarkImage();
    return "Imagen lista. Pulsa Guardar cambios para aplicarla.";
  });
});
$("#watermark-remove").addEventListener("click", () => {
  if (!confirm("¿Quitar la imagen de las próximas exportaciones? Confirma el cambio al guardar.")) return;
  watermarkUpload = null;
  watermarkRemoved = true;
  watermarkField("enabled").checked = false;
  watermarkError();
  setWatermarkImage();
  $("#watermark-status").textContent = "Imagen retirada de la vista previa. Pulsa Guardar cambios.";
});
watermarkForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!watermarkLoaded) return;
  const settings = { image_id: watermarkRemoved ? null : watermarkId, enabled: watermarkField("enabled").checked,
    position: watermarkField("position").value };
  for (const name of ["size", "opacity", "margin"]) settings[name] = watermarkField(name).valueAsNumber;
  updateWatermark("Guardando cambios…", async () => {
    await studioConfig;
    applyWatermarkSettings(await api(watermarkEndpoint, { settings, image_data: watermarkUpload }));
    return "Marca de agua guardada.";
  }, () => {
    watermarkUpload = null;
    watermarkRemoved = false;
    setWatermarkImage();
  });
});

const signatureForm = $("#signature-form");
const signatureFields = $("#signature-fields");
const signatureProject = $("#signature-project");
const signatureEnabled = $("#signature-enabled");
const signatureMark = $("#signature-mark");
const signatureClip = $("#signature-clip");
const signaturePlayer = $("#signature-player");
const signatureDraft = $("#signature-draft");
const signatureSource = $("#signature-source");
const signatureStatus = $("#signature-status");
let signatureBusy = false;
let signatureReady = false;

window.addEventListener("studio-view", event => {
  if (event.detail === "watermark-view") return;
  signatureSource.pause();
  signaturePlayer.pause();
});

async function signatureAction(message, operation) {
  if (signatureBusy) return;
  signatureBusy = true;
  signatureFields.disabled = true;
  signatureStatus.textContent = message;
  try {
    signatureStatus.textContent = await operation();
  } catch (error) {
    signatureStatus.textContent = error.message;
  } finally {
    signatureBusy = false;
    signatureFields.disabled = !signatureReady;
    $("#signature-preview").disabled = !signatureClip.value;
    signatureMark.disabled = !signatureEnabled.checked;
  }
}

function clearSignaturePreview() {
  signaturePlayer.hidden = true;
  signaturePlayer.removeAttribute("src");
  signaturePlayer.load();
}

function showSignatureDraft() {
  const enabled = signatureEnabled.checked;
  signatureDraft.dataset.framed = enabled;
  signatureDraft.hidden = !enabled && signatureSource.hidden;
  $("#signature-header").hidden = $("#signature-footer").hidden = !enabled;
}

function signatureOptions() {
  return signatureEnabled.checked
    ? { template: $("#signature-template").value, watermark: signatureMark.checked }
    : null;
}

async function loadSignatureProject() {
  signatureReady = false;
  clearSignaturePreview();
  signatureDraft.hidden = true;
  const detail = await api(`/api/projects/${signatureProject.value}`);
  const saved = detail.project.options.signature;
  signatureEnabled.checked = Boolean(saved);
  signatureMark.checked = Boolean(saved?.watermark);
  $("#signature-template").value = saved?.template || state.config.signature_template;
  signatureClip.replaceChildren(...detail.candidates.map((clip, index) =>
    new Option(clip.hook || `Clip ${index + 1}`, String(index + 1))));
  signatureSource.hidden = !detail.playable_source;
  if (detail.playable_source) signatureSource.src = `/media/${detail.project.id}/source`;
  else signatureSource.removeAttribute("src");
  showSignatureDraft();
  signatureReady = true;
  return detail.candidates.length ? "" : "Prepara el proyecto para generar una vista previa del clip.";
}

$("#show-watermark").addEventListener("click", () => signatureAction("Cargando Signature…", async () => {
  await studioConfig;
  signatureReady = false;
  if (!state.config.signature_template) return "Faltan las franjas PNG oficiales de Anqora Signature.";
  const [header, footer] = state.config.signature_bands;
  signatureDraft.style.setProperty("--signature-header", `${header / 1920 * 100}%`);
  signatureDraft.style.setProperty("--signature-footer", `${footer / 1920 * 100}%`);
  $("#signature-header").src = "/signature/header.png";
  $("#signature-footer").src = "/signature/footer.png";
  const previous = signatureProject.value || state.id;
  const { projects } = await api("/api/projects");
  signatureProject.replaceChildren(...projects.map(project =>
    new Option(project.name || projectName(project.source), project.id)));
  if (projects.some(project => project.id === previous)) signatureProject.value = previous;
  if (!projects.length) return "Crea un proyecto para configurar Signature.";
  return loadSignatureProject();
}));

signatureProject.addEventListener("change", () => signatureAction("Cargando el proyecto…", loadSignatureProject));

function signatureChanged() {
  signatureMark.disabled = !signatureEnabled.checked;
  clearSignaturePreview();
  showSignatureDraft();
  signatureStatus.textContent = "Guarda el template o genera una nueva vista previa.";
}

for (const field of [signatureEnabled, signatureMark, $("#signature-template"), signatureClip]) {
  field.addEventListener("change", signatureChanged);
}
$("#signature-reset").addEventListener("click", () => {
  signatureEnabled.checked = signatureMark.checked = false;
  $("#signature-template").value = state.config.signature_template;
  signatureChanged();
});

const saveSignature = path => api(path + "/settings", { signature: signatureOptions() });

signatureForm.addEventListener("submit", event => {
  event.preventDefault();
  signatureAction("Guardando el template…", async () => {
    await saveSignature(`/api/projects/${signatureProject.value}`);
    return "Template guardado para este proyecto.";
  });
});

$("#signature-preview").addEventListener("click", () => signatureAction("Guardando y generando la vista previa…", async () => {
  const path = `/api/projects/${signatureProject.value}`;
  await saveSignature(path);
  await api(path + "/run", { action: "render", preview: true, pick: signatureClip.value });
  while (true) {
    await new Promise(resolve => setTimeout(resolve, 900));
    if ($("#watermark-view").hidden) return "Puedes revisar el avance en el proyecto.";
    const detail = await api(path);
    if (detail.busy) continue;
    if (detail.project.error) throw new Error(detail.project.error);
    const name = basename(detail.project.last_outputs?.[0] || "");
    const file = detail.files.find(item => item.kind === "previews" && item.name === name);
    if (!file) throw new Error("Studio no encontró la vista previa renderizada.");
    signatureSource.pause();
    signatureDraft.hidden = true;
    signaturePlayer.src = encodeURI(file.url);
    signaturePlayer.hidden = false;
    return "Vista previa lista.";
  }
}));
