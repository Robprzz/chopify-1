"use strict";

const watermarkForm = $("#watermark-form");
const watermarkFields = $("#watermark-fields");
const watermarkEndpoint = "/api/watermark";
const watermarkField = (name) => watermarkForm.elements.namedItem(name);
const watermarkSave = watermarkForm.querySelector("button[type=submit]");
let watermarkId = null;
let watermarkUpload = null;
let watermarkBusy = false;

function watermarkError(message = "") {
  $("#watermark-error").textContent = message;
  $("#watermark-error").hidden = !message;
}

function previewWatermark() {
  const [width, height] = $("#watermark-aspect").value.split(":").map(Number);
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
    : watermarkId ? `/watermark-image/${watermarkId}` : "";
  $("#watermark-remove").disabled = !src;
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
  watermarkUpload = null;
  $("#watermark-file").value = "";
  watermarkSave.disabled = false;
  watermarkField("enabled").checked = settings.enabled;
  for (const name of ["size", "opacity", "margin", "position"]) watermarkField(name).value = settings[name];
  setWatermarkImage();
}

async function updateWatermark(operation, message, allowEditing) {
  if (watermarkBusy) return;
  watermarkBusy = true;
  watermarkFields.disabled = true;
  watermarkError();
  $("#watermark-status").textContent = "";
  try {
    await studioConfig;
    applyWatermarkSettings(await operation());
    allowEditing = true;
    $("#watermark-status").textContent = message;
  } catch (error) {
    watermarkError(error.message);
  } finally {
    watermarkBusy = false;
    watermarkFields.disabled = !allowEditing;
  }
}

$("#show-watermark").addEventListener("click", () => {
  state.id = null;
  switchView("watermark-view");
  updateWatermark(() => api(watermarkEndpoint), "", false);
});
$("#watermark-thumbnail").addEventListener("load", () => {
  $("#watermark-thumbnail").hidden = false;
  previewWatermark();
});
$("#watermark-thumbnail").addEventListener("error", () => {
  watermarkError("La imagen PNG no está disponible. Vuelve a cargarla o desactiva la marca.");
});
watermarkFields.addEventListener("input", previewWatermark);
$("#watermark-file").addEventListener("change", () => {
  const input = $("#watermark-file");
  const file = input.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) {
    input.value = "";
    watermarkSave.disabled = false;
    watermarkError("La imagen PNG debe pesar como máximo 2 MiB.");
    return;
  }
  watermarkSave.disabled = true;
  const reader = new FileReader();
  reader.addEventListener("load", () => {
    if (input.files[0] !== file) return;
    watermarkSave.disabled = false;
    watermarkError();
    watermarkUpload = reader.result.split(",")[1];
    setWatermarkImage();
  });
  reader.addEventListener("error", () => {
    if (input.files[0] !== file) return;
    input.value = "";
    watermarkSave.disabled = false;
    watermarkError("No se pudo leer el PNG seleccionado.");
  });
  reader.readAsDataURL(file);
});
$("#watermark-remove").addEventListener("click", () => {
  watermarkId = watermarkUpload = null;
  $("#watermark-file").value = "";
  watermarkSave.disabled = false;
  watermarkField("enabled").checked = false;
  watermarkError();
  setWatermarkImage();
});
watermarkForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const settings = { image_id: watermarkId, enabled: watermarkField("enabled").checked,
    position: watermarkField("position").value };
  for (const name of ["size", "opacity", "margin"]) settings[name] = watermarkField(name).valueAsNumber;
  updateWatermark(() => api(watermarkEndpoint, { settings, image_data: watermarkUpload }), "Marca de agua guardada.", true);
});
