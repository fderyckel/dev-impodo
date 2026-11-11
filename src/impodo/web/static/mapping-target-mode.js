"use strict";

document.addEventListener("DOMContentLoaded", () => {
  for (const dataset of document.querySelectorAll("[data-mapping-dataset]")) {
    const mode = dataset.querySelector("[data-mapping-target-mode]");
    if (!mode) continue;

    const createPolicy = dataset.querySelector("[data-mapping-create-policy]");
    const updateSummary = dataset.querySelector("[data-mapping-update-summary]");
    const updateOnlySummary = dataset.querySelector("[data-mapping-update-only-summary]");
    const showMode = () => {
      if (createPolicy) createPolicy.hidden = mode.value !== "create";
      if (updateSummary) updateSummary.hidden = !["upsert", "update"].includes(mode.value);
      if (updateOnlySummary) updateOnlySummary.hidden = mode.value !== "update";
    };
    mode.addEventListener("change", showMode);
    showMode();
  }
});
