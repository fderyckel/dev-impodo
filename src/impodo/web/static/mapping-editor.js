"use strict";

document.addEventListener("DOMContentLoaded", () => {
  let getState = () => ({ dirty: false, busy: false });
  let cancelPendingContinuation = () => {};
  const targetModels = Array.from(
    document.querySelectorAll("[data-target-model]")
  );
  for (const control of targetModels) {
    control.addEventListener("change", () => {
      const query = new URLSearchParams(window.location.search);
      for (const targetModel of targetModels) {
        query.set(targetModel.name, targetModel.value);
      }
      window.impodoMappingPosition?.remember();
      window.location.assign(`${window.location.pathname}?${query.toString()}`);
    });
  }

  const {
    hydrateSourceOptions,
    releaseSourceOptions,
    initializeLazySourceSelect,
  } = window.impodoMappingSourceColumns;

  const scalarDraftRows = new Map();
  const scalarRowState = (row) => {
    const visibleTarget = row.querySelector(
      'input[name^="visible_scalar_target_"]'
    );
    if (!visibleTarget?.name || !visibleTarget.value) {
      return null;
    }
    const controls = Array.from(
      row.querySelectorAll('[name^="scalar_"]')
    ).filter(
      (control) =>
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement ||
        control instanceof HTMLTextAreaElement
    );
    const entries = [];
    const states = [];
    for (const control of controls) {
      const state = {
        name: control.name,
        values:
          control instanceof HTMLSelectElement && control.multiple
            ? Array.from(control.selectedOptions, (option) => option.value)
            : [control.value],
        checked:
          control instanceof HTMLInputElement &&
          ["checkbox", "radio"].includes(control.type)
            ? control.checked
            : null,
      };
      states.push(state);
      if (control.disabled) {
        continue;
      }
      if (state.checked === false) {
        continue;
      }
      for (const value of state.values) {
        entries.push([control.name, value]);
      }
    }
    return {
      key: `${visibleTarget.name}\u0000${visibleTarget.value}`,
      visibleName: visibleTarget.name,
      targetField: visibleTarget.value,
      providerValue: row.querySelector("[data-value-source]")?.value || "",
      controlNames: [...new Set(controls.map((control) => control.name))],
      entries,
      states,
    };
  };
  const rememberScalarRow = (row) => {
    const state = scalarRowState(row);
    if (state) {
      scalarDraftRows.set(state.key, state);
    }
  };
  const restoreScalarRow = (row) => {
    const current = scalarRowState(row);
    const saved = current ? scalarDraftRows.get(current.key) : null;
    if (!saved) {
      return;
    }
    const stateByName = new Map(
      saved.states.map((state) => [state.name, state])
    );
    for (const control of row.querySelectorAll('[name^="scalar_"]')) {
      const state = stateByName.get(control.name);
      if (!state) {
        continue;
      }
      if (
        control instanceof HTMLSelectElement &&
        control.matches("[data-lazy-source-column]")
      ) {
        initializeLazySourceSelect(control);
        hydrateSourceOptions(control);
      }
      if (
        control instanceof HTMLInputElement &&
        ["checkbox", "radio"].includes(control.type)
      ) {
        control.checked = Boolean(state.checked);
      } else if (control instanceof HTMLSelectElement && control.multiple) {
        const selected = new Set(state.values);
        for (const option of control.options) {
          option.selected = selected.has(option.value);
        }
      } else if (
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement ||
        control instanceof HTMLTextAreaElement
      ) {
        control.value = state.values[0] || "";
      }
      if (
        control instanceof HTMLSelectElement &&
        control.matches("[data-lazy-source-column]")
      ) {
        releaseSourceOptions(control);
      }
    }
  };

  const relationDraftRows = new Map();
  const relationRowState = (row) => {
    const visibleTarget = row.querySelector(
      'input[name^="visible_relation_target_"]'
    );
    if (!visibleTarget?.name || !visibleTarget.value) {
      return null;
    }
    const controls = Array.from(
      row.querySelectorAll('[name^="relation_"]')
    ).filter(
      (control) =>
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement ||
        control instanceof HTMLTextAreaElement
    );
    const entries = [];
    const states = [];
    for (const control of controls) {
      const state = {
        name: control.name,
        tagName: control.tagName,
        values:
          control instanceof HTMLSelectElement && control.multiple
            ? Array.from(control.selectedOptions, (option) => option.value)
            : [control.value],
        checked:
          control instanceof HTMLInputElement &&
          ["checkbox", "radio"].includes(control.type)
            ? control.checked
            : null,
      };
      states.push(state);
      if (control.disabled || state.checked === false) {
        continue;
      }
      for (const value of state.values) {
        entries.push([control.name, value]);
      }
    }
    const source = row.querySelector('select[name^="relation_source_"]');
    const provider = row.querySelector("[data-relation-value-source]");
    return {
      key: `${visibleTarget.name}\u0000${visibleTarget.value}`,
      visibleName: visibleTarget.name,
      targetField: visibleTarget.value,
      providerValue:
        provider?.value || (source?.selectedOptions.length ? "source" : ""),
      controlNames: [...new Set(controls.map((control) => control.name))],
      entries,
      states,
    };
  };
  const rememberRelationRow = (row) => {
    const state = relationRowState(row);
    if (state) {
      relationDraftRows.set(state.key, state);
    }
  };
  const restoreRelationRow = (row) => {
    const current = relationRowState(row);
    const saved = current ? relationDraftRows.get(current.key) : null;
    if (!saved) {
      return;
    }
    const stateByName = new Map(
      saved.states.map((state) => [`${state.tagName}\u0000${state.name}`, state])
    );
    for (const control of row.querySelectorAll('[name^="relation_"]')) {
      const state = stateByName.get(`${control.tagName}\u0000${control.name}`);
      if (!state) {
        continue;
      }
      if (
        control instanceof HTMLSelectElement &&
        control.matches("[data-lazy-source-column]")
      ) {
        initializeLazySourceSelect(control);
        hydrateSourceOptions(control);
      }
      if (
        control instanceof HTMLInputElement &&
        ["checkbox", "radio"].includes(control.type)
      ) {
        control.checked = Boolean(state.checked);
      } else if (control instanceof HTMLSelectElement && control.multiple) {
        const selected = new Set(state.values);
        for (const option of control.options) {
          option.selected = selected.has(option.value);
        }
      } else if (
        control instanceof HTMLInputElement ||
        control instanceof HTMLSelectElement ||
        control instanceof HTMLTextAreaElement
      ) {
        control.value = state.values[0] || "";
      }
      if (
        control instanceof HTMLSelectElement &&
        control.matches("[data-lazy-source-column]")
      ) {
        releaseSourceOptions(control);
      }
    }
  };

  const mappingForm = document.querySelector("[data-mapping-form]");
  if (mappingForm) {
    const saveStatus = mappingForm.querySelector(
      "[data-mapping-save-status]"
    );
    const nextRecommendedTable = mappingForm.querySelector(
      "[data-next-recommended-table]"
    );
    const confirmMapping = mappingForm.querySelector("[data-confirm-mapping]");
    let dirty = false;
    let submitting = false;
    let activeOperation = null;
    let editGeneration = 0;

    const updateMappingVersionFields = (payload) => {
      let workingVersionUpdated = false;
      const workingVersion = mappingForm.querySelector(
        'input[name="expected_working_draft_version"]'
      );
      const parentVersion = mappingForm.querySelector(
        'input[name="expected_parent_version"]'
      );
      if (
        workingVersion &&
        Object.hasOwn(payload, "expected_working_draft_version")
      ) {
        workingVersion.value = payload.expected_working_draft_version ?? "";
        workingVersionUpdated = true;
      }
      if (
        parentVersion &&
        Object.hasOwn(payload, "expected_parent_version")
      ) {
        parentVersion.value = payload.expected_parent_version ?? "";
      }
      return workingVersionUpdated;
    };

    const navigateToMappingResult = (redirectUrl, operation) => {
      const target = new URL(
        (operation && window.impodoMappingJourney?.resultUrl?.(redirectUrl, operation)) ||
          redirectUrl || window.location.pathname,
        window.location.href
      );
      const current = new URL(window.location.href);
      const samePage =
        target.origin === current.origin &&
        target.pathname === current.pathname &&
        target.search === current.search;
      if (samePage) {
        window.history.replaceState(window.history.state, "", target);
        window.location.reload();
        return;
      }
      window.location.assign(target);
    };

    const valueMatch = window.impodoMappingValueMatch?.create(mappingForm);
    const saveRecovery = window.impodoMappingSaveRecovery.create({
      mappingForm,
      saveStatus,
      updateMappingVersionFields,
      navigateToMappingResult,
      onCommitted: (operation, payload) => {
        valueMatch?.acceptSelectionRefreshes(operation);
        return window.impodoMappingJourney?.committed?.(operation, payload);
      },
      hasNewerEdits: (operation) => operation.editGeneration !== editGeneration,
      setDirty: (value) => {
        dirty = value;
      },
    });
    getState = () => ({ dirty, busy: submitting || saveRecovery.hasUnresolvedOperation() });
    cancelPendingContinuation = () => window.impodoMappingJourney?.cancelContinuation?.(activeOperation);

    const stopSubmitting = (failureMessage, statusMessage) => {
      if (!submitting) {
        return;
      }
      submitting = false;
      window.impodoMappingJourney?.cancelContinuation?.(activeOperation);
      mappingForm.removeAttribute("aria-busy");
      saveRecovery.showFailure(failureMessage);
      if (saveStatus) {
        saveStatus.textContent = statusMessage;
        saveStatus.classList.add("unsaved");
      }
    };

    document.addEventListener("impodo:server-disconnected", () => {
      stopSubmitting(
        "Impodo stopped responding before it confirmed this action. Keep this tab open. When Impodo responds again, check the save outcome before retrying.",
        "Save outcome unknown. Wait for Impodo, then check before retrying."
      );
    });
    document.addEventListener("impodo:session-ended", () => {
      stopSubmitting(
        "This Impodo session ended before it confirmed this action. Keep this tab open and copy any unsaved changes to the most recently opened Impodo tab.",
        "Session ended. This save was not confirmed."
      );
    });

    mappingForm.addEventListener("focusin", (event) => {
      window.impodoMappingPosition?.rememberInteraction(event.target);
    });
    mappingForm.addEventListener("pointerdown", (event) => {
      if (event.target?.closest?.('button[type="submit"]')) {
        window.impodoMappingPosition?.remember();
      }
    });

    const savedAt = saveStatus?.dataset.savedAt;
    if (saveStatus && savedAt) {
      const savedDate = new Date(savedAt);
      if (!Number.isNaN(savedDate.getTime())) {
        const attention = saveStatus.textContent.includes("needs attention");
        saveStatus.title = savedDate.toLocaleString();
        saveStatus.textContent = `Saved ${savedDate.toLocaleTimeString([], {
          hour: "2-digit", minute: "2-digit",
        })}${attention ? " — needs attention." : "."}`;
      }
    }

    const marksMappingDirty = (control) => {
      if (
        !(control instanceof HTMLInputElement) &&
        !(control instanceof HTMLSelectElement) &&
        !(control instanceof HTMLTextAreaElement)
      ) {
        return false;
      }
      return Boolean(control.name) && ![
        "csrf_token",
        "expected_parent_version",
        "expected_working_draft_version",
        "warning_acknowledgement",
      ].includes(control.name);
    };

    const markMappingDirty = (event) => {
      if (!marksMappingDirty(event.target)) {
        return;
      }
      dirty = true;
      editGeneration += 1;
      if (saveStatus) {
        saveStatus.textContent = "Unsaved changes.";
        saveStatus.classList.add("unsaved");
      }
      if (nextRecommendedTable) {
        nextRecommendedTable.hidden = true;
      }
      if (confirmMapping) {
        confirmMapping.disabled = true;
        confirmMapping.title = "Check the latest changes before confirming.";
      }
      window.impodoMappingPosition?.rememberInteraction(event.target);
      const scalarRow = event.target.closest("[data-scalar-mapping-row]");
      if (scalarRow) {
        window.queueMicrotask(() => rememberScalarRow(scalarRow));
      }
      const relationRow = event.target.closest("[data-relation-mapping-row]");
      if (relationRow) {
        window.queueMicrotask(() => rememberRelationRow(relationRow));
      }
    };
    mappingForm.addEventListener("input", markMappingDirty);
    mappingForm.addEventListener("change", markMappingDirty);
    document.addEventListener("submit", (event) => {
      if (!["mapping-identity-check", "mapping-relationship-check"].includes(event.target?.id)) return;
      if (!dirty && !submitting) return;
      event.preventDefault();
      saveRecovery.showFailure(submitting
        ? "Wait for the current save to finish before checking Odoo."
        : "Save progress before checking these choices. Your edits are still on this page.");
    });

    const sparseMappingEntries = (submitter) => {
      const data = new FormData(mappingForm);
      data.set("action", submitter?.value || "");
      for (const row of mappingForm.querySelectorAll(
        "[data-scalar-mapping-row]"
      )) {
        if (row.querySelector("[data-value-source]")?.value) {
          continue;
        }
        for (const control of row.querySelectorAll('[name^="scalar_"]')) {
          data.delete(control.name);
        }
      }
      for (const row of mappingForm.querySelectorAll(
        "[data-relation-mapping-row]"
      )) {
        const provider = row.querySelector("[data-relation-value-source]");
        const source = row.querySelector('select[name^="relation_source_"]');
        if (provider?.value || source?.selectedOptions.length > 0) {
          continue;
        }
        for (const control of row.querySelectorAll('[name^="relation_"]')) {
          data.delete(control.name);
        }
      }
      for (const state of scalarDraftRows.values()) {
        const visibleTargets = new Set(data.getAll(state.visibleName));
        if (!visibleTargets.has(state.targetField)) {
          data.append(state.visibleName, state.targetField);
        }
        for (const name of state.controlNames) {
          data.delete(name);
        }
        if (!state.providerValue) {
          continue;
        }
        for (const [name, value] of state.entries) {
          data.append(name, value);
        }
      }
      for (const state of relationDraftRows.values()) {
        const visibleTargets = new Set(data.getAll(state.visibleName));
        if (!visibleTargets.has(state.targetField)) {
          data.append(state.visibleName, state.targetField);
        }
        for (const name of state.controlNames) {
          data.delete(name);
        }
        if (!state.providerValue) {
          continue;
        }
        for (const [name, value] of state.entries) {
          data.append(name, value);
        }
      }
      return Array.from(data.entries(), ([name, value]) => [
        name,
        typeof value === "string" ? value : "",
      ]);
    };
    mappingForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      window.impodoMappingPosition?.remember();
      if (submitting) {
        return;
      }
      if (saveRecovery.blockIfNeeded()) {
        return;
      }
      const action = event.submitter?.value || "";
      const choosesOdooDefault =
        action === "confirm_defaults" ||
        action === "refresh_defaults" ||
        action.endsWith(":odoo_default");
      const changesFieldDisposition =
        choosesOdooDefault ||
        action.startsWith("set_disposition:") ||
        action.startsWith("clear_disposition:");
      if (["submit", "confirm_rows"].includes(action) && dirty) {
        saveRecovery.showFailure(
          "These changes have not been checked yet. Check matches before confirming. Your edits remain on this page."
        );
        if (saveStatus) {
          saveStatus.textContent = "Unsaved changes need checking.";
          saveStatus.classList.add("unsaved");
        }
        return;
      }
      if ((action === "remove_readonly" || changesFieldDisposition) && dirty) {
        saveRecovery.showFailure(
          "Save or check your current edits before changing an Odoo-field decision. Your edits remain on this page."
        );
        if (saveStatus) {
          saveStatus.textContent = "Unsaved changes need saving first.";
          saveStatus.classList.add("unsaved");
        }
        return;
      }
      const operation = saveRecovery.createOperation({
        action,
        choosesOdooDefault,
        changesDisposition: changesFieldDisposition,
        entries: sparseMappingEntries(event.submitter),
        journeyContinuation: window.impodoMappingJourney?.continuationFor?.(event.submitter),
      });
      activeOperation = operation;
      operation.editGeneration = editGeneration;
      submitting = true;
      mappingForm.setAttribute("aria-busy", "true");
      saveRecovery.clear();
      if (saveStatus) {
        if (action === "save_progress") {
          saveStatus.textContent = "Saving progress...";
        } else if (action === "remove_readonly") {
          saveStatus.textContent = "Removing Odoo-managed field matches...";
        } else if (choosesOdooDefault) {
          saveStatus.textContent =
            "Saving the Odoo decision and checking matches...";
        } else if (changesFieldDisposition) {
          saveStatus.textContent = "Saving the Odoo-field decision...";
        } else if (action === "confirm_rows") {
          saveStatus.textContent = "Confirming checked rows...";
        } else if (action === "submit") {
          saveStatus.textContent = "Confirming checked matches...";
        } else {
          saveStatus.textContent = "Checking matches...";
        }
        saveStatus.classList.remove("unsaved");
      }
      try {
        const csrfToken = mappingForm.querySelector(
          'input[name="csrf_token"]'
        )?.value;
        const mappingSaveUrl = mappingForm.getAttribute("action");
        if (!mappingSaveUrl) {
          throw new Error("The mapping save URL is missing.");
        }
        const response = await saveRecovery.fetchSave(
          mappingSaveUrl,
          {
            method: "POST",
            headers: {
              Accept: "application/json",
              "Content-Type": "application/json",
              "X-CSRF-Token": csrfToken || "",
            },
            body: JSON.stringify({ entries: operation.entries }),
          }
        );
        const payload = await saveRecovery.responseJson(response);
        if (!response.ok) {
          if (payload.status) {
            saveRecovery.applyMutationOutcome(payload, operation);
          } else {
            if (action !== "submit") {
              dirty = true;
            }
            const message =
              (payload.detail || "The matches could not be saved.") +
              saveRecovery.failureSuffix(operation);
            saveRecovery.showFailure(message, {
              operationId: operation.operationId,
            });
            if (saveStatus) {
              saveStatus.textContent = "Not saved. Your edits are retained.";
              saveStatus.classList.add("unsaved");
            }
          }
          return;
        }
        if (payload.status !== "committed" && payload.status !== "pending") {
          throw new Error("Impodo returned an incomplete save receipt.");
        }
        if (payload.status === "pending") {
          await saveRecovery.resolveMutationOutcome(operation);
        } else {
          saveRecovery.applyMutationOutcome(payload, operation);
        }
      } catch (_error) {
        await saveRecovery.resolveMutationOutcome(operation);
      } finally {
        activeOperation = null;
        submitting = false;
        mappingForm.removeAttribute("aria-busy");
      }
    });

    window.addEventListener("beforeunload", (event) => {
      if (!dirty || submitting) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    });

  }



  window.impodoMappingEditor = {
    initializeLazySourceSelect,
    mappingForm,
    getState: () => getState(),
    cancelPendingContinuation: () => cancelPendingContinuation(),
    restoreRelationRow,
    restoreScalarRow,
  };
});
