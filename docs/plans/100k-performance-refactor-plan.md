# 100,000-row performance refactor plan

## Status and outcome

**Status:** In progress since 2026-08-05. P1 CPU work and the direct-dataset P3
bounded source/session path are implemented. Direct 100,000-row Products and
BOM-shaped source phases pass their 120-second and 900-MiB gates. The complete
Products workflow takes 88.661 seconds but peaks at 1,608.6 MiB because P4
still materializes the canonical and quality object graphs. Related/derived
BOM P3 behavior and P4 remain release blockers.

This plan raises the supported browser preparation scope from 25,000 to
100,000 physical source rows without weakening deterministic evidence,
lineage, validation, approval, or the read-only Odoo boundary.

The primary outcome is:

> Prepare and durably publish 100,000 physical source rows in less than
> 120 seconds on the reference Windows workstation.

The current 25,000-row limit remains authoritative until every release gate in
this plan passes. A fast evaluator-only probe is not sufficient evidence.

This plan is the performance extension of the
[data-quality and staging delivery plan](data-quality-and-staging-plan.md).
The separate [Slice 5 durable preflight plan](slice-5-durable-preflight-plan.md)
remains authoritative for comparing the exact frozen prepared rows with Odoo.

## Exact performance contract

### Primary operation

The timed operation is the complete local **Prepare and review data** action,
measured immediately before the first source artifact is materialized and
ending only after the normalization summary is durably committed.

The measurement includes:

- frozen source checksum verification and CSV/XLSX parsing;
- submitted mapping and derived-entity evaluation;
- canonical rows, lineage, reconciliation, and control totals;
- atomic canonical publication;
- quality rules, identity collisions, relationship propagation, source
  accounting, quarantine, and atomic quality publication;
- transformation effects, normalization groups, eligible-dataset hashing, and
  atomic normalization publication.

It excludes user review time, Odoo connector latency, target-record volume,
and workbook generation. Those operations have separate measurements below.

### Required gate

All three fresh-process runs of the primary fixture must meet:

| Measure | Required result |
| --- | ---: |
| Physical source rows | 100,000 |
| End-to-end local preparation | less than 120 seconds |
| Peak Windows working set | less than 900 MiB |
| Project DuckDB and temporary spill size | recorded; non-gating |
| Source or canonical rows silently lost | 0 |
| Odoo calls during preparation | 0 |

The run must also produce identical content hashes and portable evidence for
identical inputs. A failure must roll back incomplete publication and retain
the last valid current evidence.

Elapsed time, Python allocation/working-set pressure, repeated scans, and
N+1-shaped work are the optimization priorities. Database and temporary spill
sizes remain useful regression observations, but do not drive refactoring or
block the release unless they cause a correctness, security, or operational
failure.

### Read-only comparison gate

**Compare with Odoo** must not repeat preparation. For a frozen 100,000-row
input it must:

- perform zero source artifact reads and zero mapping, quality, or
  normalization evaluations;
- construct its request plan from the exact frozen canonical rows;
- make no Odoo call inside a source-row or field loop;
- use only the allowlisted read operations and bounded request pages;
- report connector time separately from local planning, classification, and
  persistence time;
- complete the deterministic local-snapshot comparison fixture in less than
  120 seconds.

Real remote-target latency is recorded but cannot be used to pass or fail the
local Python performance contract.

## Current baseline

The current integrated narrow fixture produced 25,001 canonical and quality
rows from 25,000 physical rows in 45.392 seconds, with 348.0 MiB peak RSS and a
66.3 MiB project database. It has only three columns, one grouped parent, and
one child per physical row. It does not represent wide mappings, a deep
relationship chain, or the complete normalization path.

The current design also retains several representations at once: physical
tables, staged tables, prepared records, canonical rows, transformation
effects, quality results, and normalization effects. Linear extrapolation from
the existing memory measurements is not an acceptable route to 100,000 rows.

## Implementation record

### 2026-08-05 - P1 CPU and complexity pass

Implemented after the repository-only file reorganization:

- relationship readiness now builds a parent-to-dependent graph once and uses
  a queue; rows and relationship edges are not rescanned to reach a fixed
  point;
- singleton coordinate, identity, and dependency indexes avoid one list per
  row, duplicate references are deduplicated per dependent, and queue state is
  derived from the safe-to-unsafe transition;
- dataset mappings, relationship value maps, identity labels, scalar labels,
  ordinal mappings, and derived-reference rules are compiled once per dataset;
- prepared and canonical rows, source lineage, reconciliation dispositions,
  and configured control totals are grouped or accumulated in one outer pass;
- source checksums use bounded buffered reads;
- publication code calculates each source run hash once and reuses it;
- normalization candidates stream from transformation-impact rows instead of
  first copying the complete candidate tuple;
- portable-evidence traversal uses an iterative `collections.abc` validator,
  and staging applies it one row at a time instead of materializing a second
  complete run; the numeric-Odoo-ID prohibition is unchanged.

The opt-in deep-chain fixture is in `tests/test_quality.py` and is excluded
from ordinary discovery unless `IMPODO_RUN_QUALITY_SCALE=1`. At 100,000 rows
and 99,999 edges, fixture construction, quality evaluation, and final quality
hashing totalled **31.835 seconds**. The comparable
pre-validator-optimization result was **51.363 seconds**, a **38.0% CPU-time
reduction**, with identical staging and quality hashes.

This is not the complete **Prepare and review data** gate. The all-quarantined
chain still reached 845.5 MiB peak working set because it deliberately retains
100,000 quality issues and quarantine entries. The supported browser limit
therefore remains 25,000 rows. P3/P4 bounded evidence production, the
integrated mixed fixture, and three final end-to-end runs remain required.

Focused evaluator, staging, relationship, portable-evidence, quality
publication, and normalization tests pass. The post-benchmark full-suite run
during concurrent Slice 5 development executed 230 tests with 6 errors and 3
skips. All six errors have the same Slice 5 planner cause: legacy `asset_lines`
fixtures cannot satisfy the new fail-closed requirement that every Odoo read
be safely narrowed. No failure was reported in the optimized staging, quality,
normalization, or new preparation-scale paths. The planner fixtures must be
reconciled by the active Slice 5 work before the repository suite can be green.

### 2026-08-05 - Complete wide preparation diagnostic

An opt-in full-workflow fixture now exercises the real application services,
CSV loading, mapping evaluation, canonical publication, quality publication,
normalization publication, one business control total, and one visible
normalization effect per row. It uses 30 source columns and 20 mapped scalar
fields. The scale guard alone is patched inside the test; no evaluator,
publication, validation, hashing, or persistence rule is bypassed.

On the Lenovo Windows reference machine, 10,000 rows took **40.271 seconds**
and peaked at **383.8 MiB**. At 100,000 rows the workflow took **429.175
seconds** and peaked at **2,897.3 MiB**, failing the required 120-second and
900-MiB gates. The 100,000-row phase breakdown was:

| Phase | Elapsed |
| --- | ---: |
| Source loading and mapping evaluation | 121.394 s |
| Canonical staging publication | 134.635 s |
| Quality evaluation and publication | 58.315 s |
| Normalization evaluation and publication | 113.651 s |

All 100,000 rows were durably staged, classified ready, and included in the
eligible normalization dataset; the control total passed and all three
content hashes were published. This is a performance failure, not a logic or
data-loss failure. The supported browser limit remains 25,000 rows. The
database reached 221.8 MiB, which remains a recorded non-gating observation.

### 2026-08-06 - Proposal phases 0 through 2

Implemented the contained measurement, CPU, and encoded-publication work
without changing the portable contracts or the supported browser limit:

- the opt-in full-workflow probe now selects deterministic `products` and
  effect-heavy `bom` workloads and records mapped cells, canonical rows,
  lineage links, issues, effects, groups, serialized characters, phase times,
  hashes, database size, and peak working set;
- transformation comparison has primitive type-aware fast paths and preserves
  display semantics such as `False` being distinct from `0`;
- quality manager rules reuse one dataset index, and preflight incoming
  identity resolution uses a deterministic identity index instead of scanning
  candidate rows for each request;
- staging, quality, and normalization publication no longer performs a whole
  `from_json(to_json())` validation round trip;
- every large row item is canonically encoded once, and the exact same encoded
  text is both persisted and fed to an incremental canonical-document hash;
- DuckDB bulk ingestion now uses bounded typed parameter arrays with `UNNEST`
  instead of serializing and reparsing an outer nested JSON document;
- published staging and quality hashes are passed to downstream phases instead
  of recalculating complete upstream hashes, and quality summary counts are
  accumulated once per publication.

The incremental encoder is covered by an exact equivalence test against the
existing canonical JSON hash. Existing publication round-trip, idempotency,
rollback, invalidation, and batching tests remain green. The complete ordinary
suite executed **256 tests in 18.759 seconds: 247 passed and 9 opt-in tests were
skipped**.

Fresh-process 100,000-row results on the MacBook Air M5 were initially reported
as follows:

| Workload | Complete preparation | Ending RSS | Project DB | Effects | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Products | 23.178 s | 1,102.3 MiB | 224.3 MiB | 100,000 | Time passed; RAM failed |
| BOM | 27.090 s | 1,237.1 MiB | 302.3 MiB | 300,000 | Time passed; RAM failed |

On macOS, the original harness fell back from unavailable Windows `peak_wset`
to a single end-of-run RSS observation. The memory column is therefore ending
RSS, not peak working set. Both ending values already exceed 900 MiB and prove
that those runs fail the RAM gate, but they do not locate or quantify the true
peak. These Mac results are also not compared numerically with the earlier
Lenovo Windows results.

A controlled same-Mac A/B used the exact committed 10,000-row fixture, source
checksum, Python environment, and benchmark command. The baseline was commit
`b90782697bfef9c6a3554aa4ab90b41fe6c5cd81`; the optimized snapshot differed
only in the 12 source files changed by proposal phases 1 and 2. Each side ran
three times in a fresh process:

| Same-Mac median | Committed baseline | Optimized | Change |
| --- | ---: | ---: | ---: |
| Complete preparation | 5.487 s | 2.401 s | **56.2% lower** |
| Ending RSS | 539.9 MiB | 339.0 MiB | **37.2% lower** |
| Source loading and evaluation | 1.416 s | 1.203 s | **15.0% lower** |
| Canonical publication | 1.838 s | 0.292 s | **84.1% lower** |
| Quality | 0.674 s | 0.276 s | **59.1% lower** |
| Normalization | 1.497 s | 0.526 s | **64.9% lower** |

All six runs used source SHA-256
`1787ff4b764acb36336768d8258c0edaefd2d253c4840b476b42cb4b8018ebad`
and passed the same assertions: 10,000 staged rows, 10,000 ready rows, no
review, quarantine, blocked rows, or failed control total, and 10,000 eligible
and changed normalization records. Exact incremental-hash equivalence,
publication round-trip, idempotency, and rollback behavior are separately
covered by the ordinary semantic suite.

The controlled result demonstrates that the code changes reduce elapsed time
and end-of-run resident memory. It did not continuously sample the macOS peak,
so it is not evidence that peak memory fell by the same amount.

### 2026-08-06 - Durable typed-row quality boundary

The committed staging contract already contained a symmetric typed-value codec
for decimals, dates, datetimes, logical references, and business references.
This slice completed the architectural transition that codec was intended to
enable:

- quality no longer accepts or indexes `PreparedBundle`; relationship
  propagation reads typed references from canonical rows;
- after canonical publication, preparation releases the transient staged
  object containing both prepared and canonical graphs;
- quality and normalization consume the exact published canonical run reloaded
  from DuckDB with its expected content hash;
- durable reload now fetches bounded row batches, hashes the exact stored row
  text incrementally, restores typed rows one at a time, and rejects missing or
  reordered evidence;
- a cross-platform sampler records working set throughout preparation and also
  reports ending RSS, avoiding the previous macOS measurement ambiguity.

The corrected complete-workflow diagnostics were:

| Workload | Rows | Complete preparation | Observed peak | Ending RSS | Durable reload | Result |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Products | 100,000 | 26.929 s | 1,605.0 MiB | 829.0 MiB | 4.543 s | Time passed; RAM failed |
| BOM | 100,000 | 61.961 s | 1,685.6 MiB | 789.6 MiB | 8.855 s | Time passed; RAM failed |

The BOM diagnostic ran after several large local probes and is retained as an
observed result rather than a stable timing baseline. Both workloads remained
below 120 seconds and preserved all expected rows and effects. The complete
ordinary suite executed **263 tests in 39.969 seconds: 253 passed and 10
environment-gated tests were skipped**.

This slice lowers downstream residency and establishes one typed durable
authority, but it cannot lower the true peak materially: the peak occurs while
source evaluation still retains physical tables, prepared records, canonical
rows, and transformation impacts together. P3 bounded source transformation
and pending durable staging is therefore the next implementation slice.

## Benchmark fixtures

The performance harness must generate deterministic, hash-stable fixtures that
respect the same file, row, column, and cell limits as the browser.

1. **Integrated mixed fixture:** 100,000 physical rows across related datasets,
   with 30 source columns, 20 mapped scalar fields, three incoming
   relationships, representative transformations, and declared totals.
2. **Deep relationship fixture:** 100,000 canonical rows in a dependency chain,
   with an unsafe root so propagation must reach every dependent.
3. **Effect-heavy fixture:** 100,000 eligible rows with at least one visible
   prepared-value effect per row, exercising normalization batching and group
   aggregation.
4. **Existing narrow fixture:** retained unchanged for trend comparison with
   the recorded 1,000, 10,000, and 25,000-row results.

Each benchmark runs in a fresh process because process-lifetime peak working
set cannot be reset reliably on Windows. Record:

- repository revision and dirty-worktree state;
- Python, DuckDB, openpyxl, and operating-system versions;
- processor, available memory, storage type, and temporary-directory path;
- fixture content hash, source bytes, physical and canonical row counts;
- elapsed time for loading, transformation, canonical finalization,
  publication, quality, normalization, and total execution;
- peak working set, project-database size, batch counts, relationship edge
  count, transformation-effect count, and serialization bytes;
- Odoo request count, result pages, and connector time for comparison fixtures.

The 100,000-row suite remains opt-in and is not part of ordinary fast unit-test
discovery. Semantic regression tests remain part of the normal suite.

## Provisional time budget

The total gate is fixed; these internal budgets guide profiling and may be
rebalanced when measurements identify the actual bottleneck.

| Work | Budget |
| --- | ---: |
| Source verification and bounded parsing | 20 seconds |
| Transformation and canonical batch append | 35 seconds |
| Cross-row finalization and control totals | 10 seconds |
| Quality evaluation and publication | 20 seconds |
| Normalization evaluation and publication | 20 seconds |
| Final validation, hashes, and commits | 10 seconds |
| Planned total | 115 seconds |
| Contingency | 5 seconds |

## Work packages

### P0 - Establish repeatable evidence

**Status:** Partial. The fresh-process deep-relationship fixture and a complete
wide, effect-heavy preparation fixture now provide deterministic hashes, phase
timings, and peak working-set capture. The related-dataset mixed fixture still
needs to be added, but the existing complete fixture already fails both release
gates and is sufficient to retain the current product limit.

Add the opt-in benchmark harness and phase timers before changing algorithms.
Run the existing narrow fixture at 1,000, 10,000, and 25,000 rows, then attempt
the 100,000-row fixtures without changing the product limit. Record failures,
timeouts, and peak memory rather than treating them as missing results.

Instrumentation must report counts and timings only. It must not log source
values, credentials, numeric Odoo IDs, or other protected evidence.

**Gate:** the same fixture produces stable counts and hashes across runs, and
the measurement itself does not materially change runtime or memory.

### P1 - Remove avoidable superlinear Python work

**Status:** Implemented for the currently identified evaluator and quality hot
paths. Focused semantic tests and the 100,000-row chain pass. Further P1 work
must be driven by a profile or a demonstrated N+1/repeated scan.

Make contained changes before the persistence redesign:

- replace repeated relationship fixed-point scans with a parent-to-dependent
  graph and queue so every row and edge is processed at most once;
- compile dataset indexes by ID and name once;
- group prepared and canonical records by dataset in one pass;
- compile source labels, ordinal mappings, formula context, and static
  transformation descriptions once per dataset;
- construct the source-value-by-ordinal map once per row, not once per mapped
  field;
- accumulate configured control totals during one outer record pass;
- calculate source hashes through bounded buffered reads rather than
  `read_bytes()`.

Relationship tests must cover deep chains, fan-out, cycles, missing and
ambiguous parents, multiple references, warning policies, and deterministic
issue ordering. The optimized output must remain semantically identical.

**Gate:** the 100,000-row dependency chain is linear by structural counters;
doubling rows does not cause repeated full-row or full-edge scans.

### P2 - Compare from frozen durable rows

**Status:** Pending; remains governed by the Slice 5 plan.

Implement Slice 5 before raising the scale limit. Replace comparison-time
source preparation with a version-checked `FrozenPreflightInput` built from
the current canonical, quality, and frozen normalization runs.

Adapt eligible canonical rows to the existing prepared-record engine contract
without a second transformation authority. Fetch current pointers and their
bound hashes in one consistent repository snapshot, and validate them again
before publishing comparison results.

**Gate:** comparison reads no CSV/XLSX bytes, does not publish new staging,
quality, or normalization evidence, and stops before Odoo access when frozen
evidence is stale or incomplete.

### P3 - Stream source preparation into temporary durable staging

**Status:** Partial. Bounded CSV/XLSX readers, compiled row transformers,
durable preparation sessions, batched impacts and lineage, duplicate-identity
finalization, exact encoded publication, failure cleanup, and the direct
Products/BOM-shaped path are implemented. Related/derived BOM behavior remains
on the materializing path.

The implementation-ready scope, sequencing, failure model, and gates are in
the [100,000-row bounded preparation implementation plan](100k-bounded-preparation-plan.md).

Replace the materializing `SourceTable -> SourceTable -> PreparedBundle ->
CanonicalStagingRun` path with bounded row batches and a transactional staging
session:

```text
bounded source reader
-> compiled pure row transformer
-> append temporary canonical rows and transformation effects
-> finalize cross-row rules and reconciliation
-> validate deterministic evidence
-> atomically publish the current run
```

Direct datasets can flow through immediately. Operations that require global
knowledge must spill bounded state into temporary DuckDB tables:

- source-identity duplicate detection;
- derived lookup and hierarchy accumulation;
- physical-to-canonical lineage fan-out;
- deterministic final ordering and reconciliation;
- relationship edges and unresolved incoming keys.

The source checksum may be calculated while reading, but no run becomes current
until the final checksum matches the frozen selection. Temporary rows are
discarded on mismatch or failure.

The storage-independent domain boundary remains a pure compiled row
transformer plus explicit sinks. DuckDB coordinates persistence; it does not
become a second mapping language.

**Gate:** batch size changes do not change row IDs, run hashes, dispositions,
lineage, totals, or issue evidence. Peak memory stays bounded as row count
increases from 25,000 to 100,000.

Current controlled evidence: direct Products completed source/session work in
44.384 seconds at 750.6 MiB peak, and direct BOM-shaped input completed in
50.555 seconds at 744.0 MiB peak. The ordinary focused suite and exact direct
parity checks pass. The product limit remains unchanged because Increment 4
and the complete P4 gate are still open.

### P4 - Stream quality and normalization evidence

**Status:** Next measured RAM blocker. Single-use upstream hashes, exact incremental document
hashing, encode-once row persistence, typed `UNNEST` batches, and removal of
the normalization-candidate tuple copy are implemented. Quality now consumes
verified durable typed canonical rows without `PreparedBundle`, and durable
reload is row-bounded. Quality still materializes the complete canonical run,
and bounded evaluation and effect production remain pending.

Quality must consume persisted canonical rows in bounded batches. It may retain
compact global indexes required for identity and relationship rules, but not a
second complete canonical object graph. The relationship queue from P1 remains
the only propagation authority.

Normalization candidates must flow directly into a batched effect sink.
Retain only group aggregates, counts, and bounded examples in memory; do not
hold both all transformation-impact rows and all normalization candidates.

Remove repeated whole-document work inside one publication operation:

- calculate a run content hash once and reuse it;
- encode each portable row once per publication;
- validate the exact bytes or values that are persisted;
- use bounded bulk relations rather than per-row DuckDB calls.

Do not change canonical hash meaning merely for speed. If an incremental hash
or envelope changes the portable contract, write an architecture decision,
bump the relevant contract/evaluator version, migrate stored evidence safely,
and prove invalidation behavior.

**Gate:** the effect-heavy fixture stays within the memory limit, publication
remains atomic and idempotent, and stored evidence round-trips with its exact
content hash.

### P5 - Close the 100,000-row release gate

**Status:** Failed on the first complete 100,000-row fixture. The 25,000-row
product limit remains authoritative while P3 and P4 are implemented.

Run the focused semantic suites, full repository suite, browser workflow tests,
all 100,000-row fixtures, and the deterministic local-target comparison. Review
profiles for CPU time, allocation volume, serialization, DuckDB time, and
unexpected repeated scans.

Only after all gates pass:

- change the browser evaluation limit from 25,000 to 100,000;
- update the canonical staging, quality, operations, and acceptance documents;
- update user-facing scale messages and tests in the same change;
- retain the 25,000-row measurements as historical evidence;
- record the three final 100,000-row runs and their worst result.

If any required fixture exceeds 120 seconds or 900 MiB, retain the 25,000-row
product limit and continue profiling. Do not raise the limit based on an
evaluator-only or best-of-many result.

## Documentation ledger

Every performance pull request must update documentation with the code:

1. Update this plan's work-package status and record the hypothesis tested.
2. Append before-and-after measurements to
   [testing acceptance](../testing/acceptance.md); never overwrite historical
   results.
3. Record fixture hash, row/edge/effect counts, runtime, peak memory, database
   size, machine, date, and repository revision.
4. Update a normative contract only when implemented behavior or a supported
   limit actually changes.
5. Add an architecture decision when changing a content-hash contract,
   transaction boundary, or authoritative evidence source.
6. Keep [the documentation index](../README.md) current when files move or new
   authoritative documents are added.

Each optimization must say whether it improved CPU, peak memory, database
size, connector calls, or only code clarity. A claimed improvement without a
comparable before-and-after measurement remains unverified.

## Non-negotiable correctness and Odoo boundaries

- Registered source bytes remain immutable and hash-bound.
- Every physical and canonical row remains reconciled and traceable.
- Missing or ambiguous relationships block or follow their explicit governed
  policy; optimization must never guess.
- Portable mappings and evidence contain business keys, never numeric Odoo
  record IDs.
- Preparation, quality, normalization, and review remain Odoo-free.
- Comparison remains read-only and batched. No `fields_get`, `search_read`, ORM,
  RPC, or repository query may occur inside a source-row loop.
- No generic Odoo RPC, PostgreSQL write, or future import permission is added by
  this performance work.
- The normal data-manager UI continues to show business language and one clear
  next action; benchmark and implementation details remain support evidence.

## Out of scope

- increasing individual source-file or cell-size security limits;
- using unbounded multiprocessing or memory proportional to all intermediate
  representations;
- installing Node.js or an unmanaged workstation runtime;
- weakening canonical validation, lineage, quarantine, approvals, or atomic
  publication;
- Odoo writes, import execution, or clean-package certification.
