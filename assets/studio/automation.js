"use strict";

const automationFields = $("#automation-fields");
const automationInputs = [...automationFields.querySelectorAll("input[name]")];
const priorityWeights = [...automationFields.querySelectorAll("[data-priority-weight]")];
const priorityCurve = [...automationFields.querySelectorAll("[data-priority-curve]")];
const priorityLevels = [...automationFields.querySelectorAll("[data-priority-level]")];
const automationEndpoint = "/api/automation/settings";
let automationSnapshot = null;
let automationBusy = false;

function applyAutomationSettings(snapshot) {
  automationSnapshot = snapshot;
  for (const field of automationInputs) {
    const value = snapshot.settings[field.name];
    if (field.type === "checkbox") field.checked = value;
    else field.value = value;
  }
  const engine = snapshot.settings.priority_engine;
  priorityWeights.forEach(field => { field.value = engine.weights[field.dataset.priorityWeight]; });
  priorityCurve.forEach((field, index) => { field.value = engine.curve[index]; });
  priorityLevels.forEach(field => { field.value = engine.levels[field.dataset.priorityLevel]; });
}

async function updateAutomation(message, operation, success) {
  if (automationBusy) return;
  automationBusy = true;
  automationFields.disabled = true;
  $("#automation-save").disabled = true;
  $("#automation-reload").disabled = true;
  $("#automation-status").textContent = message;
  const errorBox = $("#automation-error");
  errorBox.hidden = true;
  try {
    await studioConfig;
    applyAutomationSettings(await operation());
    $("#automation-status").textContent = success;
  } catch (error) {
    $("#automation-status").textContent = "";
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    automationBusy = false;
    automationFields.disabled = !automationSnapshot;
    $("#automation-save").disabled = !automationSnapshot;
    $("#automation-reload").disabled = false;
  }
}

function loadAutomationSettings() {
  return updateAutomation("Cargando ajustes…", () => api(automationEndpoint), "");
}

$("#show-automation").addEventListener("click", () => {
  state.id = null;
  switchView("automation-view");
  loadAutomationSettings();
});
$("#automation-reload").addEventListener("click", loadAutomationSettings);
$("#automation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!automationSnapshot || automationBusy) return;
  const settings = { ...automationSnapshot.settings };
  for (const field of automationInputs) {
    if (field.type === "checkbox") settings[field.name] = field.checked;
    else if (field.type === "number") settings[field.name] = field.valueAsNumber;
    else settings[field.name] = field.value.trim();
  }
  settings.priority_engine = {
    weights: Object.fromEntries(priorityWeights.map(field => [field.dataset.priorityWeight, field.valueAsNumber])),
    curve: priorityCurve.map(field => field.valueAsNumber),
    levels: Object.fromEntries(priorityLevels.map(field => [field.dataset.priorityLevel, field.valueAsNumber])),
  };
  const payload = { revision: automationSnapshot.revision, settings };
  updateAutomation("Guardando ajustes…", () => api(automationEndpoint, payload), "Ajustes guardados.");
});
