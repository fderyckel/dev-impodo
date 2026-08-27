"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const normalizationApproveDialog = document.querySelector(
    "[data-normalization-approve-dialog]"
  );
  const normalizationApproveForm = document.querySelector(
    "[data-normalization-approve-form]"
  );
  const normalizationApproveTrigger = document.querySelector(
    "[data-normalization-approve-trigger]"
  );
  const normalizationApproveConfirm = document.querySelector(
    "[data-normalization-approve-confirm]"
  );

  normalizationApproveTrigger?.addEventListener("click", () => {
    normalizationApproveDialog?.showModal();
  });

  normalizationApproveConfirm?.addEventListener("click", () => {
    normalizationApproveDialog?.close();
    normalizationApproveForm?.requestSubmit();
  });

  const normalizationRejectDialog = document.querySelector(
    "[data-normalization-reject-dialog]"
  );
  const normalizationRejectMessage = normalizationRejectDialog?.querySelector(
    "[data-normalization-reject-message]"
  );
  const normalizationRejectReason = normalizationRejectDialog?.querySelector(
    "[data-normalization-reject-reason]"
  );
  const normalizationRejectConfirm = normalizationRejectDialog?.querySelector(
    "[data-normalization-reject-confirm]"
  );
  let pendingNormalizationRejectForm = null;

  for (const trigger of document.querySelectorAll(
    "[data-normalization-reject-trigger]"
  )) {
    trigger.addEventListener("click", () => {
      const form = trigger.closest("[data-normalization-reject-form]");
      if (!form || !normalizationRejectDialog) {
        return;
      }
      pendingNormalizationRejectForm = form;
      if (normalizationRejectMessage) {
        const groupName = trigger.dataset.groupName || "This prepared change";
        normalizationRejectMessage.textContent =
          `${groupName} will be sent back. This stops approval until the ` +
          "source or field rule is corrected.";
      }
      if (normalizationRejectReason) {
        normalizationRejectReason.value = "";
        normalizationRejectReason.setCustomValidity("");
      }
      normalizationRejectDialog.showModal();
      normalizationRejectReason?.focus();
    });
  }

  normalizationRejectReason?.addEventListener("input", () => {
    normalizationRejectReason.setCustomValidity("");
  });

  normalizationRejectConfirm?.addEventListener("click", () => {
    const form = pendingNormalizationRejectForm;
    const reason = normalizationRejectReason?.value.trim() || "";
    if (!form || !normalizationRejectReason) {
      return;
    }
    if (!reason) {
      normalizationRejectReason.setCustomValidity("Explain what needs fixing.");
      normalizationRejectReason.reportValidity();
      return;
    }
    const storedReason = form.querySelector(
      "[data-normalization-reject-reason-value]"
    );
    if (storedReason) {
      storedReason.value = reason;
    }
    pendingNormalizationRejectForm = null;
    normalizationRejectDialog?.close();
    form.requestSubmit();
  });

  normalizationRejectDialog?.addEventListener("close", () => {
    pendingNormalizationRejectForm = null;
  });

  const normalizationReview = document.querySelector(
    "[data-normalization-review]"
  );
  const normalizationPositionStorageKey = normalizationReview
    ? `impodo.normalization.position:${normalizationReview.dataset.normalizationPositionKey}`
    : "";
  const rememberNormalizationPosition = (form) => {
    if (!normalizationPositionStorageKey) {
      return;
    }
    const rows = Array.from(
      document.querySelectorAll("[data-normalization-group]")
    );
    const row = form.closest("[data-normalization-group]");
    const tableScroll = document.querySelector(
      "[data-normalization-table-scroll]"
    );
    try {
      window.sessionStorage.setItem(
        normalizationPositionStorageKey,
        JSON.stringify({
          scrollY: window.scrollY,
          groupId: row?.dataset.normalizationGroup || "",
          rowIndex: row ? rows.indexOf(row) : -1,
          rowOffset: row?.getBoundingClientRect().top ?? null,
          tableScrollLeft: tableScroll?.scrollLeft || 0,
        })
      );
    } catch {
      // The server-side anchor still keeps the decision table in view.
    }
  };
  const restoreNormalizationPosition = () => {
    if (!normalizationPositionStorageKey) {
      return;
    }
    let stored = null;
    try {
      stored = JSON.parse(
        window.sessionStorage.getItem(normalizationPositionStorageKey) ||
          "null"
      );
      window.sessionStorage.removeItem(normalizationPositionStorageKey);
    } catch {
      stored = null;
    }
    if (!stored) {
      return;
    }
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const rows = Array.from(
          document.querySelectorAll("[data-normalization-group]")
        );
        const sameRow = rows.find(
          (candidate) =>
            candidate.dataset.normalizationGroup === stored.groupId
        );
        const fallbackIndex = Math.min(
          Math.max(0, Number(stored.rowIndex) || 0),
          Math.max(0, rows.length - 1)
        );
        const row = sameRow || rows[fallbackIndex];
        const targetTop =
          row && Number.isFinite(stored.rowOffset)
            ? window.scrollY +
              row.getBoundingClientRect().top -
              stored.rowOffset
            : stored.scrollY;
        window.scrollTo({
          top: Math.max(0, targetTop || 0),
          behavior: "auto",
        });
        const tableScroll = document.querySelector(
          "[data-normalization-table-scroll]"
        );
        if (tableScroll) {
          tableScroll.scrollLeft = stored.tableScrollLeft || 0;
        }
      });
    });
  };
  for (const form of document.querySelectorAll(
    "[data-normalization-reject-form]"
  )) {
    form.addEventListener("submit", () => {
      rememberNormalizationPosition(form);
    });
  }

  restoreNormalizationPosition();
});
