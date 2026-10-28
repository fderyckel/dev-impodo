"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const revealTarget = (id) => {
    const target = document.getElementById(id);
    if (!target) {
      return;
    }
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) {
        parent.open = true;
      }
    }
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  };

  const revealCurrentTarget = () => {
    let id = "";
    if (window.location.hash.length > 1) {
      try {
        id = decodeURIComponent(window.location.hash.slice(1));
      } catch (_error) {
        id = "";
      }
    }
    const preview = document.querySelector("[data-derived-entity-preview]");
    revealTarget(id || preview?.id);
  };

  for (const trigger of document.querySelectorAll("[data-derived-entity-target]")) {
    trigger.addEventListener("click", () => {
      revealTarget(trigger.dataset.derivedEntityTarget);
    });
  }

  for (const form of document.querySelectorAll("[data-exact-join-form]")) {
    const inputs = {
      left: form.querySelector('[data-exact-join-input="left"]'),
      right: form.querySelector('[data-exact-join-input="right"]'),
    };
    const grain = form.querySelector("#exact-join-grain");

    const syncExactJoin = () => {
      const selectedIds = new Set([inputs.left?.value, inputs.right?.value].filter(Boolean));
      const duplicate = Boolean(
        inputs.left?.value && inputs.left.value === inputs.right?.value
      );
      inputs.right?.setCustomValidity(
        duplicate ? "Choose two different source datasets." : ""
      );

      for (const side of ["left", "right"]) {
        const datasetId = inputs[side]?.value || "";
        for (const select of form.querySelectorAll(`[data-exact-key-side="${side}"]`)) {
          for (const option of select.querySelectorAll("option[data-dataset-id]")) {
            const available = option.dataset.datasetId === datasetId;
            option.hidden = !available;
            option.disabled = !available;
          }
          if (
            select.selectedOptions[0]?.dataset.datasetId &&
            select.selectedOptions[0].dataset.datasetId !== datasetId
          ) {
            select.value = "";
          }
        }

        const selectedOption = inputs[side]?.selectedOptions[0];
        const useChoice = form.querySelector(`[data-exact-use-side="${side}"]`);
        if (useChoice) {
          const physical = selectedOption?.dataset.physical === "true";
          useChoice.hidden = !physical;
          const useSelect = useChoice.querySelector("select");
          if (useSelect) {
            useSelect.disabled = !physical;
          }
        }
      }

      for (const group of form.querySelectorAll("[data-exact-projection-dataset]")) {
        const visible = selectedIds.has(group.dataset.exactProjectionDataset);
        group.hidden = !visible;
        if (!visible) {
          for (const checkbox of group.querySelectorAll('input[name="projection"]')) {
            checkbox.checked = false;
          }
        }
      }

      if (grain && inputs.left?.selectedOptions[0]?.value) {
        grain.textContent = `One output row will represent one ${inputs.left.selectedOptions[0].textContent.trim()} row.`;
      }
    };

    inputs.left?.addEventListener("change", syncExactJoin);
    inputs.right?.addEventListener("change", syncExactJoin);
    syncExactJoin();
  }

  for (const form of document.querySelectorAll("[data-exact-join-group-form]")) {
    const maximumFields = 200;
    const primary = form.querySelector("[data-group-primary-dataset]");
    const additions = Array.from(form.querySelectorAll("[data-group-addition]"));
    const primaryKeys = Array.from(form.querySelectorAll("[data-group-primary-key]"));
    const additionKeys = Array.from(form.querySelectorAll("[data-group-addition-key]"));
    const rows = Array.from(form.querySelectorAll("[data-group-field-row]"));
    const search = form.querySelector("[data-group-field-search]");
    const filter = form.querySelector("[data-group-field-filter]");
    const count = form.querySelector("[data-group-field-count]");
    const limitError = form.querySelector("[data-group-field-limit-error]");
    const submit = form.querySelector('button[type="submit"]');
    const manuallySelected = new WeakSet();
    const manuallyNamed = new WeakSet();
    let initialized = false;

    const token = (value) => {
      const separator = value.indexOf("|");
      return separator < 0 ? ["", value] : [value.slice(0, separator), value.slice(separator + 1)];
    };

    const selectedParticipants = () => {
      const result = new Set(primary?.value ? [primary.value] : []);
      for (const addition of additions) {
        const isPrimary = addition.dataset.datasetId === primary?.value;
        addition.disabled = isPrimary;
        if (isPrimary) {
          addition.checked = false;
        } else if (addition.checked) {
          result.add(addition.dataset.datasetId);
        }
      }
      return result;
    };

    const applyFieldFilter = () => {
      const query = (search?.value || "").trim().toLocaleLowerCase();
      const category = filter?.value || "all";
      for (const row of rows) {
        const checkbox = row.querySelector("[data-group-field-checkbox]");
        const output = row.querySelector("[data-group-output-name]");
        const matchesSearch = !query || [
          row.dataset.datasetName,
          row.dataset.sourceName,
          output?.value,
        ].some((value) => (value || "").toLocaleLowerCase().includes(query));
        const matchesCategory = category === "all"
          || row.dataset.groupFieldCategory === category
          || (category === "selected" && checkbox?.checked);
        row.hidden = row.dataset.participates !== "true" || !matchesSearch || !matchesCategory;
      }
    };

    const syncGroupFields = ({ resetRecommendations = false } = {}) => {
      const participants = selectedParticipants();
      const primaryId = primary?.value || "";
      const activeAdditions = additions.filter((item) => item.checked);
      const labelCounts = new Map();
      for (const row of rows) {
        if (!participants.has(row.dataset.datasetId)) {
          continue;
        }
        const label = (row.dataset.sourceName || "").trim().toLocaleLowerCase();
        labelCounts.set(label, (labelCounts.get(label) || 0) + 1);
      }

      const primaryKeyTokens = new Set(
        primaryKeys.map((item) => item.value).filter(Boolean),
      );
      const addedKeyTokens = new Set(
        additionKeys
          .filter((item) => participants.has(item.dataset.datasetId))
          .map((item) => item.value)
          .filter(Boolean),
      );
      const selectedTokens = new Set();

      for (const row of rows) {
        const checkbox = row.querySelector("[data-group-field-checkbox]");
        const output = row.querySelector("[data-group-output-name]");
        const reason = row.querySelector("[data-group-field-reason]");
        if (!checkbox || !output) {
          continue;
        }
        const datasetId = row.dataset.datasetId || "";
        const fieldToken = `${datasetId}|${row.dataset.columnKey || ""}`;
        const participates = participants.has(datasetId);
        const isPrimary = datasetId === primaryId;
        const isEmpty = row.dataset.candidateType === "empty";
        const collision = (labelCounts.get(
          (row.dataset.sourceName || "").trim().toLocaleLowerCase(),
        ) || 0) > 1;
        const isAddedKey = !isPrimary && addedKeyTokens.has(fieldToken);
        const recommended = participates && !isEmpty && (
          isPrimary || (!isAddedKey && !collision)
        );

        row.dataset.participates = String(participates);
        if (isEmpty) {
          row.dataset.groupFieldCategory = "empty";
          reason.textContent = "Empty in the accepted source; excluded by default.";
        } else if (!isPrimary && collision) {
          row.dataset.groupFieldCategory = "decision";
          reason.textContent = "Same label in another table; choose explicitly.";
        } else if (recommended) {
          row.dataset.groupFieldCategory = "recommended";
          reason.textContent = isPrimary && collision
            ? "Primary version kept; other versions need a decision."
            : "Recommended output field.";
        } else {
          row.dataset.groupFieldCategory = "excluded";
          reason.textContent = isAddedKey
            ? "Added-table join key; the primary version is kept."
            : "Not part of the current combination.";
        }

        checkbox.disabled = !participates;
        if (!participates) {
          checkbox.checked = false;
        } else if (
          initialized
          && (resetRecommendations || !manuallySelected.has(checkbox))
        ) {
          checkbox.checked = recommended;
        }
        if (resetRecommendations) {
          manuallySelected.delete(checkbox);
        }

        if (resetRecommendations || !manuallyNamed.has(output)) {
          output.value = !isPrimary && collision
            ? output.dataset.qualifiedName
            : output.dataset.defaultName;
        }
        if (resetRecommendations) {
          manuallyNamed.delete(output);
        }
        if (participates && checkbox.checked) {
          selectedTokens.add(fieldToken);
        }
      }

      for (const group of form.querySelectorAll("[data-group-projection-dataset]")) {
        group.hidden = !participants.has(group.dataset.groupProjectionDataset);
      }
      for (const key of additionKeys) {
        key.disabled = !activeAdditions.some(
          (item) => item.dataset.datasetId === key.dataset.datasetId,
        );
      }

      const hiddenKeyCount = activeAdditions.length > 1
        ? Array.from(primaryKeyTokens).filter((item) => !selectedTokens.has(item)).length
        : 0;
      const selectedCount = selectedTokens.size;
      const effectiveCount = selectedCount + hiddenKeyCount;
      if (count) {
        count.textContent = `${selectedCount} / ${maximumFields} output fields selected${
          hiddenKeyCount ? `; ${hiddenKeyCount} internal key field${hiddenKeyCount === 1 ? "" : "s"}` : ""
        }`;
      }
      const overLimit = effectiveCount > maximumFields;
      if (limitError) {
        limitError.hidden = !overLimit;
      }
      if (submit) {
        submit.disabled = overLimit;
      }
      initialized = true;
      applyFieldFilter();
    };

    for (const checkbox of form.querySelectorAll("[data-group-field-checkbox]")) {
      checkbox.addEventListener("change", () => {
        manuallySelected.add(checkbox);
        syncGroupFields();
      });
    }
    for (const output of form.querySelectorAll("[data-group-output-name]")) {
      output.addEventListener("input", () => manuallyNamed.add(output));
    }
    primary?.addEventListener("change", () => syncGroupFields());
    for (const input of [...additions, ...primaryKeys, ...additionKeys]) {
      input.addEventListener("change", () => syncGroupFields());
    }
    search?.addEventListener("input", applyFieldFilter);
    filter?.addEventListener("change", applyFieldFilter);
    form.querySelector("[data-group-use-recommended]")?.addEventListener("click", () => {
      syncGroupFields({ resetRecommendations: true });
    });
    form.querySelector("[data-group-select-visible]")?.addEventListener("click", () => {
      for (const row of rows) {
        if (row.hidden) {
          continue;
        }
        const checkbox = row.querySelector("[data-group-field-checkbox]");
        if (checkbox && !checkbox.disabled) {
          checkbox.checked = true;
          manuallySelected.add(checkbox);
        }
      }
      syncGroupFields();
    });
    form.querySelector("[data-group-clear-visible]")?.addEventListener("click", () => {
      for (const row of rows) {
        if (row.hidden) {
          continue;
        }
        const checkbox = row.querySelector("[data-group-field-checkbox]");
        if (checkbox && !checkbox.disabled) {
          checkbox.checked = false;
          manuallySelected.add(checkbox);
        }
      }
      syncGroupFields();
    });
    syncGroupFields();
    if (form.hasAttribute("data-group-server-error")) {
      const alert = document.querySelector('main [role="alert"]');
      if (alert) {
        alert.setAttribute("tabindex", "-1");
        alert.focus();
      }
    }
  }

  window.addEventListener("hashchange", revealCurrentTarget);
  window.addEventListener("pageshow", revealCurrentTarget);
  revealCurrentTarget();
});
