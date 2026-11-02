/* Refresh selection choices in place while retaining unsubmitted run decisions. */
(() => {
  let pending = false;
  document.addEventListener("click", async (event) => {
    const button = event.target.closest?.("[data-refresh-selection]");
    const form = button?.closest(".target-match-form");
    if (!button || !form || pending) return;
    pending = true;
    const selections = new Map([...form.querySelectorAll("select[name]")]
      .map((select) => [select.name, select.value]));
    const status = form.querySelector("[data-selection-refresh-status]");
    const label = button.textContent;
    for (const control of form.querySelectorAll("button, select")) control.disabled = true;
    button.textContent = "Refreshing…";
    const body = new FormData();
    body.set("csrf_token", form.querySelector('[name="csrf_token"]').value);
    body.set("target_model", button.dataset.targetModel);
    body.set("target_field", button.dataset.targetField);
    body.set("selection_refreshes", form.querySelector('[name="selection_refreshes"]').value);
    for (const name of ["expected_definition_hash", "expected_working_draft_version", "expected_evidence_hash"]) {
      body.set(name, form.querySelector(`[name="${name}"]`).value);
    }
    for (const [name, value] of selections) body.set(name, value);
    try {
      const response = await fetch(button.dataset.refreshUrl, { method: "POST", body });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "The choices could not be refreshed. Try again.");
      }
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = page.querySelector(".target-match-form");
      if (!replacement || !form.isConnected) return;
      for (const select of replacement.querySelectorAll("select[name]")) {
        const previous = selections.get(select.name);
        if (previous && [...select.options].some((option) => option.value === previous)) {
          select.value = previous;
        }
      }
      form.replaceWith(replacement);
      const summary = document.querySelector(".target-match-summary");
      const updatedSummary = page.querySelector(".target-match-summary");
      if (summary && updatedSummary) summary.replaceWith(updatedSummary);
      const refreshedStatus = replacement.querySelector("[data-selection-refresh-status]");
      refreshedStatus.hidden = false;
      refreshedStatus.textContent = "Choices refreshed. Review unavailable values, then save matches to use this list.";
      const next = [...replacement.querySelectorAll("[data-refresh-selection]")].find((item) =>
        item.dataset.targetModel === button.dataset.targetModel && item.dataset.targetField === button.dataset.targetField);
      next?.focus();
    } catch (error) {
      if (status) {
        status.hidden = false;
        status.textContent = error.message || "The choices could not be refreshed. Try again.";
      }
    } finally {
      pending = false;
      if (form.isConnected) {
        for (const control of form.querySelectorAll("button, select")) control.disabled = false;
        button.textContent = label;
      }
    }
  });
})();
