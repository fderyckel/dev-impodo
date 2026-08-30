"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const datasetNameInputs = Array.from(
    document.querySelectorAll("[data-dataset-name]")
  );
  const datasetNameViolations = (input) => {
    const name = input.value;
    if (!name) {
      return ["Enter a name."];
    }
    const violations = [];
    if (name.length > 63) {
      violations.push("Use no more than 63 characters.");
    }
    if (!/^[a-z]/.test(name)) {
      violations.push("Start with a lowercase letter from a to z.");
    }
    if (/[^a-z0-9_]/.test(name)) {
      violations.push("Use only lowercase letters, numbers, and underscores.");
    }
    if (
      datasetNameInputs.some(
        (candidate) => candidate !== input && candidate.value === name
      )
    ) {
      violations.push("Give each table a different name.");
    }
    return violations;
  };
  const validateDatasetName = (input, reveal) => {
    const violations = datasetNameViolations(input);
    const message = violations.length
      ? `This name is not accepted: ${violations.join(" ")}`
      : "";
    const error = input
      .closest("label")
      ?.querySelector("[data-dataset-name-error]");
    input.setCustomValidity(message);
    input.setAttribute("aria-invalid", String(Boolean(message)));
    if (error) {
      error.textContent = reveal ? message : "";
      error.hidden = !reveal || !message;
    }
  };
  for (const input of datasetNameInputs) {
    validateDatasetName(input, false);
    input.addEventListener("input", () => {
      for (const candidate of datasetNameInputs) {
        validateDatasetName(
          candidate,
          candidate.dataset.datasetNameValidationShown === "true"
        );
      }
    });
    input.addEventListener("blur", () => {
      input.dataset.datasetNameValidationShown = "true";
      validateDatasetName(input, true);
    });
    input.addEventListener("invalid", () => {
      input.dataset.datasetNameValidationShown = "true";
      validateDatasetName(input, true);
    });
  }

  const odooCaptureAssessmentDialog = document.querySelector(
    "[data-odoo-capture-assessment-dialog]"
  );
  if (
    odooCaptureAssessmentDialog &&
    typeof odooCaptureAssessmentDialog.showModal === "function"
  ) {
    if (odooCaptureAssessmentDialog.open) {
      odooCaptureAssessmentDialog.close();
    }
    odooCaptureAssessmentDialog.showModal();
  }
  document
    .querySelector("[data-close-odoo-capture-assessment]")
    ?.addEventListener("click", () => odooCaptureAssessmentDialog?.close());

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
