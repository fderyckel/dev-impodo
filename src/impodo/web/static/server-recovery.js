"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const banner = document.querySelector("[data-server-recovery]");
  if (!banner) {
    return;
  }

  const title = banner.querySelector("[data-server-recovery-title]");
  const message = banner.querySelector("[data-server-recovery-message]");
  const retry = banner.querySelector("[data-server-recovery-retry]");
  const help = banner.querySelector("[data-server-recovery-help]");
  const disconnectedHelp = help?.textContent.trim() || "";
  const healthUrl = banner.dataset.healthUrl || "/health";
  const failureLimit = Math.max(
    2,
    Number.parseInt(banner.dataset.failureLimit || "3", 10) || 3
  );
  const intervalMs = Math.max(
    1000,
    Number.parseInt(banner.dataset.heartbeatIntervalMs || "4000", 10) || 4000
  );
  const timeoutMs = Math.max(
    500,
    Number.parseInt(banner.dataset.heartbeatTimeoutMs || "2000", 10) || 2000
  );
  let consecutiveFailures = 0;
  let disconnected = false;
  let sessionEnded = false;
  let activeRequest = null;
  let nextCheck = null;
  let recoveredNotice = null;

  const schedule = () => {
    window.clearTimeout(nextCheck);
    if (sessionEnded) {
      return;
    }
    nextCheck = window.setTimeout(() => void checkHealth(), intervalMs);
  };

  const showDisconnected = () => {
    if (disconnected || sessionEnded) {
      return;
    }
    disconnected = true;
    banner.classList.remove("success");
    banner.classList.add("error");
    if (title) {
      title.textContent = "Impodo is not responding";
    }
    if (message) {
      message.textContent =
        "Keep this tab open while Impodo tries to reconnect. Your saved work is unchanged, and unsaved entries remain on this page.";
    }
    if (retry) {
      retry.hidden = false;
    }
    if (help) {
      help.textContent = disconnectedHelp;
    }
    banner.hidden = false;
    document.dispatchEvent(new CustomEvent("impodo:server-disconnected"));
  };

  const showSessionEnded = () => {
    if (sessionEnded) {
      return;
    }
    sessionEnded = true;
    disconnected = false;
    banner.classList.remove("success");
    banner.classList.add("error");
    if (title) {
      title.textContent = "This Impodo session has ended";
    }
    if (message) {
      message.textContent =
        "Impodo is still running, but this tab can no longer use it. Your saved work is unchanged, and unsaved entries remain on this page.";
    }
    if (retry) {
      retry.hidden = true;
    }
    if (help) {
      help.textContent =
        "Use the most recently opened Impodo tab. If there is none, restart Impodo once. Copy any unsaved entries from this page before closing it.";
    }
    banner.hidden = false;
    document.dispatchEvent(new CustomEvent("impodo:session-ended"));
  };

  const showRecovered = () => {
    if (!disconnected || sessionEnded) {
      return;
    }
    disconnected = false;
    banner.classList.remove("error");
    banner.classList.add("success");
    if (title) {
      title.textContent = "Impodo is responding again";
    }
    if (message) {
      message.textContent =
        "Review this page and any save outcome before repeating your last action.";
    }
    if (retry) {
      retry.hidden = false;
    }
    if (help) {
      help.textContent = disconnectedHelp;
    }
    banner.hidden = false;
    document.dispatchEvent(new CustomEvent("impodo:server-reconnected"));
    window.clearTimeout(recoveredNotice);
    recoveredNotice = window.setTimeout(() => {
      if (!disconnected) {
        banner.hidden = true;
      }
    }, 5000);
  };

  const checkHealth = async () => {
    if (activeRequest) {
      schedule();
      return;
    }
    const controller = new AbortController();
    activeRequest = controller;
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(healthUrl, {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (response.status === 401) {
        showSessionEnded();
        return;
      }
      let payload = null;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
      if (!response.ok || payload?.status !== "ok") {
        throw new Error("Health check failed");
      }
      consecutiveFailures = 0;
      showRecovered();
    } catch (_error) {
      consecutiveFailures += 1;
      if (consecutiveFailures >= failureLimit) {
        showDisconnected();
      }
    } finally {
      window.clearTimeout(timeout);
      if (activeRequest === controller) {
        activeRequest = null;
      }
      schedule();
    }
  };

  retry?.addEventListener("click", () => {
    window.clearTimeout(nextCheck);
    activeRequest?.abort();
    activeRequest = null;
    void checkHealth();
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(nextCheck);
    window.clearTimeout(recoveredNotice);
    activeRequest?.abort();
  });

  window.impodoServerRecovery = { checkNow: checkHealth };
  schedule();
});
