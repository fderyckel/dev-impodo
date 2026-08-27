"use strict";

document.addEventListener("DOMContentLoaded", () => {
  for (const form of document.querySelectorAll("[data-single-submit]")) {
    let submitting = false;
    const button = form.querySelector('button[type="submit"]');
    const idleLabel = button?.textContent || "";

    form.addEventListener("submit", (event) => {
      if (submitting) {
        event.preventDefault();
        return;
      }
      submitting = true;
      form.setAttribute("aria-busy", "true");
      if (button) {
        button.disabled = true;
        button.textContent = button.dataset.submittingLabel || idleLabel;
      }
    });

    window.addEventListener("pageshow", () => {
      submitting = false;
      form.removeAttribute("aria-busy");
      if (button) {
        button.disabled = false;
        button.textContent = idleLabel;
      }
    });
  }

  const setupBlockers = document.querySelector(
    "[data-setup-blockers][data-auto-focus='true']"
  );
  setupBlockers?.focus();

  const readCredentialDialog = document.querySelector(
    "[data-read-credential-dialog]"
  );
  const readCredentialForm = readCredentialDialog?.querySelector(
    "[data-read-credential-form]"
  );
  const readCredentialInput = readCredentialDialog?.querySelector(
    "[data-read-credential-input]"
  );
  const readCredentialError = readCredentialDialog?.querySelector(
    "[data-read-credential-error]"
  );
  const readCredentialStatus = readCredentialDialog?.querySelector(
    "[data-read-credential-status]"
  );
  const saveReadCredential = readCredentialDialog?.querySelector(
    "[data-save-read-credential]"
  );
  let readCredentialReturnFocus = null;

  const showReadCredentialError = (message) => {
    if (!readCredentialError) {
      return;
    }
    readCredentialError.textContent = message;
    readCredentialError.hidden = false;
  };

  const openReadCredentialDialog = ({
    message = "",
    resume = "stay",
    trigger = null,
  } = {}) => {
    if (!readCredentialDialog || !readCredentialForm) {
      return false;
    }
    readCredentialReturnFocus = trigger || document.activeElement;
    readCredentialDialog.dataset.resume = resume;
    if (message) {
      showReadCredentialError(message);
    }
    if (!readCredentialDialog.open) {
      readCredentialDialog.showModal();
    }
    window.requestAnimationFrame(() => readCredentialInput?.focus());
    return true;
  };
  window.impodoOpenReadCredentialDialog = openReadCredentialDialog;

  for (const trigger of document.querySelectorAll(
    "[data-open-read-credential]"
  )) {
    trigger.addEventListener("click", () => {
      openReadCredentialDialog({ trigger });
    });
  }
  for (const close of readCredentialDialog?.querySelectorAll(
    "[data-close-read-credential]"
  ) || []) {
    close.addEventListener("click", () => readCredentialDialog?.close());
  }
  readCredentialDialog?.addEventListener("close", () => {
    if (readCredentialReturnFocus?.isConnected) {
      window.requestAnimationFrame(() => readCredentialReturnFocus.focus());
    }
    readCredentialReturnFocus = null;
  });

  readCredentialForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    readCredentialForm.setAttribute("aria-busy", "true");
    if (saveReadCredential) {
      saveReadCredential.disabled = true;
      saveReadCredential.textContent = "Saving key...";
    }
    if (readCredentialError) {
      readCredentialError.textContent = "";
      readCredentialError.hidden = true;
    }
    try {
      const response = await fetch(readCredentialForm.action, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: new FormData(readCredentialForm),
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok) {
        throw new Error(payload.detail || "The Odoo key could not be saved.");
      }
      if (readCredentialInput) {
        readCredentialInput.value = "";
      }
      if (readCredentialStatus) {
        readCredentialStatus.textContent =
          payload.message || "The read-only Odoo key is ready.";
      }
      const saved = new CustomEvent("impodo:read-credential-saved", {
        cancelable: true,
        detail: payload,
      });
      document.dispatchEvent(saved);
      readCredentialDialog?.close();
      if (saved.defaultPrevented) {
        return;
      }
      if (readCredentialDialog?.dataset.resume === "submit") {
        const resumeAction = readCredentialDialog.dataset.resumeAction || "";
        const resumeForm = Array.from(document.forms).find(
          (form) => form.getAttribute("action") === resumeAction
        );
        if (resumeForm) {
          resumeForm.requestSubmit();
          return;
        }
        window.location.assign(payload.return_to || window.location.href);
      }
    } catch (error) {
      showReadCredentialError(
        error instanceof Error
          ? error.message
          : "The Odoo key could not be saved."
      );
    } finally {
      readCredentialForm.removeAttribute("aria-busy");
      if (saveReadCredential) {
        saveReadCredential.disabled = false;
        saveReadCredential.textContent = "Save key";
      }
    }
  });

  if (readCredentialDialog?.dataset.autoOpen === "true") {
    openReadCredentialDialog({
      resume: readCredentialDialog.dataset.resume || "stay",
    });
  }

  const conceptDialogTriggers = new WeakMap();
  for (const trigger of document.querySelectorAll(
    "[data-concept-help-trigger]"
  )) {
    trigger.addEventListener("click", (event) => {
      const dialogId = trigger.getAttribute("aria-controls");
      const dialog = dialogId ? document.getElementById(dialogId) : null;
      if (!dialog || typeof dialog.showModal !== "function") {
        return;
      }
      event.preventDefault();
      conceptDialogTriggers.set(dialog, trigger);
      if (!dialog.open) {
        dialog.showModal();
      }
    });
  }

  for (const dialog of document.querySelectorAll(
    "[data-concept-help-dialog]"
  )) {
    dialog.addEventListener("close", () => {
      const trigger = conceptDialogTriggers.get(dialog);
      if (trigger?.isConnected) {
        window.requestAnimationFrame(() => trigger.focus());
      }
      conceptDialogTriggers.delete(dialog);
    });
  }

  const projectDeleteDialog = document.querySelector(
    "[data-project-delete-dialog]"
  );
  const projectDeleteTrigger = document.querySelector(
    "[data-project-delete-trigger]"
  );
  projectDeleteTrigger?.addEventListener("click", () => {
    if (typeof projectDeleteDialog?.showModal === "function") {
      projectDeleteDialog.showModal();
    }
  });

  const projectListDeleteDialog = document.querySelector(
    "[data-project-list-delete-dialog]"
  );
  const projectListDeleteTitle = projectListDeleteDialog?.querySelector(
    "[data-project-list-delete-title]"
  );
  const projectListDeleteConfirm = projectListDeleteDialog?.querySelector(
    "[data-project-list-delete-confirm]"
  );
  let pendingProjectDeleteForm = null;
  let pendingProjectDeleteTrigger = null;
  for (const trigger of document.querySelectorAll(
    "[data-project-list-delete-trigger]"
  )) {
    trigger.addEventListener("click", () => {
      const form = trigger.closest("[data-project-list-delete-form]");
      if (!form || typeof projectListDeleteDialog?.showModal !== "function") {
        return;
      }
      pendingProjectDeleteForm = form;
      pendingProjectDeleteTrigger = trigger;
      if (projectListDeleteTitle) {
        projectListDeleteTitle.textContent = `Delete ${form.dataset.projectName}?`;
      }
      projectListDeleteDialog.showModal();
    });
  }
  projectListDeleteConfirm?.addEventListener("click", () => {
    const form = pendingProjectDeleteForm;
    projectListDeleteDialog?.close();
    form?.requestSubmit();
  });
  projectListDeleteDialog?.addEventListener("close", () => {
    if (pendingProjectDeleteTrigger?.isConnected) {
      window.requestAnimationFrame(() => pendingProjectDeleteTrigger.focus());
    }
    pendingProjectDeleteForm = null;
    pendingProjectDeleteTrigger = null;
  });

  const sidebar = document.querySelector("#app-sidebar");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarToggleLabel = document.querySelector(
    "[data-sidebar-toggle-label]"
  );
  const mobileNavToggle = document.querySelector("[data-mobile-nav-toggle]");
  const sidebarScrim = document.querySelector("[data-sidebar-scrim]");
  const mobileViewport = window.matchMedia("(max-width: 860px)");
  const sidebarStorageKey = "impodo.sidebar.collapsed";

  const storedSidebarState = () => {
    try {
      return window.localStorage.getItem(sidebarStorageKey) === "true";
    } catch {
      return false;
    }
  };

  const persistSidebarState = (collapsed) => {
    try {
      window.localStorage.setItem(sidebarStorageKey, String(collapsed));
    } catch {
      // The navigation still works when browser storage is unavailable.
    }
  };

  const setSidebarCollapsed = (collapsed, persist = true) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    sidebarToggle?.setAttribute("aria-expanded", String(!collapsed));
    if (sidebarToggle) {
      sidebarToggle.title = collapsed
        ? "Expand navigation"
        : "Collapse navigation";
    }
    if (sidebarToggleLabel) {
      sidebarToggleLabel.textContent = collapsed
        ? "Expand navigation"
        : "Collapse navigation";
    }
    if (persist) {
      persistSidebarState(collapsed);
    }
  };

  const setMobileNavigationOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    mobileNavToggle?.setAttribute("aria-expanded", String(open));
    sidebarToggle?.setAttribute("aria-expanded", String(open));
    if (sidebarToggle) {
      sidebarToggle.title = "Close navigation";
    }
    if (sidebarToggleLabel) {
      sidebarToggleLabel.textContent = "Close navigation";
    }
  };

  if (sidebar) {
    setSidebarCollapsed(storedSidebarState(), false);

    sidebarToggle?.addEventListener("click", () => {
      if (mobileViewport.matches) {
        setMobileNavigationOpen(false);
        mobileNavToggle?.focus();
        return;
      }
      setSidebarCollapsed(
        !document.body.classList.contains("sidebar-collapsed")
      );
    });

    mobileNavToggle?.addEventListener("click", () => {
      setMobileNavigationOpen(
        !document.body.classList.contains("sidebar-open")
      );
    });

    sidebarScrim?.addEventListener("click", () => {
      setMobileNavigationOpen(false);
      mobileNavToggle?.focus();
    });

    for (const link of sidebar.querySelectorAll("a")) {
      link.addEventListener("click", () => {
        if (mobileViewport.matches) {
          setMobileNavigationOpen(false);
        }
      });
    }

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        document.body.classList.contains("sidebar-open")
      ) {
        setMobileNavigationOpen(false);
        mobileNavToggle?.focus();
      }
    });

    mobileViewport.addEventListener("change", () => {
      setMobileNavigationOpen(false);
      if (!mobileViewport.matches) {
        setSidebarCollapsed(storedSidebarState(), false);
      }
    });
  }

});
