"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const hydrateSourceOptions = (select) => {
    if (select.dataset.sourceOptionsLoaded === "true") {
      return;
    }
    const template = select
      .closest(".mapping-dataset")
      ?.querySelector("template[data-source-column-options]");
    if (!(template instanceof HTMLTemplateElement)) {
      return;
    }
    const selectedValues = new Set(
      Array.from(select.selectedOptions, (option) => option.value)
    );
    const placeholder = Array.from(select.options).find(
      (option) => option.value === ""
    );
    select.replaceChildren();
    if (placeholder) {
      select.append(placeholder.cloneNode(true));
    }
    select.append(template.content.cloneNode(true));
    for (const option of select.options) {
      option.selected = selectedValues.has(option.value);
    }
    select.dataset.sourceOptionsLoaded = "true";
  };

  const releaseSourceOptions = (select) => {
    if (select.dataset.sourceOptionsLoaded !== "true") {
      return;
    }
    const selectedValues = new Set(
      Array.from(select.selectedOptions, (option) => option.value)
    );
    const retained = Array.from(select.options)
      .filter((option) => option.value === "" || option.selected)
      .map((option) => option.cloneNode(true));
    select.replaceChildren(...retained);
    for (const option of select.options) {
      const selected = selectedValues.has(option.value);
      option.selected = selected;
      option.defaultSelected = selected;
    }
    select.dataset.sourceOptionsLoaded = "false";
  };

  const initializeLazySourceSelect = (select) => {
    if (select.dataset.lazySourceInitialized === "true") {
      return;
    }
    select.dataset.lazySourceInitialized = "true";
    select.addEventListener("pointerdown", () => hydrateSourceOptions(select));
    select.addEventListener("focus", () => hydrateSourceOptions(select));
    select.addEventListener("blur", () => releaseSourceOptions(select));
  };

  for (const select of document.querySelectorAll(
    "select[data-lazy-source-column]"
  )) {
    initializeLazySourceSelect(select);
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-use-original-spelling]");
    if (!trigger) return;
    const row = trigger.closest(".identity-match-row");
    const select = row?.querySelector('select[name^="identity_source_"]');
    if (!(select instanceof HTMLSelectElement)) return;
    hydrateSourceOptions(select);
    const key = trigger.dataset.columnKey;
    if (!Array.from(select.options).some((option) => option.value === key)) return;
    select.value = key;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    row.querySelector("[data-original-spelling-warning]").hidden = true;
  });

  window.impodoMappingSourceColumns = {
    hydrateSourceOptions,
    releaseSourceOptions,
    initializeLazySourceSelect,
  };
});
