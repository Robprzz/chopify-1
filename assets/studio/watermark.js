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
