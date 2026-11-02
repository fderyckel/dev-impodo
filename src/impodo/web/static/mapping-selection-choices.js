/* Inline review context for refreshed scalar choices and missing saved links. */
(() => {
  const annotateUnavailable = (select, cell, savedTarget, payload, targetChoices, blankChoice) => {
    const previousChoice = (payload.previous_target_choices || []).find(
      (choice) => String(choice.value) === savedTarget
    );
    const previous = document.createElement("small");
    const label = previousChoice?.label;
    previous.textContent = `Previously ${label && label !== savedTarget ? `${label} — ` : ""}${savedTarget} · unavailable in Odoo`;
    previous.id = `unavailable-choice-${window.crypto.randomUUID()}`;
    previous.className = "muted";
    select.setAttribute("aria-describedby", previous.id);
    select.setAttribute("aria-invalid", "true");
    cell.append(previous);
    select.addEventListener("change", () => {
      const available = targetChoices.some((choice) => String(choice.value) === select.value) || select.value === blankChoice;
      select.setAttribute("aria-invalid", available ? "false" : "true");
      previous.hidden = available;
    });
  };

  const freshnessText = (language, payload, checkedLabel, empty) => {
    let text = `${language ? "Languages" : "Odoo choices"} checked ${checkedLabel}.`;
    if (payload.selection_refresh) {
      text += payload.selection_added || payload.selection_removed
        ? ` ${payload.selection_added} added · ${payload.selection_removed} removed. Save and check matches to use this list.`
        : " Up to date.";
    }
    if (empty) text += ` No ${language ? "languages" : "choices"} are available in Odoo. Enable them in Odoo, then refresh.`;
    return text;
  };

  window.impodoSelectionChoices = { annotateUnavailable, freshnessText };
})();
