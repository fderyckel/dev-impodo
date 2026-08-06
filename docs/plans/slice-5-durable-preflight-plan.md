# Slice 5 plan: read-only Odoo preflight from approved data

## Status and outcome

**Status:** Complete on 2026-08-06 for the bounded 25,000-row browser
workflow. Slice 6 may start for explicitly approved advanced coverage
families.

Slice 5 makes the approved, durable prepared rows the only browser input to
the read-only Odoo comparison. It removes the current comparison-time source
reload and transformation pass. It does not add an Odoo writer, certify a clean
package, approve an export plan, or rehearse an import.

The implementation keeps the existing routes, labels, navigation, and review
journey. `PreflightService` loads frozen staging, quality, and normalization
evidence; `PreflightRepository` publishes the bound report, decisions, and
protected target snapshots atomically; and the live and local connectors
remain limited to `fields_get` and `search_read`.

The data-manager workflow remains deliberately small:

```text
Prepare and review data
-> approve the exact prepared dataset
-> Compare with Odoo
-> review what is new, different, already matching, or unsafe
-> create the existing review workbook when useful
```

The normal page uses business labels and one clear next action. Request
domains, model names, field names, snapshot hashes, planner versions, numeric
Odoo IDs, and internal issue codes remain in protected evidence or collapsed
**Support details**.

Closure evidence:

- the complete local suite ran 262 tests in 19.680 seconds: 252 passed and 10
  environment-gated tests were skipped;
- durable comparison works after source deletion and application restart;
- tampered canonical rows stop before target access, while repeated successful
  comparisons retain history and leave source normalization unchanged;
- stored decision rows are retrieved through real bounded DuckDB pages;
- browser/profile runtime plans produce equivalent portable classifications
  and resolutions, reference-mode rows resolve without import decisions, and
  overlapping target chunks fail closed on conflicts;
- the opt-in 25,000-row durable comparison completed in 5.273 seconds at
  507.3 MiB peak working set with a 69.5 MiB project database. It issued one
  metadata request and 50 bounded record chunks for 25,000 target rows, stored
  2,915,076 bytes of protected snapshot JSON, a 6,665,592-byte manifest, and a
  688,774-byte workbook.

These figures are deterministic workstation regression guards, not production
sizing guarantees or live-Odoo acceptance.

## Why this slice comes before certification or export

Slice 4 froze an exact eligible-dataset hash, while the former browser
comparison called preparation again and compared a newly materialized
`PreparedBundle`. Slice 5 removes that seam: comparison now consumes the
durable object that the data manager approved.

An exporter built on that seam could prepare different values between approval
and execution. Slice 5 closes the seam by proving that the exact frozen rows
are the rows sent into preflight. Clean-package certification and any future
write plan can then bind to one durable source-side input and one exact
target-side snapshot.

## Decisions locked for this slice

### Compare never prepares again

`Prepare data for review` remains the only action that reads source artifacts,
evaluates mappings, publishes canonical staging, runs quality checks, and
builds normalization evidence.

`Compare with Odoo` must:

- never call `stage_browser_mapping()`, `evaluate_quality()`,
  `evaluate_normalization()`, or normalization publication;
- never read registered CSV or XLSX bytes;
- never change staging, quality, normalization, or their current pointers;
- load and validate the exact current staging, quality, and frozen
  normalization evidence;
- stop before any connector call when that evidence is missing, stale,
  incomplete, tampered with, or no longer frozen.

The comparison recompiles the submitted mapping into the shared
`CompiledMigrationPlan` and requires its semantic hash to match canonical
staging. It may not re-evaluate source values.

### Adapt canonical rows; do not create a second evaluator

Add one pure, storage-independent adapter from durable `CanonicalRow` evidence
to the existing `PreparedRecord` contract. It reconstructs:

- source trace identity and canonical row hash;
- dataset, source row, target model, source identity, target identity, and
  scope;
- typed proposed scalar values;
- symbolic incoming and target relationships;
- structured row issues;
- dataset source hashes.

The adapter applies no trim, parsing, lookup, formula, fallback, default, or
validation rule. If a stored value cannot be decoded losslessly, comparison
fails closed.

Canonical serialization needs a matching portable decoder for `Decimal`,
date, timezone-aware datetime, null, booleans, integers, strings,
`LogicalReference`, and nested relationship collections. Round-trip tests must
prove type and value equality. Numeric Odoo IDs remain forbidden from this
adapter and every portable source-side object.

### Bind every decision to one approved canonical row

Extend the prepared/preflight trace contract with a portable
`source_trace_id`. Browser records use the canonical row hash. Expert-profile
records receive a deterministic trace derived from their existing source
coordinates and identity.

Every preflight decision retains this trace. This prevents a physical source
coordinate from being mistaken for the complete identity of a derived,
combined, parent, child, or lookup record.

### Recompute the frozen eligible dataset before Odoo access

The durable loader selects only row IDs marked eligible by the exact current
quality run. It recomputes the same eligible-dataset hash used by Slice 4 from:

- the current staging content hash;
- the current quality content hash;
- every eligible canonical row in deterministic order.

The recomputed hash must equal both the normalization evaluation and the
frozen dry-run hash. Counts must equal the normalization summary. A mismatch
is a local evidence failure and results in zero Odoo calls.

Set-aside, excluded, and blocked rows remain visible in source-side accounting
but never enter target request planning. Reference-mode eligible rows remain
available for relationship resolution without becoming create/update
candidates.

### Keep one shared preflight engine

The existing `PreflightEngine`, metadata validator, target catalog,
relationship resolver, comparison rules, and classification precedence remain
authoritative for both browser and expert-profile paths.

Slice 5 adds a browser durable-input adapter and strengthens shared planning
and snapshot evidence. It does not fork browser-specific matching or
classification logic.

### No unbounded Odoo record reads

An empty eligible dataset must never produce an empty Odoo domain that reads an
entire model. Composite or relational identities must not silently fall back
to an unrestricted `search_read` either.

The logical planner must:

- merge requested fields and business keys once per model;
- build exact simple or composite business-key requirements where supported;
- use governed dotted relationship fields only when their semantics are
  explicit and covered by tests;
- split large key sets into deterministic bounded domain requests;
- require an explicit governed target domain for any case that cannot be
  safely narrowed;
- block unsupported narrowing before contacting Odoo;
- emit no record request for a model with no eligible key or required
  reference lookup.

An unrestricted model scan is out of scope for the browser workflow. A future
explicit full-scan policy would need its own row cap, estimate, approval, and
evidence.

### Merge chunks safely

Metadata requirements remain one `fields_get` request per model. Record
requirements may produce several `search_read` domain chunks per model, each
with the same merged field projection and deterministic ordering.

Connectors must merge same-model chunks instead of overwriting them. Repeated
numeric IDs with identical projected values are deduplicated internally;
conflicting duplicates, missing pages, fingerprint changes, or incomplete
responses fail the snapshot. Numeric Odoo IDs may exist only in the protected
target snapshot and in-memory indexes.

No connector, repository, or database call may occur inside a source-row
loop. Target catalogs and relationship indexes are built once from the merged
snapshot.

## Durable preflight input and evidence

### `FrozenPreflightInput`

Introduce a storage-independent input envelope containing:

- exact submitted mapping ID, version, and content hash;
- exact current staging run ID, content hash, and canonical rows;
- exact current quality run ID, content hash, and eligible row IDs;
- exact current frozen normalization run ID, content hash, lifecycle version,
  and eligible-dataset hash;
- compiled migration plan plus business dataset and field labels;
- the eligible `PreparedBundle` reconstructed from canonical rows;
- deterministic source trace IDs and dataset source hashes;
- an input content hash covering all of the above semantic bindings.

Construction validates current repository pointers and content inside one
read transaction or from one version-checked snapshot. The service validates
again when publishing results so a concurrent upstream change cannot publish a
current comparison for obsolete evidence.

### `PreflightRequirementPlan`

Add a versioned, deterministic logical plan containing:

- merged metadata fields per model;
- merged target-record fields per model;
- normalized key and governed-domain requirements;
- deterministic bounded domain chunks;
- reference-model requirements;
- source-record count and model/chunk counts;
- planner contract version and semantic hash.

The semantic hash excludes credentials, base URL, runtime page size, retry
count, and numeric Odoo IDs. Protected plan evidence may contain business keys
and domains; the portable manifest exposes only the plan hash and safe counts.

### `PreflightRunEvidence`

Evolve the existing readiness evidence rather than create a second
classification authority. One immutable run binds:

- the complete `FrozenPreflightInput` identity;
- the requirements-plan hash;
- exact target fingerprint, Odoo version, database, module versions, and
  capture time;
- complete metadata and record snapshot hashes;
- engine/contract versions and result semantic hash;
- dataset counts, row decisions, field differences, relationship resolutions,
  and issues;
- actor and comparison time as lifecycle metadata.

The portable manifest includes the frozen normalization and requirements-plan
bindings. It contains no credentials, endpoint URL, raw authorization data,
request domains, or numeric Odoo IDs.

## Target snapshot policy

Compute non-null canonical hashes for every local, remote, and fixture metadata
and record snapshot. Snapshot completeness is mandatory before classification.
Metadata and records must share one exact fingerprint.

Persist the minimum projected target snapshot as protected project evidence so
the comparison can be explained after restart:

- only models and fields present in the approved requirement plan;
- numeric Odoo IDs only in the protected target-snapshot relation;
- deterministic ordering and hashes;
- no credentials, tokens, endpoint authorization headers, or unrelated Odoo
  fields;
- the same project classification, access, retention, invalidation, and
  deletion controls as other customer evidence.

Snapshot records are not offered as a normal download. The JSON manifest and
workbook remain portable projections that use business keys instead of target
IDs.

## Persistence and invalidation

The schema-v18 preflight migration evolves the readiness store with the
following evidence. Schema v19 additionally binds canonical staging to the
shared compiled migration plan:

- immutable preflight/readiness run headers and a distinct current pointer;
- exact staging, quality, normalization, eligible-dataset, target, plan, and
  snapshot bindings;
- dataset summary rows;
- portable decision and relationship-resolution rows for bounded paging;
- protected metadata and target-record snapshot rows;
- deterministic result and artifact hashes;
- append-only completion, supersession, and invalidation evidence.

Keep the existing `readiness_run` history readable. Historical rows without a
frozen normalization binding are never made current automatically. The
project's generic `current_run_id` and `approval_status` remain display
summaries, not the preflight authority.

Publication is atomic and optimistic:

- snapshot and decision rows are inserted in bounded batches;
- counts and hashes are verified before the current pointer changes;
- a failed read, classification, insert, or verification leaves the previous
  successful current run intact;
- an upstream change during comparison prevents publication;
- identical frozen input and identical target snapshot produce the same
  semantic result hash, while a new comparison retains its own lifecycle ID
  and timestamp;
- the manifest and workbook are regenerable projections, not independent
  authorities.

### Invalidation matrix

| Change | Frozen normalization | Current preflight |
| --- | --- | --- |
| Source, derived plan, mapping, schema, staging, quality, normalization, ownership, classification, or retention | Existing Slice 4 rules apply | Invalidate and retain history |
| Normalization group decision or approval before freeze | Not yet eligible | No preflight may run |
| Browser filter, page, search, manifest download, or workbook generation | Preserve | Preserve |
| Odoo record values change and the data manager compares again | Preserve | Publish a new current preflight; retain the previous snapshot |
| Odoo URL, database, connection mode, permitted models, or captured schema changes | Existing upstream rules may invalidate normalization | Invalidate current preflight |
| API key or local session credential rotation for the same target contract | Preserve | Preserve existing evidence; the next read uses the new credential |
| Failed comparison or artifact generation | Preserve | Keep the last successful run; show its timestamp clearly |

Preflight freshness for execution is deliberately not invented here. Slice 7
will define rehearsal recency and clean-package certification rules.

## Data-manager UI

### One comparison action

After Slice 4 approval, the Review page keeps one primary action:

> **Compare with Odoo**  
> Reads the approved prepared data and checks it against Odoo. Nothing is sent
> to Odoo.

While the synchronous bounded comparison runs, disable repeat submission and
show **Comparing with Odoo... Keep this page open.** Background jobs and queue
infrastructure remain outside this slice.

The implementation must reuse the settled components and language from the
ongoing whole-UI revamp. Slice 5 does not redesign global navigation, layout,
styling, or unrelated pages. UI-copy acceptance tests should be finalized only
after that revamp's wording is stable.

### Plain result states

Show business-facing counts:

- **New in Odoo** (`CREATE`);
- **Different from Odoo** (`UPDATE`);
- **Already matches** (`UNCHANGED`);
- **Needs attention** (`AMBIGUOUS` or target-dependent `BLOCKED`);
- **Set aside** (source-side quality outcome, not sent to preflight).

Rows explain the issue in plain language, for example **More than one Odoo
record has this reference** or **The related category was not found**. Model,
field, domain, trace, snapshot, and issue-code evidence remains under
**Support details**.

The page offers one next action:

- **Review records needing attention** when any exist;
- **Create review workbook** when the comparison completed safely;
- **Compare again** when there is a recoverable target-read failure;
- the relevant upstream correction link when frozen evidence is no longer
  current.

No button says Import, Export, Execute, Approve import, or Send to Odoo.

## Authorization boundary

Add a dedicated `preflight.run` capability rather than continuing to reuse
`mapping.submit`. Project viewing remains sufficient to see the plain summary;
protected audit/snapshot evidence continues to require the existing governed
access boundary.

The local data manager receives `preflight.run` through the existing local
actor. A later hosted adapter can separate mapping authors from operators
allowed to contact a target. This capability grants read-only comparison only
and is unrelated to `export_plan.approve` or `export_plan.execute`.

## Implementation sequence

### 5A - Lock the durable input contract

Add lossless portable-value decoding, `source_trace_id`,
`FrozenPreflightInput`, and a pure canonical-row-to-prepared-record adapter.
Extract browser profile and label compilation from the source-evaluation path
so it can run without artifacts.

**Gate:** persisted typed rows and relationships round-trip exactly; adapter
output contains no Odoo IDs; no transformation or validation rule executes.

### 5B - Load and prove the exact frozen rows

Add one repository/application query that retrieves current staging, quality,
and normalization evidence consistently. Recompute eligible rows, counts, and
the Slice 4 eligible-dataset hash. Refactor `Compare with Odoo` to use this
loader and remove its call to `_prepare()`.

**Gate:** comparison succeeds when source artifacts are unavailable but durable
evidence is valid; any stale pointer, hash mismatch, lifecycle mismatch,
tamper, or incomplete row set produces zero Odoo calls.

### 5C - Strengthen request planning and connectors

Introduce the versioned requirement plan, exact composite-key support where
safe, deterministic domain chunks, same-model request merging, snapshot
hashing, and one-time target indexes. Refuse unsupported or unbounded reads.

**Gate:** connector calls scale with models, chunks, and result pages rather
than source rows; empty datasets issue no unrestricted query; duplicate target
keys remain visible; overlapping chunks cannot hide conflicts.

### 5D - Persist immutable preflight evidence

Add the preflight schema, current pointer, frozen-input bindings, plan and snapshot
hashes, protected target snapshot rows, bounded decision rows, atomic
publication, restart retrieval, and invalidation. Make the portable manifest
and workbook projections of stored evidence.

**Gate:** the exact comparison survives restart, bounded result pages do not
load the full target snapshot, failed publication preserves the prior current
run, and portable artifacts contain no numeric Odoo IDs or credentials.

### 5E - Complete the data-manager comparison journey

Wire the dedicated capability, busy state, plain result cards, simple failure
recovery, bounded row review, one next action, and collapsed support evidence.
Reuse the settled UI revamp without changing unrelated screens.

**Gate:** a data manager can compare approved data and understand the result
without knowing Odoo models, domains, IDs, hashes, planners, or connector
methods. Every page states that Odoo remains unchanged.

### 5F - Acceptance, scale, and documentation

Run deterministic adapters, browser/profile parity, invalidation, concurrency,
rollback, snapshot integrity, composite identity, relationship, duplicate,
empty-input, set-aside, masking, capability, paging, and read-only connector
fixtures. Repeat the integrated 25,000-row measurement from durable retrieval
through persisted preflight results using a deterministic target snapshot.

**Gate:** the supported browser scope remains 25,000 physical source rows or is
lowered to the largest completed end-to-end probe; there is no N+1 or unbounded
Odoo access; the focused suite passes. The whole browser copy suite may be
baselined separately after the active UI revamp stabilizes.

For the deterministic workstation fixture, record elapsed time, peak working
set, project-database size, request counts, target rows, domain chunks,
snapshot size, manifest size, and workbook size. These are diagnostic
baselines, not pass/fail gates or production sizing guarantees.

## Required acceptance cases

- Compare uses no source artifact read and calls no source evaluator;
- Compare never publishes or changes staging, quality, or normalization;
- exact current submitted mapping, staging, quality, and frozen normalization
  are required before any connector call;
- recomputed eligible rows, count, and hash equal the Slice 4 frozen evidence;
- set-aside, excluded, and blocked rows never enter target request planning;
- reference-mode eligible rows support resolution without import decisions;
- `Decimal`, date, datetime, null, boolean, integer, string, logical reference,
  and relationship collections survive canonical persistence and adaptation;
- every browser decision binds the exact canonical source trace hash;
- a changed or corrupted stored row fails before Odoo access;
- a missing source file after approval does not prevent comparison from valid
  durable evidence;
- empty eligible datasets do not issue an unbounded record read;
- simple, composite, scoped, and safely supported relational identities create
  bounded exact requirements;
- unsupported narrowing blocks rather than scanning a complete model;
- fields shared by several datasets use one model projection and their keys
  become deterministic bounded chunks;
- large key sets split into deterministic bounded domain chunks;
- same-model chunks merge and deduplicate identical target records;
- conflicting repeated target records, changed fingerprints, missing pages,
  or incomplete snapshots fail closed;
- metadata uses one request per model and record calls are bounded by chunks
  and pages, never rows;
- relationship and identity indexes are built once, with no connector or
  repository lookup per row;
- target duplicates remain `AMBIGUOUS` rather than being silently selected;
- leading zeros, decimal precision, dates, datetimes, null policies,
  Many2one, and Many2many comparison semantics match the existing engine;
- equivalent browser and expert-profile fixtures yield equivalent portable
  source identities, resolutions, differences, and classifications;
- metadata and record snapshot hashes are always non-null and deterministic
  for exact fixture evidence;
- the preflight result binds the frozen normalization, requirement plan,
  target fingerprint, and both snapshot hashes;
- current preflight retrieval rejects obsolete upstream or target bindings;
- a repeated comparison creates a new target snapshot/run while retaining
  history and preserving source normalization;
- failed comparison or persistence retains the last successful current run;
- restart retrieval and bounded paging work without connector access;
- manifest and workbook generation perform no source evaluation, target read,
  or classification;
- portable evidence rejects credentials and numeric Odoo IDs recursively;
- protected target snapshot rows are not exposed in the normal UI or portable
  downloads;
- missing `preflight.run` capability blocks before target access;
- only `fields_get` and `search_read` cross the Odoo connector boundary;
- no UI text implies that comparison imports, exports, certifies, approves, or
  changes Odoo;
- older projects migrate safely and historical readiness rows do not
  become falsely current.

## Closed blind spots

- Comparison loads frozen durable rows and never calls preparation or reads a
  registered source artifact.
- Canonical decoding restores typed values and symbolic references
  losslessly before adapting them to `PreparedRecord`.
- Every browser decision carries the canonical row trace hash.
- The transient eligible-bundle adapter and readiness workflow facade were
  removed.
- Reports bind the frozen normalization run, eligible-dataset hash, compiled
  input, requirement plan, result, manifest, and deterministic target snapshot
  hashes.
- Empty or locally unresolvable keys issue no record read; unsupported
  narrowing fails before connector access.
- Same-model chunks merge and deduplicate safely, and conflicting repeated
  target rows fail closed.
- Result rows are stored separately and paged in DuckDB instead of loading the
  full result set for a normal browser page.
- Failed report publication removes its unpublished artifact and preserves the
  previous current pointer.
- Target comparison uses the dedicated `preflight.run` capability.

## Out of scope

- any Odoo create, write, unlink, import, generic RPC, SQL, or server action;
- clean-package certification, execution approval, target rehearsal, expiry,
  or recency policy;
- final dependency-ordered import plans, numeric-ID crosswalks, execution
  journals, retries, rollback, or reconciliation;
- new transformations, quality families, fuzzy matching, survivorship, or
  automatic duplicate merging;
- unrestricted model scans or user-authored Odoo domains in the normal UI;
- background queues, distributed jobs, or streaming beyond the supported
  browser limit;
- redesigning the global UI or repairing copy-specific browser tests while the
  separate UI revamp remains active.

## Definition of done

Slice 5 is complete when a data manager can approve prepared data, select
**Compare with Odoo**, and receive a durable, restart-safe, plain-language
classification produced from those exact approved rows and exact protected
target snapshots. The comparison must make only bounded read-only Odoo calls,
preserve complete source and target evidence, expose no technical identifiers
in the normal journey, and grant no certification, export approval, or Odoo
write capability.
