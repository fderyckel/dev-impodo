/* Stage 2 locations reveal existing forms without saving or reading Odoo. */
"use strict";

(() => {
  const steps = [
    ["records", "Choose records"],
    ["related", "Related values"],
    ["matching", "Matching rules"],
  ];
  const validStep = (value) => steps.some(([key]) => key === value);

  const bind = (root) => {
    if (root.impodoSchemaJourney) return root.impodoSchemaJourney;
    const panels = Array.from(root.querySelectorAll("[data-schema-step-panel]"));
    const cards = Array.from(root.querySelectorAll("[data-schema-key-card]"));
    const title = root.querySelector("[data-schema-step-title]");
    const heading = root.querySelector("[data-schema-heading]");
    const initialStep = validStep(root.dataset.step) ? root.dataset.step : "records";
    const pageTitle = document.title;
    let firstInvalid = null;

    const locationFor = (href) => {
      try {
        const url = new URL(href, window.location.href);
        return url.origin === window.location.origin &&
          url.pathname === window.location.pathname ? url : null;
      } catch (_error) {
        return null;
      }
    };
    const focus = (element, scroll = true) => {
      if (!element) return;
      if (!element.hasAttribute("tabindex") && !element.matches("input, select, textarea, button, a, summary")) {
        element.setAttribute("tabindex", "-1");
      }
      if (scroll) element.scrollIntoView({ block: "start" });
      element.focus({ preventScroll: true });
    };
    const show = (step, { focusHeading = false } = {}) => {
      if (!validStep(step)) return;
      root.dataset.step = step;
      for (const panel of panels) panel.hidden = panel.dataset.schemaStepPanel !== step;
      const index = steps.findIndex(([key]) => key === step);
      const label = steps[index][1];
      if (title) title.textContent = `Step ${index + 1} of ${steps.length} · ${label}`;
      document.title = `${label} · ${pageTitle}`;
      for (const link of document.querySelectorAll("a.sidebar-subnav-link[href]")) {
        const url = locationFor(link.href);
        if (!url || !validStep(url.searchParams.get("step"))) continue;
        const current = url.searchParams.get("step") === step;
        link.classList.toggle("active", current);
        if (current) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      }
      if (focusHeading) focus(heading);
    };
    const reveal = (element, { focusTarget = false, scroll = true } = {}) => {
      const panel = element?.closest("[data-schema-step-panel]");
      if (!panel) return false;
      show(panel.dataset.schemaStepPanel);
      let ancestor = element;
      while (ancestor && ancestor !== root) {
        if (ancestor.tagName === "DETAILS") ancestor.open = true;
        ancestor = ancestor.parentElement;
      }
      if (focusTarget) focus(element, scroll);
      return true;
    };
    const hashTarget = (url) => {
      if (!url.hash) return null;
      try {
        return document.getElementById(decodeURIComponent(url.hash.slice(1)));
      } catch (_error) {
        return null;
      }
    };
    const restoreLocation = ({ initial = false } = {}) => {
      const url = new URL(window.location.href);
      const target = hashTarget(url);
      if (target && reveal(target, { focusTarget: !initial })) return;
      const step = url.searchParams.get("step");
      show(validStep(step) ? step : initialStep, { focusHeading: !initial });
    };

    for (const card of cards) {
      card.addEventListener("toggle", () => {
        if (card.open) for (const other of cards) if (other !== card) other.open = false;
      });
    }
    const cardWithError = cards.find((card) => card.querySelector("[data-key-server-error]"));
    const initialCard = cardWithError || cards.find((card) => card.open) ||
      cards.find((card) => card.dataset.keyConfirmed !== "true") || cards[0];
    for (const card of cards) card.open = card === initialCard;

    document.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target.closest?.("a[href]");
      if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
      const url = locationFor(link.href);
      if (!url) return;
      const target = hashTarget(url);
      const step = url.searchParams.get("step");
      const panel = target?.closest("[data-schema-step-panel]");
      if (!panel && !validStep(step)) return;
      event.preventDefault();
      if (root.querySelector('[aria-busy="true"]')) return;
      if (panel) {
        url.searchParams.set("step", panel.dataset.schemaStepPanel);
        window.history.pushState(window.history.state, "", url.href);
        reveal(target, { focusTarget: true });
      } else {
        url.hash = "";
        window.history.pushState(window.history.state, "", url.href);
        show(step, { focusHeading: true });
      }
    });
    root.addEventListener("invalid", (event) => {
      if (firstInvalid) return;
      firstInvalid = event.target;
      reveal(firstInvalid, { focusTarget: true });
      window.queueMicrotask(() => { firstInvalid = null; });
    }, true);
    document.addEventListener("submit", (event) => {
      if (!event.defaultPrevented) return;
      const error = Array.from(event.target.querySelectorAll("[data-key-field-error]"))
        .find((item) => !item.hidden);
      if (error) reveal(error, { focusTarget: true });
    });
    window.addEventListener("popstate", () => restoreLocation());
    window.addEventListener("hashchange", () => restoreLocation());
    restoreLocation({ initial: true });
    const error = root.querySelector("[data-schema-load-error], [data-supporting-save-error], [data-key-server-error]");
    if (error) reveal(error);
    root.impodoSchemaJourney = { show, reveal };
    return root.impodoSchemaJourney;
  };

  window.ImpodoSchemaJourney = { bind };
  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector("[data-schema-journey]");
    if (root) bind(root);
  });
})();
