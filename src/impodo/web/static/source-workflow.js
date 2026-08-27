"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const sourceFileRemoveDialog = document.querySelector(
    "[data-source-file-remove-dialog]"
  );
  const sourceFileRemoveTitle = sourceFileRemoveDialog?.querySelector(
    "#source-file-remove-title"
  );
  const sourceFileRemoveConfirm = sourceFileRemoveDialog?.querySelector(
    "[data-source-file-remove-confirm]"
  );
  let pendingSourceFileRemoveForm = null;

  for (const trigger of document.querySelectorAll(
    "[data-source-file-remove-trigger]"
  )) {
    trigger.addEventListener("click", () => {
      const form = trigger.closest("[data-source-file-remove-form]");
      if (!form || !sourceFileRemoveDialog) {
        return;
      }
      pendingSourceFileRemoveForm = form;
      if (sourceFileRemoveTitle) {
        const fileName = form.dataset.sourceFileName || "this file";
        sourceFileRemoveTitle.textContent = `Remove “${fileName}”?`;
      }
      sourceFileRemoveDialog.showModal();
    });
  }

  sourceFileRemoveConfirm?.addEventListener("click", () => {
    const form = pendingSourceFileRemoveForm;
    pendingSourceFileRemoveForm = null;
    sourceFileRemoveDialog?.close();
    form?.requestSubmit();
  });

  sourceFileRemoveDialog?.addEventListener("close", () => {
    pendingSourceFileRemoveForm = null;
  });

  const sourceReviewPage = document.querySelector(
    "[data-source-review-page]"
  );
  const sourceReviewPositionKey = sourceReviewPage
    ? `impodo.source.position:${sourceReviewPage.dataset.sourcePositionKey}`
    : "";
  const rememberSourceReviewPosition = (form) => {
    if (!sourceReviewPositionKey) {
      return;
    }
    const active = document.activeElement;
    const card = form.closest("[data-source-review-card]");
    const horizontal = Array.from(
      card?.querySelectorAll(".table-scroll") || []
    ).map((element) => element.scrollLeft);
    try {
      window.sessionStorage.setItem(
        sourceReviewPositionKey,
        JSON.stringify({
          scrollY: window.scrollY,
          fileId: card?.dataset.sourceReviewCard || "",
          cardOffset: card?.getBoundingClientRect().top ?? null,
          focusName: active?.name || "",
          focusValue: active?.value || "",
          horizontal,
        })
      );
    } catch {
      // The server-side section anchor remains available as a fallback.
    }
  };
  const restoreSourceReviewPosition = () => {
    if (!sourceReviewPositionKey) {
      return;
    }
    let stored = null;
    try {
      stored = JSON.parse(
        window.sessionStorage.getItem(sourceReviewPositionKey) || "null"
      );
      window.sessionStorage.removeItem(sourceReviewPositionKey);
    } catch {
      stored = null;
    }
    if (!stored) {
      return;
    }
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const cards = Array.from(
          document.querySelectorAll("[data-source-review-card]")
        );
        const card = cards.find(
          (candidate) =>
            candidate.dataset.sourceReviewCard === stored.fileId
        );
        const targetTop =
          card && Number.isFinite(stored.cardOffset)
            ? window.scrollY +
              card.getBoundingClientRect().top -
              stored.cardOffset
            : stored.scrollY;
        window.scrollTo({
          top: Math.max(0, targetTop || 0),
          behavior: "auto",
        });
        const horizontal = Array.from(
          card?.querySelectorAll(".table-scroll") || []
        );
        for (const [index, scrollLeft] of (
          stored.horizontal || []
        ).entries()) {
          if (horizontal[index]) {
            horizontal[index].scrollLeft = scrollLeft;
          }
        }
        const controls = Array.from(card?.querySelectorAll("[name]") || []);
        const focusTarget = controls.find(
          (control) =>
            control.name === stored.focusName &&
            (!stored.focusValue || control.value === stored.focusValue)
        );
        focusTarget?.focus({ preventScroll: true });
      });
    });
  };
  for (const form of document.querySelectorAll("[data-source-review-form]")) {
    form.addEventListener("submit", () => {
      rememberSourceReviewPosition(form);
    });
  }
  for (const group of document.querySelectorAll("[data-source-choice-group]")) {
    const wholeWorksheet = group.querySelector("[data-source-whole]");
    const separateRegions = Array.from(
      group.querySelectorAll("[data-source-region]")
    );
    wholeWorksheet?.addEventListener("change", () => {
      if (!wholeWorksheet.checked) return;
      for (const region of separateRegions) region.checked = false;
    });
    for (const region of separateRegions) {
      region.addEventListener("change", () => {
        if (region.checked && wholeWorksheet) wholeWorksheet.checked = false;
      });
    }
  }

  restoreSourceReviewPosition();
});
