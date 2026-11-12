# Export rows needing attention from Final review

**Status:** Implemented, with verification recorded in the delivery. The
dedicated download returns a newly generated workbook without publishing a
stored artifact. It uses numbered sheets for oversized row sets and reports
Excel cell and column limits explicitly. Current instructions belong to the
[Final review guide](../user/workflow/05-final-review.md#download-a-correction-worklist).
**Reviewed:** 12 November 2026.

## Implementation decisions

The download and its browser counts use one structured projection of current
saved findings. The optional browser summary shares the workbook's correction
advice. Informational notes stay out of the worklist. Records set aside by a
failed data check remain included; **Rows to use** exclusions are counted
separately.

Original values from combined tables are joined through the saved column
projections, including chained joins and renamed outputs. The preparation-rule
hash is verified before capture and again before returning the download.

Excel generation streams rows and releases temporary worksheet files on both
success and failure. In a local duplicate-group fixture, 100 affected records
took approximately 0.03 seconds and 0.5 MiB of additional process memory.
10,000 affected records took approximately 2.5 seconds and 28 MiB. All findings,
duplicate peers, and affected-data records were read back and counted. These
are workbook-generation measurements, not live route timings or a maximum
supported-volume claim.

Focused tests cover the export, shared advice, retained source locations,
freshness, permissions, Odoo-source restrictions, Excel limits and cleanup,
both Odoo majors, and Recipe reuse. The complete repository gate and portable
behavioral suite remain required for the verified delivery snapshot. Browser
screenshot refresh and hosted platform qualification have not been run during
this implementation; the running Impodo session is preserved.

## The data manager's goal

From Stage 5, download every record that needs attention, understand the
specific problem, and give the person correcting the data a useful worklist.
Each finding should explain which source rows and columns are involved, what
Impodo found, and where someone can correct it.

For example, “Rows 18 and 42 both use Document number SO-104 and Line number
20” explains a duplicate. “Duplicate key” leaves the data assistant to discover
both the matching rule and the conflicting row.

## What already exists

The current Stage 5 workbook includes **Needs attention**, **Records to load**,
**Fields set aside**, and optional sheets for filtered or deferred records.
Its action queue already contains a dataset, record, field, prepared value,
reason, recommended action, source row, and affected count.

The browser places its workbook actions inside the final-review completion
section. Workbook creation requires passed control totals, matching current
quality evidence, package readiness, and either a ready comparison or an
accepted decision to set aside affected groups. These conditions make the
current action unsuitable for downloading unresolved problems as a correction
worklist.

Existing omitted-row evidence also retains less detail than the proposed
export requires. It contains a label and reason, but does not provide the full
set of affected values, column origins, duplicate peers, or correction choices.
Some of that information can be joined from existing saved evidence; coverage
must be verified for each finding family before claiming a complete report.

## Proposed action and scope

Add **Download rows needing attention** beside the Stage 5 problem summary.
Keep it available when unresolved rows prevent progression. Show the number
of affected records and findings beside the action. Export the complete
current result across every page and dataset, regardless of the browser's
current row filter.

Include these records by default:

- Records with their own missing values, invalid values, matching problems,
  or relationship problems.
- Records needing a review decision under the saved checks.
- Records set aside during preparation or Final review because of data issues.
- Parents, children, and siblings held back by an affected record, with a
  reference to the original problem.

Rows intentionally omitted by **Rows to use** stay outside this correction
worklist. Overview should state their count separately. Ordinary safe changes
and informational transformations do not create worklist entries.

Make the preparation findings downloadable before a successful Odoo comparison
exists. Mark that report **Odoo comparison not completed** and show only
findings supported by the saved preparation. After comparison, include its
current findings too. Never mix a newer preparation with an older comparison.

Whole-dataset problems, such as an unexpected total, belong in the report as
**Applies to the whole table**. Problems affecting the complete import should
say **Applies to the whole load**. Neither requires inventing a faulty row.

## Proposed Excel layout

| Sheet | What the data assistant can do |
| --- | --- |
| Overview | See the project, Data version, company, report date, checks completed, distinct affected records, direct problems, linked records held back, and findings by reason. |
| Needs attention | Filter one line per affected record and distinct finding. See the affected columns, values, explanation, suggested action, correction location, and related rows. Whole-table and whole-load findings have an explicit scope. |
| Affected data, one sheet per table | Read each affected prepared record once, with its available source values and source locations. Highlight the columns involved and link back to every finding for that record. |

Keep the main worklist columns in this order:

1. **Priority** identifies **Must fix** or **Review** from the saved finding.
2. **Table**, **Company**, and **Record** identify the business record.
3. **Source location** identifies the source file, worksheet, and row when
   retained. Show a clearly labelled dataset row when a physical Excel row is
   unavailable.
4. **Column or columns** names the original source headings and the affected
   Odoo field label where relevant.
5. **Source value** and **Value prepared for Odoo** show what was checked when
   access and retained evidence permit it.
6. **Problem** and **Why this needs attention** describe the observed condition.
7. **Related rows** and **Group** connect duplicate peers and dependency causes.
8. **Suggested action**, **Where to correct**, and **Who can help** identify the
   next task and its owner. Use a saved owner or a clear role suggestion.
9. **Load effect** distinguishes a direct blocker, a review item, and a record
   set aside because another record needs correction.

Use filters, frozen headings, wrapped explanations, and readable column widths.
Use words alongside colours. Keep support codes and evidence identifiers in
supporting detail. Preserve identifiers such as `000123` as text. Distinguish
an actual empty value from **Not retained** and **Restricted**.

The affected-data sheets should include the record's mapped business columns
and the source columns needed to understand its findings. A combined record
must list every retained contributing source location. One source row can
contribute to several prepared records, so Overview must count physical source
rows and prepared records separately when both are available.

## Example explanations

These examples illustrate the proposed wording. Concrete values, matching
columns, peer rows, and company context must come from the saved result.

| Affected rows and columns | Why this needs attention | Suggested action |
| --- | --- | --- |
| Sales lines, rows 18 and 42; Document number and Line number. | Both rows use Document number SO-104 and Line number 20. Those two columns together identify a source row. | Check whether these represent the same line. Correct the line number if they are different lines, or review which row to keep if one repeats the other. |
| Accounts, rows 8 and 19; Account code. | After the saved spacing rule, both values become 4000 in Company A. | Review the original values together. Correct the source or the spacing rule if it combines different accounts. |
| Suppliers, row 27; Payment terms. | Payment terms NET45 matched no record in the Odoo data checked for Company A. | Check the spelling and company, choose the intended existing payment terms, or ask the accounting owner to create the required terms before comparing again. |
| Suppliers, row 31; Payment terms. | NET30 matches two payment terms in the checked company. Impodo cannot choose which one you intended. | Review the matching records and select the intended terms, or use a matching rule that distinguishes them. |
| Contacts, row 14; Name. | Name is empty. Odoo requires a name to create this contact. | Supply the name in the source, or select the source column containing it in Match data. |
| Orders, row 23; Customer. | This order is held back because customer C-018, Customers row 9, has no required name. | Correct Customers row 9 first, then prepare and compare again. |
| Whole table; Delivery zone. | The Odoo field selected for Delivery zone is no longer available in the checked setup. | Refresh Odoo data and choose the intended available field in Match data. Ask the Odoo owner if the field should exist. |

A duplicate explanation must distinguish repeated source identifiers, several
incoming records targeting the same Odoo identity, several Odoo matches, and
repeated relationship values. Show the full selected identifier and company
or parent context where these participate in matching. A repeated reference
alone does not establish a duplicate.

For a missing relationship, distinguish a missing incoming parent, no matching
Odoo record, several matches, and a match incompatible with the chosen company.
If access or an incomplete lookup prevents a conclusion, say **Could not verify
the related record**. Do not claim the record is absent. Missing Odoo fields
must remain distinguishable from missing records in an existing field.

Suggested actions should follow the observed cause. Do not automatically
recommend deletion, creation in Odoo, or a broader matching rule. When several
corrections are plausible, explain the decision the data manager must make.
An unfamiliar finding should still identify its scope and correction route
and state that a more specific explanation is unavailable.

## Report use and freshness

The first version is a correction worklist. The data assistant makes changes
in the governed source, the owning Impodo choice, or Odoo as appropriate, then
reruns the relevant checks. Editing the exported workbook does not apply
corrections to Impodo.

Bind the report to one exact set of preparation and comparison results. Keep
its report date and comparison coverage visible. If those results are outdated,
offer the applicable recheck; do not label an older report as current. A
downloaded file remains a record of the findings at its report date.

Apply existing export permissions and value restrictions. File-source values
can be displayed only from their verified saved source or prepared evidence.
Odoo-source values remain subject to the current portable-export restrictions;
the export must explain that limitation rather than imply full value coverage.
Never include numeric Odoo record IDs or credentials. Write source text as
literal Excel values so a value beginning with `=` cannot execute as a formula.

## Implementation considerations

Build on the existing workbook writer, styling, artifact storage, and plain
issue guidance. Add a distinct correction-export action and artifact so the
existing full review workbook can retain its accepted purpose and readiness
rules. Export eligibility should require current, authorized, internally
consistent evidence; resolving the reported data problems is not a prerequisite.

Prepare one shared structured explanation for each finding, including its
scope, affected records, source headings, target labels, causal links,
duplicate peers, and supported next steps. Reuse that explanation in the
browser and workbook to keep the correction advice consistent. Enrich display
data through verified joins rather than changing historical evidence or
putting raw values into the value-free quality ledger.

Read the complete saved finding membership in bounded bulk operations. Avoid
collecting only the visible page or repeatedly calling the paged browser
service. Use stable ordering and deduplicate by record and distinct finding.
Index mappings, labels, decisions, and causal links once. Fetch affected row
values in batches and reuse the saved cause-membership index. Do not rerun the
dependency closure, revalidate data, or contact Odoo while exporting.

Keep local database work inside a bounded synchronous operation using the
existing operation-scope helpers. Release database owners before an await,
artifact streaming, or unrelated work. Keep schema validation scoped to each
owner and contract. Do not introduce a shared DuckDB connection cache or a
connection or query per row. Capture generation and publication separately
where artifact handling requires it, then recheck access and bindings before
publishing the completed artifact atomically.

Measure elapsed time, physical connections, schema checks, SQL counts, and
memory on representative small and large fixtures. Use background generation
if those measurements require it. Define explicit workbook row and column
limits, with complete splitting into numbered sheets or files when needed.
Never silently truncate rows, findings, peer lists, or contributing locations.

Implementation owners include the Stage 5 template and preflight router,
the preparation-quality and preflight services, the saved quality and
comparison repositories, and the artifact writer. The existing omitted-row
summary alone is insufficient for the new affected-data sheets.

## Acceptance criteria for implementation

- Export works before comparison for current preparation findings, after a
  blocked comparison, and after an accepted set-aside decision. The report
  clearly states which checks have completed.
- Every affected record and distinct finding appears across all datasets and
  pages. Multiple findings do not inflate the distinct-record total. A global
  finding can be reported even when no individual row can be blamed.
- Direct causes and all inherited records reconcile with the saved findings.
  Intentionally filtered rows remain separately counted.
- Duplicate groups show every peer and the actual selected columns, including
  transformations and company or parent context where relevant. Valid repeated
  references do not become export-invented errors.
- Missing incoming rows, missing Odoo records, unavailable fields, ambiguous
  matches, company conflicts, and unverifiable lookups have distinct wording.
- Combined-table source locations remain traceable. Missing historical detail
  is explicit and never presented as an empty source value.
- Stale evidence, access changes, redaction, literal text, leading zeros,
  long identifiers, failed generation, and large output are covered.
- Tests verify no Odoo calls, no per-row repository access, bounded database
  ownership and SQL work, deterministic ordering, and atomic publication.
- Qualification covers Odoo 19 and 20, local and remote access, and Authoring
  and Recipe paths where they share this export. Odoo-source cases verify
  the current restrictions rather than assuming file-source value access.
- With the implementation, update the owning workflow pages, contracts,
  registry, code documentation, and screenshots. Run focused behavior and
  performance tests and the complete repository gate. Hosted qualification
  remains manual under the maintainer's existing policy.

## Evidence reviewed for this proposal

- [Stage 5 user workflow](../user/workflow/05-final-review.md).
- [Preflight contract](../developer/contracts/preflight.md).
- [Quality and quarantine contract](../developer/contracts/quality-and-quarantine.md).
- [Workbook routes](../../src/impodo/web/routers/preflight.py), especially
  `generate_readiness_package` and `download_readiness_workbook`.
- [Stage 5 template](../../src/impodo/web/templates/workspace_summary.html).
- [Workbook writer](../../src/impodo/adapters/artifacts/reporting.py), especially
  `_attention_items` and `_ReviewActionProjection`.
- [Workbook evidence service](../../src/impodo/application/preflight_service.py),
  especially `review_workbook_evidence`.
- [Omitted-row projection](../../src/impodo/domain/preflight/frozen_input.py),
  especially `_workbook_omitted_rows`.
- [Quality detail reads](../../src/impodo/adapters/duckdb/quality_review_details.py)
  and [quality contract types](../../src/impodo/domain/preparation/quality.py).
- [Existing workbook tests](../../tests/integration/artifacts/test_reporting_cli.py)
  and [bulk deferred-review tests](../../tests/integration/web/test_bulk_deferred_workflow.py).
