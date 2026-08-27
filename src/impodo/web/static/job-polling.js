"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const preparationJob = document.querySelector("[data-preparation-job]");
  if (preparationJob) {
    const statusUrl = preparationJob.dataset.statusUrl;
    const state = preparationJob.querySelector("[data-preparation-state]");
    const message = preparationJob.querySelector("[data-preparation-message]");
    const progress = preparationJob.querySelector("[data-preparation-progress]");
    const percent = preparationJob.querySelector("[data-preparation-percent]");
    const rows = preparationJob.querySelector("[data-preparation-rows]");
    const spinner = preparationJob.querySelector("[data-preparation-spinner]");
    const activeActions = preparationJob.querySelector("[data-preparation-active]");
    const cancelButton = preparationJob.querySelector("[data-preparation-cancel]");
    const failed = preparationJob.querySelector("[data-preparation-failed]");
    const failure = preparationJob.querySelector("[data-preparation-failure]");
    const failureTitle = preparationJob.querySelector("[data-preparation-failure-title]");
    const failureCode = preparationJob.querySelector("[data-preparation-failure-code]");
    const retryAction = preparationJob.querySelector("[data-preparation-retry]");
    const cancelled = preparationJob.querySelector("[data-preparation-cancelled]");
    const complete = preparationJob.querySelector("[data-preparation-complete]");
    const continueLink = preparationJob.querySelector("[data-preparation-continue]");
    let pollTimer;

    const formatRows = (completed, total) => {
      if (!total) {
        return "Starting safely";
      }
      return `${Number(completed).toLocaleString()} of ${Number(total).toLocaleString()} source rows`;
    };

    const showStatus = (job) => {
      const active = job.status === "QUEUED" || job.status === "RUNNING";
      if (message) message.textContent = job.message;
      if (progress) progress.value = job.progress_percent;
      if (percent) percent.textContent = `${job.progress_percent}%`;
      if (rows) rows.textContent = formatRows(job.completed_rows, job.total_rows);
      if (spinner) spinner.hidden = !active;
      if (activeActions) activeActions.hidden = !active;
      if (cancelButton && job.cancel_requested) {
        cancelButton.disabled = true;
        cancelButton.textContent = "Stopping safely…";
      }
      if (state) {
        state.classList.remove("ready", "review", "blocked");
        state.textContent = active
          ? "In progress"
          : job.status === "FAILED"
            ? "Needs attention"
            : "Stopped";
        state.classList.add(active ? "review" : "blocked");
      }
      if (job.status === "FAILED") {
        if (failed) failed.hidden = false;
        if (failure) {
          failure.textContent = job.retry_allowed
            ? "No source file or Odoo record was changed. Review the reason above, then return to the saved setup or try again."
            : "Your saved evidence remains available. Restart Impodo before continuing.";
        }
        if (failureTitle) {
          failureTitle.textContent =
            job.failure_message || "Preparation stopped before it could finish.";
        }
        if (retryAction) retryAction.hidden = !job.retry_allowed;
        if (failureCode) {
          failureCode.hidden = !job.failure_code;
          const code = failureCode.querySelector("code");
          if (code) code.textContent = job.failure_code;
        }
      } else if (job.status === "CANCELLED") {
        if (cancelled) cancelled.hidden = false;
      } else if (
        job.status === "SUCCEEDED" ||
        job.status === "REVIEW_REQUIRED"
      ) {
        if (state) {
          state.textContent = job.status === "REVIEW_REQUIRED" ? "Review needed" : "Ready";
          state.classList.remove("blocked", "review");
          state.classList.add(job.status === "REVIEW_REQUIRED" ? "review" : "ready");
        }
        if (complete) complete.hidden = false;
        if (continueLink && job.redirect_url) continueLink.href = job.redirect_url;
        if (job.redirect_url) window.location.assign(job.redirect_url);
      }
      return active;
    };

    const pollPreparation = async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Progress is temporarily unavailable");
        if (showStatus(await response.json())) {
          pollTimer = window.setTimeout(pollPreparation, 750);
        }
      } catch {
        if (message) message.textContent = "Reconnecting to preparation…";
        pollTimer = window.setTimeout(pollPreparation, 1500);
      }
    };

    if (statusUrl) {
      pollPreparation();
    }
    window.addEventListener("pagehide", () => window.clearTimeout(pollTimer));
  }

  const odooCaptureJob = document.querySelector("[data-odoo-capture-job]");
  if (odooCaptureJob) {
    const statusUrl = odooCaptureJob.dataset.statusUrl;
    const state = odooCaptureJob.querySelector("[data-odoo-capture-state]");
    const message = odooCaptureJob.querySelector("[data-odoo-capture-message]");
    const progress = odooCaptureJob.querySelector("[data-odoo-capture-progress]");
    const percent = odooCaptureJob.querySelector("[data-odoo-capture-percent]");
    const rows = odooCaptureJob.querySelector("[data-odoo-capture-rows]");
    const accounting = odooCaptureJob.querySelector("[data-odoo-capture-accounting]");
    const spinner = odooCaptureJob.querySelector("[data-odoo-capture-spinner]");
    const activeActions = odooCaptureJob.querySelector("[data-odoo-capture-active]");
    const cancelButton = odooCaptureJob.querySelector("[data-odoo-capture-cancel]");
    const failed = odooCaptureJob.querySelector("[data-odoo-capture-failed]");
    const failure = odooCaptureJob.querySelector("[data-odoo-capture-failure]");
    const cancelled = odooCaptureJob.querySelector("[data-odoo-capture-cancelled]");
    const complete = odooCaptureJob.querySelector("[data-odoo-capture-complete]");
    const continueLink = odooCaptureJob.querySelector("[data-odoo-capture-continue]");
    let pollTimer;

    const showCaptureStatus = (job) => {
      const active = job.status === "QUEUED" || job.status === "RUNNING";
      if (message) message.textContent = job.message;
      if (progress) progress.value = job.progress_percent;
      if (percent) percent.textContent = `${job.progress_percent}%`;
      if (rows) {
        rows.textContent = job.completed_rows
          ? `${Number(job.completed_rows).toLocaleString()} records read`
          : "No record page completed yet";
      }
      if (accounting) {
        accounting.textContent = `${Number(job.page_count).toLocaleString()} page(s) · ${Number(job.response_bytes).toLocaleString()} response bytes · ${Number(job.normalized_bytes).toLocaleString()} normalized bytes`;
      }
      if (spinner) spinner.hidden = !active;
      if (activeActions) activeActions.hidden = !active;
      if (cancelButton && job.cancel_requested) {
        cancelButton.disabled = true;
        cancelButton.textContent = "Stopping safely…";
      }
      if (state) {
        state.classList.remove("ready", "review", "blocked");
        state.textContent = active
          ? "In progress"
          : job.status === "SUCCEEDED"
            ? "Frozen"
            : job.status === "FAILED"
              ? "Could not finish"
              : "Stopped";
        state.classList.add(
          active ? "review" : job.status === "SUCCEEDED" ? "ready" : "blocked"
        );
      }
      if (job.status === "FAILED") {
        if (failed) failed.hidden = false;
        if (failure) failure.textContent = job.failure_message;
      } else if (job.status === "CANCELLED") {
        if (cancelled) cancelled.hidden = false;
      } else if (job.status === "SUCCEEDED") {
        if (complete) complete.hidden = false;
        if (continueLink && job.redirect_url) continueLink.href = job.redirect_url;
        if (job.redirect_url) window.location.assign(job.redirect_url);
      }
      return active;
    };

    const pollOdooCapture = async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Capture progress is temporarily unavailable");
        if (showCaptureStatus(await response.json())) {
          pollTimer = window.setTimeout(pollOdooCapture, 750);
        }
      } catch {
        if (message) message.textContent = "Reconnecting to capture…";
        pollTimer = window.setTimeout(pollOdooCapture, 1500);
      }
    };

    if (statusUrl) pollOdooCapture();
    window.addEventListener("pagehide", () => window.clearTimeout(pollTimer));
  }

  const loadJob = document.querySelector("[data-load-job]");
  if (loadJob) {
    const statusUrl = loadJob.dataset.statusUrl;
    const state = loadJob.querySelector("[data-load-state]");
    const stepState = loadJob.querySelector("[data-load-step-state]");
    const message = loadJob.querySelector("[data-load-message]");
    const progress = loadJob.querySelector("[data-load-progress]");
    const percent = loadJob.querySelector("[data-load-percent]");
    const rows = loadJob.querySelector("[data-load-rows]");
    const total = loadJob.querySelector("[data-load-total]");
    const created = loadJob.querySelector("[data-load-created]");
    const updated = loadJob.querySelector("[data-load-updated]");
    const attention = loadJob.querySelector("[data-load-attention]");
    const attentionCard = loadJob.querySelector("[data-load-attention-card]");
    const relationships = loadJob.querySelector("[data-load-relationships]");
    const guidance = loadJob.querySelector("[data-load-guidance]");
    const spinner = loadJob.querySelector("[data-load-spinner]");
    const activeActions = loadJob.querySelector("[data-load-active]");
    const failed = loadJob.querySelector("[data-load-failed]");
    const failure = loadJob.querySelector("[data-load-failure]");
    const complete = loadJob.querySelector("[data-load-complete]");
    const continueLink = loadJob.querySelector("[data-load-continue]");
    const run = loadJob.querySelector("[data-load-run]");
    let pollTimer;

    const showLoadStatus = (job) => {
      const active = job.status === "QUEUED" || job.status === "RUNNING";
      if (message) message.textContent = job.message;
      if (progress) progress.value = job.progress_percent;
      if (percent) percent.textContent = `${job.progress_percent}%`;
      if (total) total.textContent = Number(job.total_rows).toLocaleString();
      if (created) created.textContent = Number(job.created_count).toLocaleString();
      if (updated) updated.textContent = Number(job.updated_count).toLocaleString();
      if (attention) attention.textContent = Number(job.attention_count).toLocaleString();
      if (rows) {
        const completedLabel = `${Number(job.completed_rows).toLocaleString()} of ${Number(job.total_rows).toLocaleString()} records completed`;
        rows.textContent = job.status === "FAILED" && job.not_attempted_count
          ? `${completedLabel} · ${Number(job.not_attempted_count).toLocaleString()} not attempted`
          : completedLabel;
      }
      if (attentionCard) {
        attentionCard.classList.toggle("blocked", Boolean(job.attention_count));
        attentionCard.classList.toggle("ready", !job.attention_count);
      }
      if (relationships) {
        relationships.hidden = !job.relationship_pending_count;
        relationships.textContent = job.relationship_pending_count
          ? `${Number(job.relationship_pending_count).toLocaleString()} new record(s) are waiting for their relationship step.`
          : "";
      }
      if (guidance) {
        guidance.textContent = job.phase === "VERIFYING"
          ? "Impodo is now checking the saved results against Odoo."
          : "Accepted totals are not called verified until Impodo reads the completed records back from Odoo.";
      }
      if (spinner) spinner.hidden = !active;
      if (activeActions) activeActions.hidden = !active;
      if (stepState) {
        stepState.textContent = active
          ? "In progress"
          : job.status === "SUCCEEDED"
            ? "Complete"
            : "Needs attention";
      }
      if (run) {
        run.hidden = !job.execution_run_id;
        const code = run.querySelector("code");
        if (code) code.textContent = job.execution_run_id;
      }
      if (state) {
        state.classList.remove("ready", "review", "blocked");
        state.textContent = active
          ? "In progress"
          : job.status === "SUCCEEDED"
            ? "Finished"
            : "Stopped";
        state.classList.add(
          active ? "review" : job.status === "SUCCEEDED" ? "ready" : "blocked"
        );
      }
      if (job.status === "FAILED") {
        if (failed) failed.hidden = false;
        if (failure) failure.textContent = job.failure_message;
      } else if (job.status === "SUCCEEDED") {
        if (complete) complete.hidden = false;
        if (continueLink && job.redirect_url) continueLink.href = job.redirect_url;
        if (job.redirect_url) window.location.assign(job.redirect_url);
      }
      return active;
    };

    const pollLoad = async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Load progress is temporarily unavailable");
        if (showLoadStatus(await response.json())) {
          pollTimer = window.setTimeout(pollLoad, 750);
        }
      } catch {
        if (message) message.textContent = "Reconnecting to the saved load progress…";
        pollTimer = window.setTimeout(pollLoad, 1500);
      }
    };

    if (statusUrl) pollLoad();
    window.addEventListener("pagehide", () => window.clearTimeout(pollTimer));
  }

  const integratedRun = document.querySelector("[data-integrated-run-review]");
  if (integratedRun) {
    const statusUrl = integratedRun.dataset.statusUrl || "";
    const initialHash = integratedRun.dataset.viewHash || "";
    let pollTimer;

    const pollIntegratedRun = async () => {
      try {
        const response = await fetch(statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Run progress is temporarily unavailable");
        const status = await response.json();
        if (status.view_hash && status.view_hash !== initialHash) {
          window.location.reload();
          return;
        }
        if (status.active) {
          pollTimer = window.setTimeout(pollIntegratedRun, 1000);
        }
      } catch {
        pollTimer = window.setTimeout(pollIntegratedRun, 2000);
      }
    };

    if (statusUrl) pollIntegratedRun();
    window.addEventListener("pagehide", () => window.clearTimeout(pollTimer));
  }

});
