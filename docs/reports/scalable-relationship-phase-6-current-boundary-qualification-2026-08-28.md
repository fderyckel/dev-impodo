---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 6 current-boundary qualification — 2026-08-28

## Decision supported by this report

This report tests whether the generic relationship planner remains practical
at a conservative current limit while preserving the data manager's freedom
to choose datasets, rows, business keys, and optional relationships. The
product owner selected no more than 25,000 scheduled records for this gate.
This report does not raise the current direct, relationship, derived, or
materialized preparation limits.

The current snapshot-owned representation passes the 25,000-row macOS scale
gate. It does not pass the earlier 100,000-row aspiration. A live Odoo 19 test
also found one real Product/BOM dependency that the simplified fixtures did
not represent. Impodo now freezes and journals that generated Product-variant
binding through a schema-approved generic projection. Phase 6 remains in
progress until the accepted gate runs from a clean revision on Windows and the
changed Match data decision point receives refreshed browser evidence.

## Evidence boundary

Python 3.12.13 ran on macOS 26.5.1 arm64. The retained measurements identify
repository revision `f7139f6435fbc4536abad5e51a3aff5da51a3eea` plus the
uncommitted Phase 6 harness and documentation changes. Other correction work
was also present in the shared worktree. The evidence is diagnostic and
reproducible, but the dirty worktree prevents release qualification.

The Match data relationship catalogue now exposes one optional generated-link
selector when a different selected table targets a model with a captured,
read-only many2one projection to the relationship model. Focused form and
browser workflow tests passed, but screenshots were not refreshed from this
dirty shared worktree. The full suite was not used as release evidence; a
clean revision, refreshed browser evidence, and the Windows run remain
explicit gates below.

The raw non-secret results are local diagnostic artifacts:

- `.tmp/scalable-relationship-phase6-execution-25k-mac-v7.json`;
- `.tmp/scalable-relationship-phase6-worker-25k-mac-v13.json`;
- `.tmp/scalable-relationship-phase6-execution-mac-diagnostic.json`.

No API key, target URL, user identity, Product value, or Odoo record identifier
is stored in those files or this report. The remote probes read the API key
without terminal echo and deleted every record they created.

## Current-limit decision

Impodo currently distinguishes three preparation boundaries:

- A native-columnar, single-dataset direct mapping can admit as many as
  100,000 physical rows.
- A direct Python-fallback or direct relationship route can admit as many as
  50,000 physical rows.
- A derived or materialized route can admit as many as 25,000 physical rows.

Phase 6 uses the last value as its conservative common boundary. The execution
fixture contains 4,000 Product rows, 998 BOM headers, 20,000 BOM component
lines, and two unit rows. These add up to exactly 25,000 scheduled records.
The production preparation fixture contains 5,000 Products and 20,000 related
BOM lines because that existing worker fixture has two direct source datasets.

## macOS execution evidence

Each of three fresh child processes built, serialized, restored, previewed,
and executed the exact 25,000-row snapshot through `ExecutionService` and the
recording Odoo writer contract. The worker used 50 rows per create batch.

| Gate | Result |
| --- | ---: |
| Scheduled rows | 25,000 |
| Relationship edges | 44,998 |
| Snapshot size | 36,713,910 bytes |
| Median fixture construction | 5.249 seconds |
| Median preview | 1.576 seconds |
| Median execution | 4.449 seconds |
| Median peak RSS | 791.734 MiB |
| Median peak increment | 716.469 MiB |
| Create/import calls | 501 |
| Target lookups | 0 |
| Relationship-completion writes | 0 |

All three runs produced the same snapshot semantic hash, call-sequence hash,
dataset order, row count, journal-call counts, final status, and artifact size.
The schedule placed units before Products, Products before BOM headers, and
BOM headers before component lines.

The measurements are close enough to the 900 MiB worker gate that the current
25,000-row cap remains useful. They do not justify a larger embedded snapshot.
A separate 100,002-row diagnostic produced a 146,951,228-byte snapshot and
peaked at 2,847.5 MiB. That negative result confirms that the earlier
100,000-row aspiration must remain deferred.

## Production worker and restart evidence

The project-first performance fixture had three stale assumptions that hid the
current worker path. The harness now constructs `PreparationWorkspace` from
the current Project, Data version, run, and workspace services; resolves
prepared snapshots through the current Project-first artifact layout; and
keeps foundation identities deterministic across fresh benchmark processes.
The BOM fixture also avoids applying a second scalar provider to a target
identity field.

Three fresh outer processes each ran first and repeat preparation in separate
production workers. Before every repeat, the harness deleted both registered
CSV sources. Repeat preparation therefore had to use the exact two immutable
prepared snapshots.

| Gate | Worst or median result |
| --- | ---: |
| Maximum first wall time | 6.354 seconds |
| Maximum repeat wall time | 6.735 seconds |
| Maximum first worker peak | 521.156 MiB |
| Maximum repeat worker peak | 516.813 MiB |
| Maximum parent repeat delta | -0.359 MiB |
| Median first project storage | 103.440 MiB |
| Median repeat project storage | 129.690 MiB |

All workers exited. No repeat reopened a source. Every repeat reused both
prepared snapshots without changing their modification times. All three fresh
runs produced identical staging, quality, and normalization hashes and
reported 100 percent native row coverage with zero Python row or cell
callbacks.

## Live Odoo 19 evidence

The supplied remote demo reports Odoo `19.0+e`. The installed module versions
include Base `19.0.1.3`, Product `19.0.1.2`, Manufacturing `19.0.2.0`, and UoM
`19.0.1.0`. The credential has read, create, write, and delete access to
`product.category`, `uom.uom`, `product.template`, `product.product`,
`mrp.bom`, and `mrp.bom.line`.

The captured schema confirms these required relationships:

- `mrp.bom.product_tmpl_id` requires `product.template`.
- `mrp.bom.product_uom_id` requires `uom.uom`.
- `mrp.bom.line.bom_id` requires `mrp.bom`.
- `mrp.bom.line.product_id` requires `product.product`.
- `mrp.bom.line.product_uom_id` requires `uom.uom`.

A write-capable adapter probe created three Product templates, observed their
three generated variants, created two BOM headers, and created three component
lines. The structure represented A depending on B and C, while B also depended
on C. Six bounded adapter requests created and read back the three model
classes. Exact BOM, Product-variant, quantity, and unit relationships matched.
The probe then deleted its component lines, BOM headers, and Product templates.

A second bounded fixture exercised the wider Manufacturing shape requested by
the product owner. It created two work centers, linked one maintenance-equipment
record to the first work center, and created two BOM operations with explicit
sequences 10 and 20. Two components retained their consuming-operation links,
and one by-product retained its producing-operation link to operation 20. The
fixture then created and confirmed one manufacturing order; Odoo generated two
work orders whose operation order matched the BOM. Eighteen create, exact
read-back, and confirmation requests covered the nine reviewed model classes.
Cleanup cancelled and deleted the order and deleted every BOM, operation,
component, by-product, equipment, work-center, and Product record created by
the fixture.

This wider fixture does not justify a generic arbitrary-method executor.
Confirmation was a qualification-only action used to prove that the imported
BOM configuration generates the expected work orders. Work centers, equipment,
operations, component-operation links, by-products, and operation sequence all
fit the existing authored-row dependency graph.

The final remote probe exercised the implemented production execution path,
not a hand-authored write sequence. `ExecutionService` imported two Product
templates in one batch, read their generated variants in one exact bounded
page, durably recorded the required component-variant receipt, imported the
BOM header, and then imported the component line with the numeric generated
receipt. Exact Odoo read-back proved the Product, BOM, and component link. The
request shape was three native import calls and one generated-receipt read.
The probe deleted both Product templates, the BOM, and its component line.

## Generated target binding design

The real schema exposes a gap in the simplified acceptance fixture. Creating a
`product.template` gives execution a template receipt and causes Odoo to create
a `product.product` variant. A BOM header consumes the template receipt. A BOM
line consumes the generated variant receipt. The previous snapshot and journal
contract could not associate both target models with one incoming Product row.

The implementation adds one narrow generic contract instead of a Product or
BOM branch:

1. The reviewed mapping identifies a captured many2one field on the created
   model that projects the required generated target model.
2. The execution snapshot freezes the source row, projection field, related
   model, and dependent fields in its semantic hash.
3. After the source component has durable create receipts, Impodo reads the
   projection field back by bounded exact ID pages.
4. The journal records each projected receipt before dependent components can
   run.
5. Restart repeats exact read-back for an incomplete projection page. It never
   recreates the source row or guesses the generated identifier.

The captured schema and reviewed mapping remain authoritative. The executor
does not inspect model names, call an arbitrary Odoo method, or add a
Product-specific path. If the generated record is missing, ambiguous,
model-incompatible, or outside the captured company context, execution stops
before the dependent write.

Mapping contract version 13 stores the optional captured projection field.
Execution snapshot version 7 freezes it on the relationship field intent.
Exact read-back pages are capped at 500 created identifiers. The execution
journal accepts projected receipts only while the source row is partially
applied and rejects a changed projection key or Odoo identifier. A crash after
the Product create but before projection read-back therefore resumes by
re-reading the created template; it does not create that template again.

The implementation regression run passed 135 focused mapping, snapshot,
execution, form, and DuckDB-journal tests; 26 integrated Recipe-application
tests; 22 end-to-end Match data browser tests; and 17 focused architecture and
documentation tests. Ruff, Python compilation, documentation quality, and
diff-integrity checks also passed. The shared worktree's full architecture run
still has two unrelated baseline failures: two unreviewed production import
edges from concurrent correction work, and the existing 2,068-line Match data
workflow test module. Neither is used as Phase 6 release evidence.

## Gate disposition

| Phase 6 gate | Status | Evidence or remaining work |
| --- | --- | --- |
| Current-boundary correctness | Passed on macOS | 25,000 rows and 44,998 edges completed in exact dependency order. |
| Determinism | Passed on macOS | Three execution runs and three worker first/repeat pairs retained exact semantic hashes. |
| Request count | Passed for the current fixture | Execution used 501 fixed-size create/import calls and no lookup or completion loop. The production-path live probe used three import calls and one generated-receipt read. |
| Wall time | Passed on macOS | Execution and production worker results remain below the existing 120-second gates. |
| Memory | Passed on macOS | Median execution peak was 791.734 MiB; preparation peaked at 521.156 MiB. |
| Artifact and storage size | Passed on macOS | The execution snapshot is 36.7 MB; worker storage is recorded above. |
| Restart and source reuse | Passed on macOS | Repeat workers reused immutable snapshots after source deletion; a projection interruption re-read the created source without recreating it. |
| Live Odoo 19 schema and read-back | Passed for bounded probes | Multi-level and extended BOM fixtures matched; the production execution path also used and journalled a generated variant. Every probe cleaned up. |
| Generated Product-variant binding | Passed | The mapping, snapshot, bounded writer read, durable receipt, dependency gate, and restart path are generic and verified. |
| Browser evidence | Incomplete | Focused form and workflow tests passed; refresh the Match data screenshot from the clean revision. |
| Clean revision | Not run | The shared worktree contains unrelated correction work and Phase 6 changes. |
| Windows repeat | Not run | A clean Windows host must repeat the accepted current-boundary protocol. |

## Conclusion

The snapshot-owned design remains proportionate at 25,000 rows, so Phase 6
does not need a new relationship-plan aggregate or sidecar artifact for the
current cap. The 100,000-row negative diagnostic confirms that a later limit
increase would need a different representation or a material memory reduction.

Phase 6 is not complete, but the generated-target implementation and its live
read-back and restart gates now pass. The remaining release evidence is the
clean-revision run, refreshed Match data browser evidence, and the Windows
repeat. No Production load or row-limit change is authorized by this report.

## Related documentation

- [Accepted scalable relationship dependency plan](../plans/scalable-relationship-dependency-planning.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Load into Odoo implementation](../developer/workflow/06-load-into-odoo.md)
- [Remote Odoo acceptance runbook](../developer/runbooks/remote-odoo-acceptance.md)
