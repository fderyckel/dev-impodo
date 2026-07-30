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

  const modelPicker = document.querySelector("[data-model-picker]");
  if (modelPicker) {
    const search = modelPicker.querySelector("[data-model-search]");
    const showAll = modelPicker.querySelector("[data-show-all-models]");
    const count = modelPicker.querySelector("[data-model-count]");
    const choices = Array.from(
      modelPicker.querySelectorAll("[data-model-choice]")
    );
    const updateModelChoices = () => {
      const query = search?.value.trim().toLocaleLowerCase() || "";
      let visibleCount = 0;
      let selectedCount = 0;
      for (const choice of choices) {
        const checkbox = choice.querySelector('input[name="permitted_models"]');
        const selected = Boolean(checkbox?.checked);
        const inFocus = choice.dataset.inFocus === "true";
        const matches = (choice.dataset.modelSearchText || "").includes(query);
        const visible =
          matches && (Boolean(showAll?.checked) || inFocus || selected);
        choice.hidden = !visible;
        visibleCount += visible ? 1 : 0;
        selectedCount += selected ? 1 : 0;
      }
      if (count) {
        count.textContent =
          `${visibleCount} models shown · ${selectedCount} selected`;
      }
    };
    search?.addEventListener("input", updateModelChoices);
    showAll?.addEventListener("change", updateModelChoices);
    for (const checkbox of modelPicker.querySelectorAll(
      'input[name="permitted_models"]'
    )) {
      checkbox.addEventListener("change", updateModelChoices);
    }
    updateModelChoices();
  }

  for (const catalog of document.querySelectorAll("[data-field-catalog]")) {
    const search = catalog.querySelector("[data-field-search]");
    const showReadonly = catalog.querySelector("[data-show-readonly-fields]");
    const count = catalog.querySelector("[data-field-count]");
    const rows = Array.from(catalog.querySelectorAll("[data-field-row]"));
    const updateFieldRows = () => {
      const query = search?.value.trim().toLocaleLowerCase() || "";
      let visibleCount = 0;
      for (const row of rows) {
        const matches = (row.dataset.fieldSearchText || "").includes(query);
        const writable =
          row.dataset.readonly !== "true" || Boolean(showReadonly?.checked);
        const visible = matches && writable;
        row.hidden = !visible;
        visibleCount += visible ? 1 : 0;
      }
      if (count) {
        count.textContent =
          `${visibleCount} of ${rows.length} captured fields shown`;
      }
    };
    search?.addEventListener("input", updateFieldRows);
    showReadonly?.addEventListener("change", updateFieldRows);
    updateFieldRows();
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

  for (const form of document.querySelectorAll("[data-start-stack-form]")) {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-start-stack-button]");
      form.setAttribute("aria-busy", "true");
      if (button) {
        button.disabled = true;
        button.textContent = "Starting and checking…";
      }
    });
  }

  for (const form of document.querySelectorAll("[data-control-stack-form]")) {
    form.addEventListener("submit", (event) => {
      const action = event.submitter?.dataset.controlStackActionValue || "";
      const actionInput = form.querySelector("[data-control-stack-action]");
      if (actionInput) {
        actionInput.value = action;
      }
      form.setAttribute("aria-busy", "true");
      for (const button of form.querySelectorAll("[data-control-stack-button]")) {
        button.disabled = true;
      }
      if (event.submitter) {
        event.submitter.textContent =
          action === "restart" ? "Restarting and checking…" : "Stopping and checking…";
      }
    });
  }
});
