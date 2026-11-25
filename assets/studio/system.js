"use strict";

const systemAPI = (action, data, signal) => api("/api/system/" + action, data, signal);
const systemStatus = { snapshot: null, diagnosis: null, update: null, step: null, busy: false };
const systemMedia = { busy: false, dirty: false, revision: 0 };
const mediaForm = $("#system-ffmpeg-form");
const mediaField = mediaForm.elements.ffmpeg_dir;
const systemSteps = [
  ["Dependencias", "Comprueba que Studio usa el entorno virtual y Python 3.11 o posterior.", "#system-dependencies"],
  ["FFmpeg y FFprobe", "Ejecuta el diagnóstico. Si no encuentra ambos programas, selecciona su carpeta.", "#system-dependencies"],
  ["Ollama", "Comprueba el servicio. Puedes autorizar su inicio local o continuar con reglas locales.", "#system-dependencies"],
  ["Modelo local", "Elige un modelo instalado si vas a usar AI Engine semántico.", "#system-dependencies"],
  ["Almacén protegido", "Comprueba si Windows puede cifrar las credenciales para tu usuario.", "#system-security"],
  ["TikTok opcional", "Elige Sandbox o Production y configura la aplicación si vas a conectarla.", "#system-integrations"],
  ["Carpeta de proyectos", "Conserva la carpeta habitual o selecciona otra para el próximo inicio.", "#system-general"],
  ["Acceso directo", "Crea el acceso directo en el escritorio del usuario.", "#system-maintenance"],
  ["Diagnóstico final", "Comprueba las dependencias y guarda la configuración inicial.", "#system-dependencies"],
];
const profileLabels = { sandbox: "Sandbox", production: "Production" };
const credentialLabels = { environment: "Variables de entorno", session: "Memoria de esta sesión", protected: "Almacén protegido de Windows" };

function systemGuide() {
  const index = systemStatus.step;
  const step = systemSteps[index];
  $("#system-wizard").hidden = index === null;
  for (const section of document.querySelectorAll(".system-section")) {
    section.hidden = index !== null && "#" + section.id !== step[2];
  }
  if (index === null) return;
  $("#wizard-progress").textContent = `Paso ${index + 1} de ${systemSteps.length} · ${step[0]}`;
  $("#wizard-guidance").textContent = step[1];
  $("#wizard-back").disabled = index === 0;
  $("#wizard-next").hidden = index === systemSteps.length - 1;
}

function systemRender(snapshot, fill, mediaRevision) {
  systemStatus.snapshot = snapshot;
  const settings = snapshot.settings;
  const integrations = snapshot.integrations;
  if (fill) {
    $("#system-general").elements.projects_dir.value = settings.projects_dir;
    $("#system-general").elements.open_browser.checked = settings.open_browser;
    if (!systemMedia.dirty && mediaRevision === systemMedia.revision) {
      mediaField.value = settings.ffmpeg_dir;
    }
    $("#system-ollama-auto").checked = settings.ollama_auto_start;
    $("#system-profile").value = settings.tiktok_profile;
    $("#system-credentials").elements.profile.value = settings.tiktok_profile;
  }
  for (const control of document.querySelectorAll(".system-windows")) control.hidden = !snapshot.windows;
  $("#system-projects-current").textContent = snapshot.projects_dir === snapshot.projects_next
    ? `Proyectos · ${snapshot.projects_dir}` : `Proyectos actuales · ${snapshot.projects_dir} · Próximo inicio · ${snapshot.projects_next}`;
  const fields = [["Python", snapshot.python.version], ["Entorno virtual", snapshot.python.venv ? "Activo" : "Inactivo"],
    ...Object.entries(snapshot.python.packages)];
  $("#system-python").innerHTML = fields.map(([key, value]) =>
    `<div><dt>${escapeHTML(key)}</dt><dd>${escapeHTML(value ?? "Pendiente")}</dd></div>`).join("");
  $("#system-secret-save").disabled = !integrations.protected_available;
  $("#system-storage").textContent = integrations.protected_available
    ? "Windows DPAPI protege las credenciales y la autorización persistente para tu usuario."
    : "El almacén protegido no está disponible. Usa credenciales temporales.";
  $("#system-profile-summary").textContent = Object.entries(integrations.profiles).map(([name, mode]) => {
    const available = [mode.protected && "guardadas", mode.temporary && "temporales", mode.environment && "entorno"].filter(Boolean);
    return profileLabels[name] + " · " + (available.join(", ") || "sin credenciales");
  }).join(" · ");
  const auth = { verified: integrations.account, unverified: "Pendiente de verificación", disconnected: "Sin cuenta conectada", expired: "Autorización caducada · vuelve a conectar" };
  const expiry = integrations.access_until ? " · Vencimiento " + new Date(integrations.access_until * 1000).toLocaleString() : "";
  $("#system-account").textContent = `${profileLabels[integrations.profile]} · ${auth[integrations.authorization]}`
    + (integrations.source ? " · " + credentialLabels[integrations.source] : "") + expiry
    + (integrations.authorization_saved ? " · autorización protegida" : "")
    + (integrations.oauth_error ? " · " + integrations.oauth_error : "");
  $("#system-connect").disabled = !integrations.source || !["disconnected", "expired"].includes(integrations.authorization);
  for (const id of ["verify", "renew", "disconnect", "forget"]) {
    $("#system-" + id).disabled = integrations.authorization === "disconnected";
  }
  const info = snapshot.installation;
  $("#system-version").textContent = `Rama · ${info.branch || "sin verificar"} · Commit · ${info.installed || "sin verificar"}`
    + (info.dirty === true ? " · Cambios locales" : "");
  $("#system-log-path").textContent = "Registros · " + snapshot.logs;
  const activity = Object.values(snapshot.active).filter(Boolean);
  $("#system-active").textContent = activity.length ? "Hay trabajos activos. El cierre detiene la captura Live y espera transferencias de TikTok en curso." : "Sin trabajos activos.";
}

function systemRenderDiagnosis(report) {
  systemStatus.diagnosis = report;
  const media = report.ffmpeg;
  if (!systemMedia.busy && !systemMedia.dirty) {
    $("#system-ffmpeg").textContent = media
      ? [media.directory, media.versions.ffmpeg, media.versions.ffprobe].join(" · ")
      : "FFmpeg y FFprobe no encontrados. Indica la carpeta que contiene ambos ejecutables.";
  }
  const ai = report.ollama;
  const privacy = { local_cloud_disabled: "Proceso local iniciado con OLLAMA_NO_CLOUD", external_unverified: "Privacidad del proceso externo sin verificar", offline: "" };
  $("#system-ollama").textContent = [
    ai.installed ? "Instalación detectada" : "Instalación sin detectar",
    ai.server ? "Servidor conectado" : "Servidor detenido o sin verificar",
    ai.version, ai.model, privacy[ai.privacy], ai.last_error,
  ].filter(Boolean).join(" · ");
  const model = $("#system-model");
  model.replaceChildren(new Option("Selección automática en producción", ""));
  for (const name of ai.models) model.add(new Option(name, name));
  if (ai.model && ai.models.includes(ai.model)) model.value = ai.model;
}

function systemRenderMedia(media) {
  $("#system-ffmpeg").textContent = media
    ? [media.directory, ...Object.values(media.tools).map(tool => tool.version ??
      (tool.timed_out ? `${tool.filename}: se agotó el tiempo de espera al comprobar la versión`
        : tool.exists ? `${tool.filename}: encontrado, pero no pudo ejecutarse o no devolvió una versión compatible`
        : `${tool.filename}: archivo no encontrado`))].join(" · ")
    : "FFmpeg y FFprobe no encontrados. Indica la carpeta que contiene ambos ejecutables.";
}

function systemShortcut(state) {
  $("#system-shortcut-status").textContent = {
    created: "Acceso directo de esta instalación disponible.",
    missing: "Todavía puedes crear el acceso directo de esta instalación.",
    occupied: "El nombre del acceso directo pertenece a otra instalación.",
    unavailable: "La creación del acceso directo está disponible en Windows.",
  }[state];
}

async function systemRefresh(fill = true) {
  const mediaRevision = systemMedia.revision;
  systemRender(await systemAPI("status"), fill, mediaRevision);
}

async function systemRun(operation, success = "") {
  if (systemStatus.busy) return;
  systemStatus.busy = true;
  const error = $("#system-error");
  const message = $("#system-message");
  error.hidden = true;
  message.textContent = "Procesando…";
  try {
    await studioConfig;
    await operation();
    message.textContent = success;
  } catch (err) {
    message.textContent = "";
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    systemStatus.busy = false;
  }
}

async function systemRunMedia(action, data, pending, apply) {
  if (systemMedia.busy) return;
  systemMedia.busy = true;
  const feedback = $("#system-ffmpeg-feedback");
  const controls = mediaForm.querySelectorAll("button, input");
  controls.forEach(control => { control.disabled = true; });
  mediaForm.setAttribute("aria-busy", "true");
  feedback.textContent = pending;
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("Se agotó el tiempo de espera de Studio."));
    }, action === "choose-folder" ? 185000 : 20000);
  });
  try {
    const request = studioConfig.then(() => systemAPI(action, data, controller.signal));
    feedback.textContent = apply(await Promise.race([request, timeout]));
  } catch (error) {
    let message = controller.signal.aborted
      ? "Se agotó el tiempo de espera de Studio. Vuelve a intentarlo."
      : error.message;
    if (error instanceof SyntaxError) message = "Studio devolvió una respuesta inesperada. Vuelve a intentarlo.";
    if (error instanceof TypeError) message = "No se pudo comunicar con Studio. Comprueba que siga abierto.";
    if (action === "choose-folder") message += " Puedes pegar la ruta de la carpeta en el campo.";
    if (action === "save" && controller.signal.aborted) {
      message = "Studio no confirmó el guardado a tiempo. Recarga la página para comprobar la ruta guardada.";
    }
    feedback.textContent = message;
  } finally {
    clearTimeout(timer);
    systemMedia.busy = false;
    controls.forEach(control => { control.disabled = false; });
    mediaForm.removeAttribute("aria-busy");
  }
}

async function systemOpen() {
  switchView("system-view");
  await systemRun(async () => {
    await systemRefresh();
    const snapshot = systemStatus.snapshot;
    if (!snapshot.settings.setup_complete && systemStatus.step === null) systemStatus.step = 0;
    systemGuide();
    if (snapshot.windows) {
      systemAPI("shortcut", { action: "status" }).then(result => systemShortcut(result.shortcut))
        .catch(error => { $("#system-shortcut-status").textContent = error.message; });
    } else systemShortcut("unavailable");
  });
}

async function systemFirstStart() {
  if (!state.config?.managed_settings) return;
  try {
    const data = await systemAPI("status");
    if (!data.settings.setup_complete) await systemOpen();
  } catch (error) {
    notice(error.message, true);
  }
}

$("#show-system").addEventListener("click", systemOpen);
window.addEventListener("studio-ready", systemFirstStart);
if (state.config) systemFirstStart();
$("#system-open-wizard").addEventListener("click", () => { systemStatus.step = 0; systemGuide(); });
$("#wizard-back").addEventListener("click", () => { systemStatus.step--; systemGuide(); });
$("#wizard-next").addEventListener("click", () => { systemStatus.step++; systemGuide(); });
$("#wizard-finish").addEventListener("click", () => systemRun(async () => {
  systemRenderDiagnosis(await systemAPI("diagnose", {}));
  await systemAPI("save", { setup_complete: true });
  systemStatus.step = null;
  systemGuide();
}, "Configuración inicial guardada."));

function systemSave(data, success) {
  return systemRun(async () => {
    await systemAPI("save", data);
    await systemRefresh();
  }, success);
}

$("#system-general").addEventListener("submit", event => {
  event.preventDefault();
  const fields = event.target.elements;
  return systemSave({ projects_dir: fields.projects_dir.value.trim(), open_browser: fields.open_browser.checked },
    "Preferencias guardadas para el próximo inicio.");
});
mediaField.addEventListener("input", () => {
  systemMedia.dirty = true;
  $("#system-ffmpeg").textContent = "";
  $("#system-ffmpeg-feedback").textContent = "";
});
mediaForm.addEventListener("submit", event => {
  event.preventDefault();
  systemRunMedia("save", { ffmpeg_dir: mediaField.value.trim() }, "Guardando ubicación…", saved => {
    if (typeof saved?.ffmpeg_dir !== "string") throw new Error("Studio no confirmó la ubicación guardada.");
    mediaField.value = saved.ffmpeg_dir;
    systemMedia.dirty = false;
    systemMedia.revision++;
    return "Ubicación guardada y aplicada a los próximos trabajos.";
  });
});
$("#system-save-ollama").addEventListener("click", () => systemSave(
  { ollama_auto_start: $("#system-ollama-auto").checked }, "Preferencia de Ollama guardada."));
$("#system-save-profile").addEventListener("click", () => systemSave(
  { tiktok_profile: $("#system-profile").value }, "Perfil activo actualizado."));
$("#system-diagnose").addEventListener("click", () =>
  systemRunMedia("diagnose", { ffmpeg_dir: mediaField.value.trim() }, "Comprobando FFmpeg y FFprobe…", report => {
    systemRenderMedia(report.ffmpeg);
    return "Diagnóstico completado.";
  }));
$("#system-check-ollama").addEventListener("click", () => systemRun(async () => systemRenderDiagnosis(await systemAPI("ollama", {}))));
$("#system-save-model").addEventListener("click", () => systemRun(async () => {
  const status = await editorialApi("status");
  await editorialApi("config", { revision: status.config.revision,
    settings: { ...status.config.settings, model: $("#system-model").value } });
}, "Modelo guardado."));

for (const control of document.querySelectorAll("[data-system-folder]")) {
  control.addEventListener("click", () => {
    const field = $(`input[name="${control.dataset.systemFolder}"]`);
    if (field === mediaField) {
      return systemRunMedia("choose-folder", {}, "Esperando al selector de carpetas…", ({ path }) => {
        if (!path) return "Selección cancelada.";
        field.value = path;
        systemMedia.dirty = true;
        $("#system-ffmpeg").textContent = "";
        return "Carpeta seleccionada. Pulsa Guardar ubicación para aplicarla.";
      });
    }
    return systemRun(async () => {
      try {
        const { path } = await systemAPI("choose-folder", {});
        if (path) field.value = path;
      } catch (err) {
        throw new Error(`${err.message} Puedes pegar la ruta de la carpeta en el campo.`);
      }
    });
  });
}
for (const control of document.querySelectorAll("[data-system-shortcut]")) {
  control.addEventListener("click", () => systemRun(async () => {
    systemShortcut((await systemAPI("shortcut", { action: control.dataset.systemShortcut })).shortcut);
  }));
}
$("#system-credentials").addEventListener("submit", event => {
  event.preventDefault();
  const action = event.submitter?.value || "save";
  if (action === "delete" && !window.confirm("¿Eliminar las credenciales y la autorización guardadas de este perfil?")) return;
  const fields = event.target.elements;
  const data = { action, profile: fields.profile.value };
  if (action === "save" || action === "temporary") Object.assign(data, { key: fields.key.value, secret: fields.secret.value });
  fields.key.value = fields.secret.value = "";
  systemRun(async () => {
    await systemAPI("credentials", data);
    await systemRefresh(false);
  }, "Configuración de credenciales actualizada.");
});
for (const [id, action, body] of [
  ["connect", "connect", {}], ["verify", "verify", {}], ["renew", "verify", { renew: true }],
  ["disconnect", "disconnect", {}], ["forget", "forget", {}],
]) {
  $("#system-" + id).addEventListener("click", () => {
    if (id === "forget" && !window.confirm("¿Eliminar la autorización local? Puedes revocar el acceso también desde TikTok.")) return;
    systemRun(async () => {
      const result = ["connect", "disconnect"].includes(action)
        ? await tiktokApi(action, body) : await systemAPI(action, body);
      if (result.url) location.assign(result.url);
      else await systemRefresh(false);
    });
  });
}
$("#system-logs").addEventListener("click", () => systemRun(() => systemAPI("logs", {})));
$("#system-update-check").addEventListener("click", () => systemRun(async () => {
  $("#system-update").hidden = true;
  const info = await systemAPI("update-check", {});
  systemStatus.update = info;
  $("#system-update").hidden = !info.eligible || !info.available;
  $("#system-update-status").textContent = `Local · ${info.installed} · Remoto · ${info.remote}`
    + (info.eligible ? "" : " · Conserva tus cambios y revisa la rama antes de actualizar.");
}));
$("#system-update").addEventListener("click", () => {
  const target = systemStatus.update;
  if (!target?.eligible || !window.confirm(`¿Actualizar a ${target.remote} y cerrar Studio?`)) return;
  systemRun(() => systemAPI("update", { installed: target.installed, remote: target.remote, confirmed: true }),
    "Actualización instalada. Vuelve a abrir Anqora Media Studio desde el acceso directo.");
});
for (const control of document.querySelectorAll("[data-system-shutdown]")) {
  control.addEventListener("click", () => {
    if (!window.confirm("¿Cerrar Studio con esta modalidad?")) return;
    systemRun(() => systemAPI("shutdown", { mode: control.dataset.systemShutdown, confirmed: true }),
      "Cierre solicitado. Puedes cerrar esta pestaña.");
  });
}
