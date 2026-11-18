/* Keep the next-action highlight aligned with the saved load company. */
"use strict";

(() => {
  const form = document.querySelector("[data-company-choice-form]");
  const company = form?.querySelector('select[name="company_id"]');
  if (!form || !company) return;
  const savedCompany = form.dataset.savedCompanyId;
  const update = () => {
    form.toggleAttribute("data-next-dirty", company.value !== savedCompany);
  };
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.addEventListener("reset", () => window.queueMicrotask(update));
  update();
})();
