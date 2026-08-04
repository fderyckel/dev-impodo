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

  const mappingForm = document.querySelector("[data-mapping-form]");
  if (mappingForm) {
    const saveStatus = mappingForm.querySelector(
      "[data-mapping-save-status]"
    );
    const saveProgress = mappingForm.querySelector(
      "[data-save-mapping-progress]"
    );
    let dirty = false;
    let submitting = false;

    const savedAt = saveStatus?.dataset.savedAt;
    if (saveStatus && savedAt) {
      const savedDate = new Date(savedAt);
      if (!Number.isNaN(savedDate.getTime())) {
        saveStatus.textContent = `Saved ${savedDate.toLocaleString()}.`;
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
    };
    mappingForm.addEventListener("input", markMappingDirty);
    mappingForm.addEventListener("change", markMappingDirty);

    mappingForm.addEventListener("submit", (event) => {
      submitting = true;
      mappingForm.setAttribute("aria-busy", "true");
      if (saveStatus) {
        const action = event.submitter?.value || "";
        saveStatus.textContent =
          action === "save_progress"
            ? "Saving progress..."
            : action === "submit"
              ? "Saving, validating, and submitting..."
              : "Saving and validating...";
        saveStatus.classList.remove("unsaved");
      }
    });

    window.addEventListener("beforeunload", (event) => {
      if (!dirty || submitting) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    });

    document.addEventListener("keydown", (event) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        event.key.toLocaleLowerCase() === "s" &&
        saveProgress
      ) {
        event.preventDefault();
        mappingForm.requestSubmit(saveProgress);
      }
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
          `${visibleCount} of ${choices.length} available models shown · ` +
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
        throw new Error("Invalid integer");
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
        throw new Error("Invalid decimal");
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
      throw new Error("Invalid boolean");
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
        throw new Error("Invalid date");
      }
      if (dateFormat === "iso") {
        const parsed = new Date(`${value}T00:00:00Z`);
        if (
          Number.isNaN(parsed.getTime()) ||
          parsed.toISOString().slice(0, 10) !== value
        ) {
          throw new Error("Invalid date");
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
        throw new Error("Invalid date");
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
          throw new Error("Invalid datetime");
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
          throw new Error("Invalid datetime");
        }
        return parsed.toISOString();
      }
      if (
        !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$/.test(
          value
        )
      ) {
        throw new Error("Invalid datetime");
      }
      const parsed = new Date(
        /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`
      );
      if (Number.isNaN(parsed.getTime())) {
        throw new Error("Invalid datetime");
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

  for (const row of document.querySelectorAll("[data-scalar-mapping-row]")) {
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
  }

  for (const catalog of document.querySelectorAll(
    "[data-scalar-field-catalog]"
  )) {
    const search = catalog.querySelector("[data-scalar-field-search]");
    const mappedOnly = catalog.querySelector("[data-show-mapped-scalars]");
    const count = catalog.querySelector("[data-scalar-field-count]");
    const topScroll = catalog.querySelector(
      "[data-scalar-table-scroll-top]"
    );
    const topScrollSpacer = catalog.querySelector(
      "[data-scalar-table-scroll-spacer]"
    );
    const tableScroll = catalog.querySelector("[data-scalar-table-scroll]");
    const scalarTable = tableScroll?.querySelector(".mapping-table");
    const rows = Array.from(
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
    if (tableScroll && "ResizeObserver" in window) {
      const scrollResizeObserver = new ResizeObserver(updateScalarTableScroll);
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
        count.textContent =
          `${visible} of ${rows.length} fields shown / ${mapped} mapped`;
      }
      window.requestAnimationFrame(updateScalarTableScroll);
    };
    search?.addEventListener("input", updateScalarFieldRows);
    mappedOnly?.addEventListener("change", updateScalarFieldRows);
    for (const provider of catalog.querySelectorAll("[data-value-source]")) {
      provider.addEventListener("change", updateScalarFieldRows);
    }
    updateScalarFieldRows();
  }
});
