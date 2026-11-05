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
  let conflicts = [];
  let failureMessage = "";

  const conflictLink = (item) => {
    const link = document.createElement("a");
    link.className = "button secondary";
    link.textContent = "Review this field";
    link.href = item.fix_url;
    link.dataset.reviewRelatedField = "";
    link.dataset.reviewAnchor = item.anchor;
    link.dataset.datasetId = item.dataset_id;
    return link;
  };
  const showConflicts = () => {
    if (!mappingConflict) return;
    const journey = document.querySelector("[data-mapping-journey-state]");
    if (journey) {
      journey.textContent = "Linked fields need attention";
      journey.className = "status review";
    }
    const list = host.querySelector("[data-related-conflicts]");
    if (list && conflicts.length) {
      list.replaceChildren(...conflicts.map((item) => {
        const entry = document.createElement("li");
        const title = document.createElement("strong");
        title.textContent = item.title;
        const message = document.createElement("p");
        message.textContent = item.message;
        entry.append(title, message, conflictLink(item));
        return entry;
      }));
    } else if (list && !list.children.length && failureMessage) {
      showError(failureMessage);
    }
    const mapping = document.querySelector("[data-mapping-form]");
    if (!mapping) return;
    let blocker = mapping.querySelector("#next-step-blockers");
    if (!blocker) {
      blocker = document.createElement("section");
      blocker.id = "next-step-blockers";
      blocker.className = "notice warning mapping-next-step-blockers";
      const checkPanel = mapping.querySelector('[data-mapping-step-panel="check"]');
      if (checkPanel) checkPanel.append(blocker);
      else mapping.querySelector(".mapping-actions")?.before(blocker);
    }
    const title = document.createElement("strong");
    title.textContent = "Correct linked fields before checking again";
    const fields = document.createElement("p");
    fields.textContent = conflicts.map((item) => item.title).join("; ");
    const link = document.createElement("a");
    link.className = "button secondary";
    link.href = "#related-data-review";
    link.textContent = "Review affected fields";
    blocker.replaceChildren(title, fields, link);
    mapping.querySelector("[data-confirm-mapping]")?.setAttribute("disabled", "");
  };

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
    window.impodoMappingJourney?.reveal?.(section);
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
      showConflicts();
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
    conflicts = Array.isArray(event.detail.conflicts) ? event.detail.conflicts : [];
    failureMessage = event.detail.message || "";
    if (mappingConflict) host.querySelector("[data-related-review-form]")?.setAttribute("hidden", "");
    void reload();
  });
  host.addEventListener("click", (event) => {
    if (event.target.closest("[data-reload-related-review]")) void reload();
    const link = event.target.closest("[data-review-related-field]");
    if (link) {
      const target = document.getElementById(link.dataset.reviewAnchor);
      const dataset = Array.from(document.querySelectorAll("[data-mapping-dataset]"))
        .find((item) => item.dataset.mappingDataset === link.dataset.datasetId);
      const catalog = dataset?.querySelector("[data-relation-field-catalog]");
      if (!target && catalog) {
        event.preventDefault();
        catalog.dispatchEvent(new CustomEvent("impodo:focus-linked-field", {
          detail: { url: link.href, targetId: link.dataset.reviewAnchor },
        }));
        return;
      }
      if (!target) {
        if (document.querySelector("[data-mapping-save-status]")?.classList.contains("unsaved")) {
          event.preventDefault();
          showError("Save progress before opening another table.");
        }
        return;
      }
      event.preventDefault();
      window.impodoMappingJourney?.reveal?.(target);
      for (let parent = target; parent; parent = parent.parentElement) {
        if (parent instanceof HTMLDetailsElement) parent.open = true;
      }
      target.scrollIntoView({ block: "center" });
      target.setAttribute("tabindex", "-1");
      (target.querySelector("select:not([disabled]), input:not([type='hidden']):not([disabled])") || target).focus({ preventScroll: true });
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
