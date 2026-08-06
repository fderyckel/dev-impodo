"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const sidebar = document.querySelector("#app-sidebar");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarToggleLabel = document.querySelector(
    "[data-sidebar-toggle-label]"
  );
  const mobileNavToggle = document.querySelector("[data-mobile-nav-toggle]");
  const sidebarScrim = document.querySelector("[data-sidebar-scrim]");
  const mobileViewport = window.matchMedia("(max-width: 860px)");
  const sidebarStorageKey = "impodo.sidebar.collapsed";

  const storedSidebarState = () => {
    try {
      return window.localStorage.getItem(sidebarStorageKey) === "true";
    } catch {
      return false;
    }
  };

  const persistSidebarState = (collapsed) => {
    try {
      window.localStorage.setItem(sidebarStorageKey, String(collapsed));
    } catch {
      // The navigation still works when browser storage is unavailable.
    }
  };

  const setSidebarCollapsed = (collapsed, persist = true) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    sidebarToggle?.setAttribute("aria-expanded", String(!collapsed));
    if (sidebarToggle) {
      sidebarToggle.title = collapsed
        ? "Expand navigation"
        : "Collapse navigation";
    }
    if (sidebarToggleLabel) {
      sidebarToggleLabel.textContent = collapsed
        ? "Expand navigation"
        : "Collapse navigation";
    }
    if (persist) {
      persistSidebarState(collapsed);
    }
  };

  const setMobileNavigationOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    mobileNavToggle?.setAttribute("aria-expanded", String(open));
    sidebarToggle?.setAttribute("aria-expanded", String(open));
    if (sidebarToggle) {
      sidebarToggle.title = "Close navigation";
    }
    if (sidebarToggleLabel) {
      sidebarToggleLabel.textContent = "Close navigation";
    }
  };

  if (sidebar) {
    setSidebarCollapsed(storedSidebarState(), false);

    sidebarToggle?.addEventListener("click", () => {
      if (mobileViewport.matches) {
        setMobileNavigationOpen(false);
        mobileNavToggle?.focus();
        return;
      }
      setSidebarCollapsed(
        !document.body.classList.contains("sidebar-collapsed")
      );
    });

    mobileNavToggle?.addEventListener("click", () => {
      setMobileNavigationOpen(
        !document.body.classList.contains("sidebar-open")
      );
    });

    sidebarScrim?.addEventListener("click", () => {
      setMobileNavigationOpen(false);
      mobileNavToggle?.focus();
    });

    for (const link of sidebar.querySelectorAll("a")) {
      link.addEventListener("click", () => {
        if (mobileViewport.matches) {
          setMobileNavigationOpen(false);
        }
      });
    }

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        document.body.classList.contains("sidebar-open")
      ) {
        setMobileNavigationOpen(false);
        mobileNavToggle?.focus();
      }
    });

    mobileViewport.addEventListener("change", () => {
      setMobileNavigationOpen(false);
      if (!mobileViewport.matches) {
        setSidebarCollapsed(storedSidebarState(), false);
      }
    });
  }

  const projectDeleteDialog = document.querySelector(
    "[data-project-delete-dialog]"
  );
  const projectDeleteTitle = projectDeleteDialog?.querySelector(
    "#project-delete-title"
  );
  const projectDeleteConfirm = projectDeleteDialog?.querySelector(
    "[data-project-delete-confirm]"
  );
  let pendingProjectDeleteForm = null;

  for (const trigger of document.querySelectorAll(
    "[data-project-delete-trigger]"
  )) {
    trigger.addEventListener("click", () => {
      const form = trigger.closest("[data-project-delete-form]");
      if (!form || !projectDeleteDialog) {
        return;
      }
      pendingProjectDeleteForm = form;
      if (projectDeleteTitle) {
        const projectName = form.dataset.projectName || "this project";
        projectDeleteTitle.textContent = `Delete “${projectName}”?`;
      }
      projectDeleteDialog.showModal();
    });
  }

  projectDeleteConfirm?.addEventListener("click", () => {
    const form = pendingProjectDeleteForm;
    pendingProjectDeleteForm = null;
    projectDeleteDialog?.close();
    form?.requestSubmit();
  });

  projectDeleteDialog?.addEventListener("close", () => {
    pendingProjectDeleteForm = null;
  });

  const mappingPositionStorageKey =
    `impodo.mapping.position:${window.location.pathname}`;
  const rememberMappingPosition = () => {
    const active = document.activeElement;
    const activeRow = active?.closest?.("[data-target-field]");
    const horizontal = Array.from(
      document.querySelectorAll("[data-scalar-table-scroll][id]")
    ).map((element) => [element.id, element.scrollLeft]);
    try {
      window.sessionStorage.setItem(
        mappingPositionStorageKey,
        JSON.stringify({
          scrollY: window.scrollY,
          focusName: active?.name || "",
          focusValue: active?.value || "",
          targetField: activeRow?.dataset.targetField || "",
          targetOffset: activeRow?.getBoundingClientRect().top ?? null,
          horizontal,
        })
      );
    } catch {
      // Navigation remains usable when browser storage is unavailable.
    }
  };
  const restoreMappingPosition = () => {
    let stored = null;
    try {
      stored = JSON.parse(
        window.sessionStorage.getItem(mappingPositionStorageKey) || "null"
      );
      window.sessionStorage.removeItem(mappingPositionStorageKey);
    } catch {
      stored = null;
    }
    if (!stored || !document.querySelector("[data-mapping-form]")) {
      return;
    }
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const rows = Array.from(
          document.querySelectorAll("[data-target-field]")
        );
        const row = rows.find(
          (candidate) => candidate.dataset.targetField === stored.targetField
        );
        const targetTop =
          row && Number.isFinite(stored.targetOffset)
            ? window.scrollY +
              row.getBoundingClientRect().top -
              stored.targetOffset
            : stored.scrollY;
        window.scrollTo({
          top: Math.max(0, targetTop || 0),
          behavior: "instant",
        });
        for (const [id, scrollLeft] of stored.horizontal || []) {
          const element = document.getElementById(id);
          if (element) {
            element.scrollLeft = scrollLeft;
          }
        }
        const controls = Array.from(document.querySelectorAll("[name]"));
        const focusTarget = controls.find(
          (control) =>
            control.name === stored.focusName &&
            (!stored.focusValue || control.value === stored.focusValue)
        );
        focusTarget?.focus({ preventScroll: true });
      });
    });
  };

  const normalizationReview = document.querySelector(
    "[data-normalization-review]"
  );
  const normalizationPositionStorageKey = normalizationReview
    ? `impodo.normalization.position:${normalizationReview.dataset.normalizationPositionKey}`
    : "";
  const rememberNormalizationPosition = (form) => {
    if (!normalizationPositionStorageKey) {
      return;
    }
    const rows = Array.from(
      document.querySelectorAll("[data-normalization-group]")
    );
    const row = form.closest("[data-normalization-group]");
    const tableScroll = document.querySelector(
      "[data-normalization-table-scroll]"
    );
    try {
      window.sessionStorage.setItem(
        normalizationPositionStorageKey,
        JSON.stringify({
          scrollY: window.scrollY,
          groupId: row?.dataset.normalizationGroup || "",
          rowIndex: row ? rows.indexOf(row) : -1,
          rowOffset: row?.getBoundingClientRect().top ?? null,
          tableScrollLeft: tableScroll?.scrollLeft || 0,
        })
      );
    } catch {
      // The server-side anchor still keeps the decision table in view.
    }
  };
  const restoreNormalizationPosition = () => {
    if (!normalizationPositionStorageKey) {
      return;
    }
    let stored = null;
    try {
      stored = JSON.parse(
        window.sessionStorage.getItem(normalizationPositionStorageKey) ||
          "null"
      );
      window.sessionStorage.removeItem(normalizationPositionStorageKey);
    } catch {
      stored = null;
    }
    if (!stored) {
      return;
    }
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const rows = Array.from(
          document.querySelectorAll("[data-normalization-group]")
        );
        const sameRow = rows.find(
          (candidate) =>
            candidate.dataset.normalizationGroup === stored.groupId
        );
        const fallbackIndex = Math.min(
          Math.max(0, Number(stored.rowIndex) || 0),
          Math.max(0, rows.length - 1)
        );
        const row = sameRow || rows[fallbackIndex];
        const targetTop =
          row && Number.isFinite(stored.rowOffset)
            ? window.scrollY +
              row.getBoundingClientRect().top -
              stored.rowOffset
            : stored.scrollY;
        window.scrollTo({
          top: Math.max(0, targetTop || 0),
          behavior: "auto",
        });
        const tableScroll = document.querySelector(
          "[data-normalization-table-scroll]"
        );
        if (tableScroll) {
          tableScroll.scrollLeft = stored.tableScrollLeft || 0;
        }
      });
    });
  };
  for (const form of document.querySelectorAll(
    "[data-normalization-decision-form]"
  )) {
    form.addEventListener("submit", () => {
      rememberNormalizationPosition(form);
    });
  }

  const targetModels = Array.from(
    document.querySelectorAll("[data-target-model]")
  );
  for (const control of targetModels) {
    control.addEventListener("change", () => {
      const query = new URLSearchParams(window.location.search);
      for (const targetModel of targetModels) {
        query.set(targetModel.name, targetModel.value);
      }
      rememberMappingPosition();
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
    return {
      key: `${visibleTarget.name}\u0000${visibleTarget.value}`,
      visibleName: visibleTarget.name,
      targetField: visibleTarget.value,
      hasSource: Boolean(source?.selectedOptions.length),
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
    const saveError = mappingForm.querySelector("[data-mapping-save-error]");
    const confirmMapping = mappingForm.querySelector("[data-confirm-mapping]");
    let dirty = false;
    let submitting = false;

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
      const scalarRow = event.target.closest("[data-scalar-mapping-row]");
      if (scalarRow) {
        window.queueMicrotask(() => rememberScalarRow(scalarRow));
      }
      const relationRow = event.target.closest("[data-relation-mapping-row]");
      if (relationRow) {
        window.queueMicrotask(() => rememberRelationRow(relationRow));
      }
      if (saveStatus) {
        saveStatus.textContent = "Unsaved changes.";
        saveStatus.classList.add("unsaved");
      }
      if (confirmMapping) {
        confirmMapping.disabled = true;
        confirmMapping.title = "Check the latest changes before confirming.";
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
        const source = row.querySelector('select[name^="relation_source_"]');
        if (source && source.selectedOptions.length > 0) {
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
        if (!state.hasSource) {
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
      if (submitting) {
        return;
      }
      const action = event.submitter?.value || "";
      if (action === "submit" && dirty) {
        if (saveError) {
          saveError.textContent =
            "These changes have not been checked yet. Check matches before confirming.";
          saveError.hidden = false;
        }
        if (saveStatus) {
          saveStatus.textContent = "Unsaved changes need checking.";
          saveStatus.classList.add("unsaved");
        }
        return;
      }
      submitting = true;
      mappingForm.setAttribute("aria-busy", "true");
      if (saveError) {
        saveError.hidden = true;
        saveError.textContent = "";
      }
      if (saveStatus) {
        saveStatus.textContent =
          action === "save_progress"
            ? "Saving progress..."
            : action === "submit"
              ? "Confirming checked matches..."
              : "Checking matches...";
        saveStatus.classList.remove("unsaved");
      }
      let responseReceived = false;
      try {
        const csrfToken = mappingForm.querySelector(
          'input[name="csrf_token"]'
        )?.value;
        const mappingSaveUrl = mappingForm.getAttribute("action");
        if (!mappingSaveUrl) {
          throw new Error("The mapping save URL is missing.");
        }
        const response = await fetch(mappingSaveUrl, {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken || "",
          },
          body: JSON.stringify({
            entries: sparseMappingEntries(event.submitter),
          }),
        });
        responseReceived = true;
        let payload = {};
        try {
          payload = await response.json();
        } catch (_error) {
          payload = {};
        }
        if (!response.ok) {
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
            workingVersion.value =
              payload.expected_working_draft_version ?? "";
          }
          if (
            parentVersion &&
            Object.hasOwn(payload, "expected_parent_version")
          ) {
            parentVersion.value = payload.expected_parent_version ?? "";
          }
          throw new Error(
            payload.detail ||
              "The matches could not be saved. Please try again."
          );
        }
        dirty = false;
        if (saveStatus) {
          saveStatus.textContent = payload.message || "Matches saved.";
        }
        rememberMappingPosition();
        window.location.assign(payload.redirect_url || window.location.pathname);
      } catch (error) {
        submitting = false;
        mappingForm.removeAttribute("aria-busy");
        const message =
          !responseReceived && error instanceof TypeError
            ? "This browser tab can no longer reach its Impodo session. Reopen Impodo in the newly opened tab; keep this tab open while copying any unsaved choices."
            : error instanceof Error
            ? error.message
            : "The matches could not be saved.";
        if (saveError) {
          saveError.textContent =
            action === "submit"
              ? `${message} Your checked matches are unchanged.`
              : `${message} Your unsaved changes are still on this page.`;
          saveError.hidden = false;
        }
        if (saveStatus) {
          if (action === "submit") {
            saveStatus.textContent =
              "Confirmation was not completed. Checked matches are unchanged.";
          } else {
            dirty = true;
            saveStatus.textContent =
              "Save failed. Unsaved changes are retained.";
            saveStatus.classList.add("unsaved");
          }
        }
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
    const valueMatchRows = valueMatchDialog?.querySelector(
      "[data-value-match-rows]"
    );
    const useValueMatches = valueMatchDialog?.querySelector(
      "[data-use-value-matches]"
    );
    let activeValueMatch = null;
    let valueMatchRequest = null;

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
      const existing = parseValueMatches(activeValueMatch.storage);
      let suggested = 0;

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
          option.textContent = String(choice.label || choice.value);
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
          selectedOption.textContent = String(
            choice?.label || choice?.value || selectedTarget
          );
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
        select.addEventListener("change", () =>
          window.queueMicrotask(() => releaseTargetSelect(select))
        );
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
    };

    for (const trigger of mappingForm.querySelectorAll(
      "[data-open-value-match]"
    )) {
      trigger.addEventListener("click", async () => {
        if (!valueMatchDialog) {
          return;
        }
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
          if (origin !== "target_catalog") {
            showValueMatchError(
              "Choose Existing Odoo records before matching these choices."
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
        valueMatchRequest = new AbortController();
        try {
          const response = await fetch(
            valueMatchDialog.dataset.valueMatchEndpoint || "",
            {
              method: "POST",
              headers: { Accept: "application/json" },
              body,
              signal: valueMatchRequest.signal,
            }
          );
          let payload = {};
          try {
            payload = await response.json();
          } catch (_error) {
            payload = {};
          }
          if (!response.ok) {
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
        }
      });
    }

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
      activeValueMatch.storage.dispatchEvent(
        new Event("input", { bubbles: true })
      );
      const summary = activeValueMatch.storage
        .closest("[data-scalar-mapping-row], [data-relation-mapping-row]")
        ?.querySelector("[data-value-match-summary]");
      if (summary) {
        summary.textContent = `${mappings.length} of ${activeValueMatch.sourceCount} source choice(s) matched`;
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

    for (const storage of mappingForm.querySelectorAll(
      "[data-value-match-storage]"
    )) {
      const row = storage.closest(
        "[data-scalar-mapping-row], [data-relation-mapping-row]"
      );
      row?.addEventListener("change", (event) => {
        const name = event.target?.name || "";
        const changesValueIdentity =
          name.startsWith("scalar_source_") ||
          name.startsWith("scalar_value_source_") ||
          name.startsWith("relation_source_") ||
          name.startsWith("relation_origin_") ||
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
    }

    window.addEventListener("beforeunload", (event) => {
      if (!dirty || submitting) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    });

  }

  const modelPicker = document.querySelector("[data-model-picker]");
  if (modelPicker) {
    const search = modelPicker.querySelector("[data-model-search]");
    const showAll = modelPicker.querySelector("[data-show-all-models]");
    const count = modelPicker.querySelector("[data-model-count]");
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
    updateModelChoices();
  }

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
    const syncSimpleChoice = (select, input) => {
      if (!select || !input) {
        return;
      }
      input.value = select.value;
      showDraft();
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
      showDraft();
    });
    combinedScope?.addEventListener("input", () => {
      const scopeFields = values(combinedScope.value);
      if (primaryScope) {
        primaryScope.value = scopeFields.length === 1 ? scopeFields[0] : "";
      }
      showDraft();
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
      showDraft();
    });
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

  const targetForm = document.querySelector("[data-target-form]");
  targetForm?.addEventListener("submit", (event) => {
    const button = event.submitter;
    if (!button?.matches("[data-test-connection-button]")) {
      return;
    }
    const action = document.createElement("input");
    action.type = "hidden";
    action.name = "action";
    action.value = "test";
    targetForm.append(action);
    targetForm.setAttribute("aria-busy", "true");
    button.disabled = true;
    button.textContent = "Testing connection...";
  });

  for (const form of document.querySelectorAll("[data-preflight-compare]")) {
    form.addEventListener("submit", (event) => {
      if (form.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
      }
      form.setAttribute("aria-busy", "true");
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-preflight-comparison-status]");
      if (button) {
        button.disabled = true;
        button.textContent = "Comparing with Odoo...";
      }
      if (status) {
        status.hidden = false;
      }
    });
  }

  const localStackDialog = document.querySelector("[data-local-stack-dialog]");
  const localStackEntry = document.querySelector("[data-local-stack-entry]");
  const apiKeyEntries = Array.from(
    document.querySelectorAll("[data-api-key-entry]")
  );
  const connectionModes = Array.from(
    document.querySelectorAll('input[name="odoo_connection_mode"]')
  );
  const updateLocalStackVisibility = () => {
    const selected = connectionModes.find((control) => control.checked);
    const localMode = !selected || selected.value === "LOCAL";
    if (localStackEntry) {
      localStackEntry.hidden = !localMode;
    }
    for (const entry of apiKeyEntries) {
      entry.hidden = localMode;
    }
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

  const transformationImpactForm = document.querySelector(
    "[data-transformation-impact-prepare]"
  );
  transformationImpactForm?.addEventListener("submit", (event) => {
    if (transformationImpactForm.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }
    const button = transformationImpactForm.querySelector(
      "[data-transformation-impact-button]"
    );
    const status = document.querySelector(
      "[data-transformation-impact-status]"
    );
    transformationImpactForm.setAttribute("aria-busy", "true");
    if (button) {
      button.disabled = true;
      button.textContent = "Preparing the comparison…";
    }
    if (status) {
      status.hidden = false;
    }
  });

  const displayPreviewValue = (value) => {
    if (value === null || value === undefined) {
      return "(empty)";
    }
    if (value === "") {
      return '""';
    }
    return String(value);
  };

  const transformPreviewValue = (row, raw, missing) => {
    const trim = row.querySelector("[data-transform-trim]")?.checked;
    const collapse = row.querySelector("[data-transform-collapse]")?.checked;
    const emptyAsNull = row.querySelector(
      "[data-transform-empty-null]"
    )?.checked;
    const caseMode =
      row.querySelector("[data-transform-case]")?.value || "preserve";
    let value = missing ? null : String(raw ?? "");
    if (value !== null && trim) {
      value = value.trim();
    }
    if (value !== null && collapse) {
      value = value.replace(/\s+/g, " ");
    }
    const searchValue = row.querySelector("[data-rule-search]")?.value || "";
    const replacementValue =
      row.querySelector("[data-rule-replacement]")?.value || "";
    const searchMode =
      row.querySelector("[data-rule-search-mode]")?.value || "literal";
    const replaceAll = row.querySelector("[data-rule-replace-all]")?.checked;
    if (value !== null && searchValue) {
      if (searchMode === "pattern") {
        throw new Error("Save to validate the advanced find pattern");
      }
      value = replaceAll
        ? value.split(searchValue).join(replacementValue)
        : value.replace(searchValue, replacementValue);
    }
    if (value !== null && caseMode === "uppercase") {
      value = value.toUpperCase();
    }
    if (value !== null && caseMode === "lowercase") {
      value = value.toLowerCase();
    }
    if (value !== null && caseMode === "sentence") {
      value = value.replace(/[A-Za-z]/, (character) => character.toUpperCase());
    }
    if (value !== null && caseMode === "title") {
      value = value.replace(
        /\b\p{L}+/gu,
        (word) => word.charAt(0).toLocaleUpperCase() + word.slice(1).toLocaleLowerCase()
      );
    }
    if (value === "" && emptyAsNull) {
      return null;
    }
    return value;
  };

  const canonicalPreviewValue = (row, value) => {
    if (value === null) {
      return null;
    }
    const valueType = row.querySelector("[data-canonical-type]")?.value;
    if (valueType === "string") {
      return value;
    }
    if (valueType === "integer") {
      if (!/^[+-]?\d+$/.test(value)) {
        throw new Error("Enter a whole number");
      }
      return String(Number.parseInt(value, 10));
    }
    if (valueType === "decimal") {
      const locale =
        row.querySelector("[data-decimal-policy] select")?.value || "invariant";
      const localePatterns = {
        invariant: /^[+-]?\d+(?:\.\d+)?$/,
        en_US: /^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$/,
        de_DE: /^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$/,
        fr_FR: /^[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:,\d+)?$/,
      };
      if (!localePatterns[locale]?.test(value)) {
        throw new Error("Enter a valid number");
      }
      let normalized = value;
      if (locale === "en_US") {
        normalized = normalized.replaceAll(",", "");
      } else if (locale === "de_DE") {
        normalized = normalized.replaceAll(".", "").replace(",", ".");
      } else if (locale === "fr_FR") {
        normalized = normalized.replace(/[\s\u00a0\u202f]/g, "").replace(",", ".");
      }
      const places = row.querySelector("[data-round-places]")?.value;
      if (places !== undefined && places !== "") {
        throw new Error("Save to validate exact decimal rounding");
      }
      return normalized;
    }
    if (valueType === "boolean") {
      const token = value.toLocaleLowerCase();
      if (["true", "1", "yes", "y"].includes(token)) {
        return "true";
      }
      if (["false", "0", "no", "n"].includes(token)) {
        return "false";
      }
      throw new Error("Choose a valid yes or no value");
    }
    const dateFormat =
      row.querySelector("[data-date-policy] select")?.value || "iso";
    if (valueType === "date") {
      const patterns = {
        iso: /^(\d{4})-(\d{2})-(\d{2})$/,
        dmy_slash: /^(\d{2})\/(\d{2})\/(\d{4})$/,
        mdy_slash: /^(\d{2})\/(\d{2})\/(\d{4})$/,
        dmy_dot: /^(\d{2})\.(\d{2})\.(\d{4})$/,
      };
      const match = value.match(patterns[dateFormat]);
      if (!match) {
        throw new Error("Enter a valid date");
      }
      if (dateFormat === "iso") {
        const parsed = new Date(`${value}T00:00:00Z`);
        if (
          Number.isNaN(parsed.getTime()) ||
          parsed.toISOString().slice(0, 10) !== value
        ) {
          throw new Error("Enter a valid date");
        }
        return value;
      }
      const year = match[3];
      const month = dateFormat === "mdy_slash" ? match[1] : match[2];
      const day = dateFormat === "mdy_slash" ? match[2] : match[1];
      const normalized = `${year}-${month}-${day}`;
      const parsed = new Date(`${normalized}T00:00:00Z`);
      if (
        Number.isNaN(parsed.getTime()) ||
        parsed.toISOString().slice(0, 10) !== normalized
      ) {
        throw new Error("Enter a valid date");
      }
      return normalized;
    }
    if (valueType === "datetime") {
      if (dateFormat !== "iso") {
        const match = value.match(
          dateFormat === "dmy_dot"
            ? /^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})$/
            : /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2}):(\d{2})$/
        );
        if (!match) {
          throw new Error("Enter a valid date and time");
        }
        const year = match[3];
        const month = dateFormat === "mdy_slash" ? match[1] : match[2];
        const day = dateFormat === "mdy_slash" ? match[2] : match[1];
        const normalized =
          `${year}-${month}-${day}T${match[4]}:${match[5]}:${match[6]}`;
        const parsed = new Date(`${normalized}Z`);
        if (
          Number.isNaN(parsed.getTime()) ||
          parsed.toISOString().slice(0, 19) !== normalized
        ) {
          throw new Error("Enter a valid date and time");
        }
        return parsed.toISOString();
      }
      if (
        !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$/.test(
          value
        )
      ) {
        throw new Error("Enter a valid date and time");
      }
      const parsed = new Date(
        /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`
      );
      if (Number.isNaN(parsed.getTime())) {
        throw new Error("Enter a valid date and time");
      }
      return parsed.toISOString();
    }
    return value;
  };

  const validateRulePreview = (row, value) => {
    if (value === null || row.querySelector("[data-canonical-type]")?.value !== "string") {
      return;
    }
    const exactLength = row.querySelector("[data-rule-exact-length]")?.value;
    if (exactLength && value.length !== Number.parseInt(exactLength, 10)) {
      throw new Error(`Expected exactly ${exactLength} characters`);
    }
    const location =
      row.querySelector("[data-rule-segment-location]")?.value || "none";
    const characterClass =
      row.querySelector("[data-rule-character-class]")?.value || "none";
    if (location !== "none" && characterClass !== "none") {
      const countValue = row.querySelector("[data-rule-segment-length]")?.value;
      const count = Number.parseInt(countValue || "0", 10);
      if (["first", "last"].includes(location) && !count) {
        throw new Error("Enter how many first or last characters to check");
      }
      if (["first", "last"].includes(location) && value.length < count) {
        throw new Error(`Expected at least ${count} characters`);
      }
      const segment =
        location === "first"
          ? value.slice(0, count)
          : location === "last"
            ? value.slice(-count)
            : value;
      const patterns = {
        digits: /^[0-9]+$/,
        uppercase: /^[A-Z]+$/,
        lowercase: /^[a-z]+$/,
      };
      if (!patterns[characterClass]?.test(segment)) {
        throw new Error("The character check does not pass");
      }
    }
    if (row.querySelector("[data-rule-pattern]")?.value) {
      throw new Error("Save to validate the advanced custom pattern");
    }
  };

  const initializeScalarRow = (row) => {
    if (row.dataset.scalarRowInitialized === "true") {
      return;
    }
    row.dataset.scalarRowInitialized = "true";
    for (const select of row.querySelectorAll(
      "select[data-lazy-source-column]"
    )) {
      initializeLazySourceSelect(select);
    }
    const provider = row.querySelector("[data-value-source]");
    const sourceControl = row.querySelector("[data-provider-source]");
    const literalControl = row.querySelector("[data-provider-literal]");
    const literalLabel = row.querySelector("[data-literal-label]");
    const source = row.querySelector("[data-source-column]");
    const literal = row.querySelector("[data-literal-value]");
    const canonicalType = row.querySelector("[data-canonical-type]");
    const previewRaw = row.querySelector("[data-preview-raw]");
    const previewProposed = row.querySelector("[data-preview-proposed]");
    const updateScalarRow = () => {
      const mode = provider?.value || "";
      const usesSource = ["source", "source_with_fallback"].includes(mode);
      const usesLiteral = ["constant", "source_with_fallback"].includes(mode);
      if (sourceControl) {
        sourceControl.hidden = !usesSource;
      }
      if (literalControl) {
        literalControl.hidden = !usesLiteral;
      }
      if (literalLabel) {
        literalLabel.textContent =
          mode === "source_with_fallback" ? "Fallback value" : "Constant value";
      }

      const type = canonicalType?.value || "string";
      const decimalPolicy = row.querySelector("[data-decimal-policy]");
      const roundingPolicy = row.querySelector("[data-rounding-policy]");
      const datePolicy = row.querySelector("[data-date-policy]");
      const timezonePolicy = row.querySelector("[data-timezone-policy]");
      if (decimalPolicy) {
        decimalPolicy.hidden = type !== "decimal";
      }
      if (roundingPolicy) {
        roundingPolicy.hidden = type !== "decimal";
      }
      if (datePolicy) {
        datePolicy.hidden = !["date", "datetime"].includes(type);
      }
      if (timezonePolicy) {
        timezonePolicy.hidden = type !== "datetime";
      }
      const caseControl = row.querySelector("[data-transform-case]");
      if (caseControl) {
        caseControl.disabled = type !== "string";
      }
      const textRules = row.querySelector("[data-text-rules]");
      if (textRules) {
        textRules.hidden = false;
      }
      const textRuleTypeWarning = row.querySelector(
        "[data-text-rule-type-warning]"
      );
      if (textRuleTypeWarning) {
        textRuleTypeWarning.hidden = type === "string";
      }
      const segmentLocation = row.querySelector(
        "[data-rule-segment-location]"
      )?.value;
      const segmentLength = row.querySelector(
        "[data-segment-length-control]"
      );
      if (segmentLength) {
        segmentLength.hidden = !["first", "last"].includes(segmentLocation);
      }
      for (const policy of row.querySelectorAll(
        'input[name^="scalar_compare_"], input[name^="scalar_validate_only_"], input[name^="scalar_required_"]'
      )) {
        policy.disabled = mode === "odoo_default";
      }

      if (!previewRaw || !previewProposed) {
        return;
      }
      previewProposed.classList.remove("preview-error");
      if (!mode) {
        previewRaw.textContent = "Choose a provider";
        previewProposed.textContent = "";
        return;
      }
      if (mode === "odoo_default") {
        previewRaw.textContent = "Not sent";
        previewProposed.textContent = "Odoo runtime default";
        return;
      }

      const selectedOption = source?.selectedOptions[0];
      let missing =
        usesSource && selectedOption?.dataset.samplePresent !== "true";
      let raw = mode === "constant" ? literal?.value ?? "" : selectedOption?.dataset.sample;
      previewRaw.textContent = displayPreviewValue(
        usesSource ? selectedOption?.dataset.sample : raw
      );
      try {
        let transformed = transformPreviewValue(row, raw, missing);
        if (mode === "source_with_fallback" && transformed === null) {
          raw = literal?.value ?? "";
          missing = false;
          transformed = transformPreviewValue(row, raw, missing);
        }
        const formula = row.querySelector("[data-rule-formula]")?.value.trim();
        if (formula) {
          throw new Error("Save to validate the formula");
        }
        const proposed = canonicalPreviewValue(row, transformed);
        validateRulePreview(row, proposed);
        previewProposed.textContent = displayPreviewValue(proposed);
      } catch (error) {
        previewProposed.textContent =
          error instanceof Error
            ? error.message
            : "Invalid preview; save to validate";
        previewProposed.classList.add("preview-error");
      }
    };
    for (const control of row.querySelectorAll("select, input, textarea")) {
      control.addEventListener("change", updateScalarRow);
      control.addEventListener("input", updateScalarRow);
    }
    updateScalarRow();
  };

  for (const row of document.querySelectorAll("[data-scalar-mapping-row]")) {
    initializeScalarRow(row);
  }

  for (const catalog of document.querySelectorAll(
    "[data-scalar-field-catalog]"
  )) {
    const search = catalog.querySelector("[data-scalar-field-search]");
    const searchSubmit = catalog.querySelector("[data-field-search-submit]");
    const mappedOnly = catalog.querySelector("[data-show-mapped-scalars]");
    const count = catalog.querySelector("[data-scalar-field-count]");
    const topScroll = catalog.querySelector(
      "[data-scalar-table-scroll-top]"
    );
    const topScrollSpacer = catalog.querySelector(
      "[data-scalar-table-scroll-spacer]"
    );
    const tableScroll = catalog.querySelector("[data-scalar-table-scroll]");
    let scalarTable = tableScroll?.querySelector(".mapping-table");
    let rows = Array.from(
      catalog.querySelectorAll("[data-scalar-field-row]")
    );
    const updateScalarTableScroll = () => {
      if (!topScroll || !topScrollSpacer || !tableScroll) {
        return;
      }
      const scrollWidth = tableScroll.scrollWidth;
      topScrollSpacer.style.width = `${scrollWidth}px`;
      topScroll.hidden = scrollWidth <= tableScroll.clientWidth + 1;
      if (!topScroll.hidden) {
        topScroll.scrollLeft = tableScroll.scrollLeft;
      }
    };
    topScroll?.addEventListener("scroll", () => {
      if (tableScroll && tableScroll.scrollLeft !== topScroll.scrollLeft) {
        tableScroll.scrollLeft = topScroll.scrollLeft;
      }
    });
    tableScroll?.addEventListener("scroll", () => {
      if (topScroll && topScroll.scrollLeft !== tableScroll.scrollLeft) {
        topScroll.scrollLeft = tableScroll.scrollLeft;
      }
    });
    window.addEventListener("resize", updateScalarTableScroll);
    let scrollResizeObserver;
    if (tableScroll && "ResizeObserver" in window) {
      scrollResizeObserver = new ResizeObserver(updateScalarTableScroll);
      scrollResizeObserver.observe(tableScroll);
      if (scalarTable) {
        scrollResizeObserver.observe(scalarTable);
      }
    }
    const updateScalarFieldRows = () => {
      const query = search?.value.trim().toLowerCase() || "";
      let visible = 0;
      let mapped = 0;
      for (const row of rows) {
        const provider = row.querySelector("[data-value-source]");
        const isMapped = Boolean(provider?.value);
        const matches =
          (row.dataset.fieldSearchText || "").includes(query) &&
          (!mappedOnly?.checked || isMapped);
        row.hidden = !matches;
        row.dataset.mapped = String(isMapped);
        visible += matches ? 1 : 0;
        mapped += isMapped ? 1 : 0;
      }
      if (count) {
        const matchingTotal = catalog.dataset.scalarMatchingTotal || rows.length;
        const mappedTotal = catalog.dataset.scalarMappedTotal || mapped;
        count.textContent =
          `Showing ${visible} of ${matchingTotal} fields · ${mappedTotal} matched`;
      }
      window.requestAnimationFrame(updateScalarTableScroll);
    };
    const initializeCatalogRows = () => {
      rows = Array.from(
        catalog.querySelectorAll("[data-scalar-field-row]")
      );
      for (const row of rows) {
        restoreScalarRow(row);
        initializeScalarRow(row);
        const provider = row.querySelector("[data-value-source]");
        if (
          provider &&
          provider.dataset.catalogCountInitialized !== "true"
        ) {
          provider.dataset.catalogCountInitialized = "true";
          provider.addEventListener("change", updateScalarFieldRows);
        }
      }
    };
    let fieldSearchTimer;
    let fieldSearchController;
    const catalogSearchUrl = (requestedUrl = null) => {
      const url = new URL(requestedUrl || window.location.href);
      if (requestedUrl === null) {
        const searchValue = search?.value.trim() || "";
        if (searchValue) {
          url.searchParams.set("field_query", searchValue);
        } else {
          url.searchParams.delete("field_query");
        }
        if (mappedOnly?.checked) {
          url.searchParams.set("mapped_only", "1");
        } else {
          url.searchParams.delete("mapped_only");
        }
        url.searchParams.set("scalar_page", "1");
      }
      return url;
    };
    const loadScalarCatalog = async (requestedUrl = null) => {
      window.clearTimeout(fieldSearchTimer);
      fieldSearchController?.abort();
      const activeController = new AbortController();
      fieldSearchController = activeController;
      const url = catalogSearchUrl(requestedUrl);
      catalog.setAttribute("aria-busy", "true");
      if (count) {
        count.textContent = "Searching Odoo fields\u2026";
      }
      try {
        const response = await fetch(url, {
          headers: { Accept: "text/html" },
          signal: activeController.signal,
        });
        if (!response.ok) {
          throw new Error("The field list could not be updated. Please try again.");
        }
        const documentResult = new DOMParser().parseFromString(
          await response.text(),
          "text/html"
        );
        const incomingCatalog = documentResult.querySelector(
          "[data-scalar-field-catalog]"
        );
        const incomingTableScroll = incomingCatalog?.querySelector(
          "[data-scalar-table-scroll]"
        );
        if (!incomingCatalog || !incomingTableScroll || !tableScroll) {
          throw new Error("Field search returned an incomplete result.");
        }
        tableScroll.replaceChildren(
          ...Array.from(incomingTableScroll.childNodes, (node) =>
            document.importNode(node, true)
          )
        );
        scalarTable = tableScroll.querySelector(".mapping-table");
        if (scalarTable && scrollResizeObserver) {
          scrollResizeObserver.observe(scalarTable);
        }
        const pagination = catalog.querySelector("[data-scalar-pagination]");
        const incomingPagination = incomingCatalog.querySelector(
          "[data-scalar-pagination]"
        );
        if (pagination && incomingPagination) {
          pagination.replaceWith(document.importNode(incomingPagination, true));
        }
        for (const name of [
          "scalarCatalogTotal",
          "scalarMatchingTotal",
          "scalarMappedTotal",
        ]) {
          catalog.dataset[name] = incomingCatalog.dataset[name] || "0";
        }
        initializeCatalogRows();
        updateScalarFieldRows();
        window.history.replaceState(
          {},
          "",
          `${url.pathname}${url.search}${url.hash}`
        );
        if (mappingForm) {
          const saveUrl = new URL(
            mappingForm.getAttribute("action") || window.location.pathname,
            window.location.href
          );
          saveUrl.search = url.search;
          mappingForm.setAttribute(
            "action",
            `${saveUrl.pathname}${saveUrl.search}`
          );
        }
      } catch (error) {
        if (error?.name === "AbortError") {
          return;
        }
        if (count) {
          count.textContent =
            error instanceof Error
              ? error.message
              : "Field search failed.";
        }
      } finally {
        if (fieldSearchController === activeController) {
          catalog.removeAttribute("aria-busy");
        }
      }
    };
    const scheduleScalarCatalogSearch = () => {
      window.clearTimeout(fieldSearchTimer);
      updateScalarFieldRows();
      if (count) {
        count.textContent = "Searching Odoo fields\u2026";
      }
      fieldSearchTimer = window.setTimeout(() => loadScalarCatalog(), 200);
    };
    search?.addEventListener("input", scheduleScalarCatalogSearch);
    search?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadScalarCatalog();
      }
    });
    searchSubmit?.addEventListener("click", () => loadScalarCatalog());
    mappedOnly?.addEventListener("change", () => loadScalarCatalog());
    catalog.addEventListener("click", (event) => {
      const link = event.target.closest("[data-scalar-pagination] a");
      if (!link) {
        return;
      }
      event.preventDefault();
      loadScalarCatalog(link.href);
    });
    initializeCatalogRows();
    updateScalarFieldRows();
  }

  for (const catalog of document.querySelectorAll(
    "[data-relation-field-catalog]"
  )) {
    const search = catalog.querySelector("[data-relation-field-search]");
    const searchSubmit = catalog.querySelector(
      "[data-relation-search-submit]"
    );
    const count = catalog.querySelector("[data-relation-field-count]");
    const results = catalog.querySelector("[data-relation-field-results]");
    let relationSearchTimer;
    let relationSearchController;

    const initializeRelationRows = () => {
      for (const row of catalog.querySelectorAll(
        "[data-relation-mapping-row]"
      )) {
        restoreRelationRow(row);
        for (const select of row.querySelectorAll(
          "select[data-lazy-source-column]"
        )) {
          initializeLazySourceSelect(select);
        }
      }
    };
    const relationCatalogUrl = (requestedUrl = null) => {
      const url = new URL(requestedUrl || window.location.href);
      if (requestedUrl === null) {
        const searchValue = search?.value.trim() || "";
        if (searchValue) {
          url.searchParams.set("relation_query", searchValue);
        } else {
          url.searchParams.delete("relation_query");
        }
        url.searchParams.set("relation_page", "1");
      }
      return url;
    };
    const loadRelationCatalog = async (requestedUrl = null) => {
      window.clearTimeout(relationSearchTimer);
      relationSearchController?.abort();
      const activeController = new AbortController();
      relationSearchController = activeController;
      const url = relationCatalogUrl(requestedUrl);
      catalog.setAttribute("aria-busy", "true");
      if (count) {
        count.textContent = "Searching linked Odoo fields\u2026";
      }
      try {
        const response = await fetch(url, {
          headers: { Accept: "text/html" },
          signal: activeController.signal,
        });
        if (!response.ok) {
          throw new Error(
            "The linked-field list could not be updated. Please try again."
          );
        }
        const documentResult = new DOMParser().parseFromString(
          await response.text(),
          "text/html"
        );
        const incomingCatalog = documentResult.querySelector(
          "[data-relation-field-catalog]"
        );
        const incomingResults = incomingCatalog?.querySelector(
          "[data-relation-field-results]"
        );
        if (!incomingCatalog || !incomingResults || !results) {
          throw new Error("Linked-field search returned an incomplete result.");
        }
        results.replaceChildren(
          ...Array.from(incomingResults.childNodes, (node) =>
            document.importNode(node, true)
          )
        );
        const pagination = catalog.querySelector("[data-relation-pagination]");
        const incomingPagination = incomingCatalog.querySelector(
          "[data-relation-pagination]"
        );
        if (pagination && incomingPagination) {
          pagination.replaceWith(document.importNode(incomingPagination, true));
        }
        for (const name of [
          "relationCatalogTotal",
          "relationMatchingTotal",
          "relationMappedTotal",
          "relationPageSize",
        ]) {
          catalog.dataset[name] = incomingCatalog.dataset[name] || "0";
        }
        const incomingCount = incomingCatalog.querySelector(
          "[data-relation-field-count]"
        );
        if (count && incomingCount) {
          count.textContent = incomingCount.textContent.trim();
        }
        initializeRelationRows();
        window.history.replaceState(
          {},
          "",
          `${url.pathname}${url.search}${url.hash}`
        );
        if (mappingForm) {
          const saveUrl = new URL(
            mappingForm.getAttribute("action") || window.location.pathname,
            window.location.href
          );
          saveUrl.search = url.search;
          mappingForm.setAttribute(
            "action",
            `${saveUrl.pathname}${saveUrl.search}`
          );
        }
      } catch (error) {
        if (error?.name === "AbortError") {
          return;
        }
        if (count) {
          count.textContent =
            error instanceof Error
              ? error.message
              : "Linked-field search failed.";
        }
      } finally {
        if (relationSearchController === activeController) {
          catalog.removeAttribute("aria-busy");
        }
      }
    };
    const scheduleRelationCatalogSearch = () => {
      window.clearTimeout(relationSearchTimer);
      if (count) {
        count.textContent = "Searching linked Odoo fields\u2026";
      }
      relationSearchTimer = window.setTimeout(
        () => loadRelationCatalog(),
        200
      );
    };
    search?.addEventListener("input", scheduleRelationCatalogSearch);
    search?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadRelationCatalog();
      }
    });
    searchSubmit?.addEventListener("click", () => loadRelationCatalog());
    catalog.addEventListener("click", (event) => {
      const link = event.target.closest("[data-relation-pagination] a");
      if (!link) {
        return;
      }
      event.preventDefault();
      loadRelationCatalog(link.href);
    });
    initializeRelationRows();
  }

  const preparationJob = document.querySelector("[data-preparation-job]");
  if (preparationJob) {
    const statusUrl = preparationJob.dataset.statusUrl;
    const state = preparationJob.querySelector("[data-preparation-state]");
    const message = preparationJob.querySelector("[data-preparation-message]");
    const progress = preparationJob.querySelector("[data-preparation-progress]");
    const percent = preparationJob.querySelector("[data-preparation-percent]");
    const rows = preparationJob.querySelector("[data-preparation-rows]");
    const spinner = preparationJob.querySelector("[data-preparation-spinner]");
    const activeActions = preparationJob.querySelector("[data-preparation-active]");
    const cancelButton = preparationJob.querySelector("[data-preparation-cancel]");
    const failed = preparationJob.querySelector("[data-preparation-failed]");
    const failure = preparationJob.querySelector("[data-preparation-failure]");
    const cancelled = preparationJob.querySelector("[data-preparation-cancelled]");
    const complete = preparationJob.querySelector("[data-preparation-complete]");
    const continueLink = preparationJob.querySelector("[data-preparation-continue]");
    let pollTimer;

    const formatRows = (completed, total) => {
      if (!total) {
        return "Starting safely";
      }
      return `${Number(completed).toLocaleString()} of ${Number(total).toLocaleString()} source rows`;
    };

    const showStatus = (job) => {
      const active = job.status === "QUEUED" || job.status === "RUNNING";
      if (message) message.textContent = job.message;
      if (progress) progress.value = job.progress_percent;
      if (percent) percent.textContent = `${job.progress_percent}%`;
      if (rows) rows.textContent = formatRows(job.completed_rows, job.total_rows);
      if (spinner) spinner.hidden = !active;
      if (activeActions) activeActions.hidden = !active;
      if (cancelButton && job.cancel_requested) {
        cancelButton.disabled = true;
        cancelButton.textContent = "Stopping safely…";
      }
      if (state) {
        state.classList.remove("ready", "review", "blocked");
        state.textContent = active
          ? "In progress"
          : job.status === "FAILED"
            ? "Could not finish"
            : "Stopped";
        state.classList.add(active ? "review" : "blocked");
      }
      if (job.status === "FAILED") {
        if (failed) failed.hidden = false;
        if (failure) failure.textContent = job.failure_message;
      } else if (job.status === "CANCELLED") {
        if (cancelled) cancelled.hidden = false;
      } else if (
        job.status === "SUCCEEDED" ||
        job.status === "REVIEW_REQUIRED"
      ) {
        if (state) {
          state.textContent = job.status === "REVIEW_REQUIRED" ? "Review needed" : "Ready";
          state.classList.remove("blocked", "review");
          state.classList.add(job.status === "REVIEW_REQUIRED" ? "review" : "ready");
        }
        if (complete) complete.hidden = false;
        if (continueLink && job.redirect_url) continueLink.href = job.redirect_url;
        if (job.redirect_url) window.location.assign(job.redirect_url);
      }
      return active;
    };

    const pollPreparation = async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Progress is temporarily unavailable");
        if (showStatus(await response.json())) {
          pollTimer = window.setTimeout(pollPreparation, 750);
        }
      } catch {
        if (message) message.textContent = "Reconnecting to preparation…";
        pollTimer = window.setTimeout(pollPreparation, 1500);
      }
    };

    if (statusUrl) {
      pollPreparation();
    }
    window.addEventListener("pagehide", () => window.clearTimeout(pollTimer));
  }

  restoreMappingPosition();
  restoreNormalizationPosition();

});
