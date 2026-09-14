"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const announceOperation = (detail) => {
    document.dispatchEvent(
      new CustomEvent("impodo:operation-started", { detail })
    );
  };

  document.addEventListener(
    "click",
    (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        event.altKey
      ) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const link = target.closest("a[href]");
      if (
        !link ||
        link.hasAttribute("download") ||
        (link.target && link.target.toLowerCase() !== "_self")
      ) return;
      const destination = new URL(link.href, window.location.href);
      if (
        destination.origin !== window.location.origin ||
        (destination.pathname === window.location.pathname &&
          destination.search === window.location.search &&
          destination.hash)
      ) return;
      queueMicrotask(() => {
        if (!event.defaultPrevented) {
          announceOperation({
            label: "Opening the requested page",
            mutation: false,
          });
        }
      });
    },
    { capture: true }
  );

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.target && form.target.toLowerCase() !== "_self") return;
      const destination = new URL(form.action || window.location.href);
      if (destination.origin !== window.location.origin) return;
      const method = (form.method || "get").toLowerCase();
      if (method === "dialog") return;
      queueMicrotask(() => {
        if (event.defaultPrevented) return;
        const button = event.submitter;
        announceOperation({
          label: button?.dataset.submittingLabel || "Completing the requested step",
          mutation: method !== "get",
        });
      });
    },
    { capture: true }
  );
});
