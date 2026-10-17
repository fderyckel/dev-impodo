"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const loadConfirmationForm = document.querySelector(
    "[data-load-confirmation-form]"
  );
  loadConfirmationForm?.addEventListener("submit", (event) => {
    if (loadConfirmationForm.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }
    loadConfirmationForm.setAttribute("aria-busy", "true");
    const button = loadConfirmationForm.querySelector('button[type="submit"]');
    const status = loadConfirmationForm.querySelector(
      "[data-load-confirmation-status]"
    );
    if (button) {
      button.disabled = true;
      button.textContent = "Loading into Odoo...";
    }
    if (status) {
      status.hidden = false;
    }
  });

  document.querySelectorAll("[data-recovery-action-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
      }
      form.setAttribute("aria-busy", "true");
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-recovery-action-status]");
      if (button) {
        button.disabled = true;
        button.textContent = button.dataset.busyLabel || "Working...";
      }
      if (status) {
        status.hidden = false;
      }
    });
  });

});
