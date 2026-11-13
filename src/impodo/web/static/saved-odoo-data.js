/* Keep bulk restore choices scoped to the Saved Odoo data form. */
(() => {
  const form = document.querySelector("[data-saved-odoo-restore]");
  if (!form) return;
  const allowedPolicies = new Set(["use_saved", "keep_destination", "fill_empty"]);
  const defaultPolicy = form.querySelector("[data-saved-odoo-default-policy]");
  const status = form.querySelector("[data-saved-odoo-policy-status]");
  const labels = {
    use_saved: "Use saved value",
    keep_destination: "Keep destination value",
    fill_empty: "Fill only when empty",
  };
  const applyPolicy = (policy) => {
    if (!allowedPolicies.has(policy)) return;
    if (defaultPolicy) defaultPolicy.value = policy;
    const fields = form.querySelectorAll("[data-saved-odoo-field-policy]");
    for (const select of fields) {
      select.value = policy;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (status) {
      status.textContent = `${labels[policy]} selected for all ${fields.length} fields. Review exceptions before restoring.`;
    }
  };
  for (const button of form.querySelectorAll("[data-saved-odoo-preset]")) {
    button.addEventListener("click", () => applyPolicy(button.dataset.savedOdooPreset));
  }
  defaultPolicy?.addEventListener("change", () => applyPolicy(defaultPolicy.value));

  const profile = form.querySelector("[data-saved-odoo-profile]");
  const version = form.querySelector("[data-saved-odoo-version]");
  const loadSettings = () => {
    const url = new URL(form.dataset.profileSelectionUrl, window.location.origin);
    if (url.origin !== window.location.origin) return;
    if (profile?.value) url.searchParams.set("profile", profile.value);
    if (version?.value) url.searchParams.set("version", version.value);
    window.location.assign(`${url.pathname}${url.search}#restore-saved-data`);
  };
  profile?.addEventListener("change", loadSettings);
  version?.addEventListener("change", loadSettings);
})();
