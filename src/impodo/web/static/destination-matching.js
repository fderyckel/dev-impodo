"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const builder = document.querySelector("[data-matching-builder]");
  if (builder) {
    builder.classList.add("is-enhanced");

    for (const card of builder.querySelectorAll("[data-matching-model]")) {
      const fields = [...card.querySelectorAll("[data-match-field]")];
      const optionalFields = [
        ...card.querySelectorAll("[data-optional-match-field]"),
      ];
      const summary = card.querySelector("[data-identity-summary]");
      const addButton = card.querySelector("[data-add-match-field]");

      const optionLabel = (select) => {
        const label = select.selectedOptions[0]?.textContent?.trim() || "";
        return label.replace(/\s+\([^()]+\)$/, "");
      };

      const updateSummary = () => {
        if (!summary) {
          return;
        }
        const selected = fields.filter((field) => field.value);
        summary.replaceChildren();
        selected.forEach((field, index) => {
          if (index > 0) {
            const plus = document.createElement("span");
            plus.className = "identity-plus";
            plus.textContent = "+";
            summary.append(plus);
          }
          const pill = document.createElement("span");
          pill.className = "identity-pill";
          pill.textContent = optionLabel(field);
          summary.append(pill);
        });
      };

      const updateAddButton = () => {
        if (!addButton) {
          return;
        }
        const hasCollapsed = optionalFields.some((field) =>
          field.classList.contains("is-collapsed")
        );
        addButton.hidden = !hasCollapsed;
      };

      optionalFields.forEach((field) => {
        const select = field.querySelector("select");
        field.classList.toggle("is-collapsed", !select?.value);
        field
          .querySelector("[data-remove-match-field]")
          ?.addEventListener("click", () => {
            if (select) {
              select.value = "";
              select.dispatchEvent(new Event("change", { bubbles: true }));
            }
            field.classList.add("is-collapsed");
            updateAddButton();
          });
      });

      addButton?.addEventListener("click", () => {
        const next = optionalFields.find((field) =>
          field.classList.contains("is-collapsed")
        );
        if (!next) {
          return;
        }
        next.classList.remove("is-collapsed");
        next.querySelector("select")?.focus();
        updateAddButton();
      });

      fields.forEach((field) => field.addEventListener("change", updateSummary));
      updateSummary();
      updateAddButton();
    }
  }

  for (const form of document.querySelectorAll("[data-create-field-form]")) {
    const provider = form.querySelector("[data-provider-select]");
    if (!provider) {
      continue;
    }
    form.classList.add("is-enhanced");
    const controls = [...form.querySelectorAll("[data-provider-control]")];
    const updateControls = () => {
      for (const control of controls) {
        control.hidden = control.dataset.providerControl !== provider.value;
      }
    };
    provider.addEventListener("change", updateControls);
    updateControls();
  }
});
