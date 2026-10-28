# Combine several source tables into one record dataset

## Status and product decision

**Status:** Completed and qualified on 2026-10-29. Grouped authoring,
automatic output recommendations, bounded complete-data review, bounded
durable preparation of the saved grouped output, validation recovery, and the
authenticated browser evidence are implemented. Current behavior is owned by
the paired Source data documentation and related-table guide; this file now
records the completed product decision and its acceptance evidence.

Impodo should let a data manager choose one primary table and enrich it from
several other tables in one decision. The primary table defines one output row
per business record. Each added table contributes zero or one row for the same
confirmed key. Impodo reviews the complete combination once, saves it once,
and presents only the final logical dataset in **Match data**.

For example, one customer delivery may contain these four accepted tables:

| Table | Responsibility | Example fields |
| --- | --- | --- |
| `customer_core` | Primary customer population | Customer reference, name, VAT number |
| `customer_contacts` | Contact enrichment | Customer reference, email, phone |
| `customer_addresses` | Address enrichment | Customer reference, street, postcode, city |
| `customer_terms` | Commercial enrichment | Customer reference, payment term code |

The data manager should create `customers_complete` in one guided action and
map that dataset once to the appropriate target model. Contact (`res.partner`)
is one example, not a source-preparation dependency. They should not have to
save three pairwise joins, manage intermediate datasets, or repeat the target
record decision.

This is a Stage 1 source-preparation feature. It remains model-neutral: the
data manager chooses Contact (`res.partner`) later in **Match data**. Source
preparation must not contact Odoo, alter the accepted files, or authorize an
Odoo write.

## Boundary before implementation

The implemented **Combine columns from multiple tables** control combines two
datasets at a time. Its output can feed another exact join, so a data manager
can build the required result as a chain. Each join is reviewed and saved
separately, and every structural output currently enters the mapping selection.
That behavior is correct but too mechanical for several partial views of the
same records.

The existing exact-join engine already provides the required safety
foundation:

- One to five typed key pairs identify the same record on both sides.
- The base row count defines and preserves the output grain.
- An added key must identify no more than one row.
- Complete review reports matches, missing matches, unused rows, invalid keys,
  duplicate keys, output rows, and physical lineage.
- Accepted rules and their physical-input uses survive Recipe publication and
  fresh-data application.
- The original pairwise browser review remains bounded by 25,000 physical rows,
  while every structural output remains bounded by 200 fields.

The proposal changes the authoring unit and logical-output visibility. It does
not weaken these rules.

## Delivered data-manager experience

Rename the normal-path choice to **Several tables describe the same records**.
The guided action should contain these steps.

### 1. Choose the result and primary table

The data manager enters one **Combined table name** and chooses the **Primary
table**. The page explains that one primary row becomes one output row.

The primary table must contain one row for each intended business record. A
repeated complete primary key blocks this workflow because Impodo cannot safely
produce one target record from several primary rows.

### 2. Choose all enrichment tables

The data manager selects every **Added table** in one list. The first delivery
supports one primary table and up to nine added tables, subject to the bounded
review and output-field constraints below.

Physical inputs default to **Preparation only**. An advanced choice may keep a
physical input available for a separate mapping, but the normal customer path
shows only the final logical dataset in **Match data**.

### 3. Confirm the shared record key once

The data manager chooses one to five **Primary key fields** once. The ordinary
path should show the first two key positions and let the data manager add more
only when the source identity requires them.

Impodo then shows one row per added table. For each primary key component, the
data manager confirms the corresponding field in that added table. When field
labels, candidate types, and complete-data uniqueness agree, Impodo may
preselect a suggestion, but the data manager must confirm it. Impodo must not
infer identity from similar customer names or silently drop a scope component
such as company or partition.

For example:

| Primary key | Contact table | Address table | Terms table |
| --- | --- | --- | --- |
| Customer reference | `CustomerId` | `AccountNo` | `CustomerRef` |
| Company | `Company` | `LegalEntity` | `CompanyCode` |

The labels may differ. Their confirmed business meaning, order, and typed
values must agree.

### 4. Decide completeness for each added table

Each added table receives two explicit decisions:

- **Optional enrichment** keeps a primary row when that added table has no
  match. **Required enrichment** blocks the combination when a primary row has
  no match.
- **Allow unused added rows** accepts records outside the primary population.
  **Require every added row to be used** blocks records that do not belong to a
  primary row.

Blank or invalid keys always need correction. Repeated keys in an added table
always block the combination. Impodo never chooses one repeated row and never
multiplies a primary row.

### 5. Choose the output fields once

The page groups fields by source table. It selects non-empty primary fields
and non-empty, non-key fields that occur in only one added table. It does not
select added-table join keys or empty fields by default. The data manager can
search, filter, select or clear visible fields, remove fields, and rename
output fields before review. A live count includes private shared-key helpers
against the 200-field limit.

Two source fields may not overwrite the same output name. When labels collide,
Impodo keeps the primary version selected and places the other versions under
**Needs decision** with names that include the source-table label. Choosing
between two competing values, or using one value only when another is blank,
remains an explicit later mapping or transformation decision; the source
combination does not invent precedence or coalesce same-named fields.

### 6. Review the whole combination

**Review complete combination** runs one complete-data review. The page shows
the primary row count and one reconciliation row for every added table:

| Added table | Added rows | Primary rows matched | Primary rows unmatched | Unused added rows | Duplicate-key rows | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `customer_contacts` | 100 | 100 | 0 | 0 | 0 | Complete |
| `customer_addresses` | 98 | 98 | 2 | 0 | 0 | Complete if optional |
| `customer_terms` | 101 | 100 | 0 | 1 | 0 | Needs attention if every row is required |

The review also reports blank and invalid keys, the expected final row count,
the actual final row count, output fields, and protected lineage examples. The
combination is **Complete** only when every added-table decision passes and the
final row count equals the primary row count.

### 7. Save one decision

**Save combined table** stores the complete rule group in one optimistic,
atomic plan revision. It invalidates dependent Match data evidence once. If a
review, concurrency check, or persistence step fails, the previous current
plan remains unchanged; Impodo never publishes part of the combination.

The saved card shows one primary table, the added tables, shared key, final
output, and reconciliation status. Intermediate execution datasets remain
implementation detail and do not appear as separate mapping work.

## Match data and Recipe behavior

The effective mapping selection exposes `customers_complete` and hides the
intermediate structural outputs. The data manager chooses the target model,
its create or update outcome, business identity, relationships, and field
providers once for that final dataset.

A published Recipe retains:

- the portable identity of every physical input;
- the primary grain and ordered enrichment-table set;
- each typed key correspondence and completeness decision;
- the selected and renamed final fields;
- the physical-input uses; and
- the rule that only the final output enters **Match data**.

Fresh application binds every required physical table from the accepted Data
version, repeats the complete review, and rebuilds the same final dataset. A
missing or ambiguous input, missing key field, incompatible type, repeated
added key, required unmatched row, or changed output collision blocks the
application before preparation becomes current.

## Recommended implementation shape

Keep `ExactJoinRule` as the execution primitive. Add a batch authoring contract
that compiles one primary table and several enrichment specifications into a
deterministic dependency-ordered exact-join chain. This reuses the current
join evaluator and Recipe semantics while creating a better operator-level
unit.

The plan contract needs two additive concepts:

1. A structural rule group identifies the exact rules that belong to one saved
   combination and names its final output.
2. A structural output-use policy marks each output as **intermediate** or
   **map this dataset**. Existing plans without this policy continue to expose
   every structural output, preserving their current meaning.

`mapping_source_selection` should execute all dependency-ordered rules but
expose only outputs marked for mapping. Preparation and lineage evaluation
still consume every intermediate result. Recipe compilation stores the output
policy as portable meaning rather than inferring it from rule order.

Add one application operation, conceptually `preview_join_group`, that loads
each accepted physical snapshot once, evaluates the complete candidate group,
and returns per-table reviews plus one final-output review. A paired
`save_join_group` operation rechecks the same request and persists every rule,
review, group, dataset-use policy, and output-use policy in one transaction.
The browser must resolve every posted dataset, field, and rule identifier
against current server-owned metadata. It must not trust posted counts,
hashes, types, or lineage.

Deleting or replacing a saved group removes or replaces the complete group in
one revision. Removal remains blocked when another saved structural rule
depends on the final output. The data manager must resolve that dependency
before Impodo changes the current plan.

## Performance and operation boundaries

Preview and save remain local synchronous operations inside one bounded
`run_local_operation` callback. They make zero Odoo calls and release all
DuckDB owners before returning to the event loop. They must not open a new
repository or repeat schema checks for each added table.

The grouped review retains the 200-output-field limit, ten participating-table
limit, and five-key-component limit. It no longer uses the pairwise editor's
25,000-total-physical-row guard. A request-scoped DuckDB evaluator scans each
verified Parquet input once into a narrow temporary key table, then performs
the complete reconciliation and streamed lineage hashing over those temporary
tables. Saved grouped outputs use a second request-scoped DuckDB adapter that
projects only required key and final-output columns, performs one left-deep
join, and streams 1,000-row pages into the existing durable preparation
session. It admits at most 100,000 physical input rows and 50,000 final rows.
Both adapters use bounded memory and threads and never cache a DuckDB
connection across requests. The general materialized fallback remains limited
to 25,000 rows.

Qualification measures the authenticated four-table authoring path and a
three-table workspace-shaped regression with 65,299 rows, 262 source fields,
97 empty fields, collisions, and two joins. The scale fixture asserts one
physical snapshot scan per participant, streams 21,766 final rows through a
durable generic-model preparation session, and proves that the materialized
fallback remains disabled. The four-table customer path stays within 12
connections and 10 schema checks. Do not add a process-wide or cross-request
DuckDB connection cache to meet that ceiling.

## Delivery increments

### Increment 1: contract and domain behavior

1. Add immutable batch-authoring, structural-group, and structural-output-use
   contracts with deterministic serialization and backward-compatible reads.
2. Compile a group to existing exact-join rules while carrying the primary key
   through internal outputs without exposing private helper fields in the final
   dataset.
3. Produce complete per-table and final reconciliation with physical lineage.
4. Reject repeated primary keys, repeated added keys, blank or invalid keys,
   required missing matches, disallowed unused rows, field collisions, missing
   dependencies, and dependency cycles.

### Increment 2: atomic workspace and Recipe lifecycle

1. Preview and save the complete group through one bounded application
   operation and one plan revision.
2. Preserve the previous current plan after validation, concurrency,
   transaction, artifact, or interruption failure.
3. Invalidate downstream mapping and preparation evidence once after a
   successful save or replacement.
4. Publish and reapply the complete portable group against fresh compatible
   files, including final-output visibility.

### Increment 3: browser experience

1. Replace the normal pairwise form with the proposed primary-and-enrichments
   guided action while retaining an advanced pairwise editor only if another
   supported workflow still needs it.
2. Render one accessible key matrix, field selector, completeness matrix,
   complete review, and saved summary.
3. Preserve entered choices after validation errors and return focus to the
   first problem.
4. Keep keyboard navigation, narrow-width layout, stale-version recovery, and
   interrupted-request recovery explicit.

### Increment 4: qualification and documentation

1. Add a fictional multi-file example and a separate composite-key scenario.
   Prove that only the final dataset enters **Match data** and that source
   preparation remains independent of the later target-model choice.
2. Add exact-value, order, lineage, hash, Recipe round-trip, concurrency,
   rollback, invalidation, authorization, CSRF, and form-tampering tests.
3. Qualify three participating tables at the motivating workspace shape without
   Odoo access, wide Python materialization, or per-row repository work.
4. Update the paired Source data pages, related-table guide, workflow registry,
   contracts, Python code map, BPMN model, and authenticated 1440 by 1024
   screenshots after the browser behavior is implemented.

## Explicit non-goals

- This feature does not append different customer populations. That is a union
  operation, not primary-table enrichment.
- It does not flatten several contacts, addresses, orders, or other repeating
  child rows onto one primary record. Those records remain separate datasets
  connected through relationships.
- It does not run free-form SQL, formulas, fuzzy matching, or automatic
  deduplication.
- It does not select an Odoo model, Odoo matching rule, or field mapping during
  Source data.
- It does not change accepted source files or contact Odoo.

## Completion evidence

The authenticated four-file customer scenario selects one primary table and
three enrichments, confirms a composite key, reviews and saves one combination,
and exposes only `customers_complete` to **Match data**. Preview and save remain
within 12 DuckDB connections and 10 schema checks. Application tests prove one
atomic revision, interruption rollback, duplicate-primary blocking, final-rule
group deletion, complete saved reviews, CSRF enforcement, and form-field
allowlisting. The Recipe round-trip proves that private intermediate outputs,
group steps, primary uniqueness, and final-only visibility survive fresh
binding. Form tests prove the recommended projection, qualified collision
names, and early 200-field rejection. The grouped browser fixture compares
DuckDB evidence with the Python oracle. The three-table scale regression proves
the bounded review and durable preparation path at 65,299 physical rows and
uses `x_generic.record` to keep the qualification target-model neutral.
