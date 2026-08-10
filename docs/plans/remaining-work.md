# Impodo remaining work

## Status and authority

**Status:** Active roadmap from 2026-08-09.

This is the only planning document in `docs/plans/`. It contains work that is
not yet complete. Completed implementation history belongs in Git, release
evidence belongs in `docs/reports/` and `docs/testing/`, and current behavior
belongs in architecture, contracts, and operations documentation.

The order below is intentional. Finish the measured local scale boundary
before expanding target risk. Clean-package certification, a target-side
gateway, and hosted deployment are conditional capabilities, not prerequisites
for an ordinary disposable-target migration.

## Current boundary

Impodo currently supports the complete reviewed disposable local or remote
Odoo 19 load and read-back path. Its native writer accepts any standard,
extension, or custom model and field present in the captured schema and exact
reviewed preview. Retained remote on-premises acceptance evidence remains
pending until the target is available.

The supported preparation limit is:

- 100,000 physical rows for exact-snapshot direct mappings compiled entirely
  to the native columnar path;
- 50,000 physical rows for direct mappings requiring the Python oracle;
- 25,000 physical rows for derived or materialized paths.

The next unconditional product goal is to extend the completed direct
columnar boundary so complete related and mixed-dataset workflows pass the
100,000-row release gate without changing evidence semantics.

## 1. Build the columnar preparation path and raise the scale limit

### Outcome

Prepare, validate, normalize, and durably publish 100,000 physical source rows
in less than 120 seconds and below 900 MiB peak working set on the reference
Windows workstation. Identical inputs must still produce identical portable
evidence, and failed publication must leave the last valid evidence current.

### Locked implementation direction

This work is local preparation infrastructure only. It has no browser workflow
or Odoo read/write scope.

- Treat CSV and XLSX as governed ingestion formats. After source confirmation,
  parse each selected physical dataset once and publish an immutable Parquet
  source snapshot. Mapping preview and preparation normally read that snapshot,
  not the registered source file.
- Retain the registered source bytes and their hashes as audit evidence, but
  create a new source snapshot only when the selected source evidence changes
  or the source-snapshot contract version changes.
- Compile supported mapping semantics into native Polars expressions. Do not
  use per-cell Python callbacks or Polars Python UDFs in the accelerated path.
- Keep DuckDB as the transactional control, evidence, grouping, and lifecycle
  store. Store bulk immutable source and prepared columnar values in Parquet;
  DuckDB owns their manifests and current pointers.
- Keep the existing Python evaluator as a test oracle while parity is being
  established. Because the product is still in development, do not build a
  legacy-data migration or a permanent user-selectable old/new backend.
- After the parity, failure, and performance gates pass, make the columnar path
  the only production path for its supported dataset class and remove duplicate
  production code.

### Target data flow

```text
registered CSV/XLSX bytes
    -> strict bounded source validation
    -> immutable source-<hash>.parquet
    -> Polars lazy transformation plan
    -> bounded prepared columnar batches/snapshot
    -> existing canonical, quality, normalization, and DuckDB evidence policy
```

### Delivery slices

Implement this track as the following ordered, independently reviewable slices.
Do not combine them into one repository-wide rewrite. Every slice must leave the
test suite green and preserve the last valid current evidence on failure.

#### Slice 1 — Baseline and source-snapshot contract

- Add the missing phase timers and fresh-process CPU/peak-working-set benchmark
  harness.
- Define `SourceSnapshot`, its deterministic source-cell/schema contract,
  content binding, paths, and adversarial fixtures.
- Add the pinned Polars dependency and dependency/license evidence, but no
  production backend switch.

**Exit:** baseline evidence is reproducible and the source-snapshot contract is
fully covered without changing preparation results.

**Implementation state (2026-08-09):** implemented and verified in the
development worktree. The contract, adversarial fixtures, structured
fresh-process harness, exact Polars dependency/lock, and license evidence are
present. A three-process 10,000-row macOS diagnostic proved the harness; clean
reference-Windows measurements remain evidence work under section 1.1, not
additional Slice 1 architecture.

#### Slice 2 — Parquet ingestion and transactional publication

- Implement bounded CSV/XLSX-to-Parquet ingestion after source confirmation.
- Add immutable snapshot storage, DuckDB manifests/current pointers, atomic
  publication, reuse, and orphan recovery.
- Make normal source consumption resolve the verified snapshot rather than
  reopen registered CSV/XLSX bytes; retain only a bounded compatibility adapter
  where the still-row-oriented preparation boundary requires it.

**Exit:** repeat preview/preparation reads the Parquet snapshot with the
registered source artifact unavailable, and ingestion/failure memory remains
bounded.

**Implementation state (2026-08-09):** implemented and verified in the
development worktree. Production source freezing now performs strict-reader
CSV/XLSX ingestion into cell-bounded Parquet fragments, validates and
atomically publishes a content-addressed file, and advances DuckDB selection
and per-dataset snapshot pointers in one transaction. Normal value preview,
related/derived materialization, and bounded direct preparation resolve the
hash-verified snapshot; focused tests remove the original source and repeat
both preview and direct preparation. Failure, truncation, deterministic reuse,
wide-batch, pointer rollback, and orphan cleanup are covered. Clean reference
Windows scale measurements remain evidence work under section 1.1.

#### Slice 3 — Columnar transformation compiler

- Introduce `ColumnarTransformationProgram` and the deterministic capability
  matrix.
- Compile the first supported scalar subset without importing Polars into the
  domain layer.
- Add operation-by-operation oracle parity fixtures and dataset-wide support or
  fallback decisions.

**Exit:** every mapping deterministically compiles to a supported program or an
  explicit whole-dataset fallback reason; runtime behavior is still unchanged.

**Implementation state (2026-08-09):** implemented and verified in the
development worktree. A backend-neutral, hashable program now records the
minimal source projection, provider branches, ordered text rules, required and
typed conversion boundaries, basic validation, source/target identities,
scope, direct lineage, sparse transformation-impact requirements, and
set/global work without importing Polars into the domain compiler. The complete
capability matrix classifies each operation as native-columnar, set/global, or
Python-oracle-only. Any oracle-only use rejects acceleration for the complete
dataset with stable path-specific reasons before source rows are inspected.
Adversarial Polars prototypes cover the supported provider, normalization,
literal replacement, value-match, case, integer, Boolean, date, explicitly
formatted datetime, required, exact-length, and ASCII character-class
semantics against the Python evaluator. ISO datetime remains an explicit
fallback because Python's accepted grammar is broader than the currently
proven native parser. Production preparation routing remains unchanged for
Slice 4.

#### Slice 4 — Native Polars direct transformation

- Implement the Polars adapter using `scan_parquet` and native expressions only.
- Produce bounded typed value, sparse issue, impact, identity, and lineage
  batches through the current canonical publication boundary.
- Add exact end-to-end parity tests and compare transformation CPU and peak
  memory against Slice 1.

**Exit:** supported direct datasets use Polars internally with identical
portable evidence and demonstrate a material transformation-time improvement
without a higher unbounded memory slope.

**Implemented in Slice 4.** Supported direct snapshots now route through
streaming native expressions in 1,000-row execution batches, while canonical
DuckDB writes retain their measured 5,000-row transport batch. Exact Python
oracle parity covers typed records, issues, identities, sparse impacts,
canonical rows, and validated run hashes across several execution chunk sizes;
unsupported semantics fall back once for the complete dataset. A fresh-process
100,000-row/30-column/20-field diagnostic completed bounded preparation in
20.7 seconds versus 26.8 seconds for the Python control. Peak working set was
848 MiB versus 830 MiB, with lower ending RSS; the remaining peak-memory
closure belongs to Slice 5. The documented multi-run parent benchmark remains
the release-grade performance gate.

#### Slice 5 — Columnar prepared publication and memory closure

- Add the prepared columnar snapshot/sink and its transactional manifest.
- Remove full-result Python conversion and the redundant scalar reparsing pass
  from the accelerated path.
- Tune streaming chunks, row groups, compression, concurrency, DuckDB memory,
  and spill settings from measured laptop and reference-Windows results.

**Exit:** the transformation-to-publication path remains bounded at 100,000
rows, failure injection preserves the previous current evidence, and CPU/memory
targets improve relative to the baseline.

**Implemented in Slice 5.** Supported native transformations now stream into a
content-addressed prepared Parquet snapshot before canonical adaptation. The
snapshot contract binds the source snapshot, mapping, schema, transformation
program, row count, physical schema, and exact file hash. DuckDB owns immutable
manifests, per-session bindings, and a current pointer that advances only after
successful canonical publication; failure injection proves that the preceding
pointer survives and unregistered files are removed. Repeated preparation
reuses the exact verified snapshot without rerunning Polars. The accelerated
reader adapts only bounded batches through an allocation-only trusted typed
record constructor and no longer performs a second generic scalar parse.

The local 100,000-row/30-column/20-field probe completed bounded preparation in
21.8 seconds at 827.5 MiB peak and 412.5 MiB ending RSS with the measured
two-thread Polars default, compared with the Slice 4 Python control of 26.8
seconds at 830 MiB peak. The immutable prepared writer uses 5,000-row Zstandard
row groups; reads use 1,000-row streaming batches; canonical DuckDB preparation
remains limited to one thread and 96 MB. Clean reference-Windows measurements
and the multi-run parent benchmark remain release evidence work under section
1.1 rather than additional Slice 5 architecture.

#### Slice 6 — Production cutover and direct-path retirement

- Make the columnar path the only production implementation for the supported
  direct-dataset class.
- Remove temporary compatibility and duplicate row-by-row production code while
  retaining the compact Python semantic oracle in tests.
- Run the complete focused, browser, security, fault-injection, deterministic,
  and opt-in scale suites and update current architecture/acceptance evidence.

**Exit:** the direct columnar path passes its 100,000-row release gate with no
UI or evidence-contract change and no permanent backend selector.

**Implemented in Slice 6.** A supported direct program now requires its exact
source snapshot and can no longer fall through to the Python evaluator. The
obsolete source-Parquet-to-native-batch compatibility iterator was removed;
prepared Parquet is the sole production boundary for supported direct data.
Unsupported direct semantics retain one explicit dataset-wide Python path and
the preceding 50,000-row limit. Limit selection compiles the actual mapping
and verifies complete source snapshots before granting 100,000 rows, so no
backend selector or UI choice was introduced.

The actual spawned worker passed the local 100,000-row gate for Products at
41.883 seconds/826.2 MiB first and 42.046 seconds/869.0 MiB repeat. The
BOM-shaped direct fixture passed at 54.958 seconds/854.7 MiB first and 54.249
seconds/807.6 MiB repeat. Repeats ran in fresh production child processes after
the registered CSV was deleted, preserved staging/normalization hashes, and
did not rewrite the prepared artifact. A two-thread Polars pool missed the BOM
memory gate at 906.7 MiB, so the measured default is now one thread with an
explicit environment override. Cancellation/retry, manifest/pointer failure,
batch-size parity, unsupported fallback, and mandatory-columnar routing are
covered. Reference-Windows repetition remains cross-platform evidence; related
and mixed-dataset 100,000-row work remains in the follow-on slices.

#### Follow-on slices — Related, derived, quality, and normalization

Only after Slice 6 closes should sections 1.8 and 1.9 extend columnar/set-based
execution to related and derived datasets and any remaining measured quality or
normalization bottleneck. They are not hidden prerequisites for proving the
direct transformation architecture, but the complete Products/BOM/mixed
100,000-row product gate remains open until they pass.

### 1.1 Complete comparable performance evidence

- Run three fresh-process measurements on the reference Windows workstation
  for the 4,000-row effect-heavy workbook and the 50,000/100,000-row fixtures.
- Record revision, fixture checksum, Python environment, batch sizes, elapsed
  time by phase, peak and ending working set, database size, counts, and hashes.
- Add subphase counters for source parsing, snapshot writing, Parquet scanning,
  Polars execution, Python adaptation, canonical construction, serialization,
  DuckDB transport, hashing, commit, checkpoint, quality, and normalization.
- Record both CPU/wall time and peak working set. A faster implementation that
  raises peak memory beyond the release boundary does not pass.
- Measure four cases separately: original-file ingestion, first preparation,
  repeated preparation from the same snapshot, and an effect-heavy mapping.
- Add the missing mixed related-dataset fixture. It must exercise relationships,
  derived entities, grouping, multi-source lineage, and ambiguity behavior.
- Keep second-platform results as regression evidence, not as a substitute for
  the Windows acceptance runs.

### 1.2 Define the source-snapshot contract

- Add a mapping-independent `SourceSnapshot` domain contract for one selected
  physical dataset. Bind it to the source-file hash, catalog hash, physical
  selection hash, dataset ID, selected table/range, reader contract version,
  schema hash, row count, Parquet content hash, and creation time.
- Use stable dataset and column keys for physical Parquet column names. Preserve
  the original source row number in a reserved integer column and keep display
  headers in the manifest rather than using them as physical identifiers.
- Specify deterministic representations for CSV strings/nulls and XLSX strings,
  booleans, integers, floating-point values, dates, datetimes, and mixed-type
  columns. Preserve the distinctions required by current mapping semantics.
- Define reserved-name escaping, timezone rules, decimal rules, null versus
  empty-string behavior, deterministic row order, and schema evolution rules.
- Add contract fixtures covering mixed XLSX cell types, Unicode, empty values,
  dates, datetimes, numeric boundaries, long strings, and rejected formulas and
  Excel errors before implementing the writer.

**Gate:** two ingestions of identical governed source evidence produce the same
logical snapshot hash, schema, row order, and cell semantics.

### 1.3 Publish immutable Parquet source snapshots

- Add Polars as a pinned runtime dependency. Do not add PyArrow unless an
  independently measured interoperability need requires it.
- Add a filesystem adapter that writes source snapshots below a fixed,
  application-constructed project snapshot directory. No browser value or
  mapping expression may control a filesystem path.
- Reuse the current strict CSV/XLSX reader and its archive, formula, error-cell,
  row, column, and cell-size protections. Stream accepted rows into bounded
  columnar batches and Parquet row groups; never materialize the complete source
  table merely to write the snapshot.
- Publish with a recoverable protocol: write to a unique temporary file, close
  it, validate schema/count/hash, atomically rename it to a content-addressed
  final path, then register it and advance its pointer in one DuckDB transaction.
- Add DuckDB source-snapshot manifest/current-pointer tables and repository
  methods. Treat published Parquet files as immutable; clean only unreferenced
  temporary or orphan files.
- Make source freezing ensure every selected physical dataset has a verified
  snapshot. Reuse a matching snapshot rather than reopening CSV/XLSX.
- Add cancellation, disk-full, truncated-file, hash-mismatch, stale-pointer,
  retry, and orphan-cleanup tests. A failure must not displace the last valid
  snapshot.

**Gate:** after snapshot publication, mapping preview and repeated direct
preparation complete with the registered CSV/XLSX made deliberately
unavailable, while registered source evidence remains unchanged.

### 1.4 Compile a backend-neutral transformation program

- Introduce a typed `ColumnarTransformationProgram` compiled once per effective
  dataset from the submitted mapping. It must describe inputs, output types,
  expression order, validation/error semantics, identities, scopes, lineage,
  and transformation-impact requirements without importing Polars into domain
  code.
- Classify every mapping operation as native-columnar, set/global, or Python
  oracle-only. Reject unsupported accelerated plans at compilation time rather
  than falling back inside a row or cell loop.
- First native slice: source/constant/fallback providers, trim, empty-as-null,
  whitespace collapse, literal replacement, simple value mappings, case
  conversion, and scalar string/integer/Boolean/date/datetime conversion and
  basic validation where exact parity is proven.
- Initially keep formulas, reference bundles, derived hierarchies, relationship
  policy, arbitrary-precision edge cases, and unsupported regex behavior on the
  oracle path.
- Use dataset-wide backend selection for the first release. Do not mix Python
  and Polars field execution inside one dataset until a later measured need
  justifies the semantic and scheduling complexity.
- Add compiler coverage that gives every submitted mapping a deterministic
  support result and reason without examining row values.

**Gate:** the program hash and support decision are deterministic, and every
native operation has adversarial parity fixtures against the Python evaluator.

### 1.5 Implement the native Polars direct-dataset backend

- Add a local adapter that converts the transformation program exclusively into
  native Polars expressions and begins with `scan_parquet`, selecting only the
  required source, identity, scope, and lineage columns.
- Preserve source-row order and source-row identity without introducing a
  global sort. Treat any expression that disables bounded streaming as an
  explicit compiler capability with a memory test.
- Produce typed proposed-value columns plus bounded/sparse issue and
  transformation-impact streams. Do not accumulate all impacts or create one
  wide mostly-empty diagnostics column per mapped field.
- Never call `map_elements`, `map_batches` with a Python callback, `to_dicts`
  for the complete dataset, or an unbounded `collect` in production code.
- Configure worker concurrency, streaming chunk sizes, Parquet row-group sizes,
  compression, DuckDB memory limits, and temporary spill directories for the
  reference laptop/Windows workstation; benchmark rather than assuming that
  maximum thread count is optimal.
- Keep a bounded adapter to the current prepared/canonical contracts during the
  first parity phase. Measure its object-construction and serialization cost
  separately so that it cannot hide the transformation result.

**Gate:** supported direct datasets produce exactly the same typed values,
issues, effects, identities, row order, totals, lineage, row IDs, and portable
hashes as the Python oracle across batch/chunk sizes `1`, `17`, and the
production default.

### 1.6 Bound prepared publication and remove avoidable reparsing

- Define a mapping-bound prepared snapshot or equivalent bounded columnar sink
  for typed Polars output. Bind it to the source-snapshot hash, mapping hash,
  schema hash, transformation-program hash, row count, and content hash.
- Publish it with the same temporary-file, validation, atomic-rename, DuckDB
  pointer, and orphan-cleanup protocol as source snapshots.
- Avoid converting a complete Polars result back into Python dictionaries,
  `SourceRow` objects, or `CanonicalRow` objects. Adapt only bounded batches and
  release each batch after durable publication.
- Once parity is proven, add a trusted typed prepared-record constructor so the
  accelerated path does not pass already typed values through the generic
  scalar parser a second time.
- Keep sparse issues, impacts, lineage, identity facts, control totals, and
  lifecycle state transactionally governed in DuckDB. Keep bulk immutable
  transformed values in Parquet unless measurement proves a DuckDB table is
  better for a specific fact.
- Decide whether DuckDB reads Parquet directly only after measurement. If it
  does, use a dedicated connection restricted to the exact snapshot directory;
  do not relax general external-access or extension controls.

**Gate:** cancellation or failure at any write, validation, rename, manifest,
or pointer step leaves the previous canonical run and snapshot current, with no
unbounded in-memory recovery structure.

### 1.7 Integrate, switch over, and retire duplicate production paths

- Route supported direct datasets through the Polars backend from bounded
  preparation. Route an unsupported dataset as a whole through the current
  Python oracle until its operations are implemented natively.
- Run both backends in tests and opt-in benchmark tooling, not simultaneously in
  the user workflow. No backend selector or new preparation choice is exposed
  in the UI.
- After the direct-dataset parity and scale gates pass, make Polars mandatory
  for the supported direct class and remove its duplicate row-by-row production
  implementation. Retain compact oracle tests for semantic regression.
- Update the mapping compiler capability matrix whenever an additional
  operation becomes native; add its parity, bounded-memory, and failure tests in
  the same change.

### 1.8 Bound related and derived preparation

- Route BOM relationships, parent grouping, derived hierarchies, aliases,
  relationship edges, and multi-source lineage through the durable preparation
  session.
- Preserve exact row IDs, ordering, lineage, reconciliation, issues, effects,
  control totals, and hashes across batch sizes `1`, `17`, and the production
  default.
- Prove missing, ambiguous, cyclic, fan-out, and deep relationship cases.
- After parity and failure gates pass, remove the duplicate materializing
  browser path and retain only a test oracle where it is still useful.

### 1.9 Bound quality and normalization

- Evaluate verified durable canonical rows in bounded batches instead of
  reconstructing the complete canonical run in memory.
- Retain only compact global indexes required for cross-row identity and
  relationship rules.
- Emit quality results, source accounting, transformation effects, and
  normalization effects through bounded sinks.
- Construct each normalization effect once while accumulating group counts and
  bounded examples; do not retain complete impact and candidate collections.
- Preserve atomic publication, restart safety, typed values, deterministic
  ordering, and exact content hashes.

### 1.10 Profile before further persistence changes

Do not mechanically convert more DuckDB tables. Profile the complete workflow
after the Windows runs, then implement only demonstrated remaining
bottlenecks:

- remove repeated row decoding or effect construction;
- reuse a bounded cursor where it improves measured behavior;
- reduce connection, commit, or checkpoint overhead without holding a
  transaction across Odoo access;
- keep row-count and byte-count bounds for every transport envelope.

### 1.11 Preserve the existing user experience

- Publish monotonic sub-progress from completed batches, such as saving
  prepared rows, running checks, grouping changes, and verifying evidence.
- Keep the main journey in business language; do not expose DuckDB or transport
  terminology to the operator.
- Do not add source-format choices, backend selectors, cache controls, snapshot
  management, or migration screens. Existing source registration, selection,
  mapping, preparation, and review actions retain their current meaning.
- Add a deliberately slowed browser test proving that progress continues and
  never advances ahead of durable work.

### 1.12 Close the 100,000-row gate

The scale limit may change only after all of these pass:

- three fresh-process runs of Products, BOM, and the mixed fixture each finish
  below 120 seconds and 900 MiB peak working set;
- batch size does not change portable evidence;
- the measured direct transformation phase is materially faster than the
  Python baseline and its peak working set remains bounded as row count grows;
- repeated preparation reads only verified Parquet snapshots and does not open
  registered CSV/XLSX bytes;
- source mismatch, cancellation, injected failure, retry, stale-session
  cleanup, and concurrent-pointer tests preserve the last valid run;
- the focused, full, browser, security, and opt-in scale suites pass;
- the deterministic local Odoo comparison performs no source preparation and
  finishes within its separate 120-second local-processing gate;
- operations, acceptance evidence, limits, and user-facing messages are
  updated in the same change.

Until the follow-on gates close, retain 100,000 rows only for verified native
columnar direct mappings, 50,000 for Python-fallback direct mappings, and
25,000 for derived/materialized paths.

## 2. Add optional clean-package certification

### Trigger

Implement this track only for a migration or organization that requires a
formal **clean for Odoo target rehearsal** claim. It must not block the routine
local load path.

### Remaining work

- Approve a versioned coverage scope for the concrete migration, including
  applicable and inapplicable data-quality families and their owners.
- Supply authoritative reference data, locale/currency/unit policy, domain
  validators, anomaly thresholds, fuzzy-match fields, and accepted business
  fixtures for that scope.
- Complete project-specific proof for:
  - localization, types, precision, dates, timezones, and defaults;
  - joins, calculations, hierarchy, cross-field, and cross-row rules;
  - reference translations, fuzzy decisions, survivorship, and anomalies;
  - Odoo company, currency, unit, selection, constraint, and default behavior;
  - privacy, retention, correction, reprocessing, scale, and reconciliation.
- Create a hash-bound package certificate and approval lifecycle for the exact
  frozen source, mapping, rules, decisions, target schema, and target snapshot.
- Invalidate the certificate whenever any bound input changes.
- Run an authorized rehearsal against the exact Odoo target and retain its
  reconciliation evidence.

### Gate

A package may be certified only when every physical and canonical row is
accounted for, no blocking or ambiguous condition remains, every reviewed
exception has evidence, control totals reconcile, all applicable coverage is
verified, privacy controls pass, and the exact package passes target rehearsal.

## 3. Retain remote acceptance and add production Odoo loading

### 3.1 Remote acceptance and operational hardening

The bounded disposable remote profile now provides target fingerprinting,
HTTPS enforcement, credential redaction, explicit target confirmation,
schema-bound capabilities, durable write outcomes, and read-back
reconciliation. The remaining work is to prove it against the intended
on-premises environment and add controls justified by that evidence.

- Run the sanitized representative harness against a fresh disposable remote
  target and retain its target fingerprint, reconciliation, repeat-preview,
  and throughput evidence.
- Confirm the target backup or restore point before each retained acceptance
  run.
- Define pause, resume, uncertain-outcome recovery, and operator guidance for
  remote failures.
- Add adaptive bounded batches only where measurements justify them; isolate a
  bad row without silently changing transaction or retry semantics.
- Inject failures at send, commit, journal, read-back, credential expiry,
  overload, and schema-change boundaries.
- Prove Odoo ACLs, record rules, field access, company scope, credential
  redaction, target locking, and rejection of caller-controlled methods or
  context.

**Gate:** a remote run never reports success from an HTTP response alone,
never retries an unknown write blindly, and reconciles every proposed row.

### 3.2 Production readiness

- Complete threat modeling, privacy assessment, penetration testing, customer
  security review, observability, retention, disaster recovery, release and
  rollback procedures, and representative customer acceptance.
- Define the conditions that promote a run from routine to standard or
  controlled assurance.
- Add separately reviewed support for business actions beyond create and
  explicit update, such as posting or workflow transitions. General model and
  field support must remain preview-derived; do not reintroduce a global model
  or field allowlist.

**Gate:** each added business action has explicit permissions, transaction
semantics, failure handling, reconciliation, rehearsal, and operational proof.

## 4. Conditional architecture work

These items remain parked until a concrete deployment requires them.

### Target-side gateway

Build the signed Odoo add-on, manifest-bound grants, target-side receipts, and
named business-action handlers only when native JSON-2 cannot provide the
required atomicity or idempotency. The gateway must expose no generic RPC,
SQL, `sudo`, or caller-selected method surface.

### Hosted composition

Add PostgreSQL repositories, object storage, durable workers, distributed
target locks, SSO actors, centralized authorization, and managed secrets only
for a hosted deployment. DuckDB and PostgreSQL compositions must produce
semantically identical portable evidence and pass the same contract, fault,
security, and Odoo integration suites.

## Cross-cutting delivery rules

- Preserve registered source bytes and never use direct Odoo database writes.
- Keep one semantic authority for mapping, preparation, quality, and
  normalization behavior.
- Bind every result to exact inputs and fail closed on stale evidence.
- Keep portable evidence free of credentials and numeric Odoo record IDs.
- Measure before optimizing and compare runs only on equivalent environments.
- Do not maintain permanent old/new execution modes after parity is proven.
- Update architecture, contracts, operations, acceptance evidence, docstrings,
  and this roadmap when a boundary changes.

## Decisions needed only when their track starts

1. Which Windows workstation and fixture revisions are the scale-release
   reference?
2. Which real migration first requires clean-package certification, and who
   owns each scope-specific rule and reference dataset?
3. Which remote target, backup evidence, assurance triggers, and recovery
   expectations define the first production profile?
4. Which concrete business action, if any, justifies a target-side gateway?
5. Which deployment requirement, if any, justifies the hosted composition?
