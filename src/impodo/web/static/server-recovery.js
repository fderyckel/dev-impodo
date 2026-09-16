"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const banner = document.querySelector("[data-server-recovery]");
  if (!banner) return;

  const title = banner.querySelector("[data-server-recovery-title]");
  const message = banner.querySelector("[data-server-recovery-message]");
  const action = banner.querySelector("[data-server-recovery-action]");
  const help = banner.querySelector("[data-server-recovery-help]");
  const disconnectedHelp = help?.textContent.trim() || "";
  const healthUrl = banner.dataset.healthUrl || "/health";
  const intervalMs = Math.max(
    1000,
    Number.parseInt(banner.dataset.heartbeatIntervalMs || "4000", 10) || 4000
  );
  const timeoutMs = Math.max(
    1000,
    Number.parseInt(banner.dataset.heartbeatTimeoutMs || "4000", 10) || 4000
  );
  const delayedMs = Math.max(
    5000,
    Number.parseInt(banner.dataset.delayedAfterMs || "20000", 10) || 20000
  );
  const disconnectedMs = Math.max(
    delayedMs,
    Number.parseInt(banner.dataset.disconnectedAfterMs || "45000", 10) || 45000
  );

  const now = () => performance.now();
  let connectivity = "CONNECTED";
  let operation = "IDLE";
  let operationStartedAt = 0;
  let operationLabel = "This step";
  let delayedAcknowledged = false;
  let mutationPending = false;
  let lastResponseAt = now();
  let lastSuccessAt = lastResponseAt;
  let lastNetworkFailureAt = 0;
  let activeRequest = null;
  let nextCheck = null;
  let recoveredNotice = null;
  let waitForVisibleCheck = false;
  let visibleCheckAfter = 0;

  const setBannerKind = (kind) => {
    banner.classList.remove("success", "warning", "error");
    banner.classList.add(kind);
    banner.setAttribute("role", kind === "error" ? "alert" : "status");
    banner.setAttribute("aria-live", kind === "error" ? "assertive" : "polite");
  };

  const showSessionEnded = () => {
    if (connectivity === "SESSION_ENDED") return;
    connectivity = "SESSION_ENDED";
    operation = mutationPending ? "OUTCOME_UNKNOWN" : operation;
    setBannerKind("error");
    if (title) title.textContent = "This Impodo session has ended";
    if (message) {
      message.textContent =
        "Impodo is running, but this tab can no longer use it. Check the outcome before repeating an action.";
    }
    if (action) action.hidden = true;
    if (help) {
      help.textContent =
        "Use the most recently opened Impodo tab. Copy unsaved entries before closing this one.";
    }
    banner.hidden = false;
    document.dispatchEvent(new CustomEvent("impodo:session-ended"));
  };

  const showDisconnected = () => {
    if (connectivity !== "DISCONNECTED") {
      connectivity = "DISCONNECTED";
      operation = mutationPending ? "OUTCOME_UNKNOWN" : operation;
      document.dispatchEvent(new CustomEvent("impodo:server-disconnected"));
    }
    setBannerKind("error");
    if (title) title.textContent = "Connection to Impodo was interrupted";
    if (message) {
      message.textContent = mutationPending
        ? "The last action's outcome is unknown. Keep this tab open and check its outcome before trying again."
        : "Keep this tab open while Impodo reconnects. Saved work is unchanged.";
    }
    if (action) { action.hidden = false; action.textContent = "Check connection"; }
    if (help) help.textContent = disconnectedHelp;
    banner.hidden = false;
  };

  const showOutcomeUnknown = () => {
    setBannerKind("warning");
    if (title) title.textContent = "Check the last action";
    if (message) {
      message.textContent =
        "Impodo is responding, but the last action's outcome is unknown. Check its result before trying again.";
    }
    if (action) action.hidden = true;
    if (help) help.textContent = "Do not repeat the action until its result is clear.";
    banner.hidden = false;
  };

  const showDelayed = () => {
    operation = "DELAYED";
    if (delayedAcknowledged) { banner.hidden = true; return; }
    setBannerKind("warning");
    if (title) title.textContent = "Impodo is still working";
    if (message) {
      message.textContent = `${operationLabel} is taking a while. Impodo is responding. The work is still in progress.`;
    }
    if (action) { action.hidden = false; action.textContent = "Got it"; }
    if (help) help.textContent = "You do not need to start this step again.";
    banner.hidden = false;
  };

  const showRecovered = () => {
    const wasDisconnected = connectivity === "DISCONNECTED";
    connectivity = "CONNECTED";
    if (!wasDisconnected) return;
    setBannerKind("success");
    if (title) title.textContent = "Impodo is responding again";
    if (message) message.textContent = "Check the outcome before repeating your last action.";
    if (action) action.hidden = true;
    if (help) help.textContent = "Impodo answered the latest connection check.";
    banner.hidden = false;
    document.dispatchEvent(new CustomEvent("impodo:server-reconnected"));
    window.clearTimeout(recoveredNotice);
    recoveredNotice = window.setTimeout(() => render(), 5000);
  };

  const render = () => {
    if (connectivity === "SESSION_ENDED") return;
    if (
      !waitForVisibleCheck &&
      lastNetworkFailureAt > lastResponseAt &&
      now() - lastResponseAt >= disconnectedMs
    ) {
      showDisconnected();
      return;
    }
    if (operation === "OUTCOME_UNKNOWN") {
      showOutcomeUnknown();
      return;
    }
    if (
      (operation === "BUSY" || operation === "DELAYED") &&
      now() - operationStartedAt >= delayedMs
    ) {
      showDelayed();
      return;
    }
    if (connectivity === "CONNECTED") banner.hidden = true;
  };

  const noteResponse = (response, { successful = response?.ok } = {}) => {
    if (connectivity === "SESSION_ENDED") return false;
    if (response?.status === 401) {
      showSessionEnded();
      return false;
    }
    lastResponseAt = now();
    if (successful) lastSuccessAt = lastResponseAt;
    showRecovered();
    render();
    return true;
  };

  const noteResponsive = () => {
    if (connectivity === "SESSION_ENDED") return;
    lastResponseAt = now();
    lastSuccessAt = lastResponseAt;
    showRecovered();
    render();
  };

  const beginOperation = ({ label = "This step", mutation = false } = {}) => {
    operation = "BUSY";
    delayedAcknowledged = false;
    operationStartedAt = now();
    operationLabel = label;
    mutationPending = Boolean(mutation);
    render();
  };

  const endOperation = ({ outcomeKnown = true } = {}) => {
    operation = outcomeKnown ? "IDLE" : mutationPending ? "OUTCOME_UNKNOWN" : "IDLE";
    mutationPending = false;
    render();
  };

  const schedule = () => {
    window.clearTimeout(nextCheck);
    if (connectivity !== "SESSION_ENDED") {
      nextCheck = window.setTimeout(() => void checkHealth(), intervalMs);
    }
  };

  const checkHealth = async () => {
    if (activeRequest) return schedule();
    const checkStartedAt = now();
    const controller = new AbortController();
    activeRequest = controller;
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(healthUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      const payload = response.ok
        ? await response.json().catch(() => null)
        : null;
      if (!noteResponse(response, { successful: payload?.status === "ok" })) {
        return;
      }
    } catch (_error) {
      lastNetworkFailureAt = now();
    } finally {
      if (checkStartedAt >= visibleCheckAfter) waitForVisibleCheck = false;
      window.clearTimeout(timeout);
      if (activeRequest === controller) activeRequest = null;
      render();
      schedule();
    }
  };

  document.addEventListener("impodo:operation-started", (event) => {
    beginOperation(event.detail || {});
  });
  document.addEventListener("impodo:operation-finished", (event) => {
    endOperation(event.detail || {});
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    visibleCheckAfter = now();
    waitForVisibleCheck = true;
    void checkHealth();
  });
  action?.addEventListener("click", () => {
    if (operation === "DELAYED" && connectivity === "CONNECTED") {
      delayedAcknowledged = true;
      banner.hidden = true;
    } else void checkHealth();
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(nextCheck);
    window.clearTimeout(recoveredNotice);
    activeRequest?.abort();
  });

  window.impodoServerRecovery = {
    beginOperation,
    checkNow: checkHealth,
    endOperation,
    noteResponse,
    noteResponsive,
    state: () => ({ connectivity, operation, lastResponseAt, lastSuccessAt }),
  };
  schedule();
});
