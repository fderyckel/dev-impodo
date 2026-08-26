"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const targetForm = document.querySelector("[data-target-form]");
  const remoteConnectionStatus = document.querySelector(
    "[data-remote-connection-status]"
  );
  const testConnectionButton = targetForm?.querySelector(
    "[data-test-connection-button]"
  );

  const setRemoteConnectionState = (state) => {
    if (!remoteConnectionStatus) {
      return;
    }
    remoteConnectionStatus.dataset.state = state;
    remoteConnectionStatus.classList.remove(
      "connection-state-ready",
      "connection-state-error",
      "connection-state-unknown"
    );
    remoteConnectionStatus.classList.add(`connection-state-${state}`);
  };

  const resetRemoteConnectionStatus = () => {
    if (!remoteConnectionStatus) {
      return;
    }
    setRemoteConnectionState("unknown");
    remoteConnectionStatus.removeAttribute("aria-busy");
    remoteConnectionStatus.setAttribute("role", "status");
    const stateLabel = remoteConnectionStatus.querySelector(
      "[data-connection-state-label]"
    );
    const heading = remoteConnectionStatus.querySelector(
      "[data-connection-heading]"
    );
    const guidance = remoteConnectionStatus.querySelector(
      "[data-connection-guidance]"
    );
    if (stateLabel) {
      stateLabel.textContent = "Not checked";
    }
    if (heading) {
      heading.textContent = "The Odoo connection has not been checked.";
    }
    if (guidance) {
      guidance.textContent =
        "Connection details changed. Check the connection again.";
    }
    for (const check of remoteConnectionStatus.querySelectorAll(
      "[data-remote-connection-check]"
    )) {
      check.classList.remove("status-ready", "status-error");
      check.classList.add("status-unknown");
      const message = check.querySelector("[data-connection-check-message]");
      if (message) {
        message.textContent = check.dataset.waitingMessage || "Not checked yet.";
      }
    }
    const support = remoteConnectionStatus.querySelector(
      "[data-connection-support]"
    );
    if (support) {
      support.hidden = true;
    }
    const checkedAt = remoteConnectionStatus.querySelector(
      "[data-connection-checked-at]"
    );
    if (checkedAt) {
      checkedAt.hidden = true;
    }
    if (testConnectionButton) {
      testConnectionButton.textContent = "Check connection";
    }
  };

  const markRemoteConnectionChecking = () => {
    if (!remoteConnectionStatus) {
      return;
    }
    resetRemoteConnectionStatus();
    remoteConnectionStatus.setAttribute("aria-busy", "true");
    const stateLabel = remoteConnectionStatus.querySelector(
      "[data-connection-state-label]"
    );
    const heading = remoteConnectionStatus.querySelector(
      "[data-connection-heading]"
    );
    const guidance = remoteConnectionStatus.querySelector(
      "[data-connection-guidance]"
    );
    if (stateLabel) {
      stateLabel.textContent = "Checking";
    }
    if (heading) {
      heading.textContent = "Impodo is checking the Odoo connection.";
    }
    if (guidance) {
      guidance.textContent = "This is a read-only check.";
    }
  };

  targetForm?.addEventListener("submit", (event) => {
    const button = event.submitter;
    if (!button?.matches("[data-test-connection-button]")) {
      return;
    }
    const action = document.createElement("input");
    action.type = "hidden";
    action.name = "action";
    action.value = "test";
    targetForm.append(action);
    targetForm.setAttribute("aria-busy", "true");
    const selectedMode = targetForm.querySelector(
      'input[name="odoo_connection_mode"]:checked'
    );
    if (selectedMode?.value === "REMOTE" && remoteConnectionStatus) {
      remoteConnectionStatus.hidden = false;
      markRemoteConnectionChecking();
    }
    button.disabled = true;
    button.textContent = "Testing connection...";
  });

  const localStackDialog = document.querySelector("[data-local-stack-dialog]");
  const localStackEntry = document.querySelector("[data-local-stack-entry]");
  const apiKeyEntries = Array.from(
    document.querySelectorAll("[data-api-key-entry]")
  );
  const connectionModes = Array.from(
    document.querySelectorAll('input[name="odoo_connection_mode"]')
  );
  const updateLocalStackVisibility = () => {
    const selected = connectionModes.find((control) => control.checked);
    const localMode = !selected || selected.value === "LOCAL";
    if (localStackEntry) {
      localStackEntry.hidden = !localMode;
    }
    for (const entry of apiKeyEntries) {
      entry.hidden = localMode && entry.dataset.showLocal !== "true";
    }
    if (remoteConnectionStatus) {
      remoteConnectionStatus.hidden = localMode;
    }
  };
  for (const control of connectionModes) {
    control.addEventListener("change", () => {
      updateLocalStackVisibility();
      resetRemoteConnectionStatus();
    });
  }
  updateLocalStackVisibility();

  for (const control of targetForm?.querySelectorAll(
    '[name="odoo_base_url"], [name="odoo_database"], [name="read_api_key"]'
  ) || []) {
    control.addEventListener("input", () => {
      const selected = connectionModes.find((item) => item.checked);
      if (selected?.value === "REMOTE") {
        resetRemoteConnectionStatus();
      }
    });
  }

  if (
    remoteConnectionStatus &&
    window.location.hash === "#remote-connection-status" &&
    !remoteConnectionStatus.hidden
  ) {
    window.requestAnimationFrame(() => remoteConnectionStatus.focus());
  }

  if (localStackDialog) {
    for (const trigger of document.querySelectorAll("[data-open-local-stack]")) {
      trigger.addEventListener("click", () => localStackDialog.showModal());
    }
    if (
      localStackDialog.dataset.autoOpen === "true" &&
      !localStackDialog.open
    ) {
      localStackDialog.showModal();
    }
  }

  for (const form of document.querySelectorAll("[data-start-stack-form]")) {
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-start-stack-button]");
      form.setAttribute("aria-busy", "true");
      if (button) {
        button.disabled = true;
        button.textContent = "Starting and checking…";
      }
    });
  }

  for (const form of document.querySelectorAll("[data-control-stack-form]")) {
    form.addEventListener("submit", (event) => {
      const action = event.submitter?.dataset.controlStackActionValue || "";
      const actionInput = form.querySelector("[data-control-stack-action]");
      if (actionInput) {
        actionInput.value = action;
      }
      form.setAttribute("aria-busy", "true");
      for (const button of form.querySelectorAll("[data-control-stack-button]")) {
        button.disabled = true;
      }
      if (event.submitter) {
        event.submitter.textContent =
          action === "restart"
            ? "Restarting and checking…"
            : "Stopping and checking…";
      }
    });
  }
});
