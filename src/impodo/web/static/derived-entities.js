"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const revealTarget = (id) => {
    const target = document.getElementById(id);
    if (!target) {
      return;
    }
    for (let parent = target; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) {
        parent.open = true;
      }
    }
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  };

  const revealCurrentTarget = () => {
    let id = "";
    if (window.location.hash.length > 1) {
      try {
        id = decodeURIComponent(window.location.hash.slice(1));
      } catch (_error) {
        id = "";
      }
    }
    const preview = document.querySelector("[data-derived-entity-preview]");
    revealTarget(id || preview?.id);
  };

  for (const trigger of document.querySelectorAll("[data-derived-entity-target]")) {
    trigger.addEventListener("click", () => {
      revealTarget(trigger.dataset.derivedEntityTarget);
    });
  }

  window.addEventListener("hashchange", revealCurrentTarget);
  window.addEventListener("pageshow", revealCurrentTarget);
  revealCurrentTarget();
});
