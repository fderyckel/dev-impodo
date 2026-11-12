"use strict";

for (const panel of document.querySelectorAll("[data-attention-export]")) {
  const status = panel.querySelector("[data-attention-export-counts]");
  const retry = panel.querySelector("[data-attention-export-retry]");
  const preview = document.querySelector("[data-attention-export-preview]");
  const problems = preview.querySelector("[data-attention-export-problems]");
  const updateCounts = async () => {
    retry.hidden = true;
    retry.disabled = true;
    try {
      const response = await fetch(status.dataset.endpoint, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Reopen Final review to refresh the export counts.");
      const recordWord = result.affected_records === 1 ? "record" : "records";
      const findingWord = result.findings === 1 ? "finding" : "findings";
      status.textContent = `${result.affected_records.toLocaleString()} affected ${recordWord} · ${result.findings.toLocaleString()} ${findingWord} across all tables and pages.`;
      problems.replaceChildren();
      for (const problem of result.problems) {
        const item = document.createElement("li");
        const scope = problem.affected_records ? `${problem.affected_records.toLocaleString()} ${problem.affected_records === 1 ? "record" : "records"}` : "Whole table or load";
        item.textContent = `${scope}: ${problem.problem}. ${problem.action} Where to correct: ${problem.correction}.`;
        problems.append(item);
      }
      preview.hidden = result.problems.length === 0;
    } catch (error) {
      preview.hidden = true;
      status.textContent = error.message || "Export counts could not be checked. Retry or reopen Final review.";
      retry.hidden = false;
    } finally {
      retry.disabled = false;
    }
  };
  retry.addEventListener("click", updateCounts);
  updateCounts();
}
