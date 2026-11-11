"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const { mappingForm } = window.impodoMappingEditor;

  const initializeRelationshipOriginControls = (controls) => {
    if (controls.dataset.relationshipOriginInitialized === "true") {
      return;
    }
    const origin = controls.querySelector("[data-relationship-origin]");
    if (!origin) {
      return;
    }
    controls.dataset.relationshipOriginInitialized = "true";

    const zeroRoot = controls.querySelector("[data-identity-zero-root]");
    const parentTable = controls.querySelector('select[name^="identity_dataset_"]');
    const parentSource = controls.querySelector('select[name^="identity_source_"]');
    const syncOriginControls = () => {
      const allowsIncoming = origin.value !== "target_catalog";
      const allowsExistingOdoo = origin.value !== "dataset";
      const usesCombinedLookup = origin.value === "target_then_dataset";
      for (const control of controls.querySelectorAll(
        "[data-relationship-incoming-control]"
      )) {
        control.hidden = !allowsIncoming;
      }
      for (const control of controls.querySelectorAll(
        "[data-relationship-odoo-control]"
      )) {
        control.hidden = !allowsExistingOdoo;
      }
      for (const control of controls.querySelectorAll(
        "[data-relationship-combined-control]"
      )) {
        control.hidden = !usesCombinedLookup;
      }
      if (zeroRoot) {
        const hasOneSource = Array.from(parentSource.selectedOptions)
          .filter((option) => option.value).length === 1;
        const available = !allowsExistingOdoo && hasOneSource &&
          parentTable.value === zeroRoot.dataset.identityZeroRoot;
        zeroRoot.querySelector("input").disabled = !available;
        zeroRoot.querySelector("[data-zero-root-requirements]").hidden = available;
      }
    };

    origin.addEventListener("change", syncOriginControls);
    if (zeroRoot) {
      parentTable.addEventListener("change", syncOriginControls);
      parentSource.addEventListener("change", syncOriginControls);
    }
    syncOriginControls();
  };

  const initializeLinkedMatchingFields = (row) => {
    const load = row.querySelector("[data-load-linked-matching-fields]");
    if (!load) return;
    const key = row.querySelector("[data-linked-matching-key]");
    const origin = row.querySelector("[data-relationship-origin]");
    const policy = row.querySelector("[data-relation-source-policy]");
    const policyChoice = policy?.querySelector("[data-categorical-policy]");
    const policyLabel = policy?.querySelector("[data-linked-matching-policy]");
    const exactNote = policy?.querySelector("[data-exact-linked-field-note]");
    const valueMatch = policy?.querySelector("[data-linked-value-match-entry]");
    const blankChoice = policy?.querySelector("[data-blank-create-storage]");
    const status = row.querySelector("[data-linked-matching-field-status]");

    const sync = () => {
      const exactOnly = Boolean(key.selectedOptions[0]?.dataset.selectedMatchingField);
      if (policyLabel) policyLabel.hidden = exactOnly;
      if (exactNote) exactNote.hidden = !exactOnly;
      if (valueMatch) valueMatch.hidden = exactOnly;
      if (exactOnly && policyChoice) policyChoice.value = "EXACT_BUSINESS_KEY";
    };
    key.addEventListener("change", () => {
      if (blankChoice) blankChoice.value = "";
      sync();
    });
    origin.addEventListener("change", () => {
      const existingOnly = origin.value === "target_catalog";
      load.closest("[data-relationship-field-picker]").hidden = !existingOnly;
      for (const option of key.querySelectorAll("[data-selected-matching-field]")) {
        option.disabled = !existingOnly;
      }
      if (!existingOnly && key.selectedOptions[0]?.dataset.selectedMatchingField) {
        key.value = "";
        key.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    load.addEventListener("click", async () => {
      load.disabled = true;
      status.textContent = "Checking fields in Odoo…";
      try {
        const body = new URLSearchParams({
          csrf_token: mappingForm.querySelector('[name="csrf_token"]').value,
          target_model: load.dataset.targetModel,
          target_field: row.dataset.targetField,
        });
        const response = await fetch(load.dataset.endpoint, { method: "POST", body });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "Impodo could not check the Odoo fields.");
        }
        const known = new Set(
          Array.from(key.querySelectorAll("[data-matching-field]"), (option) => option.dataset.matchingField)
        );
        const selected = key.value;
        let available = 0;
        for (const field of payload.fields) {
          if (known.has(field.name)) continue;
          const label = field.label === field.name ? field.label : `${field.label} (${field.name})`;
          const value = `odoo-field:${field.name}`;
          const option = Array.from(key.options).find((item) => item.value === value) || new Option();
          option.value = value;
          option.textContent = label;
          option.dataset.selectedMatchingField = field.name;
          option.disabled = origin.value !== "target_catalog";
          if (!option.parentElement) key.add(option);
          available++;
        }
        key.value = selected;
        status.textContent = available
          ? "Choose a matching field from the list above."
          : "The list already shows the available matching fields.";
        if (load.isConnected && origin.value === "target_catalog") key.focus();
      } catch (error) {
        status.textContent = error instanceof Error ? error.message : "Impodo could not check the Odoo fields.";
      } finally {
        load.disabled = false;
      }
    });
    sync();
  };

  const initializeRelationRow = (row) => {
    if (row.dataset.relationRowInitialized === "true") {
      return;
    }
    row.dataset.relationRowInitialized = "true";
    initializeLinkedMatchingFields(row);
    for (const controls of row.querySelectorAll(
      "[data-relationship-origin-controls]"
    )) {
      initializeRelationshipOriginControls(controls);
    }
    const provider = row.querySelector("[data-relation-value-source]");
    const sourceControls = row.querySelector("[data-relation-provider-source]");
    const sourcePolicy = row.querySelector("[data-relation-source-policy]");
    const constantControls = row.querySelector("[data-relation-provider-constant]");
    const businessKey = row.querySelector("[data-constant-business-key]");
    const chooser = row.querySelector("[data-constant-existing-chooser]");
    const choice = chooser?.querySelector("[data-constant-existing-choice]");
    const search = chooser?.querySelector("[data-constant-choice-search]");
    const status = chooser?.querySelector("[data-constant-choice-status]");
    const schemaReview = chooser?.querySelector("[data-constant-schema-review]");
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
      const keyCount = (option?.dataset.keyFields || "").split("|").filter(Boolean).length;
      const fields = [
        ...(option?.dataset.keyFields || "").split("|").filter(Boolean),
        ...(option?.dataset.scopeFields || "").split("|").filter(Boolean),
      ];
      for (const component of row.querySelectorAll(
        "[data-constant-component-row]"
      )) {
        const slot = Number(component.dataset.constantComponentSlot);
        const active = slot < fields.length;
        const scoped = active && slot >= keyCount && fields[slot] === "company_id";
        component.hidden = !active;
        component.style.display = active ? "" : "none";
        const input = component.querySelector("[data-constant-component-value]");
        const scopeChoice = component.querySelector("[data-constant-scope-choice]");
        const label = component.querySelector("[data-constant-component-label]");
        if (input) {
          input.hidden = scoped;
          input.style.display = scoped ? "none" : "";
          input.disabled = !active || scoped;
          input.required = active && !scoped && provider?.value === "constant_existing";
          input.setCustomValidity(
            input.required && !input.value.trim()
              ? `Enter ${fields[slot]} or choose an existing Odoo record.`
              : ""
          );
        }
        if (scopeChoice) {
          scopeChoice.hidden = !scoped;
          scopeChoice.style.display = scoped ? "" : "none";
          scopeChoice.disabled = !scoped;
          scopeChoice.required = scoped && provider?.value === "constant_existing";
        }
        if (label && active) label.textContent = fields[slot];
      }
    };
    const renderScopeChoices = (preserveSelection = false) => {
      for (const component of row.querySelectorAll("[data-constant-component-row]")) {
        const select = component.querySelector("[data-constant-scope-choice]");
        if (!select || select.disabled) continue;
        const slot = Number(component.dataset.constantComponentSlot);
        const selected = select.value;
        const values = new Set();
        for (const item of loadedChoices) {
          try {
            const parts = JSON.parse(item.value);
            if (Array.isArray(parts) && parts[slot]) values.add(parts[slot]);
          } catch (_error) {
            continue;
          }
        }
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = values.size
          ? "Choose an existing company"
          : "Check this record to load choices";
        const options = Array.from(values).sort((a, b) => a.localeCompare(b))
          .map((value) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            return option;
          });
        if (preserveSelection && selected && !values.has(selected)) {
          const previous = document.createElement("option");
          previous.value = selected;
          previous.textContent = `${selected} (check Odoo)`;
          options.push(previous);
        }
        select.replaceChildren(placeholder, ...options);
        select.value = preserveSelection || values.has(selected) ? selected : "";
      }
    };
    const renderChoices = () => {
      if (!choice) return;
      const query = search?.value.trim().toLocaleLowerCase() || "";
      const scopes = Array.from(row.querySelectorAll(
        "[data-constant-component-row]:not([hidden]) [data-constant-scope-choice]:not([disabled])"
      )).map((select) => ({
        slot: Number(select.closest("[data-constant-component-row]").dataset.constantComponentSlot),
        value: select.value,
      }));
      const options = loadedChoices
        .filter((item) => {
          if (!item.label.toLocaleLowerCase().includes(query)) return false;
          if (!scopes.some((scope) => scope.value)) return true;
          try {
            const parts = JSON.parse(item.value);
            return scopes.every((scope) => !scope.value || parts[scope.slot] === scope.value);
          } catch (_error) {
            return false;
          }
        })
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
    businessKey?.addEventListener("change", () => {
      loadedChoices = [];
      for (const component of row.querySelectorAll("[data-constant-component-row]")) {
        const input = component.querySelector("[data-constant-component-value]");
        if (input) input.value = "";
      }
      syncComponents();
      renderScopeChoices();
      renderChoices();
      if (schemaReview) schemaReview.hidden = true;
      if (status) status.textContent = "Check this record to load available Odoo choices.";
    });
    search?.addEventListener("input", renderChoices);
    for (const scopeChoice of row.querySelectorAll("[data-constant-scope-choice]")) {
      scopeChoice.addEventListener("change", renderChoices);
    }
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
        const components = Array.from(
          row.querySelectorAll("[data-constant-component-row]:not([hidden])")
        ).map((component) => component.querySelector(
          "[data-constant-scope-choice]:not([disabled]), [data-constant-component-value]:not([disabled])"
        ));
        if (selectedComponents.length !== components.length) return;
        components.forEach((component, index) => {
          component.value = selectedComponents[index];
          component.dispatchEvent(new Event("input", { bubbles: true }));
        });
        if (status) {
          const values = components
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
        const matchingValue = row.querySelector(
          '[data-constant-component-slot="0"] [data-constant-component-value]'
        )?.value.trim() || "";
        data.set("constant_value", matchingValue);
        data.set("refresh", "0");
        if (schemaReview) schemaReview.hidden = true;
        if (status) status.textContent = matchingValue
          ? "Checking this matching value in Odoo…"
          : "Loading existing Odoo choices…";
        try {
          const response = await fetch(chooser.dataset.endpoint, {
            method: "POST",
            body: data,
            headers: { Accept: "application/json" },
          });
          const payload = await response.json();
          if (!response.ok) {
            if (schemaReview) schemaReview.hidden = payload.review_odoo_details !== true;
            throw new Error(payload.detail || "The record could not be checked.");
          }
          loadedChoices = Array.isArray(payload.target_choices)
            ? payload.target_choices
            : [];
          renderScopeChoices();
          renderChoices();
          if (status) {
            status.textContent = (payload.ambiguous_values || []).length && !loadedChoices.length
              ? "More than one Odoo record matches. Choose the company for this load or use a more specific matching rule."
              : loadedChoices.length
              ? `${loadedChoices.length.toLocaleString()} unambiguous existing record(s) available. Choose a record to fill the matching value.`
              : matchingValue
              ? "No matching Odoo record was found under the current company and read access."
              : "No unambiguous existing record matches this rule.";
          }
        } catch (error) {
          loadedChoices = [];
          renderScopeChoices(true);
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
  for (const row of document.querySelectorAll("[data-identity-matching-field]")) {
    initializeLinkedMatchingFields(row);
  }
  for (const controls of document.querySelectorAll(
    "[data-relationship-origin-controls]"
  )) {
    initializeRelationshipOriginControls(controls);
  }

  window.impodoMappingEditor.initializeRelationRow = initializeRelationRow;
});
