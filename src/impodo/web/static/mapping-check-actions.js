/* Keep the next-action colour on the latest edits' check or correction. */
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-mapping-form]");
  const review = form?.querySelector("[data-review-mapping-confirmation]");
  const check = form?.querySelector("[data-check-mapping]");
  const reviewPriority = review?.getAttribute("data-next-step");
  if (!reviewPriority || !check) return;

  const update = () => {
    if (form.hasAttribute("data-next-dirty") || check.disabled) {
      review.removeAttribute("data-next-step");
    } else {
      review.setAttribute("data-next-step", reviewPriority);
    }
  };
  const observer = new MutationObserver(update);
  observer.observe(form, { attributes: true, attributeFilter: ["data-next-dirty"] });
  observer.observe(check, { attributes: true, attributeFilter: ["disabled"] });
  update();
});
