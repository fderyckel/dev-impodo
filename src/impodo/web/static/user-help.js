"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const helpSlot = document.querySelector("[data-page-help-slot]");
  const pageTitle = document.querySelector(
    ".page-heading h1, .projects-hero h1"
  );
  if (!helpSlot || !pageTitle) {
    return;
  }

  const existingHeadingRow = pageTitle.closest(".heading-with-help");
  if (existingHeadingRow) {
    existingHeadingRow.append(helpSlot);
    helpSlot.classList.remove("page-help-fallback");
    return;
  }

  const headingRow = document.createElement("div");
  headingRow.className = "heading-with-user-help";
  pageTitle.before(headingRow);
  headingRow.append(pageTitle, helpSlot);
  helpSlot.classList.remove("page-help-fallback");
});
