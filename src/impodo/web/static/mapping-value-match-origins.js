"use strict";

(() => {
  const create = (dialog, payload, targetValues, blankChoice) => {
    const table = payload.incoming_table;
    const heading = dialog.querySelector("[data-value-match-choice-heading]");
    const originHeading = dialog.querySelector("[data-value-match-origin-heading]");
    const notice = dialog.querySelector("[data-value-match-incoming-notice]");
    if (heading) heading.textContent = table ? "Matched choice" : "Odoo choice";
    if (originHeading) originHeading.hidden = !table;
    if (notice) {
      notice.hidden = !table?.message;
      notice.textContent = table?.message || "";
    }
    if (!table) return null;
    const choices = new Map((payload.relationship_resolutions || [])
      .map((choice) => [choice.source_value, choice]));
    const cells = new Map();
    const outcome = (select) => {
      if (select.value === blankChoice) return "blank";
      if (select.value) return targetValues.has(select.value) ? "target" : "review";
      return choices.get(select.dataset.sourceValue)?.status || "unavailable";
    };
    const labels = {
      target: "Odoo", incoming: table.name, blank: "Leave blank",
      missing: "Not found in Odoo or the incoming table",
      ambiguous: "Matches more than one record",
      case_only: "Capitalization needs review",
      unavailable: "Incoming match not checked", review: "Saved Odoo choice needs review",
    };
    return {
      suggestion(value) { return choices.get(value)?.target_value || ""; },
      attach(row, sourceValue, select) {
        const cell = document.createElement("td");
        cell.dataset.valueMatchOrigin = sourceValue;
        cells.set(sourceValue, cell);
        const choice = choices.get(sourceValue);
        if (choice?.status === "incoming") {
          select.options[0].textContent = `${sourceValue} (from ${table.name})`;
        } else if (choice?.status === "target") {
          select.options[0].textContent = `${choice.target_value} (from Odoo)`;
        }
        row.append(cell);
      },
      progress(selects) {
        let matched = 0, target = 0, incoming = 0;
        for (const select of selects) {
          const status = outcome(select);
          if (["target", "incoming", "blank"].includes(status)) matched += 1;
          if (status === "target") target += 1;
          if (status === "incoming") incoming += 1;
          const cell = cells.get(select.dataset.sourceValue);
          if (cell) {
            cell.textContent = labels[status];
            cell.dataset.matchOrigin = status;
          }
        }
        return { matched, text: `${matched} of ${selects.length} matched · ${target} in Odoo · ${incoming} from ${table.name}` };
      },
    };
  };
  window.impodoValueMatchOrigins = { create };
})();
