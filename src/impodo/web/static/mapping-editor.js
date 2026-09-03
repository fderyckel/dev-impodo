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
      window.impodoMappingPosition?.remember();
      window.location.assign(`${window.location.pathname}?${query.toString()}`);
    });
  }

  const hydrateSourceOptions = (select) => {
    if (select.dataset.sourceOptionsLoaded === "true") {
      return;
    }
    const template = select
      .closest(".mapping-dataset")
      ?.querySelector("template[data-source-column-options]");
    if (!(template instanceof HTMLTemplateElement)) {
      return;
    }
    const selectedValues = new Set(
      Array.from(select.selectedOptions, (option) => option.value)
    );
    const placeholder = Array.from(select.options).find(
      (option) => option.value === ""
    );
    select.replaceChildren();
    if (placeholder) {
      select.append(placeholder.cloneNode(true));
    }
    select.append(template.content.cloneNode(true));
    for (const option of select.options) {
      option.selected = selectedValues.has(option.value);
    }
    select.dataset.sourceOptionsLoaded = "true";
  };

  const releaseSourceOptions = (select) => {
    if (select.dataset.sourceOptionsLoaded !== "true") {
      return;
    }
    const selectedValues = new Set(
      Array.from(select.selectedOptions, (option) => option.value)
    );
    const retained = Array.from(select.options)
      .filter((option) => option.value === "" || option.selected)
      .map((option) => option.cloneNode(true));
    select.replaceChildren(...retained);
    for (const option of select.options) {
      const selected = selectedValues.has(option.value);
      option.selected = selected;
      option.defaultSelected = selected;
    }
    select.dataset.sourceOptionsLoaded = "false";
  };

  const initializeLazySourceSelect = (select) => {
    if (select.dataset.lazySourceInitialized === "true") {
      return;
    }
    select.dataset.lazySourceInitialized = "true";
    select.addEventListener("pointerdown", () => hydrateSourceOptions(select));
    select.addEventListener("focus", () => hydrateSourceOptions(select));
    select.addEventListener("blur", () => releaseSourceOptions(select));
  };

  for (const select of document.querySelectorAll(
    "select[data-lazy-source-column]"
  )) {
    initializeLazySourceSelect(select);
  }

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
      saved.states.map((state) => [state.name, state])
    );
    for (const control of row.querySelectorAll('[name^="relation_"]')) {
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

  const mappingForm = document.querySelector("[data-mapping-form]");
  if (mappingForm) {
    const saveStatus = mappingForm.querySelector(
      "[data-mapping-save-status]"
    );
    const confirmMapping = mappingForm.querySelector("[data-confirm-mapping]");
    let dirty = false;
    let submitting = false;

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

    const navigateToMappingResult = (redirectUrl) => {
      const target = new URL(
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

    const saveRecovery = window.impodoMappingSaveRecovery.create({
      mappingForm,
      saveStatus,
      updateMappingVersionFields,
      navigateToMappingResult,
      setDirty: (value) => {
        dirty = value;
      },
    });

    const stopSubmitting = (failureMessage, statusMessage) => {
      if (!submitting) {
        return;
      }
      submitting = false;
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
        saveStatus.textContent =
          `Saved ${savedDate.toLocaleString()}. Check matches when ready.`;
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
      if (saveStatus) {
        saveStatus.textContent = "Unsaved changes.";
        saveStatus.classList.add("unsaved");
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
      if (action === "submit" && dirty) {
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
      });
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
        saveRecovery.applyMutationOutcome(payload, operation);
      } catch (_error) {
        await saveRecovery.resolveMutationOutcome(operation);
      } finally {
        submitting = false;
        mappingForm.removeAttribute("aria-busy");
      }
    });

    const valueMatchDialog = document.querySelector(
      "[data-value-match-dialog]"
    );
    const valueMatchTitle = valueMatchDialog?.querySelector(
      "[data-value-match-title]"
    );
    const valueMatchError = valueMatchDialog?.querySelector(
      "[data-value-match-error]"
    );
    const valueMatchStatus = valueMatchDialog?.querySelector(
      "[data-value-match-status]"
    );
    const valueMatchTableWrap = valueMatchDialog?.querySelector(
      "[data-value-match-table-wrap]"
    );
    const valueMatchFreshness = valueMatchDialog?.querySelector(
      "[data-value-match-freshness]"
    );
    const valueMatchFreshnessText = valueMatchDialog?.querySelector(
      "[data-value-match-freshness-text]"
    );
    const refreshValueMatch = valueMatchDialog?.querySelector(
      "[data-refresh-value-match]"
    );
    const valueMatchRows = valueMatchDialog?.querySelector(
      "[data-value-match-rows]"
    );
    const useValueMatches = valueMatchDialog?.querySelector(
      "[data-use-value-matches]"
    );
    let activeValueMatch = null;
    let valueMatchRequest = null;
    let readCredentialRetry = null;

    const parseValueMatches = (storage) => {
      try {
        const values = JSON.parse(storage.value || "[]");
        return new Map(
          values
            .filter(
              (item) =>
                item &&
                typeof item.source_value === "string" &&
                typeof item.target_value === "string"
            )
            .map((item) => [item.source_value, item.target_value])
        );
      } catch {
        return new Map();
      }
    };

    const showValueMatchError = (message) => {
      if (valueMatchError) {
        valueMatchError.textContent = message;
        valueMatchError.hidden = false;
      }
      if (valueMatchStatus) {
        valueMatchStatus.textContent = "";
      }
      if (valueMatchTableWrap) {
        valueMatchTableWrap.hidden = true;
      }
      if (useValueMatches) {
        useValueMatches.disabled = true;
      }
      if (activeValueMatch?.kind === "relationship" && valueMatchFreshness) {
        valueMatchFreshness.hidden = false;
        if (valueMatchFreshnessText) {
          valueMatchFreshnessText.textContent =
            "Impodo could not update the saved Odoo values.";
        }
        if (refreshValueMatch) {
          refreshValueMatch.textContent = "Try again";
        }
      }
    };

    const resetValueMatchDialog = () => {
      if (valueMatchError) {
        valueMatchError.textContent = "";
        valueMatchError.hidden = true;
      }
      if (valueMatchRows) {
        valueMatchRows.replaceChildren();
      }
      if (valueMatchTableWrap) {
        valueMatchTableWrap.hidden = true;
      }
      if (valueMatchStatus) {
        valueMatchStatus.textContent = "Loading source and Odoo choices...";
      }
      if (useValueMatches) {
        useValueMatches.disabled = true;
      }
      if (valueMatchFreshness) {
        valueMatchFreshness.hidden = true;
      }
      if (valueMatchFreshnessText) {
        valueMatchFreshnessText.textContent = "";
      }
      if (refreshValueMatch) {
        refreshValueMatch.textContent = "Refresh Odoo values";
      }
    };

    const renderValueMatchRows = (payload) => {
      if (!valueMatchRows || !activeValueMatch) {
        return;
      }
      const sourceChoices = Array.isArray(payload.source_choices)
        ? payload.source_choices
        : [];
      const targetChoices = Array.isArray(payload.target_choices)
        ? payload.target_choices
        : [];
      const targetValues = new Set(
        targetChoices.map((choice) => String(choice.value))
      );
      const existing =
        activeValueMatch.pendingMatches ||
        parseValueMatches(activeValueMatch.storage);
      let suggested = 0;
      let ambiguousCount = 0;

      const targetChoiceLabel = (choice) => {
        const value = String(choice.value);
        const label = String(choice.label || choice.value);
        return label === value ? value : `${label} — ${value}`;
      };

      const updateValueMatchProgress = () => {
        const matched = Array.from(
          valueMatchRows.querySelectorAll("select[data-source-value]")
        ).filter((select) => select.value).length;
        const remaining = Math.max(0, sourceChoices.length - matched);
        if (valueMatchStatus) {
          const progress = remaining
            ? `${matched} matched. ${remaining} populated source choice(s) still need an Odoo choice.`
            : `All ${sourceChoices.length} populated source choice(s) are matched.`;
          const details = [
            `${suggested} exact code match(es) suggested.`,
          ];
          if (ambiguousCount) {
            details.push(
              `${ambiguousCount} duplicate Odoo key value(s) were left out.`
            );
          }
          valueMatchStatus.textContent = `${progress} ${details.join(" ")}`;
        }
        if (useValueMatches) {
          useValueMatches.disabled = matched === 0 && existing.size === 0;
          useValueMatches.textContent =
            matched === 0 && existing.size
              ? "Clear saved matches"
              : remaining
                ? `Use ${matched} partial match(es)`
                : "Use complete matches";
        }
      };

      const hydrateTargetSelect = (select) => {
        if (select.dataset.choicesLoaded === "true") {
          return;
        }
        const selectedValue = select.value;
        for (const choice of targetChoices) {
          const value = String(choice.value);
          if (value === selectedValue) {
            continue;
          }
          const option = document.createElement("option");
          option.value = value;
          option.textContent = targetChoiceLabel(choice);
          select.append(option);
        }
        select.dataset.choicesLoaded = "true";
      };

      const releaseTargetSelect = (select) => {
        for (const option of Array.from(select.options)) {
          if (option.value && !option.selected) {
            option.remove();
          }
        }
        select.dataset.choicesLoaded = "false";
      };

      for (const sourceChoice of sourceChoices) {
        const sourceValue = String(sourceChoice.value);
        const row = document.createElement("tr");
        const sourceCell = document.createElement("td");
        const countCell = document.createElement("td");
        const targetCell = document.createElement("td");
        const select = document.createElement("select");
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Choose Odoo choice";
        select.append(placeholder);
        select.dataset.sourceValue = sourceValue;
        select.setAttribute("aria-label", `Odoo choice for ${sourceValue}`);

        const savedTarget = existing.get(sourceValue);
        const suggestedTarget = targetValues.has(sourceValue)
          ? sourceValue
          : "";
        const selectedTarget =
          savedTarget && targetValues.has(savedTarget)
            ? savedTarget
            : suggestedTarget;
        if (selectedTarget) {
          const choice = targetChoices.find(
            (item) => String(item.value) === selectedTarget
          );
          const selectedOption = document.createElement("option");
          selectedOption.value = selectedTarget;
          selectedOption.textContent = choice
            ? targetChoiceLabel(choice)
            : selectedTarget;
          selectedOption.selected = true;
          select.append(selectedOption);
          if (!savedTarget && suggestedTarget) {
            suggested += 1;
          }
        }
        select.addEventListener("pointerdown", () =>
          hydrateTargetSelect(select)
        );
        select.addEventListener("focus", () => hydrateTargetSelect(select));
        select.addEventListener("keydown", () => hydrateTargetSelect(select));
        select.addEventListener("change", () => {
          updateValueMatchProgress();
          window.queueMicrotask(() => releaseTargetSelect(select));
        });
        select.addEventListener("blur", () => releaseTargetSelect(select));

        sourceCell.textContent = sourceValue;
        countCell.textContent = String(sourceChoice.count);
        targetCell.append(select);
        row.append(sourceCell, countCell, targetCell);
        valueMatchRows.append(row);
      }

      const ambiguousValues = Array.isArray(payload.ambiguous_values)
        ? payload.ambiguous_values
        : [];
      ambiguousCount = ambiguousValues.length;
      if (valueMatchStatus) {
        const parts = [
          `${sourceChoices.length} source choice(s) found.`,
          `${suggested} exact match(es) suggested.`,
        ];
        if (ambiguousValues.length) {
          parts.push(
            `${ambiguousValues.length} duplicate Odoo key value(s) were left out.`
          );
        }
        valueMatchStatus.textContent = parts.join(" ");
      }
      if (valueMatchTableWrap) {
        valueMatchTableWrap.hidden = false;
      }
      if (useValueMatches) {
        useValueMatches.disabled = sourceChoices.length === 0;
      }
      activeValueMatch.sourceCount = sourceChoices.length;
      if (
        activeValueMatch.kind === "relationship" &&
        payload.target_checked_at &&
        valueMatchFreshness
      ) {
        const checkedAt = new Date(payload.target_checked_at);
        const checkedLabel = Number.isNaN(checkedAt.getTime())
          ? "recently"
          : new Intl.DateTimeFormat(undefined, {
              dateStyle: "medium",
              timeStyle: "short",
            }).format(checkedAt);
        valueMatchFreshness.hidden = false;
        if (valueMatchFreshnessText) {
          valueMatchFreshnessText.textContent = payload.target_choices_reused
            ? `Using saved Odoo values checked ${checkedLabel}.`
            : `Odoo values checked and saved ${checkedLabel}.`;
        }
        if (refreshValueMatch) {
          refreshValueMatch.textContent = "Refresh Odoo values";
        }
      }
      updateValueMatchProgress();
    };

    const openValueMatch = async (trigger, { refresh = false } = {}) => {
      if (!valueMatchDialog) {
        return;
      }
      const pendingMatches =
        refresh && activeValueMatch && valueMatchRows
          ? new Map(
              Array.from(
                valueMatchRows.querySelectorAll("select[data-source-value]")
              )
                .filter((select) => select.value)
                .map((select) => [
                  select.dataset.sourceValue || "",
                  select.value,
                ])
            )
          : null;
      resetValueMatchDialog();
      valueMatchRequest?.abort();
      const kind = trigger.dataset.valueMatchKind || "";
      const row = trigger.closest(
        kind === "scalar"
          ? "[data-scalar-mapping-row]"
          : "[data-relation-mapping-row]"
      );
      const storage = row?.querySelector("[data-value-match-storage]");
      activeValueMatch = null;
      if (!(storage instanceof HTMLInputElement) || !row) {
        return;
      }
      if (valueMatchTitle) {
        valueMatchTitle.textContent = `Match source choices to ${
          trigger.dataset.targetLabel || "Odoo"
        }`;
      }
      valueMatchDialog.showModal();

      let sourceColumnKey = "";
      let businessKeyId = "";
      if (kind === "scalar") {
        const provider = row.querySelector("[data-value-source]")?.value;
        sourceColumnKey =
          row.querySelector('[name^="scalar_source_"]')?.value || "";
        if (!["source", "source_with_fallback"].includes(provider)) {
          showValueMatchError("Choose Source value first.");
          return;
        }
      } else {
        const origin = row.querySelector('[name^="relation_origin_"]')?.value;
        const source = row.querySelector('[name^="relation_source_"]');
        const selectedSources = source
          ? Array.from(source.selectedOptions, (option) => option.value)
          : [];
        sourceColumnKey = selectedSources[0] || "";
        businessKeyId =
          row.querySelector('[name^="relation_key_"]')?.value || "";
        if (!["target_catalog", "target_then_dataset"].includes(origin)) {
          showValueMatchError(
            "Choose an option that checks existing Odoo records before matching these choices."
          );
          return;
        }
        if (selectedSources.length !== 1) {
          showValueMatchError("Choose exactly one source column first.");
          return;
        }
        if (!businessKeyId) {
          showValueMatchError(
            "Choose how the related Odoo record is identified first."
          );
          return;
        }
      }
      if (!sourceColumnKey) {
        showValueMatchError("Choose one source column first.");
        return;
      }

      activeValueMatch = {
        trigger,
        storage,
        sourceCount: 0,
        kind,
        pendingMatches,
      };
      const csrfToken =
        mappingForm.querySelector('input[name="csrf_token"]')?.value || "";
      const body = new FormData();
      body.set("csrf_token", csrfToken);
      body.set("kind", kind);
      body.set("dataset_id", trigger.dataset.datasetId || "");
      body.set("source_column_key", sourceColumnKey);
      body.set("target_model", trigger.dataset.targetModel || "");
      body.set("target_field", trigger.dataset.targetField || "");
      body.set("business_key_id", businessKeyId);
      body.set("refresh", refresh ? "1" : "0");
      const activeRequest = new AbortController();
      valueMatchRequest = activeRequest;
      try {
        const response = await fetch(
          valueMatchDialog.dataset.valueMatchEndpoint || "",
          {
            method: "POST",
            headers: { Accept: "application/json" },
            body,
            signal: activeRequest.signal,
          }
        );
        let payload = {};
        try {
          payload = await response.json();
        } catch (_error) {
          payload = {};
        }
        if (!response.ok) {
          if (
            payload.read_credential_required === true &&
            typeof window.impodoOpenReadCredentialDialog === "function"
          ) {
            readCredentialRetry = { trigger, refresh };
            valueMatchDialog.close();
            window.impodoOpenReadCredentialDialog({
              message:
                payload.detail ||
                "Enter a read-only Odoo key to load these choices.",
              resume: "stay",
              trigger,
            });
            return;
          }
          throw new Error(
            payload.detail ||
              "The Odoo choices could not be loaded. Check the connection and try again."
          );
        }
        renderValueMatchRows(payload);
      } catch (error) {
        if (error?.name === "AbortError") {
          return;
        }
        showValueMatchError(
          error instanceof Error
            ? error.message
            : "The choices could not be loaded."
        );
      } finally {
        if (valueMatchRequest === activeRequest) {
          valueMatchRequest = null;
        }
      }
    };

    document.addEventListener("impodo:read-credential-saved", (event) => {
      if (!readCredentialRetry) {
        return;
      }
      event.preventDefault();
      const retry = readCredentialRetry;
      readCredentialRetry = null;
      window.requestAnimationFrame(() => {
        void openValueMatch(retry.trigger, { refresh: retry.refresh });
      });
    });

    mappingForm.addEventListener("click", (event) => {
      const trigger = event.target.closest?.("[data-open-value-match]");
      if (!trigger || !mappingForm.contains(trigger)) {
        return;
      }
      void openValueMatch(trigger);
    });

    refreshValueMatch?.addEventListener("click", () => {
      const trigger = activeValueMatch?.trigger;
      if (!trigger) {
        return;
      }
      void openValueMatch(trigger, { refresh: true });
    });

    useValueMatches?.addEventListener("click", () => {
      if (!activeValueMatch || !valueMatchRows) {
        return;
      }
      const mappings = Array.from(
        valueMatchRows.querySelectorAll("select[data-source-value]")
      )
        .filter((select) => select.value)
        .map((select) => ({
          source_value: select.dataset.sourceValue || "",
          target_value: select.value,
        }));
      activeValueMatch.storage.value = JSON.stringify(mappings);
      const policy = activeValueMatch.storage
        .closest("[data-scalar-mapping-row], [data-relation-mapping-row]")
        ?.querySelector("[data-categorical-policy]");
      if (policy instanceof HTMLSelectElement && mappings.length) {
        policy.value =
          activeValueMatch.trigger.dataset.valueMatchKind === "scalar"
            ? "EXPLICIT_VALUE_MATCH"
            : "EXPLICIT_KEY_MATCH";
        policy.dispatchEvent(new Event("change", { bubbles: true }));
      }
      activeValueMatch.storage.dispatchEvent(
        new Event("input", { bubbles: true })
      );
      const summary = activeValueMatch.storage
        .closest("[data-scalar-mapping-row], [data-relation-mapping-row]")
        ?.querySelector("[data-value-match-summary]");
      if (summary) {
        const remaining = activeValueMatch.sourceCount - mappings.length;
        summary.textContent = remaining
          ? `${mappings.length} matched · ${remaining} still need review`
          : `All ${mappings.length} populated source choice(s) matched`;
      }
      const trigger = activeValueMatch.trigger;
      activeValueMatch = null;
      valueMatchDialog?.close();
      trigger.focus();
    });

    for (const close of valueMatchDialog?.querySelectorAll(
      "[data-close-value-match]"
    ) || []) {
      close.addEventListener("click", () => valueMatchDialog?.close());
    }

    valueMatchDialog?.addEventListener("close", () => {
      valueMatchRequest?.abort();
      valueMatchRequest = null;
      activeValueMatch = null;
    });

    mappingForm.addEventListener("change", (event) => {
      const row = event.target?.closest?.(
        "[data-scalar-mapping-row], [data-relation-mapping-row]"
      );
      const storage = row?.querySelector("[data-value-match-storage]");
      if (!(storage instanceof HTMLInputElement)) {
        return;
      }
      const name = event.target?.name || "";
      const changesValueIdentity =
        name.startsWith("scalar_source_") ||
        name.startsWith("scalar_value_source_") ||
        name.startsWith("relation_source_") ||
        name.startsWith("relation_origin_") ||
        name.startsWith("relation_dataset_") ||
        name.startsWith("relation_key_");
      if (!changesValueIdentity || storage.value === "[]") {
        return;
      }
      storage.value = "[]";
      const summary = row.querySelector("[data-value-match-summary]");
      if (summary) {
        summary.textContent = "Use when source and Odoo choices differ";
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
        const input = component.querySelector("[data-constant-component-value]");
        const label = component.querySelector("[data-constant-component-label]");
        if (input) input.disabled = !active;
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

    provider?.addEventListener("change", syncProvider);
    businessKey?.addEventListener("change", syncComponents);
    search?.addEventListener("input", renderChoices);
    choice?.addEventListener("change", () => {
      const first = row.querySelector("[data-constant-component-value]");
      if (first && choice.value) {
        first.value = choice.value;
        first.dispatchEvent(new Event("input", { bubbles: true }));
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
              ? `${loadedChoices.length.toLocaleString()} unambiguous existing record(s) available.`
              : "No unambiguous existing record matches this rule.";
          }
        } catch (error) {
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
    syncComponents();
  };

  for (const row of document.querySelectorAll("[data-relation-mapping-row]")) {
    initializeRelationRow(row);
  }

  window.impodoMappingEditor = {
    initializeLazySourceSelect,
    initializeRelationRow,
    mappingForm,
    restoreRelationRow,
    restoreScalarRow,
  };
});
