"use strict";

const reviewWorkbookForms = document.querySelectorAll("[data-review-workbook-form]");
let creatingReviewWorkbook = false;

for (const form of reviewWorkbookForms) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (creatingReviewWorkbook) return;

    const status = form.querySelector("[data-review-workbook-status]");
    creatingReviewWorkbook = true;
    status.textContent = "Creating review workbook…";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { Accept: "application/json" },
        body: new FormData(form),
      });
      const result = await response.json();
      if (!response.ok) {
        status.textContent = result.detail || "Try creating the review workbook again.";
        return;
      }
      for (const download of document.querySelectorAll("[data-review-workbook-download]")) {
        download.hidden = false;
      }
      for (const workbookForm of reviewWorkbookForms) {
        workbookForm.querySelector("button").textContent = "Recreate review workbook";
      }
      status.textContent = result.message;
    } catch {
      status.textContent = "Try creating the review workbook again.";
    } finally {
      creatingReviewWorkbook = false;
    }
  });
}
