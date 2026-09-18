"use strict";

document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("[data-preflight-compare]")) {
    form.addEventListener("submit", (event) => {
      if (form.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
      }
      form.setAttribute("aria-busy", "true");
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-preflight-comparison-status]");
      if (button) {
        button.disabled = true;
        button.textContent = "Comparing with Odoo...";
      }
      if (status) {
        status.hidden = false;
      }
    });
  }

  for (const button of document.querySelectorAll("[data-copy-missing-parent-values]")) {
    button.addEventListener("click", async () => {
      const source = button.closest("article")?.querySelector(
        "[data-missing-parent-copy-values]"
      );
      if (!source) return;
      try {
        await navigator.clipboard.writeText(source.value);
        button.textContent = "Copied exact values";
      } catch (_error) {
        source.hidden = false;
        source.select();
        button.textContent = "Select and copy the values below";
      }
    });
  }

});
