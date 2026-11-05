"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const linkedDataForm = document.querySelector("[data-linked-data-form]");
  if (linkedDataForm) {
    const unsavedNotice = linkedDataForm.querySelector(
      "[data-linked-data-unsaved-notice]"
    );
    let linkedDataIsDirty = false;

    const markLinkedDataDirty = () => {
      linkedDataIsDirty = true;
      if (unsavedNotice) {
        unsavedNotice.hidden = false;
      }
    };

    for (const choice of linkedDataForm.querySelectorAll(
      '[name="related_actions"]'
    )) {
      choice.addEventListener("change", markLinkedDataDirty);
    }

    linkedDataForm.addEventListener(
      "submit",
      () => {
        linkedDataIsDirty = false;
        if (unsavedNotice) {
          unsavedNotice.hidden = true;
        }
      },
      true
    );

    for (const capturePlanForm of document.querySelectorAll(
      "[data-capture-plan-form]"
    )) {
      capturePlanForm.addEventListener(
        "submit",
        (event) => {
          if (!linkedDataIsDirty) {
            return;
          }
          event.preventDefault();
          event.stopImmediatePropagation();
          if (unsavedNotice) {
            unsavedNotice.hidden = false;
            unsavedNotice.focus({ preventScroll: true });
            unsavedNotice.scrollIntoView({ block: "nearest" });
          }
        },
        true
      );
    }

    window.addEventListener("beforeunload", (event) => {
      if (!linkedDataIsDirty) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    });
  }

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
  const isSourceSetting = (name) => /^(encoding|delimiter|warnings_acknowledged|selected_\d+|header_row_\d+)$/.test(name);
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
          focusValue: isSourceSetting(active?.name) || active?.name === "action"
            ? active?.value || "" : "",
          horizontal,
          pendingFiles: Array.from(changedSourceForms)
            .filter(([pendingForm]) => pendingForm !== form)
            .map(([pendingForm, pendingCard]) => ({
              fileId: pendingCard.dataset.sourceReviewCard,
              controls: Array.from(pendingForm.elements)
                .filter((control) => isSourceSetting(control.name))
                .map((control) => ({ name: control.name, value: control.value, checked: control.checked })),
            })),
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
    for (const pending of stored.pendingFiles || []) {
      const pendingCard = Array.from(document.querySelectorAll("[data-source-review-card]"))
        .find((card) => card.dataset.sourceReviewCard === pending.fileId);
      const pendingForm = pendingCard?.querySelector("[data-source-review-form]");
      if (!pendingForm || pendingForm.querySelector("fieldset[disabled]")) continue;
      for (const saved of pending.controls) {
        if (!isSourceSetting(saved.name)) continue;
        const control = Array.from(pendingForm.elements).find((item) => item.name === saved.name);
        if (!control || control.type === "submit" || control.type === "file") continue;
        if (["checkbox", "radio"].includes(control.type)) control.checked = saved.checked;
        else control.value = saved.value;
      }
      pendingForm.dispatchEvent(new Event("input", { bubbles: true }));
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
  const changedSourceForms = new Map();
  const pageConfirmationStatus = document.querySelector("[data-source-page-status]");
  const savedPageStatus = pageConfirmationStatus?.innerHTML || "";
  const pageWasComplete = pageConfirmationStatus?.classList.contains("complete");
  const syncPageConfirmation = () => {
    if (!pageConfirmationStatus) return;
    pageConfirmationStatus.classList.toggle("complete", Boolean(pageWasComplete && !changedSourceForms.size));
    pageConfirmationStatus.classList.toggle("attention", Boolean(changedSourceForms.size || !pageWasComplete));
    pageConfirmationStatus.innerHTML = changedSourceForms.size
      ? "Changes not confirmed"
      : savedPageStatus;
  };
  for (const card of document.querySelectorAll("[data-source-review-card]")) {
    const form = card.querySelector("form[data-source-review-form]");
    if (!form || form.querySelector("fieldset[disabled]")) continue;
    const confirmationAction = form?.querySelector(
      "[data-source-confirmation-action]"
    );
    const discard = form.querySelector("[data-source-discard-changes]");
    const notice = form.querySelector("[data-source-confirmation-notice]");
    const next = form.querySelector("[data-source-confirmation-next]");
    const confirmationStatuses = Array.from(
      card.querySelectorAll("[data-source-confirmation-status]")
    );
    const fileStatus = document.querySelector(
      `[data-source-file-status="${card.dataset.sourceReviewCard}"]`
    );
    const statuses = [...confirmationStatuses, fileStatus].filter(Boolean);
    const savedStatuses = statuses.map((status) => status.innerHTML);
    const wasConfirmed = card.dataset.sourceReviewConfirmed === "true";
    const savedAction = confirmationAction?.textContent;
    const fingerprint = () => JSON.stringify(Array.from(new FormData(form).entries()));
    const savedFingerprint = fingerprint();
    const markConfirmationDirty = () => {
      const dirty = fingerprint() !== savedFingerprint;
      if (dirty) changedSourceForms.set(form, card);
      else changedSourceForms.delete(form);
      card.classList.toggle("is-confirmed", wasConfirmed && !dirty);
      card.classList.toggle("has-unconfirmed-changes", dirty);
      statuses.forEach((status, index) => {
        status.classList.toggle("complete", wasConfirmed && !dirty);
        status.classList.toggle("attention", dirty || !wasConfirmed);
        status.innerHTML = dirty ? "Changes not confirmed" : savedStatuses[index];
      });
      if (confirmationAction) {
        confirmationAction.classList.toggle("secondary", wasConfirmed && !dirty);
        confirmationAction.classList.toggle("primary", dirty || !wasConfirmed);
        confirmationAction.textContent = dirty ? "Confirm changes" : savedAction;
      }
      if (discard) discard.hidden = !dirty;
      if (next) next.hidden = dirty;
      if (!dirty && notice) notice.hidden = true;
      syncPageConfirmation();
    };
    form.addEventListener("input", markConfirmationDirty);
    form.addEventListener("change", markConfirmationDirty);
    discard?.addEventListener("click", () => {
      form.reset();
      markConfirmationDirty();
      confirmationAction?.focus({ preventScroll: true });
    });
  }
  const revealUnconfirmedChanges = (card) => {
    const notice = card.querySelector("[data-source-confirmation-notice]");
    if (notice) notice.hidden = false;
    const confirm = card.querySelector("[data-source-confirmation-action]");
    const action = confirm?.disabled ? card.querySelector("[data-source-discard-changes]") : confirm;
    action?.focus({ preventScroll: true });
    action?.scrollIntoView({ block: "nearest" });
  };
  let sourceSubmission = false;
  document.addEventListener("submit", (event) => {
    if (!sourceReviewPage) return;
    const form = event.target;
    const pending = Array.from(changedSourceForms).find(([changed]) => changed !== form);
    if (pending && !changedSourceForms.has(form)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      revealUnconfirmedChanges(pending[1]);
      return;
    }
    sourceSubmission = true;
  }, true);
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || !changedSourceForms.size || link.target === "_blank"
      || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const destination = new URL(link.href, window.location.href);
    if (destination.origin === window.location.origin
      && destination.pathname === window.location.pathname && destination.hash) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    revealUnconfirmedChanges(changedSourceForms.values().next().value);
  }, true);
  window.addEventListener("beforeunload", (event) => {
    if (!changedSourceForms.size || sourceSubmission) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("pageshow", () => { sourceSubmission = false; });
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
