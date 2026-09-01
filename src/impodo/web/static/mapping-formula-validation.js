"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const mappingForm = document.querySelector("[data-mapping-form]");
  if (!mappingForm) {
    return;
  }

  const formulaValidationDelayMs = 500;
  const endpoint = mappingForm.dataset.formulaValidationUrl || "";
  const checkButton = mappingForm.querySelector("[data-check-mapping]");
  const checkHelp = mappingForm.querySelector("[data-formula-check-help]");
  const saveStatus = mappingForm.querySelector("[data-mapping-save-status]");
  const issueSummary = mappingForm.querySelector("[data-formula-issue-summary]");
  const issueCount = mappingForm.querySelector("[data-formula-issue-count]");
  const goToIssue = mappingForm.querySelector("[data-go-to-formula-issue]");
  const issues = new Map();
  const pendingKeys = new Set();
  const controllers = new Map();
  const timers = new Map();
  const generations = new Map();
  let requestedIssueKey = "";

  const issueKey = (datasetId, targetField) =>
    `${datasetId}\u0000${targetField}`;

  const controlIdentity = (control) => {
    const dataset = control.closest("[data-mapping-dataset]");
    const row = control.closest("[data-scalar-mapping-row]");
    const datasetId = dataset?.dataset.mappingDataset || "";
    const targetField = row?.dataset.targetField || "";
    return {
      datasetId,
      targetField,
      key: issueKey(datasetId, targetField),
    };
  };

  const issueIdentity = (issue) => ({
    datasetId: String(issue?.dataset_id || ""),
    targetField: String(issue?.target_field || ""),
    key: issueKey(
      String(issue?.dataset_id || ""),
      String(issue?.target_field || "")
    ),
  });

  const visibleControlFor = (key) =>
    Array.from(mappingForm.querySelectorAll("[data-rule-formula]")).find(
      (control) => controlIdentity(control).key === key
    );

  const feedbackFor = (control) =>
    control
      .closest(".advanced-rule")
      ?.querySelector("[data-formula-feedback]");

  const formulaApplies = (control) => {
    const provider = control
      .closest("[data-scalar-mapping-row]")
      ?.querySelector("[data-value-source]");
    return Boolean(provider?.value) && !control.disabled;
  };

  const renderFeedback = (control, issue = null, advisory = "") => {
    const feedback = feedbackFor(control);
    const message = feedback?.querySelector(
      "[data-formula-feedback-message]"
    );
    const prefix = feedback?.querySelector("[data-formula-feedback-prefix]");
    if (!feedback || !message || !prefix) {
      return;
    }
    if (issue) {
      control.setAttribute("aria-invalid", "true");
      feedback.classList.remove("advisory");
      prefix.textContent = "Must fix:";
      message.textContent = issue.display_message || issue.message ||
        "This formula is not valid.";
      feedback.hidden = false;
      feedback.closest("details")?.setAttribute("open", "");
      return;
    }
    control.setAttribute("aria-invalid", "false");
    if (advisory) {
      feedback.classList.add("advisory");
      prefix.textContent = "Formula check:";
      message.textContent = advisory;
      feedback.hidden = false;
      return;
    }
    feedback.classList.remove("advisory");
    prefix.textContent = "Must fix:";
    message.textContent = "";
    feedback.hidden = true;
  };

  const orderedIssues = () =>
    Array.from(issues.values()).sort((left, right) => {
      const datasetDifference =
        Number(left.dataset_index ?? 0) - Number(right.dataset_index ?? 0);
      if (datasetDifference) {
        return datasetDifference;
      }
      return String(left.target_field || "").localeCompare(
        String(right.target_field || "")
      );
    });

  const renderSummary = () => {
    const count = issues.size;
    const pending = pendingKeys.size;
    if (issueSummary) {
      issueSummary.hidden = count === 0;
    }
    if (issueCount && count) {
      issueCount.textContent =
        `${count} formula issue${count === 1 ? "" : "s"} must be fixed ` +
        "before checking matches.";
    }
    if (checkButton) {
      checkButton.disabled = count > 0 || pending > 0;
      checkButton.title = count
        ? "Correct formula issues before checking matches."
        : pending
        ? "Wait for formula checking to finish."
        : "";
    }
    if (checkHelp) {
      checkHelp.textContent = count
        ? "Correct formula issues before checking matches."
        : pending
        ? "Wait for formula checking to finish."
        : "";
    }
  };

  const rememberIssue = (issue, identity) => {
    if (!issue) {
      issues.delete(identity.key);
      return;
    }
    issues.set(identity.key, {
      ...issue,
      dataset_id: identity.datasetId,
      target_field: identity.targetField,
    });
  };

  const finishPending = (key) => {
    pendingKeys.delete(key);
    controllers.delete(key);
    renderSummary();
  };

  const validateControl = async (control) => {
    if (!(control instanceof HTMLTextAreaElement)) {
      return;
    }
    const identity = controlIdentity(control);
    window.clearTimeout(timers.get(identity.key));
    timers.delete(identity.key);
    controllers.get(identity.key)?.abort();
    controllers.delete(identity.key);
    const generation = (generations.get(identity.key) || 0) + 1;
    generations.set(identity.key, generation);
    if (
      !identity.datasetId ||
      !identity.targetField ||
      !formulaApplies(control)
    ) {
      pendingKeys.delete(identity.key);
      rememberIssue(null, identity);
      renderFeedback(control);
      renderSummary();
      return;
    }
    if (!control.value.trim()) {
      pendingKeys.delete(identity.key);
      rememberIssue(null, identity);
      renderFeedback(control);
      renderSummary();
      return;
    }

    const controller = new AbortController();
    controllers.set(identity.key, controller);
    pendingKeys.add(identity.key);
    rememberIssue(null, identity);
    renderFeedback(control, null, "Checking formula…");
    renderSummary();
    const csrfToken =
      mappingForm.querySelector('input[name="csrf_token"]')?.value || "";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          entries: [
            ["csrf_token", csrfToken],
            ["dataset_id", identity.datasetId],
            ["formula", control.value],
          ],
        }),
        signal: controller.signal,
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok) {
        throw new Error(payload.detail || "Formula checking is unavailable.");
      }
      if (generations.get(identity.key) !== generation) {
        return;
      }
      rememberIssue(payload.issue, identity);
      renderFeedback(control, payload.issue);
    } catch (error) {
      if (error?.name === "AbortError") {
        return;
      }
      if (generations.get(identity.key) !== generation) {
        return;
      }
      rememberIssue(null, identity);
      renderFeedback(
        control,
        null,
        "Could not check this formula now. Save progress or retry; the full " +
          "match check will still validate it."
      );
    } finally {
      if (generations.get(identity.key) === generation) {
        finishPending(identity.key);
      }
    }
  };

  const scheduleValidation = (control, delay = formulaValidationDelayMs) => {
    const identity = controlIdentity(control);
    window.clearTimeout(timers.get(identity.key));
    timers.delete(identity.key);
    generations.set(identity.key, (generations.get(identity.key) || 0) + 1);
    controllers.get(identity.key)?.abort();
    controllers.delete(identity.key);
    if (!formulaApplies(control) || !control.value.trim()) {
      pendingKeys.delete(identity.key);
      rememberIssue(null, identity);
      renderFeedback(control);
      renderSummary();
      return;
    }
    pendingKeys.add(identity.key);
    rememberIssue(null, identity);
    renderFeedback(control, null, "Checking formula…");
    renderSummary();
    timers.set(
      identity.key,
      window.setTimeout(() => void validateControl(control), delay)
    );
  };

  const initializeControl = (control) => {
    if (
      !(control instanceof HTMLTextAreaElement) ||
      control.dataset.formulaValidationInitialized === "true"
    ) {
      return;
    }
    control.dataset.formulaValidationInitialized = "true";
    const identity = controlIdentity(control);
    const issue = issues.get(identity.key);
    if (issue) {
      renderFeedback(control, issue);
    } else if (control.value.trim()) {
      scheduleValidation(control);
    } else {
      renderFeedback(control);
    }
  };

  const revealIssue = (issue) => {
    const identity = issueIdentity(issue);
    const control = visibleControlFor(identity.key);
    if (control) {
      control.closest("details")?.setAttribute("open", "");
      control.scrollIntoView({ behavior: "smooth", block: "center" });
      control.focus({ preventScroll: true });
      requestedIssueKey = "";
      return true;
    }
    const activeDataset = mappingForm.querySelector(
      `[data-mapping-dataset="${CSS.escape(identity.datasetId)}"] ` +
        "[data-scalar-field-catalog]"
    );
    if (activeDataset) {
      const mappedOnly = activeDataset.querySelector(
        "[data-show-mapped-scalars]"
      );
      if (mappedOnly?.checked) {
        mappedOnly.checked = false;
      }
      const search = activeDataset.querySelector("[data-scalar-field-search]");
      if (search) {
        requestedIssueKey = identity.key;
        search.value = identity.targetField;
        search.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      }
    }
    const url = new URL(window.location.href);
    url.searchParams.set("mapping_dataset", String(issue.dataset_index || 0));
    url.searchParams.set("field_query", identity.targetField);
    url.searchParams.set("scalar_page", "1");
    url.searchParams.delete("mapped_only");
    window.impodoMappingPosition?.remember();
    window.location.assign(url);
    return true;
  };

  const applySaveResult = (payload) => {
    if (!Array.isArray(payload?.authoring_issues)) {
      return;
    }
    for (const timer of timers.values()) {
      window.clearTimeout(timer);
    }
    for (const controller of controllers.values()) {
      controller.abort();
    }
    for (const key of generations.keys()) {
      generations.set(key, (generations.get(key) || 0) + 1);
    }
    timers.clear();
    controllers.clear();
    pendingKeys.clear();
    issues.clear();
    for (const issue of payload.authoring_issues) {
      const identity = issueIdentity(issue);
      if (identity.datasetId && identity.targetField) {
        issues.set(identity.key, issue);
      }
    }
    for (const control of mappingForm.querySelectorAll("[data-rule-formula]")) {
      renderFeedback(control, issues.get(controlIdentity(control).key));
    }
    renderSummary();
  };

  try {
    const initialIssues = JSON.parse(
      mappingForm.dataset.formulaAuthoringIssues || "[]"
    );
    if (Array.isArray(initialIssues)) {
      for (const issue of initialIssues) {
        const identity = issueIdentity(issue);
        if (identity.datasetId && identity.targetField) {
          issues.set(identity.key, issue);
        }
      }
    }
  } catch (_error) {
    issues.clear();
  }
  if (issues.size && saveStatus?.dataset.savedAt) {
    saveStatus.textContent =
      `Saved — needs attention. Correct ${issues.size} formula` +
      `${issues.size === 1 ? "" : "s"} before checking matches.`;
  }

  mappingForm.addEventListener("input", (event) => {
    if (event.target?.matches?.("[data-rule-formula]")) {
      scheduleValidation(event.target);
    }
  });
  mappingForm.addEventListener("change", (event) => {
    if (event.target?.matches?.("[data-value-source]")) {
      const formula = event.target
        .closest("[data-scalar-mapping-row]")
        ?.querySelector("[data-rule-formula]");
      if (formula) {
        scheduleValidation(formula);
      }
    }
  });
  mappingForm.addEventListener("focusout", (event) => {
    if (event.target?.matches?.("[data-rule-formula]")) {
      void validateControl(event.target);
    }
  });
  mappingForm.addEventListener(
    "submit",
    (event) => {
      if (
        event.submitter?.value !== "draft" ||
        (issues.size === 0 && pendingKeys.size === 0)
      ) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      if (saveStatus) {
        saveStatus.textContent = issues.size
          ? "Correct formula issues before checking matches."
          : "Wait for formula checking to finish.";
      }
      const firstIssue = orderedIssues()[0];
      if (firstIssue) {
        revealIssue(firstIssue);
      }
    },
    true
  );
  goToIssue?.addEventListener("click", () => {
    const firstIssue = orderedIssues()[0];
    if (firstIssue) {
      revealIssue(firstIssue);
    }
  });

  const observer = new MutationObserver(() => {
    for (const control of mappingForm.querySelectorAll("[data-rule-formula]")) {
      initializeControl(control);
    }
    if (requestedIssueKey) {
      const control = visibleControlFor(requestedIssueKey);
      if (control) {
        const issue = issues.get(requestedIssueKey);
        if (issue) {
          revealIssue(issue);
        }
      }
    }
  });
  observer.observe(mappingForm, { childList: true, subtree: true });
  for (const control of mappingForm.querySelectorAll("[data-rule-formula]")) {
    initializeControl(control);
  }
  renderSummary();

  window.impodoFormulaValidation = { applySaveResult };
});
