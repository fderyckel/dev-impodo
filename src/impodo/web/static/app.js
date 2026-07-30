"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const targetModels = Array.from(
    document.querySelectorAll("[data-target-model]")
  );
  for (const control of targetModels) {
    control.addEventListener("change", () => {
      const query = new URLSearchParams(window.location.search);
      for (const targetModel of targetModels) {
        query.set(targetModel.name, targetModel.value);
      }
      window.location.assign(`${window.location.pathname}?${query.toString()}`);
    });
  }

  const localStackDialog = document.querySelector("[data-local-stack-dialog]");
  const localStackEntry = document.querySelector("[data-local-stack-entry]");
  const connectionModes = Array.from(
    document.querySelectorAll('input[name="odoo_connection_mode"]')
  );
  const updateLocalStackVisibility = () => {
    if (!localStackEntry) {
      return;
    }
    const selected = connectionModes.find((control) => control.checked);
    localStackEntry.hidden = Boolean(selected && selected.value !== "LOCAL");
  };
  for (const control of connectionModes) {
    control.addEventListener("change", updateLocalStackVisibility);
  }
  updateLocalStackVisibility();

  if (localStackDialog) {
    for (const trigger of document.querySelectorAll("[data-open-local-stack]")) {
      trigger.addEventListener("click", () => localStackDialog.showModal());
    }
    if (
      localStackDialog.dataset.autoOpen === "true" &&
      !localStackDialog.open
    ) {
      localStackDialog.showModal();
    }
  }
});
