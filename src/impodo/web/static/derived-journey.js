"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const root = document.querySelector("[data-source-journey]");
  if (!root) return;
  const operations = Array.from(root.querySelectorAll("[data-source-operation]"));
  const editors = Array.from(root.querySelectorAll("form[data-source-editor]"));
  const reviews = Array.from(root.querySelectorAll("[data-source-review]"));
  const state = root.querySelector("[data-source-state]");
  const next = root.querySelector("[data-source-next]");
  const another = root.querySelector("[data-source-add-another]");
  const bar = root.querySelector("[data-source-action-bar]");
  if (!bar || !next) return;

  let notice = root.querySelector("[data-source-notice]");
  if (!notice) {
    notice = document.createElement("p");
    notice.dataset.sourceNotice = "";
    notice.setAttribute("role", "alert");
    notice.tabIndex = -1;
    notice.hidden = true;
    bar.prepend(notice);
  }
  let discard = root.querySelector("[data-source-discard]");
  if (!discard) {
    discard = document.createElement("button");
    discard.type = "button";
    discard.className = "button secondary";
    discard.dataset.sourceDiscard = "";
    discard.textContent = "Discard unsaved change";
    discard.hidden = true;
    bar.querySelector(".actions")?.prepend(discard);
  }

  const storageKey = `impodo.source.journey:${root.dataset.sourcePositionKey}`;
  const sourcePath = `/workspaces/${root.dataset.sourcePositionKey}/derived-entities`;
  const draftKey = `${storageKey}:odoo-return`;
  const baselines = new Map();
  const reviewed = new Map();
  const lockedEditors = new Map();
  const lockedControls = new Set();
  let active = null;
  let currentEditor = null;
  let busy = false;
  let leaving = false;
  let uncertain = false;
  let submittedVersion = "";
  let rejected = root.dataset.sourceError === "true";

  const fingerprint = (form) => lockedEditors.get(form) ?? JSON.stringify(Array.from(new FormData(form)).filter(
    ([name]) => !["csrf_token", "expected_parent_version", "return_to"].includes(name)
  ));
  for (const editor of editors) baselines.set(editor, fingerprint(editor));
  const operationFor = (element) => element?.closest("[data-source-operation]");
  let restoredEditor = null;
  try {
    const draft = JSON.parse(sessionStorage.getItem(draftKey) || "null");
    if (draft) {
      restoredEditor = editors.find((form) => new URL(form.action).pathname === draft.action
        && (form.dataset.sourceSourceId || "") === draft.sourceId);
      if (restoredEditor) {
        const values = new Map();
        for (const [name, value] of draft.entries) {
          if (name === "csrf_token" || name === "return_to") continue;
          values.set(name, [...(values.get(name) || []), value]);
        }
        for (const control of restoredEditor.elements) {
          if (!values.has(control.name)) continue;
          const items = values.get(control.name);
          if (control.type === "checkbox" || control.type === "radio") control.checked = items.includes(control.value);
          else control.value = items[0];
        }
        currentEditor = restoredEditor;
        rejected = true;
      }
      sessionStorage.removeItem(draftKey);
    }
  } catch { /* The server's current choices remain available. */ }
  const rememberDetour = () => {
    const form = dirtyEditor() || currentEditor;
    if (!form) return false;
    try {
      sessionStorage.setItem(draftKey, JSON.stringify({
        action: new URL(form.action).pathname,
        sourceId: form.dataset.sourceSourceId || "",
        entries: Array.from(new FormData(form)).filter(([name]) => name !== "csrf_token"),
      }));
      leaving = true;
      return true;
    } catch {
      showNotice("Keep this page open. Your selections could not be retained for the return from Odoo access.");
      return false;
    }
  };
  const editorFor = (review) => {
    const save = review.querySelector("form[data-source-save]");
    const candidates = editors.filter((form) => operationFor(form) === operationFor(review));
    return candidates.find((form) => ["source_dataset_id", "source_binding"].every((name) => {
      const value = save?.elements.namedItem(name)?.value;
      return !value || form.elements.namedItem(name)?.value === value;
    })) || candidates[0];
  };
  for (const review of reviews) {
    const editor = editorFor(review);
    const button = review.querySelector("form[data-source-save] button[type=submit]");
    if (editor) reviewed.set(review, { editor, fingerprint: fingerprint(editor), canSave: Boolean(button && !button.disabled) });
  }
  const dirtyEditor = () => editors.find((form) => fingerprint(form) !== baselines.get(form));
  const pendingReview = () => reviews.find((review) => operationFor(review) === active);
  const validReview = () => {
    const review = pendingReview();
    const record = reviewed.get(review);
    return review && record && fingerprint(record.editor) === record.fingerprint
      && record.canSave
      ? review : null;
  };
  const hasWork = () => Boolean(dirtyEditor() || reviews.length || rejected || uncertain);
  const reveal = (element, focus = true) => {
    if (!element) return;
    for (let parent = element; parent && parent !== root; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
    }
    requestAnimationFrame(() => {
      element.scrollIntoView({ block: "start", behavior: "auto" });
      if (focus) {
        const editor = element.matches("form[data-source-editor]") ? element
          : element.querySelector("form[data-source-editor]");
        const target = element.matches("input,select,button,a,summary") ? element
          : editor?.querySelector("input:not([type=hidden]):not(:disabled), select:not(:disabled), textarea:not(:disabled)")
            || element.querySelector("h2, h3") || element.querySelector("summary, button");
        if (target && !target.matches("input,select,button,a,summary")) {
          target.tabIndex = -1;
          target.dataset.sourceFocusTarget = "";
        }
        target?.focus({ preventScroll: true });
      }
    });
  };
  const showNotice = (message) => {
    notice.textContent = message;
    notice.hidden = false;
    notice.tabIndex = -1;
    notice.focus({ preventScroll: true });
  };
  const clearNotice = () => { notice.hidden = true; };
  const lockEditors = (locked) => {
    if (locked && !lockedEditors.size) {
      for (const form of editors) lockedEditors.set(form, fingerprint(form));
      for (const form of editors) {
        for (const control of form.elements) {
          if (control.matches("input:not([type=hidden]), select, textarea, button") && !control.disabled) {
            lockedControls.add(control);
            control.disabled = true;
          }
        }
      }
    } else if (!locked && lockedEditors.size) {
      for (const control of lockedControls) control.disabled = false;
      lockedControls.clear();
      lockedEditors.clear();
    }
  };
  const update = () => {
    lockEditors(busy || uncertain);
    const dirty = Boolean(dirtyEditor() || rejected);
    const ready = validReview();
    root.dataset.sourceActive = active?.id || "";
    root.dataset.sourceBusy = String(busy);
    if (state) state.textContent = busy ? "Saving…" : uncertain ? "Save not confirmed"
      : dirty ? "Unsaved changes" : ready ? "Ready to use"
      : pendingReview() ? "Needs attention" : "Saved";
    next.textContent = dirty || (pendingReview() && !ready) ? "Review result"
      : ready ? "Save and review changes" : "Continue to Odoo data";
    next.setAttribute("aria-label", next.textContent);
    next.setAttribute("aria-disabled", String(busy || uncertain));
    if (another) another.disabled = busy || uncertain;
    if (another) another.textContent = ready ? "Save and add another" : "Add another change";
    discard.hidden = !hasWork() || busy || uncertain;
    for (const form of editors) form.inert = busy || uncertain;
    for (const review of reviews) {
      const form = review.querySelector("form[data-source-save]");
      if (form) form.inert = busy || uncertain;
      for (const button of form?.querySelectorAll("button[type=submit]") || []) {
        button.disabled = busy || uncertain || !reviewed.get(review)?.canSave;
      }
    }
    for (const review of reviews) {
      const record = reviewed.get(review);
      const stale = record && fingerprint(record.editor) !== record.fingerprint;
      review.hidden = Boolean(stale);
    }
    for (const button of root.querySelectorAll("[data-derived-entity-target]")) {
      button.disabled = busy || uncertain;
      if (operationFor(document.getElementById(button.dataset.derivedEntityTarget)) === active) {
        button.setAttribute("aria-current", "step");
      } else button.removeAttribute("aria-current");
    }
  };
  const activate = (target, { force = false } = {}) => {
    if ((busy || uncertain) && !force) return false;
    const operation = operationFor(target);
    if (active !== operation && hasWork() && !force) {
      showNotice("Review and use your current change first, or discard it.");
      return false;
    }
    active = operation;
    for (const item of operations) {
      item.hidden = item !== active;
      if (item === active) item.open = true;
    }
    if (target?.hasAttribute("data-source-saved")) {
      for (const item of operations) item.hidden = true;
    }
    const targetForm = target?.matches("form[data-source-editor]") ? target
      : Array.from(target?.querySelectorAll("form[data-source-editor]") || []).find(
        (form) => form.dataset.sourceSourceId === root.dataset.sourceCurrentSourceId
      ) || target?.querySelector("form[data-source-editor]");
    if (targetForm) {
      currentEditor = targetForm;
      targetForm.hidden = false;
    }
    update();
    reveal(target);
    return true;
  };

  window.impodoSourceJourney = {
    reveal(id) {
      const target = document.getElementById(id);
      if (!target) return;
      if (id === "source-operation-choices" && hasWork()) {
        showNotice("Review and use your current change first, or discard it.");
        return;
      }
      activate(target);
    },
  };

  const reviewCurrent = () => {
    const form = dirtyEditor() || currentEditor || active?.querySelector("form[data-source-editor]");
    if (!form) {
      reveal(active);
      return;
    }
    currentEditor = form;
    form.hidden = false;
    activate(form, { force: true });
    reveal(form, false);
    form.requestSubmit();
  };
  const save = async (form, destination = "saved") => {
    if (busy || uncertain) return;
    if (!validReview()) {
      showNotice("Review your latest selections before using this table.");
      reviewCurrent();
      return;
    }
    busy = true;
    clearNotice();
    update();
    const body = new FormData(form);
    submittedVersion = body.get("expected_parent_version") || "";
    // Every save returns to Stage 1. Continuing to Stage 2 is a separate action.
    body.set("return_to", "");
    document.dispatchEvent(new CustomEvent("impodo:operation-started"));
    try {
      const response = await fetch(form.action, {
        method: "POST", body, credentials: "same-origin", headers: { Accept: "text/html" },
      });
      window.impodoServerRecovery?.noteResponse?.(response);
      const url = new URL(response.url);
      const savedPage = url.origin === location.origin && url.pathname === sourcePath;
      if (response.ok && response.redirected && savedPage) {
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        const name = body.get("output_dataset_name") || body.get("parent_dataset_name");
        const card = Array.from(html.querySelectorAll("[data-source-saved]")).find(
          (element) => element.dataset.sourceName === name
        );
        url.hash = destination === "summary" ? "saved-source-changes"
          : card?.id || "saved-source-changes";
        try { sessionStorage.setItem(storageKey, destination); } catch { /* Navigation still works. */ }
        leaving = true;
        location.assign(url.href);
        return;
      }
      if (response.status === 422 || response.status === 409 || response.status === 403) {
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        const message = html.querySelector('main [role="alert"] span')?.textContent
          || html.querySelector('main [role="alert"]')?.textContent;
        showNotice(message?.trim() || "This change could not be saved. Review your selections and try again.");
      } else if (response.status === 401 || !response.redirected && response.ok) {
        showNotice("Your session could not confirm this save. Your selections are still here.");
        uncertain = true;
      } else {
        uncertain = true;
        showNotice("Save not confirmed. Check saved changes before trying again.");
      }
    } catch {
      uncertain = true;
      showNotice("Save not confirmed. Check saved changes before trying again.");
    } finally {
      busy = false;
      update();
      document.dispatchEvent(new CustomEvent("impodo:operation-finished"));
      if (uncertain) showReadBack();
    }
  };
  const showReadBack = () => {
    if (bar.querySelector("[data-source-read-back]")) return;
    const check = document.createElement("button");
    check.type = "button";
    check.className = "button secondary";
    check.dataset.sourceReadBack = "";
    check.textContent = "Check saved changes";
    check.addEventListener("click", async () => {
      check.disabled = true;
      try {
        const response = await fetch(sourcePath, { credentials: "same-origin", cache: "no-store" });
        if (!response.ok || response.redirected) throw new Error("Read-back unavailable");
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        const submitted = submittedVersion;
        const current = html.querySelector('[name="expected_parent_version"]')?.value || "";
        if (submitted !== current) {
          showNotice("Saved changes are available. Open them to check the result before making another change.");
          const open = document.createElement("a");
          open.className = "button secondary";
          open.textContent = "Open saved changes";
          open.href = `${sourcePath}#saved-source-changes`;
          open.dataset.sourceOpenSaved = "";
          open.addEventListener("click", () => { leaving = true; });
          check.replaceWith(open);
        } else {
          uncertain = false;
          check.remove();
          showNotice("This change was not saved. Your selections are ready to try again.");
          update();
        }
      } catch {
        showNotice("Saved changes could not be checked. Keep this page open and try again.");
      } finally { check.disabled = false; }
    });
    bar.querySelector(".actions")?.prepend(check);
  };

  for (const editor of editors) {
    const changed = () => {
      currentEditor = editor;
      active = operationFor(editor);
      for (const choice of active?.querySelectorAll(".source-preparation-choice") || []) {
        if (!choice.contains(editor)) choice.open = false;
      }
      rejected = false;
      clearNotice();
      update();
    };
    editor.addEventListener("input", changed);
    editor.addEventListener("change", changed);
    editor.addEventListener("submit", () => { leaving = true; });
    editor.addEventListener("invalid", (event) => {
      leaving = false;
      editor.hidden = false;
      reveal(event.target);
    }, true);
  }
  for (const form of root.querySelectorAll("form[data-source-save]")) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      save(form);
    });
  }
  root.addEventListener("submit", (event) => {
    const form = event.target;
    if (busy || uncertain) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (form.matches("[data-source-editor], [data-source-save]") || !hasWork()) return;
    if (!busy && !uncertain && new URL(form.action).pathname === `${sourcePath}/models/refresh`
      && rememberDetour()) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    showNotice("Review and use your current change first, or discard it.");
  }, true);
  next.addEventListener("click", (event) => {
    if (!hasWork()) return;
    event.preventDefault();
    if (busy || uncertain) return;
    const review = validReview();
    if (review) save(review.querySelector("form[data-source-save]"), "summary");
    else reviewCurrent();
  });
  another?.addEventListener("click", () => {
    if (busy || uncertain) return;
    const review = validReview();
    if (review) save(review.querySelector("form[data-source-save]"), "another");
    else if (hasWork()) reviewCurrent();
    else activate(root.querySelector("[data-source-choices]"));
  });
  discard.addEventListener("click", () => {
    leaving = true;
    location.assign(sourcePath);
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || link === next || !hasWork() || leaving || link.target === "_blank"
      || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
      || event.button !== 0 || link.hasAttribute("download")) return;
    const url = new URL(link.href);
    if (link.hasAttribute("data-source-open-saved") && url.origin === location.origin
      && url.pathname === sourcePath && url.hash === "#saved-source-changes") {
      leaving = true;
      return;
    }
    if (!busy && !uncertain && url.origin === location.origin
      && url.pathname === `/workspaces/${root.dataset.sourcePositionKey}/target`
      && ["source-hierarchy", "source-lookup"].includes(url.searchParams.get("return_to"))
      && rememberDetour()) return;
    if (url.origin === location.origin && url.pathname === location.pathname && url.hash) return;
    event.preventDefault();
    showNotice("Review and use your current change first, or discard it.");
  }, true);
  root.addEventListener("click", (event) => {
    const summary = event.target.closest("summary");
    const operation = operationFor(summary);
    const nestedForm = summary?.parentElement.querySelector("form[data-source-editor]");
    const changed = dirtyEditor();
    if (summary && nestedForm && changed && changed !== nestedForm) {
      event.preventDefault();
      showNotice("Review and use your current change first, or discard it.");
      return;
    }
    if (!operation || summary.parentElement !== operation || operation === active) return;
    event.preventDefault();
    activate(operation);
  });
  window.addEventListener("beforeunload", (event) => {
    if (leaving || !hasWork()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    busy = false;
    leaving = false;
    update();
  });

  root.classList.add("source-journey-ready");
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => document.documentElement.style.setProperty(
      "--source-action-height", `${bar.getBoundingClientRect().height + 24}px`
    )).observe(bar);
  }
  let savedIntent = "";
  try { savedIntent = sessionStorage.getItem(storageKey) || ""; sessionStorage.removeItem(storageKey); } catch { /* Optional position hint. */ }
  let hash = "";
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch { /* Ignore malformed section links. */ }
  const preview = reviews[0];
  const target = savedIntent === "another" && !hasWork() ? root.querySelector("[data-source-choices]")
    : restoredEditor || document.getElementById(hash) || preview
      || document.getElementById(root.dataset.sourceCurrentOperation || "");
  if (preview) {
    currentEditor = editorFor(preview);
    if (currentEditor) currentEditor.hidden = true;
  }
  if (target) activate(target, { force: true });
  else activate(null, { force: true });
  if (rejected && currentEditor) reveal(currentEditor);
});
