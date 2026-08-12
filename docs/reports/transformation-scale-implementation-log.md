# Transformation scale implementation log

This report tracks the implementation of
`docs/plans/transformation-scale-architecture-plan.md`. It is deliberately
separate from the Odoo source-import refactor. All measurements are from fresh
child processes and contain counts, sizes, timings, routes, and hashes only—no
source values.

## Measurement contract

Every performance claim must compare three fresh runs with an identical fixture,
platform, Python/runtime versions, command, and benchmark instrumentation. A
positive gain means the candidate uses less of the measured resource:

`gain % = (baseline - candidate) / baseline * 100`

The benchmark command records the individual runs and medians and rejects
fixture-byte, count, semantic-hash, platform, or runtime drift. Use:

```bash
.venv/bin/python scripts/benchmark_preparation.py \
  --runs 3 --rows 100000 --columns 30 --mapped-fields 20 \
  --workload products --output .tmp/transformation-scale-candidate.json \
  --compare-to .tmp/transformation-scale-instrumented-baseline-products-100k.json
```

For the related reference shape, use the paired same-code control. It runs the
materialized and set-based routes in separate fresh processes and refuses a
staging-hash or semantic-summary difference:

```bash
.venv/bin/python scripts/benchmark_relationships.py \
  --runs 3 --products 16000 --bom-lines 80000 --batch-size 5000 \
  --output .tmp/transformation-scale-phase5-product-bom-96k-final.json
```

Python traced allocations must be measured in a separate run with
`--trace-python-allocations`, because `tracemalloc` changes CPU and memory
behavior.

## B0 — direct Products baseline

Captured on 2026-08-11 at revision
`11721d9b6ca60d6940b33f5c7ccc35ae65f68adf`, macOS 26.5.1 arm64, Python
3.12.13, DuckDB 1.5.5, Polars 1.43.2, and psutil 7.2.2. The fixture is 100,000
rows, 30 selected columns, and 20 mapped fields. Its registered CSV bytes are
identical across attempts (`sha256:8a7bd6…15b6`).

| Metric | B0 median | Required comparison direction |
| --- | ---: | --- |
| CPU | 50.091 s | Lower |
| Wall time | 45.842 s | Lower |
| Worker/process-tree peak RSS | 830.953 MiB | Lower; hard gate 900 MiB |
| Worker ending RSS | 655.547 MiB | Diagnostic only; worker exit reclaims it |
| DuckDB file | 277.262 MiB | Lower |
| DuckDB used pages | 226.750 MiB | Lower |
| Total project storage | 313.000 MiB | Lower |

The earlier low-instrumentation capture was 44.391 CPU seconds, 44.067 wall
seconds, 797.047 MiB peak RSS, and 278.512 MiB DuckDB file. It is retained as a
sanity check, not as the candidate comparator: phase wrappers and the 50 ms
process-tree sampler add measurable overhead. Candidates must use the
instrumented B0 above.

The detailed B0 explains the footprint:

| Persisted logical payload | Characters |
| --- | ---: |
| Canonical staging row JSON | 223,966,700 |
| Normalization effect JSON | 38,588,900 |
| Quality row JSON | 26,488,900 |
| Source-accounting JSON | 19,488,900 |
| Other quality/group JSON | 840 |
| **Total** | **308,534,240** |

The immutable source snapshot is only 976,068 bytes and the prepared snapshot
is 1,397,045 bytes. The large footprint therefore comes from reconstructing
and persisting several Python/JSON views of the same logical rows, not from the
compressed source-file size.

The corresponding keep/cache/consolidate decisions are recorded in
[Preparation hash inventory](transformation-hash-inventory.md).

The three B0 runs produced identical staging, quality, and normalization hashes:

- staging `sha256:30981b…33c0`
- quality `sha256:d56b0e…3d71`
- normalization `sha256:be8ea3…3684`

## B0-C — wide 1,000-customer structural twin

The reproducible customer twin contains 1,000 rows, 150 selected columns, and
20 mapped fields. Its deterministic CSV is 1,843,410 bytes, close to the
reported 2 MB source size; its source snapshot is 280,808 bytes. Three fresh
native-direct runs produced identical stage hashes.

| Metric | B0-C median |
| --- | ---: |
| CPU | 0.903 s |
| Wall time | 0.900 s |
| Peak RSS | 268.938 MiB |
| Ending RSS | 268.906 MiB |
| DuckDB file | 31.012 MiB |
| DuckDB used pages | 27.000 MiB |
| Total project storage | 33.094 MiB |

This fixture does **not** reproduce an 800–900 MiB preparation peak. It reports
100% native row-local coverage, zero Python transform row/cell callbacks, and
1,000 Python canonical-row constructions/rule-impact replays. That narrows the
original report to a different route or shape—advanced/reference/derived
fallback, much higher effect fan-out, or a parent-plus-worker total—rather than
the compressed Excel byte size or selected width by itself. The exact customer
workbook remains necessary to identify which distinction applies.

A separate tracemalloc run reported only 11.466 MiB of traced Python peak while
process RSS peaked at 269.594 MiB. Tracing increased CPU from roughly 0.9 to
2.9 seconds, which validates the decision to keep traced-allocation evidence
separate from release performance runs. The RSS/traced gap is native runtime,
allocator, database, and engine memory; it is not evidence of a Python leak.

## Implementation checkpoints

| Checkpoint | Status | Performance evidence |
| --- | --- | --- |
| Phase 0: truthful admission and B0 telemetry | Complete | B0/B0-C above; exact customer workbook remains an external diagnostic input |
| Phase 1: remove post-Polars replay | In progress — B1a objectless projection retained | CPU -3.48%, peak RSS -3.72%; zero rule replay/full domain rows, but 100,000 Python row adaptations remain |
| Phase 2: prepared values plus narrow index | Complete for direct native datasets | Canonical row JSON 223,966,700 → 0 chars; peak RSS -20.63%, DB used -39.80%; CPU regression is assigned to Phase 3/4 repeated projectors |
| Phase 3: sparse multi-dataset quality | Complete for direct native datasets | 5 → 2 prepared-value scans; clean quality/accounting physical rows 200,000 → 0; exact logical hashes retained |
| Phase 4: construct normalization once | Complete for direct native datasets | 152,000 → 76,000 effect constructions on the 4,000×19 fixture; CPU -3.19%, peak RSS -13.03%, DB used -4.21%; exact hashes retained |
| Phase 5: set-based product/BOM relationships | Complete for direct multi-dataset product/BOM; derived/grouped cardinality changes remain materialized and capped | Peak RSS -45.65%, ending RSS -57.10%; CPU +10.82%, DB used +48.06% for durable edges |
| Phase 6: conditional transport/hash optimization | Complete for the Phase-5 relationship path | Relationship DB overhead 46.5 → 5.5 MiB; set-based CPU is now 22.78% below materialization; Arrow/hash changes rejected |
| Phase 7: qualification and limit decision | Closeout in progress — legacy/dead-code removal and Mac diagnostics complete | Native per-row adaptation must be removed and observed vectorization evidence added before the clean Windows release profile can authorize a limit change |

The 16,000-product/80,000-BOM shape remains capped until the clean Phase 7
Windows qualification authorizes a limit change. Derived/grouped
cardinality-changing routes remain separately materialized and capped at
25,000 rows. These are truthful capacity limits, not regressions in the
intended final capability.

## B1a — objectless native projection (intermediate)

Captured on 2026-08-11 with the same three-run, fresh-process Products fixture
as B0. The production native path now consumes bounded column projections
without constructing `PreparedRecord` or `CanonicalRow` instances. Rule-impact
observations are aggregated by native Polars expressions; the Python semantic
oracle remains available for bounded parity tests. Control totals consume the
projected scalar values directly.

| Metric | B0 median | B1a median | Gain |
| --- | ---: | ---: | ---: |
| CPU | 50.091 s | 48.346 s | **3.48%** |
| User CPU | 45.330 s | 42.762 s | **5.66%** |
| System CPU | 4.864 s | 5.584 s | -14.80% |
| Wall time | 45.842 s | 46.063 s | -0.48% |
| Peak RSS | 830.953 MiB | 800.063 MiB | **3.72%** (30.891 MiB) |
| Ending RSS | 655.547 MiB | 644.266 MiB | **1.72%** |
| DuckDB file | 277.262 MiB | 286.012 MiB | -3.16% |
| DuckDB used pages | 226.750 MiB | 231.750 MiB | -2.21% |
| Total project storage | 313.000 MiB | 321.750 MiB | -2.80% |

Physical DuckDB allocation varies between otherwise identical runs, while the
persisted logical payload remained exactly 308,534,240 serialized characters.
This slice therefore claims no database-footprint gain. All three semantic
hashes are byte-for-byte identical to B0:

- staging `sha256:30981b…33c0`
- quality `sha256:d56b0e…3d71`
- normalization `sha256:be8ea3…3684`

The vectorization report records 100% native row-local transformation, zero
Python cell callbacks, zero Python rule-impact replay, zero full prepared
records, and zero full canonical rows. It still records 100,000 Python row
adaptations to build the compatibility row JSON and narrow facts. Consequently
B1a is retained as a measurable improvement, but **does not pass the Phase 1
gate**. Removing that final callback requires the Phase 2 narrow-index/value-
artifact boundary rather than disguising a row loop as vectorized batching.

### Rejected identity-hash cache experiment

A subsequent three-run experiment moved identity serialization from the
DuckDB adapter into the row projection and carried the digest with the batch.
It did not eliminate a serialization or change logical storage. Relative to
B1a, median CPU and wall time were worse and peak RSS was noisier; the change
was removed. Its diagnostic evidence remains in
`.tmp/transformation-scale-phase1-objectless-identity-cache-products-100k.json`
and is not an accepted checkpoint.

## B2 — prepared values plus narrow canonical index

Captured on 2026-08-12 with the same three-run, fresh-process 100,000-row
Products fixture as B0. Direct native runs now bind a versioned projection
descriptor to the existing immutable `PreparedSnapshot` and store only the
canonical row index in DuckDB. Compatibility readers reconstruct the exact
canonical stream from the SHA-256-verified Parquet artifact. Duplicate identity
issues are sparse overlay facts; Python-fallback and derived rows retain the
materialized row payload until their own bounded artifact route exists.

| Metric | B0 median | B2 median | Gain |
| --- | ---: | ---: | ---: |
| CPU | 50.091 s | 75.792 s | -51.31% |
| Wall time | 45.842 s | 74.488 s | -62.49% |
| Peak RSS | 830.953 MiB | 659.500 MiB | **20.63%** (171.453 MiB) |
| Ending RSS | 655.547 MiB | 653.531 MiB | **0.31%** |
| DuckDB file | 277.262 MiB | 196.262 MiB | **29.21%** |
| DuckDB used pages | 226.750 MiB | 136.500 MiB | **39.80%** |
| Total project storage | 313.000 MiB | 232.000 MiB | **25.88%** |

The persisted logical JSON footprint fell from 308,534,240 to 84,567,540
characters, a **72.59% reduction**. In particular,
`canonical_staging_row.row_json` fell from 223,966,700 characters to zero.
Prepared values remain in the existing 1.397 MiB compressed Parquet snapshot;
they are not copied into a second canonical value artifact.

The staging, quality, and normalization hashes remain exactly identical to B0
and B1a. Parity is tested across projector batch sizes 1, 17, and 5,000, after
the temporary session-to-snapshot binding is removed, and for sparse duplicate
overlays. Truncating the prepared artifact causes the projector to fail closed.
The forward schema upgrade adds the projection and sparse-issue tables without
changing prior canonical rows.

The CPU regression is measured and understood, not accepted as the final data
plane. Telemetry records five complete compatibility projection scans and
500,000 projected canonical rows during one 100,000-row preparation. Those
passes serve final staging hashing, quality evaluation/publication, source
accounting, and normalization. Phase 3 must replace the quality/accounting
passes with set-based defaults plus sparse exceptions; Phase 4 must consume
durable effect and eligibility facts without reconstructing the canonical
stream again. B2 therefore closes the Phase 2 storage and projector contract
while leaving the combined Phase 1 vectorization gate open.

## B3 — sparse multi-dataset quality and accounting

Captured on 2026-08-12 with three fresh 100,000-row Products processes. Direct
native preparation now persists a compact record label, a reusable hashed
target-match key, base disposition, sparse row issues, and one-to-one lineage
facts beside the canonical index. DuckDB validates ordering, identity
collisions, disposition counts, physical coverage, and one-to-one accounting
over the pending run. The materializing evaluator remains only the bounded
oracle.

The quality manifest defines clean row results and represented source
accounting as run defaults. A clean 100,000-row run therefore stores **zero**
physical `quality_row_result`, `source_accounting_entry`, and
`source_accounting_link` rows. Dirty runs store only row-result exceptions,
shared issue definitions, and quarantine entries. Complete quality and
accounting streams are projected in deterministic order for hashing, reload,
pagination, and downstream normalization.

| Metric | B0 median | B3 median | Gain |
| --- | ---: | ---: | ---: |
| CPU | 50.091 s | 101.406 s | Not comparable: current battery/core state ran the contemporaneous pre-projection control at 97.221 s |
| Wall time | 45.842 s | 96.077 s | Not comparable for the same reason |
| Peak RSS | 830.953 MiB | 653.219 MiB | **21.39%** (177.734 MiB) |
| Ending RSS | 655.547 MiB | 652.203 MiB | **0.51%** |
| DuckDB file | 277.262 MiB | 138.762 MiB | **49.95%** |
| DuckDB used pages | 226.750 MiB | 79.000 MiB | **65.16%** |
| Total project storage | 313.000 MiB | 174.500 MiB | **44.25%** |

The same-session control at commit `152275d` is retained in
`.tmp/transformation-scale-phase1-same-session-control-products-100k.json`.
It ran the earlier objectless/full-JSON route at 97.221 CPU seconds, 88.904
wall seconds, 853.500 MiB peak RSS, 275.762 MiB DuckDB, and 227.250 MiB used
pages. Against that contemporaneous control, B3 is 4.30% worse on total CPU
and 8.07% worse on wall time, but gains 23.47% peak RSS, 49.68% DuckDB file,
65.24% used pages, and 43.98% total project storage. This control proves that
the raw CPU difference from the earlier plugged-in B0/B2 captures is dominated
by machine power/core state; it does **not** justify claiming a total CPU gain.

The quality stage itself fell from 27.381 to 6.281 median-run CPU seconds in
the contemporaneous comparison, and from 33.203 CPU seconds in B2. Prepared
value projection scans fell from five to two, and projected canonical rows fell
from 500,000 to 200,000. The remaining scans are staging hashing and Phase-4
normalization hashing.

Logical serialized JSON fell from 84,567,540 B2 characters to 38,589,740
characters, a further **54.37%** reduction and an **87.49%** reduction from B0.
All three runs retained the exact staging, quality, and normalization hashes:

- staging `sha256:30981b…33c0`
- quality `sha256:d56b0e…3d71`
- normalization `sha256:be8ea3…3684`

Parity coverage includes direct single- and multi-dataset default rows,
identity collisions and sparse preparation issues, logical quality reload,
17-row review pagination, quarantine filtering, accounting order, exact hashes,
and normalization consumption without a complete Python eligible-ID set. The
forward schema version is 4; prior versions upgrade without rewriting existing
logical row JSON.

## B4 — construct normalization once

Captured on 2026-08-12 with a new effect-heavy fixture: 4,000 Products rows,
20 mapped fields, and trim changes on 19 non-identity fields. It produces
76,000 normalization effects and 19 groups. The benchmark can execute the
pre-Phase-4 replay route as an explicit same-code control, which keeps all
unrelated schema and source-workspace changes identical between measurements.

The direct route now constructs and canonical-encodes each effect once into a
bounded transaction-local fact relation. DuckDB deduplicates effects and
derives eligibility, changed rows, distinct rules/groups/sources, group counts,
and bounded examples set-wise. One set-based statement promotes the exact
encoded facts into the immutable normalization run. Publication hashes those
stored bytes and does not reconstruct or re-serialize effect objects. A small
per-group seed ledger preserves group language and detects inconsistent group
metadata without repeating it per effect.

The preparation session UUID is also the normalization run UUID. That lets the
fact stream be promoted once without copying it into a second persistent
ledger. If publication fails or discovers an identical current run, session
cleanup removes the orphan facts; successful runs retain them. Repeat
preparation against reused staging/quality runs binds eligibility by the
current quality content hash and stable row IDs, preserving re-transform and
re-export behavior.

| Metric | Replay control | B4 durable | Gain |
| --- | ---: | ---: | ---: |
| Effect objects constructed | 152,000 | 76,000 | **50.00%** |
| CPU | 12.914 s | 12.502 s | **3.19%** |
| User CPU | 10.869 s | 10.408 s | **4.24%** |
| Wall time | 12.251 s | 11.804 s | **3.65%** |
| Peak RSS | 505.031 MiB | 439.203 MiB | **13.03%** (65.828 MiB) |
| Ending RSS | 504.969 MiB | 434.313 MiB | **13.99%** |
| DuckDB file | 67.012 MiB | 66.262 MiB | **1.12%** |
| DuckDB used pages | 53.500 MiB | 51.250 MiB | **4.21%** |
| Total project storage | 68.627 MiB | 67.877 MiB | **1.09%** |

All six fresh processes used the byte-identical 1,548,277-byte CSV and emitted
identical counts, logical payload size (29,174,522 characters), and hashes:

- staging `sha256:840bfd…577b`
- quality `sha256:0e2451…1f0c`
- normalization `sha256:1d527d…3952`

The durable route reports one effect construction per persisted effect and no
complete Python effect-ID or changed-row-ID set. The remaining ordered effect
scan is the bounded governance hash reader over already encoded facts; there is
no second logical construction pass. Compatibility reload orders by effect ID,
so the internal construction ordinal does not change the public contract.
The exact eligible-dataset hash still performs the second prepared-value
projection scan; changing that hash root is explicitly a Phase 6 ADR decision,
not part of the Phase 4 effect-construction contract.

Evidence:

- `.tmp/transformation-scale-phase4-effect-heavy-replay-control-4k-final.json`
- `.tmp/transformation-scale-phase4-effect-heavy-durable-4k-final.json`

## B5 — set-based product/BOM relationships

Captured on 2026-08-12 with the immediate reference shape: 16,000 Products,
80,000 BOM lines, and one incoming product reference per line. The paired
control uses the existing complete materialized relationship evaluator. The
candidate persists normalized relationship edges while rows are ingested,
classifies them with one set-based parent join, and performs unsafe-parent
propagation with one recursive DuckDB relation. Both routes use the same
canonical rows and execute in separate fresh processes.

The timed interval includes bounded canonical ingestion, relationship fact
construction, finalization, and quality. Project setup is excluded. All six
processes emitted the same 96,000-row staging hash and the same quality summary:
96,000 ready rows, zero blocked rows, zero issues, and zero quarantine entries.
The candidate classified all 80,000 edges as `RESOLVED`.

| Metric | Materialized control | B5 set-based | Gain |
| --- | ---: | ---: | ---: |
| CPU | 27.777 s | 30.784 s | **-10.82%** |
| Wall time | 27.988 s | 31.093 s | **-11.09%** |
| Peak RSS | 796.281 MiB | 432.766 MiB | **45.65%** (363.516 MiB) |
| Ending RSS | 749.266 MiB | 321.422 MiB | **57.10%** |
| DuckDB file | 141.512 MiB | 169.512 MiB | **-19.79%** (+28.000 MiB) |
| DuckDB used pages | 96.750 MiB | 143.250 MiB | **-48.06%** (+46.500 MiB) |

This is an intentional memory-for-durable-evidence trade-off, not a claim that
every metric improved. The 363.516 MiB median peak reduction creates the needed
headroom below the 900 MiB worker gate. The 3.006 CPU-second regression and
46.500 MiB of used pages buy explicit, auditable `UNIQUE`, `MISSING`,
`DUPLICATE`, `RESOLVED`, `AMBIGUOUS`, and `UNSAFE_PARENT` states plus bounded
dependency propagation. Phase 6 may benchmark the edge transport and physical
encoding, but must not remove those semantics merely to recover space.

Bounded parity tests cover unique, missing, duplicate/ambiguous, fan-out, a
three-level unsafe chain, and a safe cycle. The exact materialized quality JSON
is preserved for blocking and warning relationship policies. Relationship-key
normalization is compiled into the same Polars program as scalar and identity
work, then reused to construct the incoming reference; quality invokes the
set-based relationship pass once regardless of edge count. Its regression test
locks that pass to nine fixed SQL statements for populated initial-unsafe and
propagation inputs; edge count cannot add a query. No Odoo call occurs in
transformation or relationship resolution.

This completes the direct multi-dataset product/BOM slice of Phase 5. It does
not raise the limit for `DerivedEntityPlan`, related source splits, structural
joins, unions, or group aggregates where output cardinality or lineage changes.
Those cases still fail high-volume admission into the existing materialized
route. Their hybrid value-artifact and multi-source-lineage work remains a
separate Phase 5 continuation rather than being misreported as supported.

The stable row IDs, canonical references, lineage bindings, and staging hash
remain unchanged, so a later Impodo transformation and repeat Odoo export can
still match and update the same rows. Odoo comparison and writing remain later
stages and are not part of this benchmark.

Evidence:

- `.tmp/transformation-scale-phase5-product-bom-96k-final.json`

### Cross-thread compatibility and validation

The concurrent Odoo-source refactor was audited before and after B5. Its
current-only project/source contracts do not reintroduce a materialized
relationship path or weaken frozen source/prepared-snapshot verification. B5
therefore starts a new exact DuckDB schema generation rather than assuming an
upgrade from databases created by the earlier generation. This follows that
refactor's fail-closed current-schema policy.

The final combined worktree passes 555 discovered unit/integration tests; 13
existing live or scale probes remain opt-in and were skipped. The Phase-5
Python files pass Ruff, `git diff --check` passes, relationship parity passes
across Polars batch sizes, and the three-pair 96,000-row benchmark passes its
staging-hash and semantic-summary equality guards.

## B6 — relationship storage and transport

Captured on 2026-08-12 against the same 16,000-Product/80,000-BOM fixture and
paired fresh-process control as B5. The Phase-5 edge schema had two ART indexes
covering parent hashes, resolved row hashes, child row hashes, and states. No
production relationship query performs a selective point lookup: resolution
and propagation intentionally consume the whole pending session with hash joins
and one recursive relation. Maintaining those indexes therefore paid CPU and
storage for the wrong access pattern.

Phase 6 removes both indexes and stores immutable child and resolved-parent
ordinals instead of repeating their 71-character canonical row IDs, child
dataset, and child source row on every edge. Audit-facing row IDs and coordinates
remain exactly derivable by joining the edge to the immutable canonical row
index. The edge still stores the target field, parent dataset, normalized key,
parent identity hash, match count, and explicit match/resolution states. Tests
assert that no relationship index is recreated and that the populated
relationship pass remains exactly nine SQL statements.

Because this changes the exact current physical schema after the concurrent
source refactor had introduced `current-s6`, the combined project generation is
`current-s7`. Older generations fail closed and are not implicitly upgraded.

| Metric | Materialized control | B6 set-based | Gain |
| --- | ---: | ---: | ---: |
| CPU | 29.690 s | 22.926 s | **22.78%** |
| Wall time | 29.901 s | 23.244 s | **22.26%** |
| Peak RSS | 806.297 MiB | 446.328 MiB | **44.64%** (359.969 MiB) |
| Ending RSS | 755.266 MiB | 439.266 MiB | **41.84%** |
| DuckDB file | 143.262 MiB | 146.512 MiB | -2.27% (+3.250 MiB) |
| DuckDB used pages | 97.250 MiB | 102.750 MiB | -5.66% (+5.500 MiB) |

Relative to the B5 set-based median, B6 reduces the DuckDB file by 23.000 MiB
and used pages by 40.500 MiB. The durable relationship overhead relative to its
paired materialized control falls from 46.500 to 5.500 MiB, an **88.17%**
reduction. CPU also improves by 7.858 seconds relative to B5, although that
cross-capture comparison remains secondary to the paired B6 result. Peak RSS
remains in the same 433–446 MiB band and far below the 900 MiB gate.

### Rejected transport and hash changes

The integrated 80,000-edge transport spike includes value construction and
insertion in 5,000-row batches:

| Transport | Median | Decision |
| --- | ---: | --- |
| Current bounded typed JSON | 0.277 s | Retain |
| DuckDB column arrays | 0.258 s | Reject: only 0.019 s faster |
| Parameterized `executemany` | 8.684 s | Reject |
| Polars/Arrow registration | 0.133 s | Reject dependency: saves only 0.144 s |

Arrow required an otherwise absent 34.2 MiB `pyarrow` package for this isolated
probe. It is not added to Impodo for a sub-150-millisecond microbenchmark gain
with no demonstrated end-to-end CPU/RSS benefit. A separate 96,000-row
canonical transport probe found column arrays 0.388 seconds faster than typed
JSON; the 80,000-impact difference was 0.029 seconds. Neither is material
against a 23-second integrated run, and changing them would enlarge the memory
and parity surface.

No hash-root ADR is introduced. Exact logical staging/quality hashes and
artifact-byte verification are no longer a limiting cost in the integrated
profile, so replacing them with ordered chunk roots would add compatibility and
forensic complexity without a measured need. No row signature or weaker
artifact verification was introduced.

All B6 runs retain the exact B5 staging hash
`sha256:4836b65a…b6b`, 96,000 ready rows, zero issues/quarantine, and 80,000
`RESOLVED` edges. Stable canonical row IDs and references remain unchanged for
later re-transformation and repeat Odoo export/update.

Evidence:

- `.tmp/transformation-scale-phase6-narrow-edge-96k-final.json`
- `.tmp/transformation-scale-phase6-relationship-transport-final.json`

## B7 — first/repeat worker qualification

Phase 7 now has one portable qualification command rather than a manual list of
probes. Every workload attempt creates a fresh outer process and then performs
first and repeat preparation in separate production worker processes. The
harness records worker CPU, wall time, peak working set, parent memory delta,
DuckDB file/used pages, total project storage, fixture identity, and exact
staging/quality/normalization hashes. It rejects a missing worker exit, a
changed hash, a changed fixture/runtime, source reopening, or prepared-snapshot
replacement.

The related fixture is an actual two-source Impodo project, not only the B5/B6
repository benchmark. It registers 16,000 Products and 80,000 BOM lines as two
immutable CSV sources, maps a required BOM `product_id` through a dataset
resolver, and runs the normal background preparation service. Before the
repeat attempt, the harness deletes both registered CSV artifacts. Repeat can
therefore succeed only through the two immutable prepared snapshots. Their
identities and modification timestamps must remain unchanged, and the staging,
quality, and normalization hashes must match the first attempt. This directly
protects later re-transformation and repeat Odoo export/update behavior.

The release matrix contains:

- 100,000 direct Products rows, with the 750 MiB design target;
- the 1,000-row/150-column customer structural twin, with the 500 MiB gate;
- 16,000 Products plus 80,000 related BOM lines;
- a 4,000-row/19-effect-per-row fixture;
- its dirty/duplicate-identity counterpart; and
- the paired materialized/set-based relationship semantic oracle.

The 100,000-row mixed/derived route is deliberately not presented as a raised
capability. Derived/grouped cardinality-changing plans remain materialized and
capped; Phase 7 qualifies only the direct and direct-related capabilities that
Phases 2–6 made bounded.

### Mac harness smoke

The Mac smoke profile passed all 27 executable performance/reclamation gates.
Each workload completed first and repeat preparation in about 1.2–1.5 seconds,
worker peaks were 228–254 MiB, and parent repeat deltas were 0.28–0.67 MiB.
The related smoke case prepared 100 Products plus 500 BOM lines, reused both
prepared snapshots after both sources were deleted, and retained identical
staging, quality, and normalization hashes. The separate relationship oracle
also retained exact staging/quality semantics.

Evidence:

- `.tmp/transformation-scale-phase7-mac-smoke-final.json`
- `.tmp/transformation-scale-phase7-mac-smoke-final/`

This proves the harness and production repeat path on macOS; it is not release
qualification. The evidence truthfully reports `release_qualified: false`
because it is a one-run smoke profile on a dirty non-Windows worktree.

The qualification harness now also fingerprints the complete Git worktree,
including tracked patches and the bytes of non-ignored untracked files. The
outer matrix and every multi-run worker benchmark verify that fingerprint
before and after each child run. Evidence is rejected as mixed-build evidence
if another task changes the worktree while a long qualification is running.
Each release scenario must also prove the requested fresh-run count and
non-zero CPU/peak samples, preventing an incomplete run or unavailable Windows
sampler from appearing to pass.

A guarded Mac smoke on 2026-08-12 passed all 54 executable gates and recorded
one stable fingerprint across the outer report and all worker reports. An
earlier full release-shape rehearsal was deliberately discarded: another task
advanced the project schema generation between the first and repeat workers,
so the repeat worker correctly rejected the mixed-build project. That event is
not counted as a product correctness or performance result.

Additional guarded evidence:

- `.tmp/transformation-scale-phase7-mac-smoke-stability-guard.json`
- `.tmp/transformation-scale-phase7-mac-smoke-stability-guard/`

A one-run Mac customer-baseline compatibility probe also confirmed that the
current production-worker fixture is byte-identical to B0-C: 1,843,410 bytes
and `sha256:cd28e8...3d8ee`. Its worse first/repeat worker peak was 252.531 MiB
against the B0-C 268.938 MiB process-tree median, a 6.10-percent reduction.
That is safely below the absolute 500 MiB gate but does **not** meet the plan's
30-percent relative-improvement gate. This dirty one-run Mac result is
diagnostic, not the Windows decision; it makes the previously unenforced gate
visible instead of allowing an incomplete release qualification.

Evidence:

- `.tmp/transformation-scale-phase7-mac-customer-baseline-compatibility.json`

### Mac release-shape diagnostics

The first full production-worker Product/BOM attempt found a gap hidden by the
B5/B6 repository benchmark. The 96,000-row project produces 96,000 trim effects
in addition to its 80,000 relationship edges. The preparation-session DuckDB
connection was capped at 96 MB and failed in normalization at 91.2 MiB used.
The first failure was during encoded-effect insertion; after that was isolated,
the original full-window query for only five examples per group hit the same
ceiling later.

The retained fix is deliberately small:

- replace the `ROW_NUMBER()` window over every eligible effect with one
  deterministic top-five `arg_min` aggregate per compact group, then parse only
  the selected effect JSON rows;
- retain the faster transaction-local effect relation and one bulk promotion
  into the immutable run, rather than maintaining every final index per batch;
  and
- set the one-thread preparation-session buffer limit to 192 MB. Both 96 MB
  and 128 MB failed the release-shape transaction; 192 MB is the first retained
  candidate and remains far below the 900 MiB process gate.

The final one-run Mac diagnostics passed both first and repeat workers:

| Fixture/attempt | CPU | Wall | Worker peak | DuckDB used | DuckDB file | Project storage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Products 100k first | 41.390 s | 40.790 s | 630.063 MiB | 79.500 MiB | 140.512 MiB | 176.250 MiB |
| Products 100k repeat | 43.547 s | 42.838 s | 628.844 MiB | 85.500 MiB | 186.012 MiB | 188.276 MiB |
| Products 16k + BOM 80k first | 40.117 s | 39.654 s | 672.172 MiB | 142.250 MiB | 230.262 MiB | 241.181 MiB |
| Products 16k + BOM 80k repeat | 42.582 s | 42.238 s | 634.625 MiB | 171.250 MiB | 371.012 MiB | 372.455 MiB |

The direct fixture retains more than 119 MiB of headroom against its 750 MiB
design target and more than 269 MiB against the hard worker gate. The related
fixture retains more than 227 MiB against the hard gate. Parent RSS after both
repeat attempts was below its pre-job value. CPU and wall remain well below
120 seconds.

The related repeat database file is larger than its used-page count because
DuckDB retains allocated/free blocks and Impodo retains immutable run history.
The release report therefore records file bytes, used/free pages, and total
project storage separately; it does not describe the 371 MiB allocated file as
371 MiB of live logical evidence. Repeat added 29.0 MiB of used pages while
retaining the prior audit run.

Both fixtures deleted their registered source artifact(s) before repeat,
reused unchanged prepared snapshots, emitted identical first/repeat staging,
quality, and normalization hashes, exited each worker, and made no Odoo call.

Evidence:

- `.tmp/transformation-scale-phase7-mac-products-100k-worker.json`
- `.tmp/transformation-scale-phase7-mac-product-bom-96k-worker.json`

These are dirty-worktree, one-run Mac diagnostics. They validate the final
shape before Windows but do not satisfy the clean three-run Windows gate.

### Phase 7 legacy/dead-code closeout

The implementation closeout removes the superseded preparation and benchmark
routes instead of carrying compatibility code during development:

- deleted the pre-direct-session `begin_session` / provisional-row /
  `finalize_session` API, codecs, repository branches, tests, and six obsolete
  DuckDB tables/indexes;
- renamed the remaining live session count from `provisional_row_count` to
  `staged_row_count`, and advanced the development schema generation to `s9`
  so older development databases fail closed rather than silently retaining
  the deleted layout;
- deleted the pre-Phase-4 in-memory normalization replay and alternate
  publication transport; bounded normalization now requires the durable
  construct-once ledger;
- removed the replay-control switch from the current benchmark harness; and
- deleted the completed DuckDB and relationship transport experiment scripts
  and their benchmark-only tests. Their conclusions and retained evidence stay
  recorded in this report.

The supported 50,000-row Python semantic route and 25,000-row
derived/materialized route remain intentionally active. The materialized side
of `benchmark_relationships.py` also remains because the release qualification
uses it as the semantic oracle for the set-based relationship path. None of
these are compatibility fallbacks for the deleted preparation-session design.

The legacy/dead-code portion of Phase 7 closeout is complete. Phase 7 itself is
not yet closed: the Phase-1 checkpoint still reports per-row canonical metadata
adaptation on the native route, while the architecture requires zero Python
row/cell callbacks and no full prepared/canonical objects in the high-volume
data plane. The qualification runner now fails closed when the 100,000-row
direct or 96,000-row related worker evidence lacks the complete observed
vectorization report; a Windows run can no longer authorize a limit change on
CPU/RSS results alone.

Route limits and operator messages remain unchanged until that execution path
and evidence gate are complete and the clean three-run Windows qualification
passes every correctness, CPU, memory, storage, vectorization, repeatability,
and customer-improvement gate below.

### Windows release command

The 30-percent customer-improvement gate needs a same-machine Phase-0 control;
the Mac artifact cannot qualify Windows. Capture the clean Phase-0 control in
an isolated worktree first. This deterministic fixture contains no customer or
Odoo data:

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
git worktree add ..\dev-impodo-scale-baseline `
  6c9f16432530269b180e6e9e08be96b1c44dc944
.\.venv\Scripts\python.exe `
  ..\dev-impodo-scale-baseline\scripts\benchmark_preparation.py `
  --runs 3 `
  --workload customers `
  --rows 1000 `
  --columns 150 `
  --mapped-fields 20 `
  --timeout-seconds 900 `
  --output "$PWD\.tmp\transformation-scale-phase0-windows-customer.json"
```

Revision `6c9f164...` is the clean commit containing the Phase-0 customer
fixture/instrumentation used by B0-C. The qualification runner rejects a
different revision, a dirty baseline worktree, fewer than three runs, changed
fixture bytes, or a different platform/Python/native-runtime set. It compares
the candidate's worse first/repeat worker peak with the baseline median and
requires at least 30 percent improvement.

Then, on a clean combined revision in PowerShell, run:

```powershell
.\.venv\Scripts\python.exe scripts\qualify_transformation_scale.py `
  --profile release `
  --require-release-qualified `
  --customer-baseline .tmp\transformation-scale-phase0-windows-customer.json `
  --timeout-per-scenario 3600 `
  --output .tmp\transformation-scale-phase7-windows-release.json `
  --evidence-dir .tmp\transformation-scale-phase7-windows-release
```

If the combined worktree cannot yet be clean, add
`--allow-dirty-worktree` only for a diagnostic release-shape run and omit
`--require-release-qualified`. Such a run can expose Windows failures but can
never authorize a route-limit change. Limits and browser/operations messages
remain unchanged until the clean Windows report says
`release_qualified: true`.
