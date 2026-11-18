"use strict";

(() => {
  const create = ({
    mappingForm,
    saveStatus,
    updateMappingVersionFields,
    navigateToMappingResult,
    setDirty,
    onCommitted,
    hasNewerEdits,
    retryOriginal,
    currentEntries,
  }) => {
    const saveOutcome = mappingForm.querySelector(
      "[data-mapping-save-outcome]"
    );
    const saveOutcomeMessage = mappingForm.querySelector(
      "[data-mapping-save-outcome-message]"
    );
    const operationReference = mappingForm.querySelector(
      "[data-mapping-operation-reference]"
    );
    const conflictRecovery = mappingForm.querySelector(
      "[data-mapping-conflict-recovery]"
    );
    const unknownRecovery = mappingForm.querySelector(
      "[data-mapping-unknown-recovery]"
    );
    const copyMappingEdits = mappingForm.querySelector(
      "[data-copy-mapping-edits]"
    );
    const reloadSavedMapping = mappingForm.querySelector(
      "[data-reload-saved-mapping]"
    );
    const checkMappingOutcome = mappingForm.querySelector(
      "[data-check-mapping-outcome]"
    );
    const retryMissing = mappingForm.querySelector("[data-retry-missing-save]");
    const mutationTimeoutMs = Number.parseInt(
      mappingForm.dataset.mutationTimeoutMs || "15000",
      10
    );
    const receiptTimeoutMs = 5000;
    const configuredReceiptWaitMs = Number.parseInt(
      mappingForm.dataset.mutationReceiptWaitMs || "180000",
      10
    );
    const configuredReceiptPollMs = Number.parseInt(
      mappingForm.dataset.mutationReceiptPollMs || "1500",
      10
    );
    const receiptWaitMs = Number.isFinite(configuredReceiptWaitMs)
      ? Math.max(configuredReceiptWaitMs, 0)
      : 180000;
    const receiptPollMs = Number.isFinite(configuredReceiptPollMs)
      ? Math.max(configuredReceiptPollMs, 250)
      : 1500;
    let unresolvedOperation = null;
    let staleConflict = false;
    let lastSubmittedAction = "";
    let conflictReloadUrl = "";

    const clear = () => {
      if (saveOutcome) {
        saveOutcome.hidden = true;
      }
      if (saveOutcomeMessage) {
        saveOutcomeMessage.textContent = "";
      }
      if (operationReference) {
        operationReference.textContent = "";
        operationReference.parentElement.hidden = true;
      }
      if (conflictRecovery) {
        conflictRecovery.hidden = true;
      }
      if (unknownRecovery) {
        unknownRecovery.hidden = true;
      }
    };

    const showFailure = (
      message,
      { operationId = "", conflict = false, unknown = false, retry = false, focus = true } = {}
    ) => {
      if (saveOutcomeMessage) {
        saveOutcomeMessage.textContent = message;
      }
      if (operationReference) {
        operationReference.textContent = operationId;
        operationReference.parentElement.hidden = !operationId;
      }
      if (conflictRecovery) {
        conflictRecovery.hidden = !conflict;
      }
      if (unknownRecovery) {
        unknownRecovery.hidden = !unknown;
      }
      if (retryMissing) retryMissing.hidden = !retry;
      if (saveOutcome) {
        saveOutcome.hidden = false;
        if (focus) {
          saveOutcome.focus();
        }
      }
    };

    const createOperationId = () => {
      if (window.crypto?.randomUUID) {
        return window.crypto.randomUUID();
      }
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(
        /[xy]/g,
        (character) => {
          const random = Math.floor(Math.random() * 16);
          const value = character === "x" ? random : (random & 0x3) | 0x8;
          return value.toString(16);
        }
      );
    };

    const fetchWithTimeout = async (url, options, timeoutMs) => {
      const controller = new AbortController();
      const timer = window.setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        const payload = await responseJson(response);
        return { ok: response.ok, statusCode: response.status, payload };
      } finally {
        window.clearTimeout(timer);
      }
    };

    const wait = (delayMs) =>
      new Promise((resolve) => window.setTimeout(resolve, delayMs));

    const responseJson = async (response) => {
      const body = await response.text();
      if (!body) {
        throw new Error("Impodo returned an empty save response.");
      }
      let payload;
      try {
        payload = JSON.parse(body);
      } catch (_error) {
        throw new Error("Impodo returned an unreadable save response.");
      }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error("Impodo returned an invalid save response.");
      }
      return payload;
    };

    const failureSuffix = (operation) => {
      if (operation.choosesOdooDefault) {
        return " Your Odoo decision remains on this page; use the save outcome before trying again.";
      }
      if (operation.action === "submit") {
        return " Your checked matches remain unchanged on this page.";
      }
      if (
        operation.action === "remove_readonly" ||
        operation.changesDisposition
      ) {
        return " Your checked matches remain on this page.";
      }
      return " Your unsaved changes are still on this page.";
    };

    const readMutationReceipt = async (currentOperationId) => {
      const baseUrl = mappingForm.dataset.mutationReceiptUrl;
      if (!baseUrl) {
        throw new Error("The save-outcome URL is missing.");
      }
      const { ok, payload } = await fetchWithTimeout(
        `${baseUrl}${encodeURIComponent(currentOperationId)}`,
        { method: "GET", headers: { Accept: "application/json" } },
        receiptTimeoutMs
      );
      if (!ok) {
        throw new Error("Impodo could not read the save outcome.");
      }
      return payload;
    };

    const applyMutationOutcome = (payload, operation) => {
      const status = String(payload.status || "");
      const currentOperationId = String(
        payload.operation_id || operation.operationId || ""
      );
      const superseded = status === "committed" && payload.follow_up_status === "superseded";
      if (status === "committed" && !superseded) {
        unresolvedOperation = null;
        staleConflict = false;
        const newerEdits = hasNewerEdits?.(operation) === true;
        updateMappingVersionFields(payload);
        if (!newerEdits) window.impodoFormulaValidation?.applySaveResult(payload);
        else window.impodoMappingJourney?.cancelContinuation(operation);
        setDirty(newerEdits);
        clear();
        if (saveStatus) {
          const savedDate = payload.saved_at
            ? new Date(payload.saved_at)
            : null;
          const savedLabel =
            savedDate && !Number.isNaN(savedDate.getTime())
              ? `Saved ${savedDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
              : "Progress saved";
          if (savedDate && !Number.isNaN(savedDate.getTime())) {
            saveStatus.title = savedDate.toLocaleString();
          }
          saveStatus.textContent = newerEdits
            ? "Earlier changes saved. Your newer edits still need saving."
            : operation.action === "save_progress"
              ? savedLabel + (payload.authoring_issues?.length ? " — needs attention." : ".")
              : payload.message || "Matches saved.";
          if (newerEdits) saveStatus.classList.add("unsaved");
          else saveStatus.classList.remove("unsaved");
        }
        const navigationHandled = onCommitted?.(operation, payload);
        if (operation.action !== "save_progress" && !newerEdits && navigationHandled !== true) {
          navigateToMappingResult(payload.redirect_url, operation);
        }
        return "committed";
      }
      window.impodoMappingJourney?.cancelContinuation(operation);
      if (superseded || (status === "rejected" && payload.failure_code === "MAPPING_VERSION_CONFLICT")) {
        unresolvedOperation = null;
        staleConflict = true;
        conflictReloadUrl = superseded ? payload.recovery?.reload_url || "" : "";
        const message =
          payload.detail || payload.message ||
          "A newer saved version exists. Your edits are still on this page. Copy them before reloading the saved version.";
        showFailure(message, {
          operationId: currentOperationId,
          conflict: true,
        });
        if (saveStatus) {
          saveStatus.textContent = superseded
            ? "The earlier check finished. Review the current matches."
            : "Conflict: newer Match data is saved. Your edits were not applied.";
          saveStatus.classList.add("unsaved");
        }
        return "conflict";
      }
      if (status === "rejected" && payload.partial_save === true) {
        unresolvedOperation = null;
        staleConflict = false;
        updateMappingVersionFields(payload);
        setDirty(true);
        showFailure(
          payload.detail ||
            "The field matches were saved, but the rows-to-use check did not finish. Correct the problem, then check matches again.",
          { operationId: currentOperationId }
        );
        if (saveStatus) {
          saveStatus.textContent =
            "Field matches saved; rows still need checking.";
          saveStatus.classList.add("unsaved");
        }
        return "rejected";
      }
      if (status === "rejected") {
        unresolvedOperation = null;
        staleConflict = false;
        if (operation.action !== "submit") {
          setDirty(true);
        }
        if (["SUPPORTING_REVIEW_REQUIRED", "SUPPORTING_MAPPING_CONFLICT"].includes(payload.failure_code)) {
          const recovery = new CustomEvent("impodo:related-review-required", {
            cancelable: true,
            detail: {
              operationId: currentOperationId,
              mappingConflict: payload.failure_code === "SUPPORTING_MAPPING_CONFLICT",
              conflicts: payload.supporting_conflicts || [],
              message: payload.detail || payload.message || "",
            },
          });
          if (!document.dispatchEvent(recovery)) {
            clear();
            if (saveStatus) {
              saveStatus.textContent = payload.failure_code === "SUPPORTING_MAPPING_CONFLICT"
                ? "Check failed. Correct the linked fields above."
                : "Related-data choices need review. Your edits are retained.";
              saveStatus.classList.add("unsaved");
            }
            return "rejected";
          }
        }
        const message =
          (payload.detail || payload.message || "The operation was rejected.") +
          failureSuffix(operation);
        showFailure(message, { operationId: currentOperationId });
        if (saveStatus) {
          saveStatus.textContent = "Save failed. Your edits are retained.";
          saveStatus.classList.add("unsaved");
        }
        return "rejected";
      }
      return showUnknownOutcome(operation, status === "not_found");
    };

    const showUnknownOutcome = (operation, missing = false) => {
      window.impodoMappingJourney?.cancelContinuation(operation);
      unresolvedOperation = operation;
      showFailure(
        missing
          ? "Impodo has no receipt yet. Check again or retry the original action with the same edits and support reference."
          : "Impodo could not verify whether this operation was saved. Keep this tab open and check the save outcome again.",
        { operationId: operation.operationId, unknown: true, retry: missing }
      );
      if (saveStatus) {
        saveStatus.textContent = "Save outcome unknown. Check before retrying.";
        saveStatus.classList.add("unsaved");
      }
      return "pending";
    };

    const resolveMutationOutcome = async (operation) => {
      const receiptDeadline = Date.now() + receiptWaitMs;
      while (true) {
        let payload;
        try {
          payload = await readMutationReceipt(operation.operationId);
        } catch (_error) {
          const remainingMs = receiptDeadline - Date.now();
          if (remainingMs <= 0) {
            return showUnknownOutcome(operation);
          }
          unresolvedOperation = operation;
          if (saveStatus) {
            saveStatus.textContent = operation.action === "draft"
              ? "Reconnecting to get the check result..."
              : "Still waiting for the save outcome. Reconnecting...";
            saveStatus.classList.remove("unsaved");
          }
          await wait(Math.min(receiptPollMs, remainingMs));
          continue;
        }
        if (!payload.operation_id) {
          payload.operation_id = operation.operationId;
        }
        if (payload.status !== "pending" && payload.status !== "not_found") {
          return applyMutationOutcome(payload, operation);
        }

        unresolvedOperation = operation;
        const remainingMs = receiptDeadline - Date.now();
        if (remainingMs <= 0) {
          return applyMutationOutcome(payload, operation);
        }
        if (saveStatus) {
          if (payload.status === "not_found") {
            saveStatus.textContent = "Waiting for Impodo to record the action...";
          } else {
            saveStatus.textContent = operation.action === "draft"
              ? "Still checking matches..."
              : "The operation is still running. Waiting for its saved outcome...";
          }
          saveStatus.classList.remove("unsaved");
        }
        await wait(Math.min(receiptPollMs, remainingMs));
      }
    };

    const blockIfNeeded = () => {
      if (unresolvedOperation) {
        showFailure(
          "Impodo has not confirmed the previous save outcome. Check that outcome before trying another action.",
          { operationId: unresolvedOperation.operationId, unknown: true, retry: retryMissing?.hidden === false }
        );
        return true;
      }
      if (staleConflict) {
        showFailure(
          "A newer saved version exists. Copy your edits, then reload the saved version before making another change.",
          {
            operationId: operationReference?.textContent || "",
            conflict: true,
          }
        );
        return true;
      }
      return false;
    };

    const createOperation = ({
      action,
      choosesOdooDefault,
      changesDisposition,
      entries,
      journeyContinuation,
    }) => {
      const currentOperationId = createOperationId();
      const submittedEntries = [...entries, ["operation_id", currentOperationId]];
      lastSubmittedAction = action;
      return {
        operationId: currentOperationId,
        action,
        choosesOdooDefault,
        changesDisposition,
        entries: submittedEntries,
        journeyContinuation,
      };
    };

    checkMappingOutcome?.addEventListener("click", async () => {
      if (!unresolvedOperation) {
        return;
      }
      if (retryMissing) retryMissing.hidden = true;
      checkMappingOutcome.disabled = true;
      checkMappingOutcome.textContent = "Checking outcome...";
      try {
        await resolveMutationOutcome(unresolvedOperation);
      } finally {
        checkMappingOutcome.disabled = false;
        checkMappingOutcome.textContent = "Check save outcome";
      }
    });

    retryMissing?.addEventListener("click", async () => {
      if (!unresolvedOperation || retryMissing.hidden || checkMappingOutcome?.disabled) return;
      retryMissing.hidden = true;
      await retryOriginal(unresolvedOperation);
    });

    copyMappingEdits?.addEventListener("click", async () => {
      const safeEntries = currentEntries(lastSubmittedAction).filter(
        ([name]) => !["csrf_token", "operation_id"].includes(name)
      );
      const copy = JSON.stringify(
        {
          format: "impodo-match-data-edits-v1",
          page: window.location.href,
          operation_id:
            unresolvedOperation?.operationId || operationReference?.textContent || "",
          entries: safeEntries,
        },
        null,
        2
      );
      try {
        await navigator.clipboard.writeText(copy);
        if (saveStatus) {
          saveStatus.textContent =
            "Edits copied. You can now reload the saved version.";
        }
      } catch (_error) {
        showFailure(
          "The browser could not copy these edits. Keep this tab open while you review or copy the values manually.",
          {
            operationId: operationReference?.textContent || "",
            conflict: true,
          }
        );
      }
    });

    reloadSavedMapping?.addEventListener("click", () => {
      window.impodoMappingPosition?.remember();
      if (conflictReloadUrl) navigateToMappingResult(conflictReloadUrl);
      else window.location.reload();
    });

    return {
      applyMutationOutcome,
      blockIfNeeded,
      clear,
      createOperation,
      failureSuffix,
      hasUnresolvedOperation: () => Boolean(unresolvedOperation || staleConflict),
      fetchSave: (url, options) =>
        fetchWithTimeout(
          url,
          options,
          Number.isFinite(mutationTimeoutMs) ? mutationTimeoutMs : 15000
        ),
      resolveMutationOutcome,
      showFailure,
    };
  };

  window.impodoMappingSaveRecovery = { create };
})();
