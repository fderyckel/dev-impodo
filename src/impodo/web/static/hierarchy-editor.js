"use strict";

document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("[data-hierarchy-editor]")) {
    let loading = false;
    const field = (name) => form.elements.namedItem(name);
    const picker = () => form.querySelector("[data-hierarchy-picker]");
    const binding = () => JSON.stringify([
      "target_model", "target_name_field", "hierarchy_parent_field",
      "hierarchy_company_id", "existing_parent_search", "hierarchy_start",
    ].map((name) => field(name)?.value || ""));
    const feedback = (message) => {
      const container = form.querySelector("[data-hierarchy-feedback]");
      const paragraph = document.createElement("p");
      paragraph.setAttribute("role", "status");
      paragraph.textContent = message;
      container?.replaceChildren(paragraph);
      return container;
    };
    const sync = () => {
      const existing = field("hierarchy_start")?.value === "existing";
      picker().hidden = !existing && !Number(field("existing_source_levels")?.value);
      field("existing_parent_choice").required = existing
        && !Number(field("existing_source_levels")?.value);
      field("hierarchy_level_2").required = !existing;
      const path = [];
      const parent = field("existing_parent_choice");
      if (existing) path.push(parent.value
        ? parent.selectedOptions[0].textContent.trim() : "Choose an Odoo parent");
      for (let level = 1; level <= 5; level++) {
        const column = field(`hierarchy_level_${level}`);
        if (column?.value) path.push(column.selectedOptions[0].textContent.trim());
      }
      form.querySelector("[data-hierarchy-path]").textContent = path.length
        ? `Your hierarchy: ${path.join(" → ")}` : "Your hierarchy: choose the levels from your file.";
    };
    const load = async (button) => {
      if (loading || !button) return;
      const captured = binding();
      const body = new FormData(form);
      body.set(button.name, button.value);
      const buttons = Array.from(picker().querySelectorAll("[data-hierarchy-load]"));
      const disabled = buttons.map((item) => item.disabled);
      loading = true;
      for (const item of buttons) item.disabled = true;
      picker().setAttribute("aria-busy", "true");
      feedback(button.value === "companies" ? "Loading available companies…" : "Loading Odoo parents…");
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 45000);
      document.dispatchEvent(new CustomEvent("impodo:operation-started"));
      try {
        const response = await fetch(button.formAction, {
          method: "POST", body, credentials: "same-origin", signal: controller.signal,
          headers: { Accept: "text/html" },
        });
        window.impodoServerRecovery?.noteResponse?.(response);
        if (captured !== binding()) {
          feedback("Your company, search, or record type changed. Load the parents again.");
          return;
        }
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        const returned = Array.from(html.querySelectorAll("[data-hierarchy-editor]")).find(
          (candidate) => candidate.dataset.sourceSourceId === form.dataset.sourceSourceId
        );
        const replacement = returned?.querySelector("[data-hierarchy-picker]");
        if (!replacement || response.redirected || ![200, 422].includes(response.status)) {
          throw new Error("Parent lookup unavailable");
        }
        if (!response.ok) {
          const companies = replacement.querySelector('[name="hierarchy_company_id"]');
          if (companies?.options.length > 1 && field("hierarchy_company_id").options.length <= 1) {
            field("hierarchy_company_id").replaceWith(companies);
          }
          const message = replacement.querySelector("[data-hierarchy-feedback]");
          if (!message) throw new Error("Parent lookup feedback unavailable");
          form.querySelector("[data-hierarchy-feedback]").replaceWith(message);
          message.focus({ preventScroll: true });
          return;
        }
        // A company refresh must not discard the already loaded parent list.
        if (button.value === "companies") {
          replacement.querySelector('[name="existing_parent_choice"]').replaceWith(
            field("existing_parent_choice")
          );
        }
        picker().replaceWith(replacement);
        sync();
        form.dispatchEvent(new Event("change", { bubbles: true }));
        const target = field(button.value === "companies"
          ? "hierarchy_company_id" : "existing_parent_choice");
        target.focus({ preventScroll: true });
        target.scrollIntoView({ block: "nearest" });
      } catch {
        feedback("Odoo choices could not be loaded. Your entries are still here. Try again, or review Odoo access.");
      } finally {
        clearTimeout(timeout);
        loading = false;
        buttons.forEach((item, index) => { item.disabled = disabled[index]; });
        picker().removeAttribute("aria-busy");
        document.dispatchEvent(new CustomEvent("impodo:operation-finished"));
      }
    };
    form.addEventListener("submit", (event) => {
      if (loading) {
        event.preventDefault();
        return;
      }
      if (!event.submitter?.hasAttribute("data-hierarchy-load")) return;
      event.preventDefault();
      load(event.submitter);
    });
    form.addEventListener("change", (event) => {
      sync();
      if (["target_model", "target_name_field", "hierarchy_parent_field"].includes(event.target.name)) {
        field("existing_parent_choice").value = "";
        sync();
        feedback("The record type or parent relationship changed. Load the parents again.");
      }
      if (event.target.name === "hierarchy_company_id") {
        field("existing_parent_choice").value = "";
        sync();
        load(form.querySelector('[data-hierarchy-load][value="parents"]'));
      }
    });
    form.addEventListener("input", sync);
    form.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.target.name !== "existing_parent_search") return;
      event.preventDefault();
      load(form.querySelector('[data-hierarchy-load][value="parents"]'));
    });
    sync();
  }
});
