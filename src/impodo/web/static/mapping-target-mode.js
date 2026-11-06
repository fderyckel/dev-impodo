"use strict";

document.addEventListener("DOMContentLoaded", () => {
  for (const dataset of document.querySelectorAll("[data-mapping-dataset]")) {
    const mode = dataset.querySelector("[data-mapping-target-mode]");
    if (!mode) continue;

    const createPolicy = dataset.querySelector("[data-mapping-create-policy]");
    const updateSummary = dataset.querySelector("[data-mapping-update-summary]");
    const showMode = () => {
      if (createPolicy) createPolicy.hidden = mode.value !== "create";
      if (updateSummary) updateSummary.hidden = mode.value !== "upsert";
    };
    mode.addEventListener("change", showMode);
    showMode();
  }
});
