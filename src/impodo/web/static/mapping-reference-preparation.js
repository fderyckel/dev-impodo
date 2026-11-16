"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const initialize = (root) => {
    root.querySelectorAll("[data-reference-editor]").forEach((editor) => {
      if (editor.dataset.referenceInitialized) return;
      editor.dataset.referenceInitialized = "true";
      const mode = editor.querySelector("[data-reference-mode]");
      const list = editor.querySelector("[data-reference-part-list]");
      const storedParts = editor.querySelector("[data-reference-parts]");
      const update = () => {
        editor.querySelector("[data-reference-combine]").hidden = mode.value !== "combine";
        editor.querySelector("[data-reference-formula]").hidden = mode.value !== "formula";
        const parts = Array.from(list.querySelectorAll("[data-reference-part]"));
        storedParts.value = JSON.stringify(parts.map((part, index) => {
          const source = part.querySelector("[data-reference-part-kind]").value === "source";
          part.querySelector("[data-reference-part-source-label]").hidden = !source;
          part.querySelector("[data-reference-part-literal-label]").hidden = source;
          part.querySelector("[data-reference-move-up]").disabled = index === 0;
          part.querySelector("[data-reference-move-down]").disabled = index === parts.length - 1;
          return source ? {source_column_key: part.querySelector("[data-reference-part-source]").value}
            : {literal: part.querySelector("[data-reference-part-literal]").value};
        }));
        editor.querySelector("[data-reference-add-part]").disabled = parts.length >= 20;
      };
      editor.addEventListener("input", update);
      editor.addEventListener("change", update);
      editor.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.matches("[data-reference-add-part]") && list.children.length < 20) {
          list.append(editor.querySelector("[data-reference-part-template]").content.cloneNode(true));
        }
        const part = button.closest("[data-reference-part]");
        if (part && button.matches("[data-reference-remove-part]")) part.remove();
        if (part && button.matches("[data-reference-move-up]") && part.previousElementSibling) {
          list.insertBefore(part, part.previousElementSibling);
        }
        if (part && button.matches("[data-reference-move-down]") && part.nextElementSibling) {
          list.insertBefore(part.nextElementSibling, part);
        }
        update();
        storedParts.dispatchEvent(new Event("change", {bubbles: true}));
      });
      update();
    });
  };
  initialize(document);
  new MutationObserver((mutations) => {
    if (mutations.some((mutation) => mutation.addedNodes.length)) initialize(document);
  }).observe(document.querySelector("[data-mapping-form]") || document.body, {childList: true, subtree: true});
});
