"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const transformationImpactForm = document.querySelector(
    "[data-transformation-impact-prepare]"
  );
  transformationImpactForm?.addEventListener("submit", (event) => {
    if (transformationImpactForm.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }
    const button = transformationImpactForm.querySelector(
      "[data-transformation-impact-button]"
    );
    const status = document.querySelector(
      "[data-transformation-impact-status]"
    );
    transformationImpactForm.setAttribute("aria-busy", "true");
    if (button) {
      button.disabled = true;
      button.textContent = "Preparing the comparison…";
    }
    if (status) {
      status.hidden = false;
    }
  });

});
