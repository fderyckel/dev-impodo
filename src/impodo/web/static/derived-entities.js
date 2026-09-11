"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const sections = new Map(
    Array.from(document.querySelectorAll("[data-derived-entity-section]"))
      .filter((section) => section.id)
      .map((section) => [section.id, section])
  );

  for (const trigger of document.querySelectorAll(
    "[data-derived-entity-trigger]"
  )) {
    trigger.addEventListener("click", () => {
      const sectionId = trigger.hash.slice(1);
      const section = sections.get(sectionId);
      if (section) {
        section.open = true;
      }
    });
  }
});
