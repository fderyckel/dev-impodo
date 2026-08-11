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
| Phase 1: remove post-Polars replay | Not started | Pending B1 comparison |
| Phase 2: prepared values plus narrow index | Not started | Pending B2 comparison |
| Phase 3: sparse multi-dataset quality | Not started | Pending B3 comparison |
| Phase 4: construct normalization once | Not started | Pending B4 comparison |
| Phase 5: set-based product/BOM relationships | Not started | Pending B5 comparison |
| Phase 6: conditional transport/hash optimization | Not started | Only if measurements justify it |
| Phase 7: qualification and limit decision | Not started | Three fresh Windows attempts per fixture |

The 16,000-product/80,000-BOM shape remains capped at 25,000 rows until Phase 3
and Phase 5 remove the materializing quality and relationship routes. This is a
truthful capacity limit, not a regression in the intended final capability.
