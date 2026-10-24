"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const revealTarget = (id) => {
    const target = document.getElementById(id);
    if (!target) {
      return;
    }
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) {
        parent.open = true;
      }
    }
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  };

  const revealCurrentTarget = () => {
    let id = "";
    if (window.location.hash.length > 1) {
      try {
        id = decodeURIComponent(window.location.hash.slice(1));
      } catch (_error) {
        id = "";
      }
    }
    const preview = document.querySelector("[data-derived-entity-preview]");
    revealTarget(id || preview?.id);
  };

  for (const trigger of document.querySelectorAll("[data-derived-entity-target]")) {
    trigger.addEventListener("click", () => {
      revealTarget(trigger.dataset.derivedEntityTarget);
    });
  }

  for (const form of document.querySelectorAll("[data-exact-join-form]")) {
    const inputs = {
      left: form.querySelector('[data-exact-join-input="left"]'),
      right: form.querySelector('[data-exact-join-input="right"]'),
    };
    const grain = form.querySelector("#exact-join-grain");

    const syncExactJoin = () => {
      const selectedIds = new Set([inputs.left?.value, inputs.right?.value].filter(Boolean));
      const duplicate = Boolean(
        inputs.left?.value && inputs.left.value === inputs.right?.value
      );
      inputs.right?.setCustomValidity(
        duplicate ? "Choose two different source datasets." : ""
      );

      for (const side of ["left", "right"]) {
        const datasetId = inputs[side]?.value || "";
        for (const select of form.querySelectorAll(`[data-exact-key-side="${side}"]`)) {
          for (const option of select.querySelectorAll("option[data-dataset-id]")) {
            const available = option.dataset.datasetId === datasetId;
            option.hidden = !available;
            option.disabled = !available;
          }
          if (
            select.selectedOptions[0]?.dataset.datasetId &&
            select.selectedOptions[0].dataset.datasetId !== datasetId
          ) {
            select.value = "";
          }
        }

        const selectedOption = inputs[side]?.selectedOptions[0];
        const useChoice = form.querySelector(`[data-exact-use-side="${side}"]`);
        if (useChoice) {
          const physical = selectedOption?.dataset.physical === "true";
          useChoice.hidden = !physical;
          const useSelect = useChoice.querySelector("select");
          if (useSelect) {
            useSelect.disabled = !physical;
          }
        }
      }

      for (const group of form.querySelectorAll("[data-exact-projection-dataset]")) {
        const visible = selectedIds.has(group.dataset.exactProjectionDataset);
        group.hidden = !visible;
        if (!visible) {
          for (const checkbox of group.querySelectorAll('input[name="projection"]')) {
            checkbox.checked = false;
          }
        }
      }

      if (grain && inputs.left?.selectedOptions[0]?.value) {
        grain.textContent = `One output row will represent one ${inputs.left.selectedOptions[0].textContent.trim()} row.`;
      }
    };

    inputs.left?.addEventListener("change", syncExactJoin);
    inputs.right?.addEventListener("change", syncExactJoin);
    syncExactJoin();
  }

  window.addEventListener("hashchange", revealCurrentTarget);
  window.addEventListener("pageshow", revealCurrentTarget);
  revealCurrentTarget();
});
