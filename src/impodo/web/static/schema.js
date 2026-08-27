"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const modelPicker = document.querySelector("[data-model-picker]");
  if (modelPicker) {
    const search = modelPicker.querySelector("[data-model-search]");
    const showAll = modelPicker.querySelector("[data-show-all-models]");
    const count = modelPicker.querySelector("[data-model-count]");
    const submit = modelPicker.querySelector("[data-model-submit]");
    const submitStatus = modelPicker.querySelector(
      "[data-model-submit-status]"
    );
    const choices = Array.from(
      modelPicker.querySelectorAll("[data-model-choice]")
    ).map((element) => ({
      element,
      checkbox: element.querySelector('input[name="permitted_models"]'),
      inFocus: element.dataset.inFocus === "true",
      searchText: element.dataset.modelSearchText || "",
    }));
    const updateModelChoices = () => {
      const query = search?.value.trim().toLocaleLowerCase() || "";
      const hasQuery = Boolean(query);
      const browseAll = Boolean(showAll?.checked);
      let visibleCount = 0;
      let selectedCount = 0;
      for (const choice of choices) {
        const selected = Boolean(choice.checkbox?.checked);
        const matches = choice.searchText.includes(query);
        const visible =
          matches && (hasQuery || browseAll || choice.inFocus || selected);
        choice.element.hidden = !visible;
        visibleCount += visible ? 1 : 0;
        selectedCount += selected ? 1 : 0;
      }
      if (count) {
        count.textContent =
          `${visibleCount} of ${choices.length} Odoo data choices shown · ` +
          `${selectedCount} selected`;
      }
    };
    search?.addEventListener("input", updateModelChoices);
    showAll?.addEventListener("change", updateModelChoices);
    for (const choice of choices) {
      choice.checkbox?.addEventListener("change", updateModelChoices);
    }
    modelPicker.addEventListener("submit", () => {
      modelPicker.setAttribute("aria-busy", "true");
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Saving choices and loading Odoo data...";
      }
      if (submitStatus) {
        submitStatus.textContent = "This may take a moment.";
      }
    });
    updateModelChoices();
  }

  const schemaLoadError = document.querySelector("[data-schema-load-error]");
  schemaLoadError?.focus();

  for (const decision of document.querySelectorAll("[data-key-decision]")) {
    const primaryKey = decision.querySelector("[data-primary-key-field]");
    const primaryScope = decision.querySelector("[data-primary-scope-field]");
    const combinedKey = decision.querySelector("[data-combined-key-fields]");
    const combinedScope = decision.querySelector("[data-combined-scope-fields]");
    const description = decision.querySelector("[data-key-description]");
    const draft = decision.querySelector("[data-key-draft-selection]");
    const draftSummary = draft?.querySelector("[data-key-selection-summary]");
    const suggestion = decision.querySelector("[data-use-key-suggestion]");
    const editor = decision.querySelector("[data-key-editor]");
    const technicalFields = decision.querySelector(".key-technical-fields");
    const fieldError = decision.querySelector("[data-key-field-error]");
    const keyForm = decision.closest("form");
    let hasFieldConflict = false;
    let labels = {};
    try {
      labels = JSON.parse(decision.dataset.fieldLabels || "{}");
    } catch (_error) {
      labels = {};
    }

    const values = (rawValue) =>
      (rawValue || "")
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
    const displayField = (name) =>
      labels[name] ? `${labels[name]} (${name})` : name;
    const showDraft = () => {
      const keyFields = values(combinedKey?.value);
      const scopeFields = values(combinedScope?.value);
      if (!draft || !draftSummary || !keyFields.length) {
        if (draft) {
          draft.hidden = true;
        }
        return;
      }
      let summary = keyFields.map(displayField).join(" + ");
      if (scopeFields.length) {
        summary += `, within ${scopeFields.map(displayField).join(" + ")}`;
      }
      draftSummary.textContent = summary;
      draft.hidden = false;
    };
    const updateKeyFieldConflicts = () => {
      const keyFields = values(combinedKey?.value);
      const scopeFields = values(combinedScope?.value);
      const keyNames = new Set(keyFields);
      const scopeNames = new Set(scopeFields);
      const allFields = [...keyFields, ...scopeFields];
      hasFieldConflict = new Set(allFields).size !== allFields.length;

      for (const option of primaryScope?.options || []) {
        option.disabled = Boolean(
          option.value &&
          keyNames.has(option.value) &&
          option.value !== primaryScope.value
        );
      }
      for (const option of primaryKey?.options || []) {
        option.disabled = Boolean(
          option.value &&
          scopeNames.has(option.value) &&
          option.value !== primaryKey.value
        );
      }
      for (const control of [
        primaryKey,
        primaryScope,
        combinedKey,
        combinedScope,
      ]) {
        if (control) {
          control.setAttribute("aria-invalid", String(hasFieldConflict));
        }
      }
      if (fieldError) {
        fieldError.textContent = hasFieldConflict
          ? "Choose each field only once. Matching fields and Within fields must be different."
          : "";
        fieldError.hidden = !hasFieldConflict;
      }
    };
    const refreshKeyDraft = () => {
      showDraft();
      updateKeyFieldConflicts();
    };
    const syncSimpleChoice = (select, input) => {
      if (!select || !input) {
        return;
      }
      input.value = select.value;
      refreshKeyDraft();
    };

    primaryKey?.addEventListener("change", () => {
      syncSimpleChoice(primaryKey, combinedKey);
    });
    primaryScope?.addEventListener("change", () => {
      syncSimpleChoice(primaryScope, combinedScope);
    });
    combinedKey?.addEventListener("input", () => {
      const keyFields = values(combinedKey.value);
      if (primaryKey) {
        primaryKey.value = keyFields.length === 1 ? keyFields[0] : "";
      }
      refreshKeyDraft();
    });
    combinedScope?.addEventListener("input", () => {
      const scopeFields = values(combinedScope.value);
      if (primaryScope) {
        primaryScope.value = scopeFields.length === 1 ? scopeFields[0] : "";
      }
      refreshKeyDraft();
    });
    suggestion?.addEventListener("click", () => {
      const keyFields = values(suggestion.dataset.keyFields);
      const scopeFields = values(suggestion.dataset.scopeFields);
      if (combinedKey) {
        combinedKey.value = keyFields.join(", ");
      }
      if (combinedScope) {
        combinedScope.value = scopeFields.join(", ");
      }
      if (primaryKey) {
        primaryKey.value = keyFields.length === 1 ? keyFields[0] : "";
      }
      if (primaryScope) {
        primaryScope.value = scopeFields.length === 1 ? scopeFields[0] : "";
      }
      if (description) {
        description.value = suggestion.dataset.description || "";
      }
      suggestion.textContent = "Suggestion selected";
      suggestion.setAttribute("aria-pressed", "true");
      if (editor) {
        editor.open = false;
      }
      refreshKeyDraft();
    });
    keyForm?.addEventListener("submit", (event) => {
      updateKeyFieldConflicts();
      if (!hasFieldConflict || event.defaultPrevented) {
        return;
      }
      event.preventDefault();
      if (editor) {
        editor.open = true;
      }
      if (technicalFields) {
        technicalFields.open = true;
      }
      fieldError?.focus();
    });
    updateKeyFieldConflicts();
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

});
