"use strict";

const publication = { clips: [], review: null, account: null, busy: false };
const publishingStatuses = {
  PREPARING: "Preparación local",
  STARTING: "Solicitud de subida iniciada",
  UPLOADING: "Subiendo a TikTok",
  PROCESSING_UPLOAD: "TikTok está procesando el vídeo",
  SEND_TO_USER_INBOX: "Enviado al buzón de TikTok · pendiente de tu revisión",
  PUBLISH_COMPLETE: "TikTok confirma que el clip se publicó",
  FAILED: "TikTok informa que la publicación falló",
  LOCAL_ERROR: "Error local antes de iniciar la subida",
  INIT_REJECTED: "TikTok rechazó el inicio de la subida",
  CHECK_STATUS: "Subida interrumpida · consulta el estado",
  CHECK_INBOX: "Resultado de la solicitud incierto · revisa el buzón de TikTok",
};
const privacyLabels = {
  PUBLIC_TO_EVERYONE: "Todo el mundo",
  MUTUAL_FOLLOW_FRIENDS: "Amigos mutuos",
  FOLLOWER_OF_CREATOR: "Seguidores",
  SELF_ONLY: "Solo tú",
};

const tiktokApi = (action, data = {}) => api("/api/tiktok/" + action, data);

function updatePublicationControls() {
  $("#tiktok-clip").disabled = publication.busy;
  $("#tiktok-description").disabled = publication.busy;
  $("#tiktok-consent").disabled = publication.busy;
  $("#tiktok-send").disabled = publication.busy || !publication.account ||
    !publication.review || !$("#tiktok-consent").checked;
  $("#tiktok-disconnect").disabled = publication.busy;
  $("#tiktok-privacy-check").disabled = publication.busy;
}

function clearPublicationReview() {
  publication.review = null;
  $("#tiktok-review").hidden = true;
  const preview = $("#tiktok-preview");
  preview.removeAttribute("src");
  preview.load();
  $("#tiktok-description").value = "";
  $("#tiktok-consent").checked = false;
  updatePublicationControls();
}

function publicationHistory(entries) {
  const list = $("#tiktok-history");
  list.replaceChildren();
  if (!entries.length) {
    list.textContent = publication.account ? "Aún no hay envíos." : "Conecta la cuenta para ver su historial.";
    return;
  }
  for (const entry of entries) {
    const item = document.createElement("article");
    item.className = "publication-entry";
    const name = document.createElement("strong");
    name.textContent = entry.filename;
    const status = document.createElement("p");
    status.textContent = publishingStatuses[entry.status] || entry.status;
    const date = document.createElement("small");
    date.textContent = new Date(entry.updated * 1000).toLocaleString();
    item.append(name, status, date);
    if (entry.last_error) {
      const error = document.createElement("p");
      error.textContent = entry.last_error;
      item.append(error);
    }
    if (entry.errors?.length > 0) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Errores registrados";
      details.append(summary);
      for (const error of entry.errors) {
        const line = document.createElement("p");
        line.textContent = new Date(error.at * 1000).toLocaleString() + " · " + error.message;
        details.append(line);
      }
      item.append(details);
    }
    if (entry.description) {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.textContent = "Copiar descripción";
      copy.addEventListener("click", safe(() => copyPublicationDescription(entry.description)));
      item.append(copy);
    }
    if (entry.publish_id && !["PUBLISH_COMPLETE", "FAILED"].includes(entry.status)) {
      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.textContent = "Consultar estado";
      refresh.disabled = publication.busy;
      refresh.addEventListener("click", safe(async () => {
        refresh.disabled = true;
        try {
          await tiktokApi("status", { id: entry.id });
          await refreshPublications();
        } finally {
          refresh.disabled = false;
        }
      }));
      item.append(refresh);
    }
    list.append(item);
  }
}

async function refreshPublications() {
  const result = await tiktokApi("state");
  publication.account = result.account;
  publication.clips = result.clips;
  $("#tiktok-account").textContent = result.account
    ? `Cuenta conectada · ${result.account}`
    : (result.oauth_error || (result.configured
      ? "Sin cuenta conectada"
      : "TikTok requiere las credenciales de Login Kit en el entorno de Studio."));
  $("#tiktok-connect").hidden = Boolean(result.account);
  $("#tiktok-connect").disabled = !result.configured || publication.busy;
  $("#tiktok-disconnect").hidden = !result.account;
  $("#tiktok-privacy-check").hidden = !result.creator_info;
  const select = $("#tiktok-clip");
  const selected = select.selectedOptions[0]?.dataset.key;
  select.replaceChildren(new Option(result.clips.length ? "Elige un clip" : "Sin exportaciones MP4", ""));
  result.clips.forEach((file, index) => {
    const option = new Option(`${file.project_name} · ${file.filename}`, String(index));
    option.dataset.key = `${file.project}/${file.filename}`;
    select.append(option);
    if (option.dataset.key === selected) select.value = option.value;
  });
  if (!select.value && publication.review) clearPublicationReview();
  publicationHistory(result.history);
  updatePublicationControls();
}

async function showPublications() {
  state.id = null;
  switchView("publications-view");
  await refreshPublications();
}

window.addEventListener("studio-ready", () => {
  if (location.hash === "#publicaciones") safe(showPublications)();
});
if (state.token && location.hash === "#publicaciones") safe(showPublications)();

$("#tiktok-connect").addEventListener("click", safe(async () => {
  const result = await tiktokApi("connect");
  location.assign(result.url);
}));

$("#tiktok-disconnect").addEventListener("click", safe(async () => {
  await tiktokApi("disconnect");
  clearPublicationReview();
  $("#tiktok-clip").value = "";
  $("#tiktok-privacy").textContent = "Elige la privacidad al finalizar en TikTok.";
  await refreshPublications();
}));

$("#tiktok-privacy-check").addEventListener("click", safe(async () => {
  const button = $("#tiktok-privacy-check");
  button.disabled = true;
  try {
    const result = await tiktokApi("privacy");
    const labels = result.options.map((option) => privacyLabels[option]);
    $("#tiktok-privacy").textContent = labels.length
      ? labels.join(" · ") + ". Elige la privacidad al finalizar en TikTok."
      : "TikTok no ofreció opciones de privacidad.";
  } finally {
    updatePublicationControls();
  }
}));

$("#tiktok-clip").addEventListener("change", safe(async () => {
  clearPublicationReview();
  const value = $("#tiktok-clip").value;
  if (value === "") return;
  const file = publication.clips[Number(value)];
  publication.busy = true;
  updatePublicationControls();
  try {
    const review = await tiktokApi("inspect", { project: file.project, filename: file.filename });
    publication.review = review.review_id;
    const preview = $("#tiktok-preview");
    preview.src = review.url;
    $("#tiktok-media-info").textContent = `${review.width} × ${review.height} · ${review.duration} s` +
      (review.vertical_9_16 ? "" : " · Formato recomendado para TikTok: 9:16");
    $("#tiktok-description").value = review.description;
    $("#tiktok-review").hidden = false;
  } catch (error) {
    $("#tiktok-clip").value = "";
    throw error;
  } finally {
    publication.busy = false;
    updatePublicationControls();
  }
}));

$("#tiktok-description").addEventListener("input", () => {
  $("#tiktok-consent").checked = false;
  updatePublicationControls();
});
$("#tiktok-consent").addEventListener("change", updatePublicationControls);
async function copyPublicationDescription(description) {
  await navigator.clipboard.writeText(description);
  notice("Descripción copiada.");
}
$("#tiktok-copy").addEventListener("click", safe(() =>
  copyPublicationDescription($("#tiktok-description").value)));

$("#tiktok-form").addEventListener("submit", safe(async (event) => {
  event.preventDefault();
  if (publication.busy || !publication.review || !publication.account ||
      !$("#tiktok-consent").checked) return;
  publication.busy = true;
  updatePublicationControls();
  try {
    const result = await tiktokApi("upload", {
      review_id: publication.review,
      description: $("#tiktok-description").value,
      approved: true,
    });
    if (result.id !== publication.review) {
      notice("Este clip ya tiene un intento registrado. Revisa el historial o el buzón de TikTok.");
    } else {
      notice(result.last_error || "Subida registrada. Consulta el estado en el historial.", Boolean(result.last_error));
    }
    clearPublicationReview();
    $("#tiktok-clip").value = "";
  } finally {
    publication.busy = false;
    updatePublicationControls();
    await refreshPublications();
  }
}));
