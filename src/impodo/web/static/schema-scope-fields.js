/* Labelled scope selection, search and removal; schema.js owns the matching-rule draft. */
(() => {
  const bind = (decision, input, changed) => {
    const controls = [...decision.querySelectorAll("[data-scope-field]")];
    if (!controls.length || !input) return null;
    const summary = decision.querySelector("[data-key-scope-selection]");
    const selected = decision.querySelector("[data-scope-selected]");
    const none = decision.querySelector("[data-scope-none]");
    const clear = decision.querySelector("[data-scope-clear]");
    const search = decision.querySelector("[data-scope-search]");
    const searchTools = decision.querySelector("[data-scope-search-tools]");
    const noResults = decision.querySelector("[data-scope-no-results]");
    const disclosure = decision.querySelector("[data-scope-disclosure]");
    const byField = new Map(controls.map((control) => [control.value, control]));
    const labelFor = (field) => byField.get(field)?.dataset.fieldLabel || field;
    let fields = input.value.split(",").map((value) => value.trim()).filter(Boolean);

    const renderSelected = () => {
      if (!selected) return;
      const document = selected.ownerDocument;
      selected.replaceChildren(...fields.map((field) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "key-scope-chip";
        button.dataset.scopeRemove = field;
        button.setAttribute("aria-label", `Remove ${labelFor(field)} from scope`);
        const label = document.createElement("span");
        label.textContent = labelFor(field);
        const remove = document.createElement("span");
        remove.textContent = "×";
        remove.setAttribute("aria-hidden", "true");
        button.append(label, remove);
        button.addEventListener("click", () => {
          const control = byField.get(field);
          if (control) control.checked = false;
          refresh();
          changed();
          summary?.focus();
        });
        return button;
      }));
    };
    const refresh = () => {
      const checked = controls.filter((control) => control.checked);
      fields = fields.filter((field) => checked.some((control) => control.value === field));
      for (const control of checked) {
        if (!fields.includes(control.value)) fields.push(control.value);
      }
      input.value = fields.join(", ");
      if (none) none.hidden = fields.length > 0;
      if (clear) clear.hidden = !fields.length;
      for (const control of controls) {
        control.closest("[data-scope-option]")?.classList.toggle("is-selected", control.checked);
      }
      renderSelected();
    };
    const filter = () => {
      const query = (search?.value || "").trim().toLowerCase();
      let visible = 0;
      for (const control of controls) {
        const row = control.closest("[data-scope-option]");
        if (!row) continue;
        row.hidden = !`${labelFor(control.value)} ${control.value}`.toLowerCase().includes(query);
        if (!row.hidden) visible += 1;
      }
      if (noResults) noResults.hidden = visible > 0;
    };
    for (const control of controls) {
      control.addEventListener("change", () => {
        refresh();
        changed();
      });
    }
    clear?.addEventListener("click", () => {
      for (const control of controls) control.checked = false;
      refresh();
      changed();
      summary?.focus();
    });
    search?.addEventListener("input", filter);
    search?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
    });
    disclosure?.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      disclosure.open = false;
      summary?.focus();
    });
    if (searchTools) searchTools.hidden = false;
    refresh();
    return {
      controls,
      setFields(values) {
        fields = [...values];
        for (const control of controls) control.checked = fields.includes(control.value);
        refresh();
      },
      setMatchingFields(values) {
        for (const control of controls) {
          control.disabled = values.includes(control.value) && !control.checked;
          control.closest("[data-scope-option]")?.classList.toggle("is-disabled", control.disabled);
        }
      },
    };
  };
  window.ImpodoSchemaScopes = { bind };
})();
