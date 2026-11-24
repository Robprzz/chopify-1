"use strict";

const telegramNames = {
  disabled: "Desactivado", connecting: "Conectando", online: "Conectado",
  reconnecting: "Reconectando", invalid_token: "Telegram rechazó el Bot Token",
  vault_error: "Almacén protegido inaccesible", local_error: "Telegram no pudo iniciar en Studio",
};
const telegramDeliveries = {
  queued: "en espera", sending: "enviando", sent: "enviados", failed: "fallidos",
  uncertain: "sin confirmación", cancelled: "cancelados",
};
let telegramRefreshing = false;
let telegramMutating = false;
let telegramPairCode = "";

async function refreshTelegram(fill = false) {
  const data = await api("/api/telegram/status");
  const settings = data.settings;
  if (fill) {
    $("#telegram-enabled").checked = settings.enabled;
    $("#telegram-immediate").checked = settings.immediate;
    $("#telegram-alerts").checked = settings.alerts;
    $("#telegram-score").value = settings.minimum_score;
    $("#telegram-priority").value = settings.minimum_priority;
    $("#telegram-frequency").value = settings.max_per_hour;
    $("#telegram-quiet-start").value = settings.quiet_start;
    $("#telegram-quiet-end").value = settings.quiet_end;
    $("#telegram-sources").value = settings.sources.join("\n");
  }
  $("#telegram-state").textContent = data.managed
    ? [telegramNames[data.connection], data.has_token ? "Token protegido" : "Sin token",
      data.paired ? "Cuenta vinculada" : "Sin vincular"].join(" · ")
    : data.installed ? telegramNames[data.connection] : "Inicia Studio con una instalación configurada.";
  for (const field of $("#telegram-settings").elements) field.disabled = !data.managed;
  $("#telegram-test").disabled = !data.has_token;
  $("#telegram-pair").disabled = !data.has_token || !settings.enabled || data.paired;
  $("#telegram-revoke").disabled = !data.paired;
  $("#telegram-notify").disabled = !data.paired || !settings.enabled;
  const code = $("#telegram-pair-code");
  if (data.paired || !data.pairing_seconds) telegramPairCode = "";
  code.hidden = !telegramPairCode;
  code.textContent = telegramPairCode
    ? `En el chat privado con tu bot, envía /pair ${telegramPairCode}. Expira en ${data.pairing_seconds} s.` : "";
  $("#telegram-deliveries").textContent = Object.entries(data.deliveries)
    .map(([state, count]) => `${count} ${telegramDeliveries[state] || state}`).join(" · ") || "Sin entregas.";
  $("#telegram-log").innerHTML = data.log.map(({stamp, message}) =>
    `<li>${escapeHTML(new Date(stamp * 1000).toLocaleString())} · ${escapeHTML(message)}</li>`).join("");
}

const telegramAction = (handler) => safe(async (event) => {
  if (telegramMutating) return;
  telegramMutating = true;
  const feedback = $("#telegram-feedback");
  feedback.textContent = "";
  try {
    feedback.textContent = await handler(event) || "";
    await refreshTelegram(event.type === "submit");
  } catch (error) {
    feedback.textContent = error.message;
  } finally {
    telegramMutating = false;
  }
});

$("#show-telegram").addEventListener("click", safe(async () => {
  switchView("telegram-view");
  await studioConfig;
  await refreshTelegram(true);
}));
$("#telegram-settings").addEventListener("submit", telegramAction(async (event) => {
  event.preventDefault();
  const token = $("#telegram-token").value.trim();
  $("#telegram-token").value = "";
  const settings = {
    enabled: $("#telegram-enabled").checked, immediate: $("#telegram-immediate").checked,
    alerts: $("#telegram-alerts").checked, minimum_score: $("#telegram-score").valueAsNumber,
    minimum_priority: $("#telegram-priority").valueAsNumber, max_per_hour: $("#telegram-frequency").valueAsNumber,
    quiet_start: $("#telegram-quiet-start").value, quiet_end: $("#telegram-quiet-end").value,
    sources: $("#telegram-sources").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean),
  };
  await api("/api/telegram/settings", token ? {settings, token} : {settings});
  return "Ajustes guardados.";
}));
$("#telegram-test").addEventListener("click", telegramAction(async () => {
  await api("/api/telegram/test", {});
  return "Telegram confirmó el Bot Token.";
}));
$("#telegram-pair").addEventListener("click", telegramAction(async () => {
  const result = await api("/api/telegram/pair", {});
  telegramPairCode = result.code;
}));
$("#telegram-revoke").addEventListener("click", telegramAction(async () => {
  await api("/api/telegram/revoke", {});
  telegramPairCode = "";
  return "Vinculación revocada.";
}));
$("#telegram-notify").addEventListener("click", telegramAction(async () => {
  await api("/api/telegram/notify", {});
  return "Notificación de prueba en cola.";
}));
setInterval(async () => {
  if ($("#telegram-view").hidden || telegramRefreshing || telegramMutating) return;
  telegramRefreshing = true;
  try { await refreshTelegram(); }
  catch (error) { $("#telegram-state").textContent = error.message; }
  finally { telegramRefreshing = false; }
}, 2500);
