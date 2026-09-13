# Make load fallout traceable to source cells

## Status and proposed decision

**Status:** Partially implemented on 2026-09-11. Outcome explanations,
source-cell workbooks, numeric-precision checks, HTML boundary normalization,
and append-only re-verification are implemented. Exact-ID fallout repair
remains proposed.

Replace the current row-oriented **Download fallout** CSV as the primary
operator artifact with an annotated copy of the accepted source workbook.
Impodo should highlight every source cell that contributed to a verified Odoo
difference, explain the prepared and Odoo values, and retain the immutable
original source file.

Add target-precision checks before loading and allow append-only re-verification
after a completed load. These two safeguards prevent a data manager from first
discovering predictable rounding after Odoo has already accepted the records
and prevent an old verification result from trapping a corrected target in a
permanent fallout state.

## The operator's goal

After loading, the data manager needs to answer four questions without joining
technical identifiers by hand:

1. Did Odoo reject a row, or did it accept the row and store a different value?
2. Which original file, worksheet, and cell contributed to the difference?
3. What value did Impodo prepare, and what value did Odoo return?
4. Should the data manager change Odoo configuration, change a mapping rule,
   accept an Odoo normalization, or correct an affected record?

The current CSV identifies a dataset, source-row number, Odoo model, and field.
It does not identify the source file, worksheet, Excel coordinate, source value,
prepared value, or Odoo value. The data manager must reconstruct those
relationships manually, so the artifact does not support the required decision.

## Current evidence and what it means

One observed Authoring load produced this result:

| Result | Count | Meaning |
| --- | ---: | --- |
| Odoo write accepted and journalled | 4,407 rows | Every planned create received an Odoo record identifier. |
| Product Category read-back verified | 1,936 rows | Odoo matched every confirmed Category field. |
| Product read-back verified | 1,003 rows | Odoo matched every confirmed Product field. |
| Product read-back fallout | 1,468 rows | Odoo returned at least one Product field that differed from the confirmed value. |
| Unknown or not written | 0 rows | No row has an uncertain transport outcome and no row was skipped after a stopped load. |

The 1,468 fallout rows contain 1,470 affected source cells because two rows
have two differing fields. The field breakdown is:

- 1,467 **Weight** cells differ because the accepted Odoo schema exposes two
  decimal places while the prepared values use three or four decimal places.
  For example, Odoo stores a prepared `0.003 kg` value as `0.00 kg`.
- Three **Description** cells differ because the source text starts with spaces
  and Odoo removes those leading spaces when it stores the HTML field. Two of
  these rows are also part of the Weight group.

These rows did not fail to load. Odoo created the records and then returned
different final values during read-back. The browser phrase **fallout row** does
not currently make that distinction clear.

## Proposed outcome screen

When all writes were accepted but read-back differs, the page should lead with
the outcome instead of the internal classification:

> **Odoo created all 4,407 records, but 1,468 Product rows need review.**
> Odoo stored a different final value in 1,470 source-linked cells. No row is
> missing, unwritten, or unknown.

The page should group the differences by business field and cause:

| Group | Example explanation | Next action |
| --- | --- | --- |
| **Weight · 1,467 cells** | Odoo allows two decimal places, but these prepared weights require three or four. | Change the Odoo precision or approve an explicit rounding rule. Do not reload the records. |
| **Description · 3 cells** | Odoo removed leading spaces while storing this HTML field. | Accept the normalization or trim the source through a reviewed rule. |

The proposed primary action is **Download highlighted source workbook (.xlsx)**.
The existing **Download fallout** CSV can remain under **Support details** for
technical use. The page should also offer **Re-check Odoo now** after a
completed, known-outcome execution.

## Annotated workbook contract

Impodo must create a new derivative workbook. It must never edit or replace the
accepted source workbook.

The derivative should preserve the original worksheets and add an **Impodo
Fallout** worksheet first. That worksheet should contain:

- a plain-language outcome summary;
- the counts of accepted, verified, different, missing, unwritten, and unknown
  rows;
- one line per affected source cell, not merely one line per affected record;
- the source file, worksheet, cell coordinate, business key, Odoo record,
  business field, source value, prepared value, Odoo value, verification
  result, and recommended action; and
- a hyperlink from each detail line to the affected source cell.

Impodo should use the existing report colors:

- `FCE8E7` with `9F2F2F` for a cell that still differs;
- `FFF5DF` with `7D4F00` for a historical difference that a later read now
  shows as matching; and
- `EDF7EF` with `4D7C5B` only after a new immutable verification result proves
  the value.

Each affected source cell should receive the matching fill, an outline, and a
comment that names the Odoo field, prepared value, observed Odoo value, Odoo
record, and next action. The workbook should retain existing cell values,
formulas, worksheets, and unaffected formatting.

The frozen field lineage determines which cells Impodo highlights:

- A direct one-column mapping highlights that one source cell.
- A rule that uses several source columns highlights every contributing cell
  with the same issue identity.
- A derived or constant value has no source cell. Impodo must say **Generated by
  a rule — no source cell** on the **Impodo Fallout** worksheet instead of
  highlighting an unrelated cell.
- A row built from several physical source rows lists and links every
  contributing cell.
- For a CSV source, Impodo creates an annotated `.xlsx` representation of the
  accepted rows and uses the same summary contract.

## Prevent predictable fallout before loading

The captured Odoo schema already carries numeric precision. Before **Confirm
and load** becomes available, Impodo should test whether each prepared numeric
value can be represented by the target field without changing it.

If a value would be rounded, truncated, overflowed, or converted, **Check
changes** should stop with a field-level issue such as
`TARGET_NUMERIC_PRECISION_LOSS`. The message should show the field, affected
count, target precision, and a few sanitized examples. It should offer two
deliberate paths:

1. The data manager changes the field precision in Odoo, refreshes **Odoo
   data**, prepares again, and runs **Check changes** again.
2. The data manager adds or approves an explicit rounding or unit-conversion
   rule in **Match data**. The transformation impact then shows every changed
   value before another load can be confirmed.

Impodo must not infer a unit conversion or silently round a value. A business
decision determines whether a small weight should retain four decimals or be
rounded.

Text and HTML fields need the equivalent target-normalization check. For Odoo
HTML, the comparison should ignore only serialization or boundary whitespace
that Odoo demonstrably normalizes. It must continue to detect changed words,
internal spacing, tags, attributes, and other meaningful content.

## Re-verification and fallout correction

The current repository permits only one reconciliation result for an execution
run. Once that result reports fallout, another read cannot become the current
verification even if Odoo is corrected later. This makes an immutable historical
result act like a permanent workflow lock.

Impodo should retain every verification attempt and move a separate current
pointer to the newest valid result. **Re-check Odoo now** should:

1. Re-probe the same target and approved principal.
2. Read only the exact Odoo identifiers and fields from the completed journal.
3. Publish a new immutable verification attempt without rewriting the previous
   attempt or execution journal.
4. Generate a new hash-bound fallout workbook from the values observed during
   that attempt.
5. Mark the load complete only when the newest attempt verifies every expected
   value and no outcome is unknown.

A completed load with known fallout also needs a governed **Resolve fallout**
path. That path should prepare exact-ID updates for only the differing fields.
It must perform a fresh read before any write, show zero creates, require
explicit confirmation, journal each update, and verify again automatically.
It must not send the original create plan again.

## Evidence and privacy boundary

The compact `ReconciliationRow` should continue to omit business values from
normal browser state. During read-back, Impodo writes a local detail
artifact containing the exact expected value, observed value, and frozen source
cell lineage for each difference. The reconciliation result stores the
artifact hash and counts. The artifact is ordinary JSON inside the
owner-restricted local project store; it is authorization-checked and
hash-verified, not encrypted with another application key. API keys remain in
the operating-system credential vault. Whole-device encryption and operating-
system account controls are the appropriate at-rest boundary for local source,
DuckDB, and report data.

The annotated workbook must be generated from the accepted source artifact,
the historical execution snapshot, and the bound detail artifact from the
same verification attempt. Generating a historical workbook from a new live
read would mix two points in time and would not be valid evidence.

The download route must require the existing authenticated workspace access,
verify all artifact hashes, use a safe filename, and prevent spreadsheet
formula injection in generated summary cells. Comments and summary values must
not expose credentials, credential hashes, or internal filesystem paths.

## Delivery sequence

### Slice 1 — make the outcome understandable

- Replace the generic fallout sentence with accepted, different, missing,
  unwritten, and unknown counts.
- Group differences by field and stable reason.
- Explain that a read-back difference is not a rejected row.

### Slice 2 — publish the local detail artifact

- Capture expected and observed values during reconciliation.
- Bind the detail artifact to the reconciliation, execution,
  snapshot, target, and credential evidence.
- Keep business values out of the compact browser projection.

### Slice 3 — generate the highlighted workbook

- Copy the accepted workbook and add the **Impodo Fallout** worksheet.
- Resolve target fields through frozen field lineage to exact source cells.
- Highlight and comment every contributing cell using the Impodo color scheme.
- Keep the CSV as a secondary technical artifact.

### Slice 4 — stop precision and normalization loss before load

- Add target-representability checks for numeric fields.
- Add reviewed normalization behavior for Odoo HTML boundary whitespace.
- Show the transformation impact and require a deliberate operator decision.

### Slice 5 — support append-only re-verification and exact fallout repair

- Store multiple immutable reconciliation attempts per execution run.
- Add **Re-check Odoo now** and retain the previous attempt as history.
- Add a governed, update-only **Resolve fallout** workflow.

## Acceptance criteria

1. A data manager can move from every fallout line to the exact source cell
   without consulting source-row numbering rules.
2. A row with two differing fields produces two highlighted cells and two
   detail lines.
3. A derived value never causes Impodo to highlight an unrelated source cell.
4. The accepted source workbook's content hash remains unchanged.
5. The workbook states the source, prepared, and observed Odoo values and gives
   one clear next action.
6. A numeric value that the captured Odoo precision cannot represent blocks
   confirmation before any write.
7. Impodo never silently rounds or infers a unit conversion.
8. Re-verification appends evidence and never overwrites an earlier result.
9. A fallout repair updates only exact recorded Odoo identifiers and differing
   fields; it creates no record.
10. Browser, workbook, repository, artifact-integrity, formula-injection,
    precision-boundary, HTML-normalization, and exact-ID correction tests pass.

## Proposed implementation ownership

| Responsibility | Proposed owner |
| --- | --- |
| Target representability and normalization rules | `domain/preparation` and `domain/odoo/html.py` |
| Local per-cell reconciliation detail | `application/workspace/execution/reconciliation.py` and the workspace artifact store |
| Append-only reconciliation attempts | `adapters/duckdb/reconciliation_repository.py` and workspace schema upgrades |
| Annotated workbook writer | a new focused adapter under `adapters/artifacts/` |
| Outcome grouping and download routes | `web/routers/execution.py` and the load presenter |
| Outcome and repair controls | `web/templates/workspace_load.html` and the governed correction services |

## Related documentation

- [User workflow: Load into Odoo](../user/workflow/06-load-into-odoo.md)
- [Developer implementation: Load into Odoo](../developer/workflow/06-load-into-odoo.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Evidence lifecycle contract](../developer/contracts/evidence-lifecycle.md)
