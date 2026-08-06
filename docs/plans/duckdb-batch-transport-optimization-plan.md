# DuckDB batch-transport optimization plan

## Status and objective

**Status:** Approved on 2026-08-06. P0 normalization subphase measurement and
the P1 normalization transport are implemented locally. Exact-parity,
type/byte-bound, and rollback tests pass. The three-run reference-Windows gate
is still required before P2 is approved.

Reduce the complete local preparation time for the observed 4,000-row XLSX
workload from **5 minutes 38 seconds to less than 60 seconds** on the reference
Windows workstation, without changing canonical evidence, review results,
governance, or the read-only Odoo boundary.

The first implementation slice is deliberately narrow: replace the confirmed
slow normalization batch transport, prove exact parity, and measure it before
changing quality or staging.

## Observed evidence

The completed local run produced the following durable evidence:

| Phase | Elapsed time |
| --- | ---: |
| Source transformation and canonical staging | 163.5 s |
| Quality evaluation and publication | 57.7 s |
| Normalization evaluation and publication | 115.5 s |
| Final status publication | 1.4 s |
| **Complete preparation** | **338.1 s** |

Four thousand physical source rows expanded into:

- 27,963 mapped-field evaluations;
- 15,873 transformation impacts and normalization effects;
- 4,000 canonical rows;
- 4,000 quality row results;
- 4,000 source-accounting entries and their row links;
- identity, lineage, review-group, hash, and audit evidence.

The browser's repeated `88%` response was not repeated preparation. It was the
750 ms status poll reporting the fixed `NORMALIZING` phase percentage while the
server built and published the review evidence.

### Isolated persistence benchmark

A disposable copy of the completed project database was used to transport the
same 15,873 stored effect rows into a new table. No source values were printed
or retained after the diagnostic.

| Transport | Rows | Elapsed time |
| --- | ---: | ---: |
| Current typed Python arrays with `UNNEST` | 15,873 | 64.272 s |
| Bounded in-memory JSON relation with `json_each(?)` | 15,873 | 0.364 s |

A 1,000-row comparison reinforced the result:

| Transport | Elapsed time |
| --- | ---: |
| `executemany()` | 3.122 s |
| Typed arrays with `UNNEST` | 3.476 s |
| Multi-row SQL `VALUES` | 5.337 s |
| Bounded JSON relation | 0.020 s |

Reading and decoding all 15,873 existing effect documents took about 0.1
seconds. The measured bottleneck is therefore Python-to-DuckDB batch binding
and insertion on this Windows/DuckDB 1.5.5 environment, not browser polling or
ordinary JSON decoding.

These are transport microbenchmarks, not end-to-end performance claims. The
complete workflow must be measured again after each implementation slice.

### Second-platform evidence (not a before/after comparison)

The repository benchmark now includes transport construction as well as the
insert, uses the production hardened DuckDB configuration, and alternates the
candidate order. On the Mac development machine, three synthetic 15,873-row
runs produced these medians:

| Transport | Batches | Median elapsed time |
| --- | ---: | ---: |
| Typed Python arrays with `UNNEST` | 16 | 0.166 s |
| Bounded typed JSON with `from_json_strict` | 16 | 0.173 s |

Typed JSON is therefore effectively tied on this platform, not a universal
speedup. This result must not be compared numerically with the Windows
customer-shaped diagnostic: the machines, inputs, and runtime behavior differ.
Its value is the absence of a material second-platform regression.

A separate 4,000-row synthetic products workflow completed locally in 3.978
seconds with a 282.4 MiB measured peak. Normalization took 0.655 seconds,
including 0.410 seconds of aggregation and 0.153 seconds of persistence plus
ordered hashing. It produced 4,000 effects, so it is a correctness and
instrumentation check rather than a proxy for the 15,873-effect Windows run.

## Current design weaknesses

### 1. Slow transport is repeated across every preparation phase

High-volume repositories build one Python list per column and bind those lists
to `unnest(?)`. The pattern is used for canonical rows, identity facts,
lineage, physical accounting, transformation impacts, quality results, source
accounting, normalization effects, and review groups.

The cost compounds because a 1,000-row batch can take several seconds even
when the database work is otherwise simple.

### 2. Normalization reconstructs the same effects more than once

The bounded evaluator first replays impacts to aggregate review groups. The
publisher then replays them to construct and insert effects, and finally reads
the stored effect JSON in deterministic order for the run hash.

The ordered hash pass is required. Reconstructing the effects twice is not.

### 3. Quality decodes canonical rows for separate outputs

The clean direct path validates canonical rows, then replays them to publish
quality row results, and replays them again to publish source-accounting
entries and links. The design is bounded, but it repeats decoding and database
queries.

### 4. Progress is truthful but too coarse

`NORMALIZING` maps directly to 88%. There is no visible distinction between
group aggregation, effect persistence, ordered hashing, and the final commit.
A healthy long-running job therefore appears stalled.

## Non-negotiable boundaries

- Registered source bytes remain unchanged and hash-bound.
- Canonical rows, lineage, reconciliation, quality, normalization, and audit
  evidence remain deterministic and complete.
- Existing portable content-hash meanings do not change.
- Batch transport is an adapter detail; it must not become another mapping or
  transformation language.
- No numeric Odoo record IDs are stored in mappings or portable evidence.
- Preparation makes zero Odoo calls.
- Later comparison remains read-only, allowlisted, and bounded; no Odoo ORM,
  RPC, or repository call may occur inside a source-row loop.
- Protected values are not logged, written to unmanaged temporary files, or
  included in benchmark output.
- Publication remains atomic, and the last valid current evidence survives a
  failed attempt.
- Existing project retention and superseded-evidence behavior is not weakened
  for speed.

## Work packages

### P0 - Add subphase evidence

**Purpose:** Separate transformation, transport, hashing, and commit time in a
repeatable fresh-process measurement.

Add count-and-time instrumentation for:

- XLSX materialization and bounded parsing;
- row transformation and impact construction;
- canonical, identity, lineage, physical-row, and impact insertion;
- direct finalization and staging hashing;
- quality validation, row-result insertion, accounting insertion, and commit;
- normalization aggregation, effect insertion, ordered hash, and commit;
- DuckDB connection, transaction, and checkpoint time where material.

Instrumentation must report counts, byte sizes, and elapsed time only. It must
not report source, proposed, or protected values.

**Gate:** Three fresh-process runs reproduce stable counts and hashes. Timing
instrumentation adds no material runtime or memory overhead.

### P1 - Replace normalization batch transport

**Purpose:** Remove the confirmed bottleneck with the smallest possible code
and evidence surface.

Replace only the insertion into `normalization_pending_effect` with a bounded
in-memory JSON relation:

1. Build at most the configured normalization batch size in memory.
2. Encode a transport-only JSON array with explicit field names.
3. Pass it as one bound parameter to DuckDB `from_json_strict` with one fixed,
   allowlisted structure declaration.
4. Let DuckDB construct the complete typed row structure in one operation;
   do not repeatedly extract individual fields with `json_each`.
5. Insert into the existing pending table inside the existing transaction.
6. Preserve the existing deterministic final ordering, encoded effect JSON,
   incremental content hash, count validation, and pointer promotion.

Bound every envelope by both row count and UTF-8 byte count. Include JSON
construction in transport benchmarks so the comparison measures the complete
adapter cost rather than database execution alone.

The outer JSON envelope is not durable evidence and must not participate in a
content hash. Each existing `effect_json` document remains byte-for-byte the
canonical item hashed and stored.

Do not introduce pandas, NumPy, or PyArrow in this slice. A new dependency is
not justified while the bounded no-dependency transport has the strongest
measurement on the affected workstation.

**Gate:** On the observed 15,873-effect shape:

- inserted effect count, order, columns, `effect_json`, groups, summary, and
  normalization content hash are identical;
- rollback and idempotency tests remain green;
- normalization completes in less than 60 seconds in all three fresh-process
  Windows runs, or the isolated persistence portion falls by at least 80%;
- peak working set does not regress materially;
- the same focused benchmark passes on a second supported operating system.

The original 45-second gate was not supported by the measurements: replacing
64.272 seconds of a 115.5-second phase with a 0.364-second insert still implies
about 51.6 seconds before normal variance. The revised gate is demanding but
mathematically reachable. The complete-workflow target remains a stretch goal
until P2 is measured.

### P2 - Adopt the proven transport for quality and staging

**Purpose:** Remove the same binding overhead from the other two dominant
phases.

After P1 passes, migrate one evidence family at a time:

1. quality row results;
2. source-accounting entries and links;
3. preparation transformation impacts;
4. canonical staging rows;
5. identity, lineage, and physical-row facts;
6. remaining high-volume evidence tables.

Use responsibility-specific insert statements or a tightly allowlisted shared
adapter. Do not accept arbitrary table or column names from application data.
Every destination keeps explicit casts and existing constraints.

Benchmark each family before proceeding. A global mechanical replacement is
not acceptable because small metadata tables do not need a bulk transport and
different row shapes have different null, boolean, integer, and JSON rules.

**Gate:** The 4,000-row workflow completes in less than 60 seconds across three
fresh-process Windows runs, with exact evidence parity. The existing 100,000-
row time and memory gates remain authoritative and must not regress.

### P3 - Remove avoidable repeated construction

**Purpose:** Reduce CPU and allocation volume after transport is no longer
masking those costs.

For normalization, construct each effect once while simultaneously:

- accumulating group counts and bounded examples;
- appending its already encoded durable representation to the pending sink;
- recording the compact changed-row membership required for the summary.

Retain one ordered database pass to promote effects and calculate the exact
canonical hash.

For clean direct quality, evaluate each canonical row once and emit both its
quality result and source-accounting entry through bounded sinks. Keep compact
global indexes only where cross-row checks require them.

Use a single cursor with `fetchmany()` when it preserves bounded memory and
ordering. Avoid reopening the same ordered query for every page unless a
profile proves that the keyset boundary is necessary.

**Gate:** Structural counters prove that each canonical row and transformation
impact is decoded or constructed only as many times as the documented
integrity contract requires. All portable hashes remain identical.

### P4 - Consolidate preparation transactions carefully

**Purpose:** Reduce connection, commit, and checkpoint overhead without
creating long locks or unbounded memory.

Evaluate one project-scoped unit of work per bounded source batch so canonical
rows, identities, lineage, physical accounting, and impacts can be inserted
through one connection. Final publication remains a separate atomic pointer
promotion after hashes and reconciliation pass.

Do not keep DuckDB open across Odoo access. This workflow currently performs
no Odoo calls, and the later comparison boundary must continue to close its
local transaction before contacting Odoo.

**Gate:** Fewer connections and commits are observed, failure cleanup remains
restart-safe, and concurrent browser polling does not delay or corrupt the
worker.

### P5 - Report meaningful progress

**Purpose:** Make honest local work visible to a non-technical data manager.

Keep the business-facing phase labels, but add bounded sub-progress events such
as:

- `Saving prepared rows`;
- `Running data checks`;
- `Grouping 15,873 changes for review`;
- `Saving 9,000 of 15,873 changes`;
- `Verifying prepared evidence`.

Progress must be monotonic and derived from real completed batches. Polling,
typing, filtering, or opening a review page must not save or validate data.

**Gate:** A deliberately slowed browser test shows continuing progress and a
clear current action without exposing technical storage terminology in the
main operator journey.

## Provisional performance budget

For the observed 4,000-row, effect-heavy XLSX shape on the reference Windows
workstation:

| Work | Budget |
| --- | ---: |
| Source parsing, transformation, and staging finalization | 25 s |
| Quality evaluation and publication | 10 s |
| Normalization evaluation and publication | 15 s |
| Final validation, commit, and job transition | 5 s |
| **Planned total** | **55 s** |
| Contingency | 5 s |

These budgets guide profiling; they are not permission to weaken evidence.
The broader 100,000-row plan retains its existing 120-second and 900-MiB
release gates.

## Verification matrix

Every implementation slice must cover:

| Concern | Required evidence |
| --- | --- |
| Semantic parity | Exact canonical, quality, normalization, and eligible-dataset hashes |
| Row parity | Exact source, canonical, issue, effect, group, and accounting counts |
| Ordering | Stable ordinals and byte-identical durable item JSON |
| Types | Null, boolean, integer, decimal, date, datetime, and Unicode cases |
| Security | No value logging, unmanaged temporary file, extension loading, or external access |
| Failure | Rollback, retry, idempotency, stale-input rejection, and last-current preservation |
| Odoo boundary | Zero Odoo calls during preparation |
| Performance | Three fresh-process Windows runs plus one second-platform regression run |
| Memory | Peak working set and database/WAL size recorded for every run |

Use deterministic synthetic data shaped like the observed workload. Real
customer values are not committed as fixtures.

## Alternatives and blind spots

- `executemany()` and multi-row `VALUES` were slower than the current transport
  in the isolated Windows comparison; they are not recommended.
- PyArrow or a DuckDB-native appender may be reconsidered if bounded JSON
  regresses on another supported platform, but dependency size, packaging,
  type fidelity, and peak memory must be measured first.
- A platform-name conditional is fragile. Prefer one transport that passes the
  required operating-system matrix, or select an implementation from a
  measured capability probe with deterministic results.
- The outer JSON envelope duplicates encoded item text temporarily. Batch size
  must remain bounded, and effect-heavy 100,000-row memory evidence remains a
  release gate.
- Typed JSON conversion must distinguish JSON null from the strings `"null"`,
  `"true"`, and `"false"`; strict fixed-shape conversion and edge-case tests
  are mandatory.
- DuckDB upgrades may change parameter-binding performance. Record and pin the
  tested DuckDB version, and rerun transport benchmarks before dependency
  updates.
- The existing project database retains superseded governed evidence. Measure
  fresh and retained-history projects separately before attributing time to
  database growth or changing retention behavior.
- Multiprocessing is not a first-line fix. It increases ordering, memory,
  packaging, and failure complexity while the confirmed bottleneck is a
  serial persistence boundary.

## Delivery sequence

1. Complete the remaining P0 phase instrumentation; normalization aggregation
   and persistence/hash measurement plus the reproducible transport benchmark
   are already present.
2. Keep P1 limited to normalization transport; its local implementation and
   parity tests are complete.
3. Review three fresh-process Windows runs. The second-platform regression and
   workflow checks are complete, but are not substitutes for Windows evidence.
4. Extend the proven adapter through P2 one evidence family at a time.
5. Profile again before approving P3 or P4.
6. Implement P5 progress reporting after stable subphase counters exist.
7. Update the 100,000-row performance plan with comparable before-and-after
   results; never replace historical measurements.

Do not raise the supported row limit as part of this plan unless every release
gate in the authoritative 100,000-row performance plan passes.
