---
name: impodo-typst-bpmn
description: Create or revise print-ready, data-manager-friendly Impodo BPM diagrams in Typst. Use for visual process handouts and BPMN-style diagrams; do not use when an executable BPMN 2.0 model is required.
---

# Impodo Typst BPM diagrams

Use this skill to create a print-friendly visual explanation of an implemented
Impodo workflow. The reader is a data manager who needs to understand who does
what and what happens next without learning BPMN notation or implementation
internals.

## Establish the workflow

1. Read the owning user workflow page, `docs/workflow.yml`, and the relevant
   contract before drawing. Verify browser labels and current capability
   boundaries from those sources.
2. State the diagram's reader, scope, and next decision. One source stage or
   one narrow business journey normally belongs on one landscape page.
3. Use the current BPMN 2.0 model as a cross-check when it exists. Typst is the
   printable presentation, not a replacement for the semantic BPMN model.

## Draw for understanding

- Use separate, clearly labelled **You** and **Impodo** lanes or cards. Add
  Odoo as a separate participant only when the diagram needs to show a real
  bounded interaction with it.
- Write each card as an actor -> action -> result. Use exact browser labels
  when the reader must find a control, then explain its outcome in plain
  language.
- Show the normal path first. Keep a decision to one clear question and label
  every branch with its business outcome. Show loops only when they change the
  reader's next action.
- Keep Data version, workspace, Recipe, source data, and migration results
  distinct. Explain a Data version as the delivery of source data when that
  distinction matters.
- State important non-actions when they avoid a likely misconception, such as
  project creation not reading source rows or writing to Odoo.

Use [`assets/impodo-bpm-starter.typ`](assets/impodo-bpm-starter.typ) as the
visual starting point. Keep the meaning and accessible labels when adapting
the layout; do not copy its example wording into a real workflow.

## Source and output

- Store editable Typst sources under `docs/bpmn/typst/`. Keep the compiled PDF
  beside its source when the project chooses to commit print artifacts.
- Use A4 landscape by default. Prefer a small number of large cards to a
  compressed all-stages-on-one-page map.
- Do not install Typst or add package dependencies merely to render a diagram.
  If the local CLI is unavailable, create the source and report that rendering
  remains unverified.
- When the CLI is available, compile with:

  ```powershell
  typst compile docs/bpmn/typst/<diagram>.typ docs/bpmn/typst/<diagram>.pdf
  ```

## Verify

Confirm that the printed reading order matches the verified workflow and that
the output has one intended page. When a PDF is produced, inspect it before
calling the diagram print-ready. Run the focused documentation checks and
`git diff --check` after documentation changes.
