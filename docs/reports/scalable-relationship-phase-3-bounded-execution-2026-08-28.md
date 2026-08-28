---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 3 bounded execution — 2026-08-28

## Decision supported by this report

Phase 3 completes the accepted execution integration without introducing a
Product, bill-of-materials, or other model-specific executor. Data managers
retain control of mappings, included rows, optional relationships, and reviewed
business keys. Impodo derives only the bounded target crosswalk, safe component
pages, and receipt barriers required by those choices.

This result supports starting Phase 4. It does not add component recovery,
raise the related-data limit, qualify the 16,000-Product and 80,000-BOM-line
shape, or authorize a Production load.

## Implemented outcome

`ExecutionSnapshot` contract version 6 carries an opaque target-record binding
for each uniquely reviewed existing row and target relationship. The binding is
a one-way hash over the target model and runtime identifier. Numeric Odoo IDs
remain inside preflight and execution process boundaries and are rejected from
portable artifacts.

Immediately before the journal starts, `ExecutionService` builds one complete
existing-target crosswalk. It:

1. Collects update-row identities, existing incoming dependencies, and direct
   target relationships from the frozen write intentions.
2. Deduplicates exact business-key domains.
3. Groups them by model and resolves pages of at most 100 keys through
   `find_ids_many`.
4. Requires one unique result for every key.
5. Recomputes and compares every available opaque binding.

A missing, ambiguous, incomplete, or retargeted key fails before the execution
journal and before any Odoo write. The JSON-2 adapter implements each page as
one bounded `search_read` OR-domain and requests only the identifier and
reviewed lookup fields. Execution never falls back to `find_ids` per row.

## Component and receipt behavior

The executor consumes each frozen topological component through pages of at
most 500 rows. A page never mixes components. Compatible creates remain
bounded by the existing 50-row transport maximum and the data manager's chosen
batch size.

After each create response, the exact row outcomes and identifiers are written
to the journal. Before a retained incoming edge can write, the dependency must
have a journalled `COMMITTED` or `PARTIALLY_APPLIED` result and an exact create
identifier. Optional cycle fields remain omitted only when the snapshot marks
them for completion, and only those fields run in the completion pass.

The existing stop-on-unknown rule is unchanged. An uncertain response records
the affected rows and prevents later components from writing.

## Lean exception handling

One real exception appeared during implementation: an update may reference a
new incoming row. Preflight comparison expresses resolved relationships as
portable business references, which had erased the incoming provenance needed
for scheduling. Snapshot construction now restores only the reviewed incoming
references in update intentions. This preserves the existing compact graph and
receipt mechanism; it does not add a second dependency abstraction or a
model-specific branch.

The approach therefore remains intentionally concrete. If later phases expose
several more provenance exceptions that cannot use the same frozen incoming
reference contract, the design should be reconsidered before adding parallel
special cases.

## Verification evidence

The following checks passed on 2026-08-28:

- 72 focused scheduler, snapshot, and execution-service tests passed.
- A 101-row existing-target fixture used two bulk lookup pages of 100 and 1
  keys and made zero service calls to the single-key lookup method.
- Exact-binding tests reject a unique key that retargets from record 50 to 51
  before a journal or write exists.
- Event-order tests prove the dependency receipt is journalled before the
  dependent write call.
- Snapshot tests prove that an update pointing to a new incoming row restores
  the incoming dependency and schedules its create first.
- Adapter tests prove positional bulk results, bounded query shape, and one
  read request for multiple exact keys.
- Ruff passed for the Phase 3 code and focused tests.
- Documentation quality and workflow-registry checks passed after this report
  was added.
- The architecture inventory records 366 production modules and 2,020 runtime
  import edges, with no runtime cycle, forbidden application-to-adapter edge,
  or unclassified production module.

The repository-wide run executed 945 tests with 13 optional skips. After the
reviewed architecture baseline was advanced for the Phase 3 import edge, three
pre-existing unrelated guards remain red: a 102-line template against its
100-line limit, a 2,068-line browser workflow test against its organization
limit, and stale source-discovery copy in one end-to-end assertion.

## Remaining boundary

Phase 4 still owns persisted active-component and transport-batch state,
restart behavior, partial-component classification, and read-back-gated
resume. Updates still use one exact `update_row` call per changed record so the
journal retains a definitive row outcome; Phase 6 must measure that write path
at representative Product and BOM scale.

This phase changes neither the disposable-target acceptance boundary nor the
separate reconciliation requirement.
