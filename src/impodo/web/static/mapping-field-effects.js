// Keep saved proof visibly stale while the operator edits its mapping.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-mapping-form]");
  if (!form) return;
  const markStale = (event) => {
    if (!/^(scalar_|relation_|relationship_|identity_|source_identity_|business_key_|target_model_|mode_|row_inclusion_)/.test(event.target.name || "")) return;
    form.querySelectorAll("[data-field-effects]").forEach((summary) => {
      summary.dataset.fieldEffectsState = "Needs refresh";
      summary.querySelector("[data-field-effects-status]").textContent = "Needs refresh";
      summary.querySelector("[data-field-effects-help]").hidden = false;
    });
  };
  form.addEventListener("input", markStale);
  form.addEventListener("change", markStale);
});
