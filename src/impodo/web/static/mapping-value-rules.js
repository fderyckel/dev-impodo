"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const {
    initializeLazySourceSelect,
  } = window.impodoMappingEditor;

  const displayPreviewValue = (value) => {
    if (value === null || value === undefined) {
      return "(empty)";
    }
    if (value === "") {
      return '""';
    }
    return String(value);
  };

  const textStepPreset = (step) =>
    step.kind === "remove_separators_between_digits"
      ? "remove_separators_between_digits"
      : step.search_mode || "literal";

  const defaultTextStep = (preset) => ({
    kind:
      preset === "remove_separators_between_digits"
        ? "remove_separators_between_digits"
        : "find_replace",
    search_value: "",
    replacement_value: "",
    search_mode: ["starts_with", "ends_with", "pattern"].includes(preset)
      ? preset
      : "literal",
    replace_all: !["starts_with", "ends_with"].includes(preset),
    characters:
      preset === "remove_separators_between_digits" ? " .-" : "",
  });

  const internationalPhoneTextSteps = () => [
    {
      ...defaultTextStep("starts_with"),
      search_value: "00",
      replacement_value: "+",
    },
    {
      ...defaultTextStep("remove_separators_between_digits"),
      characters: " .-/",
    },
  ];

  const readTextSteps = (row) => {
    const storage = row.querySelector("[data-text-step-storage]");
    if (!storage?.value) {
      return [];
    }
    try {
      const steps = JSON.parse(storage.value);
      return Array.isArray(steps) ? steps : [];
    } catch (_error) {
      return [];
    }
  };

  const textStepsFromCards = (builder) =>
    Array.from(builder.querySelectorAll("[data-text-step]")).map((card) => {
      const preset = card.querySelector("[data-text-step-kind]")?.value || "literal";
      const separators = Array.from(
        card.querySelectorAll("[data-text-step-separator]:checked")
      )
        .map((control) => control.value)
        .join("");
      return {
        kind:
          preset === "remove_separators_between_digits"
            ? "remove_separators_between_digits"
            : "find_replace",
        search_value: card.querySelector("[data-text-step-search]")?.value || "",
        replacement_value:
          card.querySelector("[data-text-step-replacement]")?.value || "",
        search_mode: ["starts_with", "ends_with", "pattern"].includes(preset)
          ? preset
          : "literal",
        replace_all: ["starts_with", "ends_with"].includes(preset)
          ? false
          : Boolean(card.querySelector("[data-text-step-replace-all]")?.checked),
        characters: separators,
      };
    });

  const syncTextStepStorage = (builder) => {
    const stepCount = builder.querySelectorAll("[data-text-step]").length;
    const storage = builder.querySelector("[data-text-step-storage]");
    if (storage) {
      storage.value = JSON.stringify(textStepsFromCards(builder));
    }
    const empty = builder.querySelector("[data-text-step-empty]");
    if (empty) {
      empty.hidden = stepCount > 0;
    }
    const add = builder.querySelector("[data-add-text-step]");
    if (add) {
      add.disabled = stepCount >= 20;
    }
    const limit = builder.querySelector("[data-text-step-limit]");
    if (limit) {
      limit.hidden = stepCount < 20;
    }
    const phoneQuickStart = builder.querySelector(
      "[data-phone-cleanup-quick-start]"
    );
    if (phoneQuickStart) {
      phoneQuickStart.hidden = stepCount > 0;
    }
  };

  const notifyTextStepsChanged = (builder) => {
    const storage = builder.querySelector("[data-text-step-storage]");
    storage?.dispatchEvent(new Event("input", { bubbles: true }));
  };

  const refreshTextStepCard = (card) => {
    const preset = card.querySelector("[data-text-step-kind]")?.value || "literal";
    const separatorFields = card.querySelector("[data-text-step-separators]");
    const findFields = card.querySelector("[data-text-step-find-fields]");
    const replaceAll = card.querySelector("[data-text-step-replace-all-label]");
    if (separatorFields) {
      separatorFields.hidden = preset !== "remove_separators_between_digits";
    }
    if (findFields) {
      findFields.hidden = preset === "remove_separators_between_digits";
    }
    if (replaceAll) {
      replaceAll.hidden = !["literal", "pattern"].includes(preset);
    }
    const searchLabel = card.querySelector("[data-text-step-search-label]");
    if (searchLabel) {
      searchLabel.textContent = {
        literal: "Text to find",
        starts_with: "Text at the beginning",
        ends_with: "Text at the end",
        pattern: "Advanced pattern",
      }[preset] || "Text to find";
    }
    const help = card.querySelector("[data-text-step-help]");
    if (help) {
      help.hidden = preset !== "pattern";
    }
    const warning = card.querySelector("[data-text-step-warning]");
    if (warning) {
      const search = card.querySelector("[data-text-step-search]")?.value || "";
      const patternToken = search.match(/\^|\$|\[|\]|\.\*/)?.[0];
      warning.hidden = !(preset === "literal" && patternToken);
      if (!warning.hidden) {
        warning.textContent =
          `${patternToken} is ordinary text here. ` +
          "Choose a beginning, end, or Advanced pattern cleanup when that is what you mean.";
      }
    }
  };

  const textStepCard = (step, index, count) => {
    const card = document.createElement("article");
    card.className = "text-step-card";
    card.dataset.textStep = "";
    card.innerHTML = `
      <div class="text-step-heading">
        <strong data-text-step-number></strong>
        <div class="text-step-actions">
          <button class="button secondary compact" type="button" data-move-text-step="up">Move up</button>
          <button class="button secondary compact" type="button" data-move-text-step="down">Move down</button>
          <button class="button danger compact" type="button" data-remove-text-step>Remove</button>
        </div>
      </div>
      <label>What should change?
        <select data-text-step-kind>
          <option value="literal">Replace text</option>
          <option value="starts_with">Replace text at the beginning</option>
          <option value="ends_with">Replace text at the end</option>
          <option value="remove_separators_between_digits">Remove separators between numbers</option>
          <option value="pattern">Advanced pattern</option>
        </select>
      </label>
      <div class="rule-inline-fields" data-text-step-find-fields>
        <label><span data-text-step-search-label>Text to find</span>
          <input maxlength="500" data-text-step-search>
        </label>
        <label>Replace with
          <input maxlength="1000" placeholder="Leave empty to remove" data-text-step-replacement>
        </label>
      </div>
      <label class="checkbox rule-checkbox" data-text-step-replace-all-label>
        <input type="checkbox" data-text-step-replace-all> Replace every match
      </label>
      <fieldset class="text-step-separators" data-text-step-separators hidden>
        <legend>Separators to remove between numbers</legend>
        <label><input type="checkbox" value=" " data-text-step-separator> Spaces</label>
        <label><input type="checkbox" value="." data-text-step-separator> Dots</label>
        <label><input type="checkbox" value="-" data-text-step-separator> Hyphens</label>
        <label><input type="checkbox" value="/" data-text-step-separator> Slashes</label>
      </fieldset>
      <p class="muted" data-text-step-help hidden>For expert use. Save progress to validate the pattern safely.</p>
      <p class="muted rule-literal-warning" data-text-step-warning role="status" hidden></p>
    `;
    card.querySelector("[data-text-step-number]").textContent = `Cleanup ${index + 1}`;
    const preset = textStepPreset(step);
    card.querySelector("[data-text-step-kind]").value = preset;
    card.querySelector("[data-text-step-search]").value = step.search_value || "";
    card.querySelector("[data-text-step-replacement]").value =
      step.replacement_value || "";
    card.querySelector("[data-text-step-replace-all]").checked =
      step.replace_all !== false;
    for (const control of card.querySelectorAll("[data-text-step-separator]")) {
      control.checked = (step.characters || "").includes(control.value);
    }
    card.querySelector('[data-move-text-step="up"]').disabled = index === 0;
    card.querySelector('[data-move-text-step="down"]').disabled =
      index === count - 1;
    refreshTextStepCard(card);
    return card;
  };

  const renderTextSteps = (builder, steps) => {
    const list = builder.querySelector("[data-text-step-list]");
    if (!list) {
      return;
    }
    list.replaceChildren(
      ...steps.map((step, index) => textStepCard(step, index, steps.length))
    );
    syncTextStepStorage(builder);
  };

  const removeSeparatorsBetweenDigits = (value, characters) => {
    const separators = new Set(Array.from(characters || ""));
    let result = "";
    let index = 0;
    while (index < value.length) {
      const character = value[index];
      if (separators.has(character) && /[0-9]/.test(result.slice(-1))) {
        let end = index + 1;
        while (end < value.length && separators.has(value[end])) {
          end += 1;
        }
        if (end < value.length && /[0-9]/.test(value[end])) {
          index = end;
          continue;
        }
      }
      result += character;
      index += 1;
    }
    return result;
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
    for (const step of readTextSteps(row)) {
      if (value === null) {
        break;
      }
      if (step.kind === "remove_separators_between_digits") {
        value = removeSeparatorsBetweenDigits(value, step.characters);
      } else if (step.search_value && step.search_mode === "pattern") {
        throw new Error("Save to validate the Advanced pattern");
      } else if (step.search_value && step.search_mode === "starts_with") {
        if (value.startsWith(step.search_value)) {
          value = `${step.replacement_value}${value.slice(step.search_value.length)}`;
        }
      } else if (step.search_value && step.search_mode === "ends_with") {
        if (value.endsWith(step.search_value)) {
          value = `${value.slice(0, -step.search_value.length)}${step.replacement_value}`;
        }
      } else if (step.search_value) {
        value = step.replace_all
          ? value.split(step.search_value).join(step.replacement_value)
          : value.replace(step.search_value, step.replacement_value);
      }
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

  const initializeTextStepBuilder = (row, updateScalarRow) => {
    const builder = row.querySelector("[data-value-rule-builder]");
    if (!builder || builder.dataset.textStepsInitialized === "true") {
      return;
    }
    builder.dataset.textStepsInitialized = "true";
    renderTextSteps(builder, readTextSteps(row));
    builder.addEventListener("click", (event) => {
      const phoneQuickStart = event.target.closest(
        "[data-use-phone-cleanup]"
      );
      if (phoneQuickStart) {
        if (textStepsFromCards(builder).length > 0) {
          return;
        }
        renderTextSteps(builder, internationalPhoneTextSteps());
        notifyTextStepsChanged(builder);
        updateScalarRow();
        return;
      }
      const add = event.target.closest("[data-add-text-step]");
      if (add) {
        const steps = textStepsFromCards(builder);
        if (steps.length >= 20) {
          return;
        }
        const preset =
          builder.querySelector("[data-new-text-step-kind]")?.value || "literal";
        renderTextSteps(builder, [...steps, defaultTextStep(preset)]);
        notifyTextStepsChanged(builder);
        updateScalarRow();
        return;
      }
      const card = event.target.closest("[data-text-step]");
      if (!card) {
        return;
      }
      const steps = textStepsFromCards(builder);
      const cards = Array.from(builder.querySelectorAll("[data-text-step]"));
      const index = cards.indexOf(card);
      if (event.target.closest("[data-remove-text-step]")) {
        steps.splice(index, 1);
      } else {
        const move = event.target.closest("[data-move-text-step]")?.dataset
          .moveTextStep;
        const targetIndex = move === "up" ? index - 1 : move === "down" ? index + 1 : index;
        if (targetIndex === index || targetIndex < 0 || targetIndex >= steps.length) {
          return;
        }
        [steps[index], steps[targetIndex]] = [steps[targetIndex], steps[index]];
      }
      renderTextSteps(builder, steps);
      notifyTextStepsChanged(builder);
      updateScalarRow();
    });
    const syncFromControl = (event) => {
      const card = event.target.closest("[data-text-step]");
      if (!card) {
        return;
      }
      refreshTextStepCard(card);
      syncTextStepStorage(builder);
      updateScalarRow();
    };
    builder.addEventListener("change", syncFromControl);
    builder.addEventListener("input", syncFromControl);
  };

  const selectionConditionOperators = {
    string: [
      ["is_blank", "is blank"], ["is_not_blank", "is not blank"],
      ["equals", "equals"], ["not_equals", "does not equal"],
      ["equals_ignore_case", "equals, ignoring case"], ["contains", "contains"],
      ["starts_with", "starts with"], ["ends_with", "ends with"],
    ],
    ordered: [
      ["is_blank", "is blank"], ["is_not_blank", "is not blank"],
      ["equals", "equals"], ["not_equals", "does not equal"],
      ["less_than", "is less than"],
      ["less_than_or_equal", "is at most"],
      ["greater_than", "is greater than"],
      ["greater_than_or_equal", "is at least"],
    ],
    boolean: [
      ["is_blank", "is blank"], ["is_not_blank", "is not blank"],
      ["is_true", "is yes / true"], ["is_false", "is no / false"],
    ],
  };

  const newSelectionRuleId = () =>
    globalThis.crypto?.randomUUID?.() ||
    "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (character) =>
      (Number(character) ^ (Math.random() * 16 >> Number(character) / 4)).toString(16)
    );

  const initializeSelectionRuleBuilder = (row, updateScalarRow) => {
    const control = row.querySelector("[data-provider-selection-rules]");
    const storage = row.querySelector("[data-selection-rule-storage]");
    const list = row.querySelector("[data-selection-rule-list]");
    const ruleTemplate = row.querySelector("[data-selection-rule-template]");
    const conditionTemplate = row.querySelector(
      "[data-selection-condition-template]"
    );
    const otherwise = row.querySelector("[data-selection-rule-otherwise]");
    if (!control || !(storage instanceof HTMLInputElement) || !list ||
        !ruleTemplate || !conditionTemplate || !(otherwise instanceof HTMLSelectElement)) {
      return null;
    }
    let state;
    try {
      state = JSON.parse(storage.value || "");
    } catch (_error) {
      state = { rules: [], otherwise_value: null };
    }
    if (!Array.isArray(state.rules)) {
      state.rules = [];
    }
    const sync = () => {
      row.dataset.selectionRulesDirty = "true";
      storage.value = JSON.stringify(state);
      storage.dispatchEvent(new Event("input", { bubbles: true }));
      updateScalarRow();
    };
    const newCondition = () => ({
      condition_id: newSelectionRuleId(),
      source_column_key: "",
      operator: "equals",
      comparison_value: "",
      value_type: "string",
    });
    const newRule = () => ({
      rule_id: newSelectionRuleId(),
      conditions: [newCondition()],
      target_value: ruleTemplate.content.querySelector(
        "[data-selection-rule-target] option"
      )?.value || "",
      join: "all",
    });
    const render = () => {
      list.replaceChildren();
      state.rules.forEach((rule, ruleIndex) => {
        const fragment = ruleTemplate.content.cloneNode(true);
        const card = fragment.querySelector("[data-selection-rule]");
        card.querySelector("[data-selection-rule-number]").textContent =
          `Rule ${ruleIndex + 1}`;
        const join = card.querySelector("[data-selection-rule-join]");
        const target = card.querySelector("[data-selection-rule-target]");
        join.value = rule.join || "all";
        if (
          rule.target_value &&
          !Array.from(target.options).some((option) => option.value === rule.target_value)
        ) {
          const unavailable = document.createElement("option");
          unavailable.value = rule.target_value;
          unavailable.textContent = `Unavailable in captured Odoo choices — ${rule.target_value}`;
          target.append(unavailable);
        }
        target.value = rule.target_value || target.options[0]?.value || "";
        rule.target_value = target.value;
        join.addEventListener("change", () => { rule.join = join.value; sync(); });
        target.addEventListener("change", () => {
          rule.target_value = target.value;
          sync();
        });
        const conditionList = card.querySelector("[data-selection-condition-list]");
        rule.conditions.forEach((condition, conditionIndex) => {
          const conditionFragment = conditionTemplate.content.cloneNode(true);
          const conditionRow = conditionFragment.querySelector(
            "[data-selection-condition]"
          );
          const source = conditionRow.querySelector(
            "[data-selection-condition-source]"
          );
          const type = conditionRow.querySelector(
            "[data-selection-condition-type]"
          );
          const operator = conditionRow.querySelector(
            "[data-selection-condition-operator]"
          );
          const valueControl = conditionRow.querySelector(
            "[data-selection-condition-value-control]"
          );
          const value = conditionRow.querySelector(
            "[data-selection-condition-value]"
          );
          if (condition.source_column_key) {
            const sourceOptionsTemplate = row
              .closest(".mapping-dataset")
              ?.querySelector("template[data-source-column-options]");
            const sharedOption = sourceOptionsTemplate?.content.querySelector(
              `option[value="${CSS.escape(condition.source_column_key)}"]`
            );
            if (sharedOption) {
              source.append(sharedOption.cloneNode(true));
            }
          }
          source.value = condition.source_column_key || "";
          initializeLazySourceSelect(source);
          type.value = condition.value_type || "string";
          value.value = condition.comparison_value ?? "";
          const refreshOperators = () => {
            const family = type.value === "boolean"
              ? "boolean"
              : (["integer", "decimal", "date", "datetime"].includes(type.value)
                ? "ordered" : "string");
            const choices = selectionConditionOperators[family];
            operator.replaceChildren(...choices.map(([key, label]) => {
              const option = document.createElement("option");
              option.value = key;
              option.textContent = label;
              return option;
            }));
            if (choices.some(([key]) => key === condition.operator)) {
              operator.value = condition.operator;
            }
            condition.operator = operator.value;
            const unary = ["is_blank", "is_not_blank", "is_true", "is_false"]
              .includes(operator.value);
            valueControl.hidden = unary;
            condition.comparison_value = unary ? null : value.value;
          };
          refreshOperators();
          source.addEventListener("change", () => {
            condition.source_column_key = source.value;
            sync();
          });
          type.addEventListener("change", () => {
            condition.value_type = type.value;
            refreshOperators();
            sync();
          });
          operator.addEventListener("change", () => {
            condition.operator = operator.value;
            refreshOperators();
            sync();
          });
          value.addEventListener("input", () => {
            condition.comparison_value = value.value;
            sync();
          });
          conditionRow.querySelector("[data-remove-selection-condition]")
            .addEventListener("click", () => {
              if (rule.conditions.length === 1) {
                return;
              }
              rule.conditions.splice(conditionIndex, 1);
              render();
              sync();
            });
          conditionList.append(conditionFragment);
        });
        card.querySelector("[data-add-selection-condition]")
          .addEventListener("click", () => {
            if (rule.conditions.length >= 8) {
              return;
            }
            rule.conditions.push(newCondition());
            render();
            sync();
          });
        card.querySelector("[data-remove-selection-rule]")
          .addEventListener("click", () => {
            if (state.rules.length === 1) {
              return;
            }
            state.rules.splice(ruleIndex, 1);
            render();
            sync();
          });
        for (const move of card.querySelectorAll("[data-move-selection-rule]")) {
          move.addEventListener("click", () => {
            const targetIndex = move.dataset.moveSelectionRule === "up"
              ? ruleIndex - 1 : ruleIndex + 1;
            if (targetIndex < 0 || targetIndex >= state.rules.length) {
              return;
            }
            [state.rules[ruleIndex], state.rules[targetIndex]] =
              [state.rules[targetIndex], state.rules[ruleIndex]];
            render();
            sync();
          });
        }
        list.append(fragment);
      });
      if (
        state.otherwise_value &&
        !Array.from(otherwise.options).some(
          (option) => option.value === state.otherwise_value
        )
      ) {
        const unavailable = document.createElement("option");
        unavailable.value = state.otherwise_value;
        unavailable.textContent =
          `Unavailable in captured Odoo choices — ${state.otherwise_value}`;
        otherwise.append(unavailable);
      }
      otherwise.value = state.otherwise_value || "";
    };
    row.querySelector("[data-add-selection-rule]").addEventListener("click", () => {
      if (state.rules.length >= 20) {
        return;
      }
      state.rules.push(newRule());
      render();
      sync();
    });
    otherwise.addEventListener("change", () => {
      state.otherwise_value = otherwise.value || null;
      sync();
    });
    render();
    return {
      setActive(active) {
        control.hidden = !active;
        if (active && state.rules.length === 0) {
          state.rules.push(newRule());
          render();
          sync();
        }
      },
    };
  };

  const initializeConcatenationBuilder = (row, updateScalarRow) => {
    const control = row.querySelector("[data-provider-concatenation]");
    const list = row.querySelector("[data-concatenation-source-list]");
    if (!control || !list) {
      return null;
    }
    const rows = () => Array.from(
      list.querySelectorAll("[data-concatenation-source-row]")
    );
    let activeCount = Math.min(
      5,
      Math.max(2, Number.parseInt(list.dataset.activeCount || "2", 10) || 2)
    );
    let active = false;
    const selectedSnapshot = (select) => ({
      value: select?.value || "",
      option: select?.selectedOptions[0]?.cloneNode(true) || null,
    });
    const applySnapshot = (select, snapshot) => {
      if (!select) {
        return;
      }
      if (
        snapshot.value &&
        !Array.from(select.options).some((option) => option.value === snapshot.value) &&
        snapshot.option
      ) {
        select.append(snapshot.option.cloneNode(true));
      }
      select.value = snapshot.value;
    };
    const clearSelection = (select) => {
      if (select) {
        select.value = "";
      }
    };
    const refresh = () => {
      const currentRows = rows();
      currentRows.forEach((sourceRow, index) => {
        const visible = index < activeCount;
        sourceRow.hidden = !visible;
        const select = sourceRow.querySelector("[data-concatenation-source]");
        if (select) {
          select.disabled = !active || !visible;
        }
        const label = sourceRow.querySelector(
          "[data-concatenation-source-label]"
        );
        if (label) {
          label.textContent = `Part ${index + 1}`;
        }
        const moveUp = sourceRow.querySelector(
          '[data-move-concatenation-source="up"]'
        );
        const moveDown = sourceRow.querySelector(
          '[data-move-concatenation-source="down"]'
        );
        const remove = sourceRow.querySelector(
          "[data-remove-concatenation-source]"
        );
        if (moveUp) {
          moveUp.disabled = !active || !visible || index === 0;
        }
        if (moveDown) {
          moveDown.disabled = !active || !visible || index === activeCount - 1;
        }
        if (remove) {
          remove.disabled = !active || !visible || activeCount <= 2;
        }
      });
      const add = control.querySelector("[data-add-concatenation-source]");
      if (add) {
        add.disabled = !active || activeCount >= 5;
      }
      for (const field of control.querySelectorAll(
        "input:not([data-concatenation-source]), select:not([data-concatenation-source])"
      )) {
        field.disabled = !active;
      }
      const separator = control.querySelector("[data-concatenation-separator]");
      const custom = control.querySelector(
        "[data-concatenation-custom-separator]"
      );
      if (custom) {
        custom.hidden = separator?.value !== "custom";
      }
      list.dataset.activeCount = String(activeCount);
    };
    control.addEventListener("click", (event) => {
      const add = event.target.closest("[data-add-concatenation-source]");
      if (add && activeCount < 5) {
        activeCount += 1;
        refresh();
        updateScalarRow();
        return;
      }
      const sourceRow = event.target.closest("[data-concatenation-source-row]");
      if (!sourceRow) {
        return;
      }
      const currentRows = rows();
      const index = currentRows.indexOf(sourceRow);
      const move = event.target.closest("[data-move-concatenation-source]")
        ?.dataset.moveConcatenationSource;
      if (move) {
        const otherIndex = move === "up" ? index - 1 : index + 1;
        if (otherIndex >= 0 && otherIndex < activeCount) {
          const selected = sourceRow.querySelector(
            "[data-concatenation-source]"
          );
          const other = currentRows[otherIndex].querySelector(
            "[data-concatenation-source]"
          );
          const selectedState = selectedSnapshot(selected);
          const otherState = selectedSnapshot(other);
          applySnapshot(selected, otherState);
          applySnapshot(other, selectedState);
        }
      } else if (
        event.target.closest("[data-remove-concatenation-source]") &&
        activeCount > 2
      ) {
        for (let slot = index; slot < activeCount - 1; slot += 1) {
          const destination = currentRows[slot].querySelector(
            "[data-concatenation-source]"
          );
          const next = currentRows[slot + 1].querySelector(
            "[data-concatenation-source]"
          );
          applySnapshot(destination, selectedSnapshot(next));
        }
        clearSelection(
          currentRows[activeCount - 1].querySelector(
            "[data-concatenation-source]"
          )
        );
        activeCount -= 1;
      } else {
        return;
      }
      refresh();
      updateScalarRow();
    });
    control.addEventListener("change", refresh);
    refresh();
    return {
      setActive(nextActive) {
        active = nextActive;
        control.hidden = !active;
        refresh();
      },
      selectedParts() {
        return rows()
          .slice(0, activeCount)
          .map((sourceRow) => sourceRow.querySelector(
            "[data-concatenation-source]"
          ));
      },
      separator() {
        const choice = control.querySelector(
          "[data-concatenation-separator]"
        )?.value || "space";
        if (choice === "custom") {
          return control.querySelector(
            "[data-concatenation-custom-separator-input]"
          )?.value || "";
        }
        return {
          nothing: "",
          space: " ",
          comma_space: ", ",
          hyphen: "-",
        }[choice] ?? " ";
      },
    };
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
    const savedPreviewRaw = previewRaw?.textContent || "";
    const savedPreviewProposed = previewProposed?.textContent || "";
    let selectionRuleBuilder;
    let concatenationBuilder;
    const updateScalarRow = () => {
      const mode = provider?.value || "";
      const usesSource = ["source", "source_with_fallback"].includes(mode);
      const usesLiteral = ["constant", "source_with_fallback"].includes(mode);
      const usesSelectionRules = mode === "conditional_rules";
      const usesConcatenation = mode === "concatenate";
      if (sourceControl) {
        sourceControl.hidden = !usesSource;
      }
      if (literalControl) {
        literalControl.hidden = !usesLiteral;
      }
      concatenationBuilder?.setActive(usesConcatenation);
      selectionRuleBuilder?.setActive(mode === "conditional_rules");
      const selectionTransformNote = row.querySelector(
        "[data-selection-rules-transform-note]"
      );
      if (selectionTransformNote) {
        selectionTransformNote.hidden = !usesSelectionRules;
      }
      for (const control of row.querySelectorAll(
        ".transform-cell input, .transform-cell select, .transform-cell textarea, .transform-cell button"
      )) {
        control.disabled = usesSelectionRules;
      }
      const formulaControl = row.querySelector("[data-formula-control]");
      const formulaInput = row.querySelector("[data-rule-formula]");
      if (formulaControl) {
        formulaControl.hidden = usesConcatenation;
      }
      if (formulaInput) {
        formulaInput.disabled = usesSelectionRules || usesConcatenation;
      }
      if (canonicalType && usesConcatenation) {
        canonicalType.value = "string";
        canonicalType.disabled = true;
      }
      const valueMatch = row.querySelector("[data-open-value-match]");
      if (valueMatch) {
        valueMatch.hidden = !usesSource;
      }
      const categoricalPolicy = row.querySelector("[data-categorical-policy]");
      if (categoricalPolicy) {
        if (usesSelectionRules) {
          categoricalPolicy.value = "EXACT_TARGET_VALUE";
        }
        categoricalPolicy.disabled = usesSelectionRules;
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
      if (mode === "conditional_rules") {
        previewRaw.textContent = row.dataset.selectionRulesDirty === "true"
          ? "Uses one or more source columns" : savedPreviewRaw;
        previewProposed.textContent = row.dataset.selectionRulesDirty === "true"
          ? "Save progress to preview the first row" : savedPreviewProposed;
        return;
      }

      const selectedOption = source?.selectedOptions[0];
      let missing =
        usesSource && selectedOption?.dataset.samplePresent !== "true";
      let raw = mode === "constant" ? literal?.value ?? "" : selectedOption?.dataset.sample;
      if (usesConcatenation) {
        const partSelects = concatenationBuilder?.selectedParts() || [];
        const selectedKeys = partSelects.map((part) => part?.value || "");
        if (selectedKeys.some((key) => !key)) {
          previewRaw.textContent = "Choose every source column";
          previewProposed.textContent = "Complete the combined value";
          previewProposed.classList.add("preview-error");
          return;
        }
        if (new Set(selectedKeys).size !== selectedKeys.length) {
          previewRaw.textContent = "A source column is repeated";
          previewProposed.textContent = "Choose each source column once";
          previewProposed.classList.add("preview-error");
          return;
        }
        const rawParts = partSelects.map((part) => {
          const option = part?.selectedOptions[0];
          return option?.dataset.samplePresent === "true"
            ? String(option.dataset.sample ?? "")
            : null;
        });
        previewRaw.textContent = rawParts
          .map((part) => displayPreviewValue(part))
          .join(" | ");
        const trimParts = row.querySelector("[data-concatenation-trim]")
          ?.checked;
        const blankParts = rawParts.map(
          (part) => part === null || part.trim() === ""
        );
        if (
          row.querySelector("[data-concatenation-blank]")?.value === "block_row" &&
          blankParts.some(Boolean)
        ) {
          previewProposed.textContent =
            "A required source part for this combined value is blank";
          previewProposed.classList.add("preview-error");
          return;
        }
        const parts = rawParts
          .filter((_part, index) => !blankParts[index])
          .map((part) => trimParts ? part.trim() : part);
        raw = parts.length > 0
          ? parts.join(concatenationBuilder?.separator() || "")
          : null;
        missing = raw === null;
      } else {
        previewRaw.textContent = displayPreviewValue(
          usesSource ? selectedOption?.dataset.sample : raw
        );
      }
      try {
        let transformed = transformPreviewValue(row, raw, missing);
        if (mode === "source_with_fallback" && transformed === null) {
          raw = literal?.value ?? "";
          missing = false;
          transformed = transformPreviewValue(row, raw, missing);
        }
        const formula = row.querySelector("[data-rule-formula]")?.value.trim();
        if (formula && !usesConcatenation) {
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
    selectionRuleBuilder = initializeSelectionRuleBuilder(row, updateScalarRow);
    concatenationBuilder = initializeConcatenationBuilder(row, updateScalarRow);
    initializeTextStepBuilder(row, updateScalarRow);
    for (const control of row.querySelectorAll("select, input, textarea")) {
      control.addEventListener("change", updateScalarRow);
      control.addEventListener("input", updateScalarRow);
    }
    updateScalarRow();
  };

  for (const row of document.querySelectorAll("[data-scalar-mapping-row]")) {
    initializeScalarRow(row);
  }


  window.impodoMappingEditor.initializeScalarRow = initializeScalarRow;
});
