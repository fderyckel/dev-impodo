"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const { mappingForm } = window.impodoMappingEditor;

  const initializeRelationRow = (row) => {
    if (row.dataset.relationRowInitialized === "true") {
      return;
    }
    row.dataset.relationRowInitialized = "true";
    const provider = row.querySelector("[data-relation-value-source]");
    const sourceControls = row.querySelector("[data-relation-provider-source]");
    const sourcePolicy = row.querySelector("[data-relation-source-policy]");
    const constantControls = row.querySelector("[data-relation-provider-constant]");
    const businessKey = row.querySelector("[data-constant-business-key]");
    const chooser = row.querySelector("[data-constant-existing-chooser]");
    const choice = chooser?.querySelector("[data-constant-existing-choice]");
    const search = chooser?.querySelector("[data-constant-choice-search]");
    const status = chooser?.querySelector("[data-constant-choice-status]");
    let loadedChoices = [];

    const syncProvider = () => {
      const mode = provider?.value || "";
      if (sourceControls) sourceControls.hidden = mode !== "source";
      if (sourcePolicy) sourcePolicy.hidden = mode !== "source";
      if (constantControls) {
        constantControls.hidden = mode !== "constant_existing";
      }
      if (businessKey) businessKey.required = mode === "constant_existing";
      syncComponents();
    };
    const syncComponents = () => {
      const option = businessKey?.selectedOptions[0];
      const fields = [
        ...(option?.dataset.keyFields || "").split("|").filter(Boolean),
        ...(option?.dataset.scopeFields || "").split("|").filter(Boolean),
      ];
      for (const component of row.querySelectorAll(
        "[data-constant-component-row]"
      )) {
        const slot = Number(component.dataset.constantComponentSlot);
        const active = slot < fields.length;
        component.hidden = !active;
        component.style.display = active ? "" : "none";
        const input = component.querySelector("[data-constant-component-value]");
        const label = component.querySelector("[data-constant-component-label]");
        if (input) {
          input.disabled = !active;
          input.required = active && provider?.value === "constant_existing";
          input.setCustomValidity(
            input.required && !input.value.trim()
              ? `Enter ${fields[slot]} or choose an existing Odoo record.`
              : ""
          );
        }
        if (label && active) label.textContent = fields[slot];
      }
    };
    const renderChoices = () => {
      if (!choice) return;
      const query = search?.value.trim().toLocaleLowerCase() || "";
      const options = loadedChoices
        .filter((item) => item.label.toLocaleLowerCase().includes(query))
        .map((item) => {
          const option = document.createElement("option");
          option.value = item.value;
          option.textContent = item.label;
          return option;
        });
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = options.length
        ? "Choose one existing Odoo record"
        : "No matching existing record loaded";
      choice.replaceChildren(placeholder, ...options);
    };

    for (const input of row.querySelectorAll(
      "[data-constant-business-key], [data-constant-component-value]"
    )) {
      input.addEventListener("invalid", () => {
        let parent = row;
        while (parent) {
          if (parent instanceof HTMLDetailsElement) parent.open = true;
          parent = parent.parentElement;
        }
      });
      input.addEventListener("input", syncComponents);
    }
    provider?.addEventListener("change", syncProvider);
    businessKey?.addEventListener("change", syncComponents);
    search?.addEventListener("input", renderChoices);
    choice?.addEventListener("change", () => {
      if (choice.value) {
        const scoped = Boolean(
          businessKey?.selectedOptions[0]?.dataset.scopeFields
        );
        let selectedComponents;
        try {
          selectedComponents = scoped ? JSON.parse(choice.value) : [choice.value];
        } catch (_error) {
          return;
        }
        if (!Array.isArray(selectedComponents)) return;
        const inputs = Array.from(
          row.querySelectorAll(
            "[data-constant-component-row]:not([hidden]) [data-constant-component-value]"
          )
        );
        if (selectedComponents.length !== inputs.length) return;
        inputs.forEach((input, index) => {
          input.value = selectedComponents[index];
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
        if (status) {
          const values = inputs
            .map((component) => component.value.trim())
            .filter(Boolean);
          const rowCount = Number(chooser?.dataset.sourceRowCount || 0);
          const sourceName = chooser?.dataset.sourceName || "source";
          status.textContent = `${values.join(" · ")} will be used for all ${rowCount.toLocaleString()} ${sourceName} rows.`;
        }
      }
    });
    chooser?.querySelector("[data-check-constant-record]")?.addEventListener(
      "click",
      async () => {
        if (!businessKey?.value) {
          if (status) status.textContent = "Choose a matching rule first.";
          return;
        }
        const data = new FormData();
        data.set(
          "csrf_token",
          mappingForm?.querySelector('[name="csrf_token"]')?.value || ""
        );
        data.set("kind", "constant_relationship");
        data.set("dataset_id", chooser.dataset.datasetId || "");
        data.set("source_column_key", "");
        data.set("target_model", chooser.dataset.constantTargetModel || "");
        data.set("target_field", chooser.dataset.targetField || "");
        data.set("business_key_id", businessKey.value);
        data.set("refresh", "0");
        if (status) status.textContent = "Checking current Odoo choices…";
        try {
          const response = await fetch(chooser.dataset.endpoint, {
            method: "POST",
            body: data,
            headers: { Accept: "application/json" },
          });
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.detail || "The record could not be checked.");
          }
          loadedChoices = Array.isArray(payload.target_choices)
            ? payload.target_choices
            : [];
          renderChoices();
          if (status) {
            status.textContent = loadedChoices.length
              ? `${loadedChoices.length.toLocaleString()} unambiguous existing record(s) available. Choose a record to fill the matching value.`
              : "No unambiguous existing record matches this rule.";
          }
        } catch (error) {
          loadedChoices = [];
          renderChoices();
          if (status) {
            status.textContent =
              error instanceof Error
                ? error.message
                : "The record could not be checked.";
          }
        }
      }
    );
    syncProvider();
  };

  for (const row of document.querySelectorAll("[data-relation-mapping-row]")) {
    initializeRelationRow(row);
  }

  window.impodoMappingEditor.initializeRelationRow = initializeRelationRow;
});
