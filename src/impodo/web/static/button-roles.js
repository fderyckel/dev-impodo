/* Colour expresses purpose; this module never submits, navigates, or saves. */
"use strict";

(() => {
  const visible = (element) => Boolean(element.getClientRects().length) &&
    window.getComputedStyle(element).visibility !== "hidden";
  const selectNext = (candidates, { visible: isVisible = visible, scope = null } = {}) => {
    let selected = null;
    let priority = -Infinity;
    for (const candidate of candidates) {
      if ((scope && !scope.contains(candidate)) || !isVisible(candidate) ||
          candidate.disabled || candidate.getAttribute("aria-disabled") === "true") continue;
      const rank = Number(candidate.getAttribute("data-next-step")) +
        (candidate.form?.hasAttribute("data-next-dirty") ? 1000 : 0);
      if (Number.isFinite(rank) && rank >= priority) {
        selected = candidate;
        priority = rank;
      }
    }
    return selected;
  };
  const bind = (root = document.body) => {
    let queued = false;
    let stopped = false;
    const update = () => {
      queued = false;
      if (stopped) return;
      const dialogs = Array.from(root.querySelectorAll("dialog[open]")).filter(visible);
      const recovery = Array.from(root.querySelectorAll("[data-next-scope]")).filter(visible);
      const scope = dialogs.at(-1) || recovery.at(-1) || null;
      const candidates = Array.from(root.querySelectorAll(".button[data-next-step]"));
      const selected = selectNext(candidates, { scope });
      for (const previous of root.querySelectorAll("[data-next-active]")) {
        if (previous !== selected) previous.removeAttribute("data-next-active");
      }
      if (selected && !selected.hasAttribute("data-next-active")) {
        selected.setAttribute("data-next-active", "");
      }
    };
    const schedule = () => {
      if (queued || stopped) return;
      queued = true;
      window.queueMicrotask(update);
    };
    const editing = (event) => {
      const control = event.target;
      if (control.name && !control.disabled && control.form?.querySelector("[data-next-step]")) {
        control.form.setAttribute("data-next-dirty", "");
      }
      schedule();
    };
    const resetting = (event) => window.queueMicrotask(() => {
      if (!event.defaultPrevented) event.target.removeAttribute("data-next-dirty");
      schedule();
    });
    const observer = new MutationObserver(schedule);
    observer.observe(root, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ["hidden", "open", "disabled", "aria-disabled", "class", "style", "data-next-step", "data-next-dirty", "data-next-scope"],
    });
    for (const event of ["toggle", "click"]) root.addEventListener(event, schedule, true);
    for (const event of ["input", "change"]) root.addEventListener(event, editing, true);
    root.addEventListener("reset", resetting, true);
    window.addEventListener("resize", schedule);
    update();
    return {
      update,
      disconnect() {
        stopped = true;
        observer.disconnect();
        for (const event of ["toggle", "click"]) root.removeEventListener(event, schedule, true);
        for (const event of ["input", "change"]) root.removeEventListener(event, editing, true);
        root.removeEventListener("reset", resetting, true);
        window.removeEventListener("resize", schedule);
      },
    };
  };
  window.ImpodoButtonRoles = { bind, selectNext };
  document.addEventListener("DOMContentLoaded", () => bind());
})();
