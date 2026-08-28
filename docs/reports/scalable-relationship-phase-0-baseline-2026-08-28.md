---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 0 baseline — 2026-08-28

## Decision supported by this report

This report gives developers a reproducible baseline for adding row-level
dependency scheduling without taking relationship choices away from the data
manager. It records current behavior before the scheduler changes. It does not
claim that the future row planner or Production loading is available.

The accepted design keeps the schedule inside the current `ExecutionSnapshot`
unless later measurements prove that a separate large-artifact boundary is
necessary. The data manager continues to choose datasets, rows, business keys,
and optional field mappings. Impodo will calculate only the safe execution
order and cycle completion work.

## Evidence boundary

The measurements used repository revision
`a3b96cc26ad9340dc78a45eb9c2f347e7f372e01` plus the uncommitted Phase 0
documentation, benchmark, and fixture slice. Python 3.12.13 ran on
macOS 26.5.1 arm64. Every execution shape ran three times in a fresh process.

The execution harness uses the current `ExecutionService`, a memory journal,
and a recording remote-writer contract. It does not contact Odoo. The load
times therefore measure Impodo's current orchestration and value construction,
not network or Odoo transaction time. Phase 6 will require a disposable Odoo
19 target for transport, permission, schema, and read-back qualification.

Raw local evidence was written to:

- `.tmp/scalable-relationship-phase0-mac.json`;
- `.tmp/scalable-relationship-phase0-preparation-mac.json`.

The `.tmp` files are diagnostic evidence and are not committed. The accepted
semantic counts and median measurements are recorded below and protected by
focused tests.

## Accepted dependency fixtures

| Shape | Business purpose | Reviewed source order | Current execution order |
| --- | --- | --- | --- |
| Product and unit | Twelve Products share two units of measure. | Products, then units | Units, then Products |
| Same-dataset hierarchy | Eight categories form a child-first parent chain. The parent field is optional in the current fixture. | Categories in child-first row order | One category dataset; current execution retains row order and patches seven parent fields |
| Optional cycle | Two new records refer to each other through optional fields. | First node, then second node | First node, then second node, followed by one relationship patch |
| Product and BOM | Two units support 100 Products. Twenty-five BOM headers refer to those Products, and 500 component lines refer to both a BOM and a Product. | Component lines, BOM headers, Products, then units | Units, Products, BOM headers, then component lines |

The Product and BOM fixture is generic. It freezes relationship behavior, not
a universal Odoo BOM schema. Later Odoo 19 qualification must still derive
writable and required fields from the captured target schema.

## Current execution measurements

The harness used ten rows per create batch. Times are three-run medians. Peak
increment covers fixture construction, snapshot serialization and validation,
preview calculation, and execution in the fresh child process.

| Shape | Rows | Relationship edges | Dataset planning | Preview | Load | Odoo calls | Relationship patches | Snapshot size | Peak increment |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Product and unit | 14 | 12 | 0.029 ms | 0.876 ms | 2.476 ms | 3 | 0 | 13.7 KiB | 0.19 MiB |
| Same-dataset hierarchy | 8 | 7 | 0.025 ms | 0.597 ms | 1.818 ms | 8 | 7 | 9.1 KiB | 0.13 MiB |
| Optional cycle | 2 | 2 | 0.026 ms | 0.297 ms | 0.994 ms | 3 | 1 | 4.1 KiB | 0.13 MiB |
| Product and BOM | 627 | 1,125 | 0.056 ms | 42.276 ms | 117.187 ms | 64 | 0 | 670.3 KiB | 8.13 MiB |

Every run completed all rows and produced the same snapshot hash, dataset
order, connector-call sequence hash, journal-call counts, and final status as
the other runs for that shape.

The dataset-planning measurement covers only the current compact dataset graph.
It is intentionally not presented as a row-planning result. Phase 2 must
measure the new row graph against the same fixtures.

## Current behavior made visible

The Product and BOM shape already loads acyclic cross-dataset dependencies in
business order. With a ten-row batch, its 64 calls are one unit create, ten
Product creates, three BOM-header creates, and fifty component-line creates.
It performs no relationship-completion update.

The same-dataset hierarchy exposes the gap that Phase 2 must close. The current
dataset graph cannot move a parent row before its child. It creates the eight
rows once and then sends seven single-record relationship patches. The future
row schedule must produce parent-first creates and zero patches for this
acyclic hierarchy.

The optional cycle provides the opposite safeguard. One relationship patch is
necessary for the current two-node order because neither new target exists at
the beginning. The future scheduler must retain an exact deterministic
completion list rather than trying to remove all second-pass behavior.

## Relationship-preparation smoke

The repaired preparation benchmark ran the existing 100-Product and 500-line
relationship shape three times. The set-based path resolved all 500 edges and
produced the same staging hash and quality summary as the materialized control.
Its median wall time was 0.650 seconds, median peak RSS was 192.3 MiB, and
median peak increment was 37.4 MiB. These small-fixture measurements confirm
that the harness works; they are not a scale qualification or an argument to
raise the current related-data limit.

## Reproduction

```console
PYTHONPATH=src .venv/bin/python scripts/benchmark_dependency_execution.py \
  --runs 3 \
  --output .tmp/scalable-relationship-phase0-mac.json

PYTHONPATH=src .venv/bin/python scripts/benchmark_relationships.py \
  --products 100 \
  --bom-lines 500 \
  --runs 3 \
  --batch-size 100 \
  --output .tmp/scalable-relationship-phase0-preparation-mac.json
```

## Phase 0 conclusion

The snapshot-owned design remains proportionate for the accepted fixtures. A
627-row Product and BOM snapshot uses about 670 KiB, so Phase 0 provides no
evidence that Impodo needs a separate relationship-plan aggregate or Parquet
artifact family now. This conclusion does not extrapolate to the later
16,000-Product and 80,000-line qualification shape.

Phase 1 can proceed with one canonical dependency extractor. Phase 2 must
compare row-planning time, peak memory, snapshot size, execution order, and
relationship patches with this baseline. If those measurements expose an
unsafe storage or memory boundary, the team can revise the representation
without changing the data manager's relationship choices.

## Related documentation

- [Accepted scalable relationship dependency plan](../plans/scalable-relationship-dependency-planning.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Load into Odoo implementation](../developer/workflow/06-load-into-odoo.md)
- [Acceptance and test strategy](../testing/acceptance.md)
