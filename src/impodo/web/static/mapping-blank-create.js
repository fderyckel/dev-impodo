"use strict";

(() => {
  const checkedDefault = JSON.stringify({default: true});

  const bind = (dialog, getActive, reload) => {
    const panel = dialog?.querySelector("[data-blank-create-panel]");
    if (!panel) return null;
    const select = panel.querySelector("[data-blank-create-choice]");
    const count = panel.querySelector("[data-blank-create-count]");
    const check = panel.querySelector("[data-check-blank-create-default]");
    check.addEventListener("click", () => {
      const active = getActive();
      if (active) void reload(active.trigger, {checkBlankDefault: true});
    });

    return {
      reset() {
        panel.hidden = true;
        select.replaceChildren();
      },
      value: () => select.value,
      state(row, key, pending, requestDefault) {
        const origin = row.querySelector('[name^="relation_origin_"]')?.value;
        const required = row.querySelector('[name^="relation_required_"]:not([name^="relation_required_create_"]):checked');
        const validateOnly = row.querySelector('[name^="relation_validate_only_"]:checked');
        const storage = row.querySelector("[data-blank-create-storage]");
        if (origin !== "target_catalog" || required || validateOnly || !storage) return null;
        return {storage, key, pending, requestDefault};
      },
      render(payload, active, label, onChange) {
        const state = active.blankCreate;
        if (!state) return;
        let saved = state.pending ?? state.storage.value;
        if (saved) saved = JSON.stringify(JSON.parse(saved));
        const rows = Number(payload.blank_source_count || 0);
        panel.hidden = rows === 0 && !saved;
        if (panel.hidden) return;
        count.textContent = `${rows.toLocaleString()} rows in the saved scope have a blank value. This choice applies when a row creates a record.`;
        check.hidden = rows === 0;
        select.replaceChildren(new Option("No fallback selected", ""));
        if (payload.blank_odoo_default_verified) {
          select.add(new Option("Use the checked Odoo default", checkedDefault));
          if (state.requestDefault) saved = checkedDefault;
        }
        for (const choice of payload.target_choices || []) {
          select.add(new Option(label(choice), JSON.stringify({key: state.key, record: String(choice.value)})));
        }
        if (saved && !Array.from(select.options).some((option) => option.value === saved)) {
          select.add(new Option("Saved fallback needs review", saved));
        }
        select.value = saved;
        select.onchange = onChange;
      },
      hasChoice(active) {
        return !panel.hidden && Boolean(select.value || active.blankCreate?.storage.value);
      },
      save(active) {
        if (!panel.hidden && active.blankCreate) active.blankCreate.storage.value = select.value;
      },
    };
  };

  window.impodoBlankCreate = {bind};
})();
