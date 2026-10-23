---
audience: developer
kind: plan
status: in-progress
---

# Compose source tables into one logical Odoo dataset

## Status, reader, and outcome

**Status:** In progress, 2026-10-23. Slices 0 and 1 are implemented and
verified. Browser authoring, Recipe reuse of dataset-use decisions, and
business-scenario qualification remain open.

| Slice | Status |
| --- | --- |
| 0: Contract and compatibility | Complete. Source-preparation contract v6 stores complete dataset-use decisions. Version 4 and 5 plans keep their prior mapping inputs. |
| 1: Application preview and save | Complete. Contract v7 retains the accepted complete-data review and its source, output, and lineage identities. |
| 2: Guided browser workflow | Not started. |
| 3: Recipe round trip | Not started. Recipe publication currently blocks a v6 plan that contains dataset-use decisions instead of dropping them. |
| 4: Related-grain qualification | Not started. |
| 5: Measured scale follow-up | Not started. |

This plan is for product, domain, application, browser, and test maintainers.
It defines a model-neutral way to combine several accepted physical source
tables when they describe one logical dataset for Odoo.

The first acceptance case is two Microsoft AX customer extracts that supply
different columns for one `res.partner` record. The same contract must also
qualify BoM headers, lines, operations, and other source families without
embedding AX table names or Odoo model names in the implementation.

The intended result is:

```text
accepted physical source datasets
        |
        v
versioned source-preparation rules
        |
        v
logical datasets with complete lineage
        |
        v
Match data and the existing preparation, review, and load workflow
```

Impodo has an exact-join rule, dependency-ordered structural evaluation,
reconciliation counts, and physical lineage. Source-preparation contract v6
can now keep physical inputs available to preparation without requiring them
in **Match data**. Contract v7 and the workspace application service can now
preview and save an exact join over the complete frozen snapshots. The browser
does not yet let a data manager author such a join or choose dataset uses.
Recipe publication of the new decisions also remains open. Those product gaps
are the remaining work of this plan.

## Decisions

### The base dataset owns the output grain

Every composed output has one declared base dataset. One base row represents
one candidate logical record. A successful exact enrichment produces no more
than one output row for that base row.

The added dataset may contain zero or one row for a join key. Repeated keys on
the added side are ambiguous and block the rule. Impodo must never select an
arbitrary first row or multiply the base rows silently.

This gives the following qualification rule:

| Source relationship | Supported composition | Meaning |
| --- | --- | --- |
| One base row to zero or one added row | Exact join | Add columns while preserving the base grain. |
| Many base rows to one added row | Exact join | Reuse one added record for several distinct base records. |
| One base row to many added rows | Not an exact enrichment | Keep a related logical dataset, or use an explicit reviewed aggregate. |
| Many rows on both sides for one key | Blocked | The proposed output grain is ambiguous. |

An inner policy remains a strict completeness requirement. It must report an
unmatched base key as an error; it must not silently remove the row. A left
policy may keep an unmatched base row only when the data manager explicitly
accepts optional enrichment.

### Join identity and Odoo identity are different decisions

The join key answers, "Which physical rows describe the same source reality?"
The portable Odoo business identity answers, "Which logical record should be
created, updated, or matched?" They may use different fields.

For AX, a key is often composite. `Partition`, `DataAreaId`, or another legal
company scope must be included whenever the record identifier is not globally
unique. A technically non-empty identifier is not sufficient if its scope or
cardinality is wrong.

Exact joins use typed equality. Display names, descriptions, fuzzy text, or
case-insensitive guesses are not valid join keys. Any normalization needed for
a key must be an explicit, reviewed transformation with the same behavior in
preview, saved preparation, and fresh Recipe application.

### Physical inputs can be preparation-only

Source snapshots remain immutable evidence. A saved composition does not
replace, rewrite, or hide their lineage. It does control which resulting
datasets require an Odoo mapping.

Source-preparation contract v6 adds a plan-level dataset-use policy with these
meanings:

- **Map directly:** expose the physical dataset in **Match data**.
- **Preparation only:** use the physical dataset to build logical outputs but
  do not require a separate Odoo mapping for it.
- **Map and prepare:** expose the physical dataset and allow it to participate
  in logical outputs.

This policy belongs to the versioned derived-entity plan, not to an individual
join. One physical input can feed several outputs, and the same rule can be
reused while the plan decides how each dataset participates in the workspace.

Version 4 and 5 plans, and version 6 plans without an explicit complete policy,
retain the previous mapping exposure. The application service accepts an
explicit policy only when it covers every physical dataset and agrees with the
saved preparation rules. A fresh Recipe application must eventually bind
preparation-only datasets by portable logical identity even though they do not
enter target-field mapping; that work remains in Slice 3.

### Several inputs use ordered binary rules

The existing exact-join rule combines two inputs. Three or more tables form a
dependency-ordered chain of named logical outputs. This keeps every step
reviewable and gives each output a stable rule hash, input binding, column
projection, reconciliation, and lineage record.

The browser may guide the user through successive additions, but it must not
compile a free-form SQL statement or an opaque multi-table query.

### Output columns are explicit

The data manager selects the columns carried from each side and resolves every
name collision. Impodo must not apply an implicit `SELECT *`. This avoids
moving hundreds of irrelevant or empty AX columns into every later stage and
makes Recipe drift checks precise.

The existing structural ceiling of 200 output columns remains in force until
separate qualification changes it.

## Business examples

### Customer enrichment

For the supplied privacy-safe samples, the likely shape is:

```text
base customer row: cust02
added party row:   cust01
join:              cust02.Partition = cust01.Partition
                   cust02.Party     = cust01.RecId
logical output:    one customer row for res.partner
target identity:   AccountNum plus legal-company scope when required
```

The exact field names and base choice must be confirmed on the complete files.
The preview blocks the save if the party-side key is duplicated, a required
customer has no party, or the composite scope is incomplete.

Both physical tables can then be **Preparation only**, while the composed
customer dataset is mapped to `res.partner`.

### BoM family

A one-to-one BoM header extension can enrich a logical BoM header. Repeating
BoM lines normally remain a logical line dataset linked to the header, and
repeating operations normally remain a logical operation dataset. They must
not be flattened onto the header.

If two physical line tables each contain exactly one partial view of the same
line, they may be composed at the line grain with a complete line key. If the
business needs a header value calculated from many lines, that is a separate
explicit group-aggregate rule with its own reconciliation, not an exact join.

## Full preview and saved evidence

The decisive preview runs against the complete frozen input snapshots within
the supported materialized browser limit. A small displayed sample may help
explain results, but it cannot supply the acceptance counts.

The preview and saved review show at least:

- base, added, matched, unmatched-base, and unused-added row counts;
- blank, invalid, and duplicate key counts for each side;
- expected and actual output row counts;
- the declared output grain and join policy;
- selected output columns and collision resolutions;
- several matched and unmatched examples with protected source references;
- the physical input dataset and row behind every output value; and
- whether each input is mapped directly, preparation-only, or both.

For an exact enrichment, successful reconciliation proves:

```text
output rows = base rows
matched base rows + unmatched base rows = base rows
duplicate added keys = 0
```

Required enrichment also proves that unmatched base rows equal zero. Saved
evidence records the complete counts, rule hash, input snapshot identities,
selected columns, and physical lineage. Editing or removing a rule or dataset
use invalidates dependent mapping and downstream evidence while retaining the
old evidence in governed history.

## Browser workflow

The current **Source data** page owns accepted files and derived-entity plans.
Extend its related-table preparation area with a fourth choice:

> Combine columns from multiple tables

The guided sequence is:

1. Name the logical output and state what one output row represents.
2. Choose the base dataset.
3. Choose the added dataset.
4. Pair one to five typed join-key columns and include the required scope.
5. Choose required or optional enrichment.
6. Select and rename output columns explicitly.
7. Run the complete preview and resolve blocking findings.
8. Choose the dataset-use policy for the two physical inputs.
9. Save the versioned plan.

After saving, **Source data** shows both immutable inputs, their role in the
logical output, and whether they will appear in **Match data**. The logical
output shows its grain, rule chain, reconciliation status, and current or
stale state.

The first delivery should add focused preview and save endpoints under the
existing derived-entity router. Suggested route ownership is:

```text
POST /workspaces/{workspace_id}/derived-entities/join/preview
POST /workspaces/{workspace_id}/derived-entities/join/save
```

Names are provisional until the current router conventions are applied.
Request parsing stays in the router. The application service owns validation,
full preview, optimistic plan-version checking, and save coordination. Domain
functions own typed equality, cardinality, projection, reconciliation, and
lineage. No route or service in this workflow calls Odoo.

## Domain and application changes

Reuse rather than duplicate these current contracts:

- `ExactJoinRule`, typed `JoinKey`, and structural reconciliation;
- dependency-ordered structural rules and explicit projections;
- `DerivedEntityPlan` hashing and version binding;
- multi-source physical lineage in staging;
- Recipe compilation and fresh-data application; and
- the consolidated Source data page projection.

Add only the missing contracts:

1. a versioned dataset-use policy on the derived-entity plan;
2. mapping-source selection that exposes only map-eligible datasets while
   retaining every preparation dependency;
3. application operations to preview and save an exact join;
4. browser views and commands for the guided workflow; and
5. persisted review evidence for the full join reconciliation.

Do not put adapter access in `domain` or concrete DuckDB construction in an
application service. Workspace-qualified application ports own the required
reads and atomic plan save. The browser composition layer selects concrete
adapters.

## Recipe portability and drift

A published Recipe records portable dataset and column identities, the base
grain, exact key pairs, join policy, projections, output names, rule ordering,
and dataset-use policies. It stores no customer rows, AX record identifiers,
file paths, observed counts, or workspace-specific dataset identifiers.

Applying the Recipe to fresh files must:

1. bind every direct and preparation-only physical input unambiguously;
2. bind every required key and projected column;
3. rebuild the same dependency-ordered logical outputs;
4. run the complete reconciliation against the new snapshots; and
5. stop on missing, ambiguous, duplicated, or incompatible inputs.

Fresh data is allowed to have different counts and values. It is not allowed
to change the rule meaning silently.

## Performance and database lifetime

The first product slice retains the current 25,000-row materialized browser
limit and 200-column structural-output limit. These are admission limits, not
performance claims.

Before changing the evaluator, measure the new preview and save routes with
representative narrow and wide fixtures. A page read runs through one
`run_page_read()` scope. Each preview or save command runs through one
`run_local_operation()` scope. Repository collaborators share that request's
DuckDB connection and schema state; there is no process-wide or cross-request
database cache.

Route tests collect DuckDB request timings and assert evidence-based ceilings
for connection acquisition and schema validation. They compare returned
counts and lineage rather than enforcing wall-clock time in normal CI.

If measurements show that the current in-memory structural evaluator is the
limiting cost, a later slice may implement a set-based DuckDB or qualified
columnar path under the same domain contract. That work belongs with the
[preparation efficiency and storage plan](preparation-efficiency-and-storage.md).
It must prove exact parity for output order, values, errors, reconciliation,
hashes, and lineage before changing route selection or supported limits.

Live responsiveness is claimed only after an authenticated restart and
recorded browser measurements. Source composition remains part of the DuckDB
data plane; it does not introduce PostgreSQL schema work into the separate
shared-server Phase 0 review.

## Delivery slices and exit gates

### Slice 0: contract and compatibility — complete, 2026-10-23

- Source-preparation contract v6 stores ordered `SourceDatasetUsePolicy`
  evidence for **Map directly**, **Preparation only**, and **Map and prepare**.
- Mapping selection filters only map-ineligible physical datasets. Preparation
  still receives the complete frozen physical selection and preserves lineage.
- Version 4 and 5 plan JSON retains its prior hash shape and mapping inputs. A
  compatibility reader also restores the earlier exact-join payload whose
  join mode occupied the rule-discriminator field.
- `DerivedEntityWorkspaceService.save_dataset_uses` checks complete physical
  coverage, rule participation, optimistic version, and source-selection hash.
- The existing atomic plan repository invalidates mapping and prepared
  evidence when it saves a policy revision.
- Recipe compilation blocks plans with dataset-use decisions until Slice 3
  defines their portable logical identity. It does not omit the decisions.

The exit gate passed. Focused tests prove that old workspaces retain their
mapping inputs and that two preparation-only join inputs build one mapped
logical dataset with complete two-source lineage. The combined workspace,
preparation, Recipe, and DuckDB regression selection passed 127 tests. No
browser route changed, so route timing and connection-count qualification does
not apply to this slice.

### Slice 1: application preview and save — complete, 2026-10-23

- `ExactJoinRequest`, `preview_join`, and `save_join` now provide a
  model-neutral application boundary for one-to-zero-or-one enrichment.
- `review_exact_join` scans every frozen row and reports complete match,
  unmatched, unused, blank, invalid, and duplicate-key counts before the
  existing structural evaluator can produce output.
- The preview supports composite keys, optional and required enrichment, and
  dependency-ordered joins whose base is a prior structural output.
- Save repeats the review, enforces the optimistic parent version, and uses
  the existing atomic repository transaction.
- Contract v7 saves the rule hash, physical snapshot identities, complete
  review, protected examples, output hash, and lineage hash. Versions 4, 5,
  and 6 retain their prior JSON hash shape.

The exit gate passed. Focused application tests cover composite keys, optional
and required matches, duplicate added keys, blank and invalid keys, output
column collisions, chained rules, stale versions, and retry after an
interrupted save. A DuckDB repository regression proves rollback and retry of
the atomic plan save. No browser route changed, so route timing,
connection-count qualification, live timing, and screenshot work do not apply
to this slice. The combined Slice 1, derived-entity, structural, preparation,
Recipe, and DuckDB regression selection passed 137 tests.

### Slice 2: guided browser workflow

- Add the fourth preparation choice, guided form, preview, findings, and saved
  summary to **Source data**.
- Show preparation-only inputs and the logical output distinctly.
- Keep route orchestration inside one local-operation scope per command.

Exit gate: an authenticated fictional customer scenario produces one logical
dataset for `res.partner`, and only that dataset is required in **Match data**.
Keyboard, validation, recovery, and narrow-screen behavior are verified.

### Slice 3: Recipe round trip

- Publish the join chain and dataset-use policies by logical identity.
- Apply them to renamed fresh files with compatible schemas.
- Block missing, ambiguous, and cardinality-drifted inputs.

Exit gate: the customer composition produces identical rule meaning and
lineage structure on fresh files without storing source values in the Recipe.

### Slice 4: related-grain qualification

- Qualify BoM header enrichment, a separate repeating line dataset, a separate
  repeating operation dataset, and a partial-line exact join.
- Explain rather than flatten every one-to-many case.
- Verify existing relationship and load-order behavior is unchanged.

Exit gate: the same generic contracts handle customer and BoM families without
model-name branches.

### Slice 5: measured scale follow-up

- Record preview and save request timings, connection counts, schema checks,
  memory, row widths, and exact fixture hashes.
- Optimize only the measured bottleneck under the existing evidence contract.
- Requalify the browser limit separately before increasing it.

Exit gate: exact-result parity and database-lifetime assertions pass, and any
new supported scale has reproducible worker and browser evidence.

## Test placement

Use focused files rather than extending an already broad source-workflow test:

- domain tests for key typing, cardinality, projection, reconciliation, and
  dataset-use compatibility;
- application tests for preview, save, invalidation, and mapping selection;
- Recipe compilation and fresh-application round trips;
- DuckDB integration tests for atomic evidence and retry;
- a dedicated browser integration file for source composition;
- route-level DuckDB timing and connection-count tests; and
- authenticated end-to-end customer and BoM scenarios with fictional data.

Existing structural tests remain regression coverage. New tests must prove
that preparation-only inputs remain bound and traceable even when they do not
appear in target mapping.

## Completion criteria

This plan is complete when:

- a data manager can create and review an exact multi-table composition in the
  browser without writing Python or SQL;
- every accepted exact join preserves the declared base grain;
- duplicate, blank, incomplete, and unmatched required keys block explicitly;
- no first-row selection, silent row loss, or unreviewed row multiplication is
  possible;
- physical inputs remain immutable and every output value retains lineage;
- preparation-only inputs do not require separate Odoo mappings;
- Recipe reuse succeeds on compatible fresh files and blocks structural drift;
- customer and BoM-family acceptance scenarios use the same generic contracts;
- preview and save make zero Odoo calls; and
- route and worker evidence supports the documented database-lifetime and
  scale claims.

## Non-goals

This work does not provide arbitrary SQL, user-authored Python, fuzzy joins,
implicit deduplication, target-record merging, or automatic flattening of
one-to-many relationships. It does not change Odoo write ownership, bypass
final review, increase a row limit without qualification, or add shared-server
PostgreSQL persistence.

## Next implementation slice requiring approval

Slice 2 adds the guided **Source data** browser controls and connects them to
the reviewed Slice 1 application operations. It requires a separate green
light before changing routes, templates, presenters, or screenshots.
