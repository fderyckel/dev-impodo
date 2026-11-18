/* Confirm optional source-rule removal through its existing guarded form. */
"use strict";

(() => {
  const dialog = document.querySelector("[data-source-remove-dialog]");
  if (!dialog) return;
  const title = dialog.querySelector("[data-source-remove-title]");
  const name = dialog.querySelector("[data-source-remove-name]");
  const confirm = dialog.querySelector("[data-source-remove-confirm]");
  const label = confirm.querySelector("[data-source-remove-confirm-label]");
  let pending = null;
  let trigger = null;
  let confirmedForm = null;

  for (const form of document.querySelectorAll("[data-source-remove-form]")) {
    form.addEventListener("submit", (event) => {
      if (form === confirmedForm || event.defaultPrevented) return;
      event.preventDefault();
      pending = form;
      trigger = event.submitter || form.querySelector('button[type="submit"]');
      title.textContent = `${form.dataset.sourceRemoveLabel}?`;
      name.textContent = form.dataset.sourceRemoveName;
      label.textContent = form.dataset.sourceRemoveLabel;
      dialog.showModal();
    });
  }

  dialog.addEventListener("close", () => {
    pending = null;
    if (trigger?.isConnected) trigger.focus();
    trigger = null;
  });
  confirm.addEventListener("click", () => {
    if (!pending) return;
    const form = pending;
    pending = null;
    dialog.close();
    confirmedForm = form;
    try {
      form.requestSubmit();
    } finally {
      confirmedForm = null;
    }
  });
})();
