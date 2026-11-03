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

  const consumeAcceptedRefreshes = (stagedChoices, storage, activeMatch, operation) => {
    // Only a committed operation consumes candidates. Keep a newer refresh
    // staged while the save was in flight, including during receipt recovery.
    const raw = operation.entries?.find(([name]) => name === "selection_refreshes")?.[1];
    const accepted = new Set(JSON.parse(raw || "[]"));
    for (const [field, reference] of stagedChoices) {
      if (accepted.has(reference)) stagedChoices.delete(field);
    }
    if (activeMatch && accepted.has(activeMatch.selectionRefresh)) {
      activeMatch.selectionRefresh = null;
    }
    storage.value = JSON.stringify([...stagedChoices.values()]);
  };

  window.impodoSelectionChoices = { annotateUnavailable, freshnessText, consumeAcceptedRefreshes };
})();
