(() => {
  const filterLabel = (dialog) => dialog?.querySelector("[data-value-match-company-filter]");

  const reset = (dialog) => {
    const label = filterLabel(dialog);
    if (label) label.hidden = true;
  };

  const render = (dialog, trigger, choices, onChange) => {
    const label = filterLabel(dialog);
    const filter = label?.querySelector("select");
    const key = trigger.closest("[data-relation-mapping-row]")
      ?.querySelector('[name^="relation_key_"]')?.selectedOptions[0];
    const scopeFields = (key?.dataset.scopeFields || "").split("|");
    const companyIndex = scopeFields.indexOf("company_id");
    if (!filter || companyIndex < 0) return;

    // Match values supports one key field, followed by the scope fields.
    const companySlot = 1 + companyIndex;
    const companiesByValue = new Map();
    for (const { value } of choices) {
      try {
        const parts = JSON.parse(value);
        const company = Array.isArray(parts) ? parts[companySlot] : null;
        if (typeof company === "string" && company) {
          companiesByValue.set(String(value), company);
        }
      } catch {
        // Only choices with a structured name and company can be filtered.
      }
    }
    const companies = [...new Set(companiesByValue.values())]
      .sort((left, right) => left.localeCompare(right));
    filter.replaceChildren(
      new Option("All companies", ""),
      ...companies.map((company) => new Option(company, company))
    );
    label.hidden = companies.length === 0;
    filter.onchange = onChange;
    return {
      hasCompany: (value) => companiesByValue.has(value),
      excludes: (value) => Boolean(filter.value && companiesByValue.get(value) !== filter.value),
    };
  };

  window.impodoValueMatchCompany = { reset, render };
})();
