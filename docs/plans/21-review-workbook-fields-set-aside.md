# Show fields set aside in the Final review workbook

**Status:** Implemented for omissions that can be traced to saved preparation
findings, comparison findings, and explicit mapping rules. The remaining
proposal covers reasons or original values that the saved evidence does not
retain, including a deliberate decision to leave a source column unmapped.

A data manager should be able to open **Fields set aside** and find the source
value, why Impodo left it out, and where to make a correction. Put this tab after
**Needs attention** and link to it from **Overview**. Use the existing charcoal
title band, striped rows, and labeled colors. Freeze the headers and provide
filters for the reason, dataset, and field.

## What the tab shows

Use one line per affected source cell when a saved decision identifies a row.
Use one line per source column for a column-wide decision, such as deliberately
leaving an internal notes column unmapped. Display the scope on each line and
count source cells and whole columns separately. If a cell was considered for
several target fields, keep one line per distinct target outcome when the
outcomes differ. Count the underlying source cell once.

| Reason | Example | What to do |
| --- | --- | --- |
| Invalid format | The date `31/02/2026` could not be parsed and was left out. | Correct the source date. |
| Value not recognized | `WEST` matched no region and the optional relationship was left unset. | Check the source value or complete the relationship choice in Match data. |
| Intentionally excluded | The entire `Internal notes` column was left unmapped by a saved choice. | Review the choice if the column should migrate. |
| Skipped by rule | An empty phone value was skipped by a rule that preserves the existing Odoo value. | Check the rule if the empty value was meant to clear the field. |

Show the source dataset and row, source field, original value when permitted,
target field where known, actual effect, reason, and next step. For a whole
column, show the column name and scope without repeating its source values.
Use red for source corrections, amber for decisions that need review, and blue
for intentional choices. Keep these colors separate from readiness priorities.

A value that Impodo successfully corrected and included belongs with the
prepared record. An unchanged comparison on its own does not qualify. If an
entire record was deferred, **Deferred issues** remains the place to find the
record and its root cause. Show a field here when its own saved outcome qualifies
and cross-reference **Needs attention** when the same issue requires action
before loading. Keep those counts separate.

## Evidence and acceptance

Build the tab from evidence bound to the workbook's saved comparison. The current
workbook already uses manifest issues, frozen file-source preparation evidence,
and confirmed normalization effects. Check which of those sources explicitly
record a field omission or a column-wide exclusion. Where a stage currently
drops that decision, preserve the reason and source coordinate at that stage
before claiming coverage. A blank prepared cell or an absent mapping alone
cannot establish why a value was left out.

Use the accepted dataset and source row for traceability. Show a physical file
name or cell reference only when captured. Export approved file-source values
as literal Excel text when needed to preserve the original representation and
prevent formula execution. Apply the current portable workbook redaction rules
to Odoo sources and use a distinct label when a value is withheld. Separate
an actual source blank from unavailable evidence. If historical evidence is
incomplete, show the coverage gap in Overview and on the tab. Reserve “No
fields were set aside” for a comparison with complete coverage and zero entries.

Download and recreation should use the same saved evidence, including after
loading. Generate the tab from bounded local reads, with no live Odoo lookup or
per-cell repository query. The tab is a read-only report of preparation and
comparison decisions. Load results belong to the verification result.

Before release, verify cell and column scope, multiple target outcomes,
explicit skips, corrected values, deferred records, incomplete history,
redaction, and safe Excel display. Check that the tab, counts, filters, and
recreation agree with saved evidence. Cover affected file and Odoo source
paths for Odoo 19 and 20, including local and remote targets, Authoring,
and Recipe runs where they share this export.
