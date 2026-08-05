# 100,000-row performance refactor plan

## Status and outcome

**Status:** Proposed on 2026-08-05. Not implemented.

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
| Peak Windows working set | less than 512 MiB |
| Project DuckDB after the run | less than 512 MiB |
| Source or canonical rows silently lost | 0 |
| Odoo calls during preparation | 0 |

The run must also produce identical content hashes and portable evidence for
identical inputs. A failure must roll back incomplete publication and retain
the last valid current evidence.

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

Add the opt-in benchmark harness and phase timers before changing algorithms.
Run the existing narrow fixture at 1,000, 10,000, and 25,000 rows, then attempt
the 100,000-row fixtures without changing the product limit. Record failures,
timeouts, and peak memory rather than treating them as missing results.

Instrumentation must report counts and timings only. It must not log source
values, credentials, numeric Odoo IDs, or other protected evidence.

**Gate:** the same fixture produces stable counts and hashes across runs, and
the measurement itself does not materially change runtime or memory.

### P1 - Remove avoidable superlinear Python work

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

### P4 - Stream quality and normalization evidence

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

If any required fixture exceeds 120 seconds or 512 MiB, retain the 25,000-row
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
