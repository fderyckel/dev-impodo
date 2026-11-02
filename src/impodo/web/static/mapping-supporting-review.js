"use strict";

// Recovery replaces only related-data controls. The mapping editor and its
// operation receipts continue to own field edits and Check matches.
document.addEventListener("DOMContentLoaded", () => {
  const host = document.querySelector("[data-related-review-host]");
  if (!host) return;
  let loading = false;
  let saving = false;
  let operationId = "";
  let mappingConflict = false;

  const panel = () => host.querySelector("[data-related-data-review]");
  const showError = (message) => {
    const error = host.querySelector("[data-related-review-error]");
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
  };
  const reveal = () => {
    const section = panel();
    if (!section) return;
    section.hidden = false;
    const details = section.querySelector("[data-related-review-details]");
    if (details) details.open = true;
    const reference = section.querySelector("[data-related-review-reference]");
    if (reference) {
      reference.textContent = operationId ? `Support reference: ${operationId}` : "";
      reference.hidden = !operationId;
    }
    section.focus();
    section.scrollIntoView({ block: "start" });
  };
  const reload = async () => {
    if (loading || saving) return;
    loading = true;
    try {
      const url = new URL(host.dataset.relatedReviewUrl, window.location.href);
      if (mappingConflict) url.searchParams.set("problem", "MAPPING_CONFLICT");
      const response = await fetch(url, {
        headers: { Accept: "text/html" },
        cache: "no-store",
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error("Review unavailable");
      const html = await response.text();
      const template = document.createElement("template");
      template.innerHTML = html;
      if (!template.content.querySelector("[data-related-data-review]")) {
        throw new Error("Review unavailable");
      }
      host.replaceChildren(template.content);
      reveal();
    } catch (_error) {
      reveal();
      showError("Impodo could not open the current choices. Keep this page open and retry Reload choices.");
    } finally {
      loading = false;
    }
  };

  document.addEventListener("impodo:related-review-required", (event) => {
    event.preventDefault();
    operationId = event.detail.operationId || "";
    mappingConflict = Boolean(event.detail.mappingConflict);
    void reload();
  });
  host.addEventListener("click", (event) => {
    if (event.target.closest("[data-reload-related-review]")) void reload();
    if (event.target.closest("[data-review-related-mappings]")) {
      const target = document.querySelector('[data-relation-mapping-row][data-target-field="company_ids"]')
        || document.querySelector("[data-relation-field-catalog]");
      if (!target) return;
      for (let parent = target; parent; parent = parent.parentElement) {
        if (parent instanceof HTMLDetailsElement) parent.open = true;
      }
      target.scrollIntoView({ block: "center" });
      target.querySelector("select, input")?.focus();
    }
  });
  host.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-related-review-form]");
    if (!form) return;
    event.preventDefault();
    const mapping = document.querySelector("[data-mapping-form]");
    if (loading || saving || mapping?.getAttribute("aria-busy") === "true") return;
    const data = new FormData(form);
    const controls = Array.from(form.elements).filter((control) => !control.disabled);
    saving = true;
    form.setAttribute("aria-busy", "true");
    controls.forEach((control) => { control.disabled = true; });
    const error = host.querySelector("[data-related-review-error]");
    if (error) error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: data,
        signal: AbortSignal.timeout(15000),
      });
      const payload = await response.json();
      if (!response.ok) {
        showError(payload.detail || "Choices could not be saved. Review them and try again.");
        return;
      }
      // Only a confirmed save may start the normal check. It reads the current
      // mapping form, including edits that have not yet been saved.
      if (!Number.isInteger(payload.version) || !payload.binding_hash) {
        throw new Error("Save not confirmed");
      }
      form.elements.namedItem("expected_version").value = String(payload.version);
      form.elements.namedItem("binding_hash").value = payload.binding_hash;
      form.elements.namedItem("operation_id").value = window.crypto.randomUUID();
      const check = mapping?.querySelector("[data-check-mapping]");
      if (check && !check.disabled) {
        panel().hidden = true;
        mapping.requestSubmit(check);
      } else {
        showError("Related-data choices saved. Correct the field issues, then choose Check matches.");
      }
    } catch (_error) {
      showError("The choices save was not confirmed. Retry these same choices before checking matches, or reload choices to review the saved state. Your field edits remain on this page.");
    } finally {
      saving = false;
      form.removeAttribute("aria-busy");
      controls.forEach((control) => { control.disabled = false; });
    }
  });
});
