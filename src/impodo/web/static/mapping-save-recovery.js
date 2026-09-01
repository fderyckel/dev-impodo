"use strict";

(() => {
  const create = ({
    mappingForm,
    saveStatus,
    updateMappingVersionFields,
    navigateToMappingResult,
    setDirty,
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
    const mutationTimeoutMs = Number.parseInt(
      mappingForm.dataset.mutationTimeoutMs || "15000",
      10
    );
    const receiptTimeoutMs = 5000;
    let unresolvedOperation = null;
    let staleConflict = false;
    let lastSubmittedEntries = [];

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
      { operationId = "", conflict = false, unknown = false, focus = true } = {}
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
        return await fetch(url, { ...options, signal: controller.signal });
      } finally {
        window.clearTimeout(timer);
      }
    };

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
      const response = await fetchWithTimeout(
        `${baseUrl}${encodeURIComponent(currentOperationId)}`,
        { method: "GET", headers: { Accept: "application/json" } },
        receiptTimeoutMs
      );
      if (!response.ok) {
        throw new Error("Impodo could not read the save outcome.");
      }
      return responseJson(response);
    };

    const applyMutationOutcome = (payload, operation) => {
      const status = String(payload.status || "");
      const currentOperationId = String(
        payload.operation_id || operation.operationId || ""
      );
      if (status === "committed") {
        unresolvedOperation = null;
        staleConflict = false;
        updateMappingVersionFields(payload);
        window.impodoFormulaValidation?.applySaveResult(payload);
        setDirty(false);
        clear();
        if (saveStatus) {
          const savedDate = payload.saved_at
            ? new Date(payload.saved_at)
            : null;
          const savedLabel =
            savedDate && !Number.isNaN(savedDate.getTime())
              ? `Saved ${savedDate.toLocaleString()}. `
              : "";
          saveStatus.textContent =
            savedLabel + (payload.message || "Matches saved.");
          saveStatus.classList.remove("unsaved");
        }
        if (operation.action !== "save_progress") {
          navigateToMappingResult(payload.redirect_url);
        }
        return "committed";
      }
      if (status === "not_found") {
        unresolvedOperation = null;
        staleConflict = false;
        if (operation.action !== "submit") {
          setDirty(true);
        }
        const message =
          "Impodo checked the operation receipt: nothing was saved. You can try the action again." +
          failureSuffix(operation);
        showFailure(message, { operationId: currentOperationId });
        if (saveStatus) {
          saveStatus.textContent = "Not saved. Your edits are retained.";
          saveStatus.classList.add("unsaved");
        }
        return "not_found";
      }
      if (
        status === "rejected" &&
        payload.failure_code === "MAPPING_VERSION_CONFLICT"
      ) {
        unresolvedOperation = null;
        staleConflict = true;
        const message =
          payload.detail ||
          "A newer saved version exists. Your edits are still on this page. Copy them before reloading the saved version.";
        showFailure(message, {
          operationId: currentOperationId,
          conflict: true,
        });
        if (saveStatus) {
          saveStatus.textContent =
            "Conflict: newer Match data is saved. Your edits were not applied.";
          saveStatus.classList.add("unsaved");
        }
        return "conflict";
      }
      if (status === "rejected") {
        unresolvedOperation = null;
        staleConflict = false;
        if (operation.action !== "submit") {
          setDirty(true);
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
      unresolvedOperation = operation;
      showFailure(
        "Impodo has not confirmed whether this operation finished. Do not repeat it yet. Check the save outcome using the button below.",
        { operationId: currentOperationId, unknown: true }
      );
      if (saveStatus) {
        saveStatus.textContent = "Save outcome unknown. Check before retrying.";
        saveStatus.classList.add("unsaved");
      }
      return "pending";
    };

    const resolveMutationOutcome = async (operation) => {
      try {
        const payload = await readMutationReceipt(operation.operationId);
        if (!payload.operation_id) {
          payload.operation_id = operation.operationId;
        }
        return applyMutationOutcome(payload, operation);
      } catch (_error) {
        unresolvedOperation = operation;
        showFailure(
          "Impodo could not verify whether this operation was saved. Do not repeat it yet. Keep this tab open and check the save outcome again.",
          { operationId: operation.operationId, unknown: true }
        );
        if (saveStatus) {
          saveStatus.textContent = "Save outcome unknown. Check before retrying.";
          saveStatus.classList.add("unsaved");
        }
        return "pending";
      }
    };

    const blockIfNeeded = () => {
      if (unresolvedOperation) {
        showFailure(
          "Impodo has not confirmed the previous save outcome. Check that outcome before trying another action.",
          { operationId: unresolvedOperation.operationId, unknown: true }
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
    }) => {
      const currentOperationId = createOperationId();
      const submittedEntries = [...entries, ["operation_id", currentOperationId]];
      lastSubmittedEntries = submittedEntries;
      return {
        operationId: currentOperationId,
        action,
        choosesOdooDefault,
        changesDisposition,
        entries: submittedEntries,
      };
    };

    checkMappingOutcome?.addEventListener("click", async () => {
      if (!unresolvedOperation) {
        return;
      }
      checkMappingOutcome.disabled = true;
      checkMappingOutcome.textContent = "Checking outcome...";
      try {
        await resolveMutationOutcome(unresolvedOperation);
      } finally {
        checkMappingOutcome.disabled = false;
        checkMappingOutcome.textContent = "Check save outcome";
      }
    });

    copyMappingEdits?.addEventListener("click", async () => {
      const safeEntries = lastSubmittedEntries.filter(
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
      window.location.reload();
    });

    return {
      applyMutationOutcome,
      blockIfNeeded,
      clear,
      createOperation,
      failureSuffix,
      fetchSave: (url, options) =>
        fetchWithTimeout(
          url,
          options,
          Number.isFinite(mutationTimeoutMs) ? mutationTimeoutMs : 15000
        ),
      resolveMutationOutcome,
      responseJson,
      showFailure,
    };
  };

  window.impodoMappingSaveRecovery = { create };
})();
