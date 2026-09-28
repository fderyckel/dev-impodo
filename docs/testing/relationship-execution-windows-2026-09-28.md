---
audience: developer
kind: report
status: current
---

# Windows relationship execution qualification

## Result

The Windows Product and Bill of Materials qualification passed on 2026-09-28
at revision `dc44b130fec328ef1291e32b60a492d440968e86`. The run used an isolated,
clean clone on Windows 11 with Python 3.14.7. It did not stop or connect to the
operator's running Impodo process.

The current 25,000-row boundary passed its execution and preparation budgets.
The focused correctness gate also passed. The authenticated eligible-field
screenshot was captured from an isolated loopback server with fictional
Product data.

This result closes the Windows-specific measurements and visual evidence. It
does not yet retire the relationship plan because the required repository-wide
clean test discovery was not green. The diagnostic run found failures in
separate target-match, preparation, scenario-fixture, browser-copy, and Polars
timezone paths, then stalled in a Production-readiness browser test. The
relationship-owned tests listed below passed before that diagnostic run.

## Three fresh execution runs

The fixture contained 4,000 Products, 998 Bills of Materials, 20,000 component
lines, and two units. It scheduled 25,000 rows and 44,998 relationship edges.
Every run completed all rows in the order `uoms`, `products`, `boms`, then
`bom_lines`. No deferred relationship write was required.

| Run | Fixture build | Load | Peak RSS | Snapshot | Connector calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 8.090 s | 1.548 s | 448.516 MiB | 36,713,978 bytes | 501 |
| 2 | 8.561 s | 1.603 s | 448.344 MiB | 36,713,978 bytes | 501 |
| 3 | 8.193 s | 2.137 s | 448.613 MiB | 36,713,978 bytes | 501 |

All three runs produced these exact stable values:

- The snapshot semantic hash was
  `sha256:546ec4a895472ab9067a3764e87b177a5809cfcbfe5a7a66d7184901567bd1cb`.
- The connector call-sequence hash was
  `sha256:f8302c2ae62f0fe99d40b593694f9b0d508dec4fc464d665ead711ac123405d2`.
- The call classes were 501 bounded `load_create` calls and zero `create`,
  lookup, update, or relationship-patch calls.

## Three fresh production-worker pairs

Each fresh outer process prepared 5,000 Products and 20,000 Bill of Materials
lines, deleted the registered source, then repeated preparation from the
immutable prepared snapshot.

| Run | First wall | First peak | Repeat wall | Repeat peak | First project | Repeat project |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 17.174 s | 337.156 MiB | 17.444 s | 326.062 MiB | 112,396,533 bytes | 138,610,933 bytes |
| 2 | 16.943 s | 331.004 MiB | 17.710 s | 321.074 MiB | 112,920,821 bytes | 138,610,933 bytes |
| 3 | 16.378 s | 337.195 MiB | 17.589 s | 325.922 MiB | 113,969,397 bytes | 140,970,229 bytes |

Every pair reused the prepared snapshot, reopened no source, and exited both
workers. The fixture was 2,595,207 bytes and retained one exact SHA-256 hash.
The staging, normalization, and quality hashes were also identical across all
three pairs. The worst wall time was 17.710 seconds against the 120-second
budget. The worst worker peak was 337.195 MiB against the 900-MiB budget.

## Determinism and connector bounds

The same 25,000-row fixture was executed with transport batch sizes 25, 50,
and 200. Every variant retained the same snapshot semantic hash, row count,
edge count, dataset schedule, snapshot size, and completed status. The
transport-dependent call sequences changed as expected: 1,001, 501, and 151
bounded calls respectively.

The domain permutation checks proved that reversing row inputs does not change
the row schedule and that dataset input permutations do not change compiled or
preflight dependency edges. The required-cycle diagnostic also remained
deterministic under dataset permutations.

## Recovery and exact read-back

Nineteen focused tests passed in 0.343 seconds. Together they proved:

- An interruption after journalling but before transport resumes only after
  exact read-back classifies the in-flight create.
- A transport interruption resumes with the same key and External IDs.
- An interruption after generated-receipt transport rereads the generated
  identifier and does not recreate the source Product.
- Optional cycles use create followed by an exact relationship patch, while a
  required create-time cycle blocks before target I/O.
- Existing-target relationships use the exact database identifier, crosswalk
  identities resolve in bounded pages, and retargeting fails before the
  journal.
- A parent receipt is journalled before its dependent write.
- Generated identifiers are read back with exact field and identifier scopes.
- Final reconciliation verifies relationship identifiers and links to an
  existing target record.

The focused gate covered
`RelationshipDependencyTests`, `ExecutionSnapshotTests`,
`DependencyExecutionBaselineTests`, `ExecutionServiceTests`,
`Json2WriteExecutorTests`, and `ReconciliationServiceTests`.

## Authenticated screenshot

The [eligible Product fields screenshot](../images/user/08g-odoo-source-eligible-fields.png)
was captured from the authenticated current interface at 1440 by 1024 CSS
pixels with device scale factor 1. The capture used a temporary Microsoft Edge
process, an ephemeral loopback port, and fictional Product schema evidence.
It made no external Odoo request.

The reproducible helper is
[`capture_odoo_source_eligible_fields.py`](../../scripts/capture_odoo_source_eligible_fields.py).

## Retained local artifacts

The non-secret JSON artifacts remain under `.tmp` in the working checkout:

| Artifact | File size | SHA-256 |
| --- | ---: | --- |
| `scalable-relationship-phase6-execution-25k-windows.json` | 6,009 bytes | `de4ae651f80a977ecc417805d7c4082e4153c5352ecab1bdb381e564463b3330` |
| `scalable-relationship-phase6-worker-25k-windows.json` | 9,529 bytes | `85f63dca44fe5f27c177c054eaebce03c15035902c44afedc49c9b6ec5344933` |
| `scalable-relationship-phase6-execution-25k-batch25-windows.json` | 2,831 bytes | `ea12010afbf6ceda8f5b898dec18c5e6e94060ecf8e55f21660fa12d1ea33715` |
| `scalable-relationship-phase6-execution-25k-batch200-windows.json` | 2,830 bytes | `8a38d65af8bce12a9e35c4ec9baa240931caec352f98f18bcfce3ddc1ac957f0` |

The raw artifacts are intentionally ignored by Git. This report retains the
portable measurements and hashes needed to review the result.

## Remaining retirement gate

Run the complete discovery suite from a clean revision after the independent
baseline failures are resolved. Then run the architecture, documentation, and
owner checks and remove the relationship qualification plan if they pass. A
future increase beyond 25,000 scheduled records remains a separate track.
