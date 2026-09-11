"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const unaryOperators = new Set([
    "is_blank",
    "is_not_blank",
    "is_true",
    "is_false",
  ]);

  for (const editor of document.querySelectorAll("[data-row-inclusion-editor]")) {
    const modes = Array.from(
      editor.querySelectorAll("[data-row-inclusion-mode]")
    );
    const builder = editor.querySelector("[data-row-inclusion-builder]");
    const rows = Array.from(
      editor.querySelectorAll("[data-row-inclusion-condition]")
    );
    const join = editor.querySelector("[data-row-inclusion-join]");
    const single = editor.querySelector("[data-row-inclusion-single-sentence]");
    const add = editor.querySelector("[data-add-row-inclusion-condition]");

    const selectedMode = () =>
      modes.find((control) => control.checked)?.value || "all_rows";

    const syncOperator = (row) => {
      const operator = row.querySelector("[data-row-inclusion-operator]");
      const value = row.querySelector("[data-row-inclusion-value]");
      const wrap = row.querySelector("[data-row-inclusion-value-wrap]");
      const unary = unaryOperators.has(operator?.value || "");
      if (wrap) wrap.hidden = unary;
      if (value) value.disabled = row.hidden || selectedMode() !== "matching_rows" || unary;
    };

    const sync = () => {
      const matching = selectedMode() === "matching_rows";
      if (builder) builder.hidden = !matching;
      const visibleRows = rows.filter((row) => !row.hidden);
      for (const row of rows) {
        for (const control of row.querySelectorAll("[name]")) {
          control.disabled = !matching || row.hidden;
        }
        syncOperator(row);
      }
      if (join) join.hidden = visibleRows.length < 2;
      if (single) single.hidden = visibleRows.length > 1;
      if (add) add.disabled = visibleRows.length >= rows.length;
      const joinControl = join?.querySelector("select");
      if (joinControl) joinControl.disabled = !matching;
    };

    for (const mode of modes) mode.addEventListener("change", sync);
    for (const row of rows) {
      row
        .querySelector("[data-row-inclusion-operator]")
        ?.addEventListener("change", () => syncOperator(row));
      row
        .querySelector("[data-remove-row-inclusion-condition]")
        ?.addEventListener("click", () => {
          const source = row.querySelector("[data-row-inclusion-source]");
          const value = row.querySelector("[data-row-inclusion-value]");
          if (source) source.value = "";
          if (value) value.value = "";
          row.hidden = true;
          if (!rows.some((candidate) => !candidate.hidden)) rows[0].hidden = false;
          sync();
          rows.find((candidate) => !candidate.hidden)?.querySelector("select")?.focus();
        });
    }
    add?.addEventListener("click", () => {
      const row = rows.find((candidate) => candidate.hidden);
      if (!row) return;
      row.hidden = false;
      sync();
      row.querySelector("select")?.focus();
    });
    sync();
  }
});
