---
audience: developer
kind: report
status: current
---

# Windows relationship execution qualification

## Result

The Windows Product and Bill of Materials qualification passed on 2026-09-28.
The final retirement repeat used isolated clean candidate revision
`0e601da96fb7d547d22f11852c41f0632eaa910b` on Windows 11 with Python
3.14.7. It did not stop or connect to the operator's running Impodo process.

The current 25,000-row boundary passed its execution and preparation budgets.
The focused correctness gate also passed. The authenticated eligible-field
screenshot was captured from an isolated loopback server with fictional
Product data.

This result closes the Windows-specific measurements, visual evidence, and
clean repository gate. The relationship qualification plan is retired. A
future increase beyond 25,000 scheduled records remains a separate deferred
qualification.

## Three fresh execution runs

The fixture contained 4,000 Products, 998 Bills of Materials, 20,000 component
lines, and two units. It scheduled 25,000 rows and 44,998 relationship edges.
Every run completed all rows in the order `uoms`, `products`, `boms`, then
`bom_lines`. No deferred relationship write was required.

| Run | Fixture build | Load | Peak RSS | Snapshot | Connector calls |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 11.109 s | 1.823 s | 448.723 MiB | 36,713,978 bytes | 501 |
| 2 | 9.996 s | 2.187 s | 447.355 MiB | 36,713,978 bytes | 501 |
| 3 | 10.954 s | 2.112 s | 449.184 MiB | 36,713,978 bytes | 501 |

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
| 1 | 22.289 s | 326.125 MiB | 21.705 s | 326.449 MiB | 113,707,253 bytes | 139,659,509 bytes |
| 2 | 20.496 s | 323.375 MiB | 21.845 s | 325.590 MiB | 113,182,965 bytes | 140,970,229 bytes |
| 3 | 20.521 s | 324.445 MiB | 22.765 s | 323.379 MiB | 113,707,253 bytes | 140,708,085 bytes |

Every pair reused the prepared snapshot, reopened no source, and exited both
workers. The fixture was 2,595,207 bytes and retained one exact SHA-256 hash.
The staging, normalization, and quality hashes were also identical across all
three pairs. The worst wall time was 22.765 seconds against the 120-second
budget. The worst worker peak was 326.449 MiB against the 900-MiB budget.

## Determinism and connector bounds

The same 25,000-row fixture was executed with transport batch sizes 25, 50,
and 200. Every variant retained the same snapshot semantic hash, row count,
edge count, dataset schedule, snapshot size, and completed status. The
transport-dependent call sequences changed as expected: 1,001, 501, and 151
bounded calls respectively.

The batch-size call-sequence hashes were
`sha256:0d393cf543831dc15cd434c466d82c212cf0ac3c98e6db9a52a55bd508e0d12e`
for 25 and
`sha256:d8ca6ef2d505fc9c91d6544a6781d47e7623787d755d9e5ba7cdca3013681649`
for 200. The batch-50 hash is recorded above.

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
| `scalable-relationship-phase6-execution-25k-windows-final.json` | 6,013 bytes | `2ea8138a99c117dab8a94c1faee16ed2e41d4c41dcae7266c79d038aa0023962` |
| `scalable-relationship-phase6-worker-25k-windows-final.json` | 9,538 bytes | `34fbb2cd0c33b62d385cee61e3cac6d0dc46fdda5f0468fefd48d63b475bb7e8` |
| `scalable-relationship-phase6-execution-25k-batch25-windows-final.json` | 2,834 bytes | `d000dc40182988e9d272605d27c665f85fa231f9524e5068d0aabfebd0d3123b` |
| `scalable-relationship-phase6-execution-25k-batch200-windows-final.json` | 2,833 bytes | `057536f14d054055b388aa03b246ec902f6a623fc53929befda12bc6b2ffeb61` |

The raw artifacts are intentionally ignored by Git. This report retains the
portable measurements and hashes needed to review the result.

## Retirement verification

The clean isolated candidate passed every repository test partition:

- application: 544 tests in 782.869 seconds;
- architecture: 65 tests in 53.778 seconds;
- domain: 418 tests in 11.292 seconds, with 2 expected skips;
- integration: 667 tests in 2,666.616 seconds, with 3 expected skips;
- end to end: 13 tests in 241.410 seconds, with 8 expected skips;
- performance: 61 tests in 710.717 seconds, with 8 expected skips.

The earlier apparent Production-readiness stall was a slow test: its isolated
run passed in 198.268 seconds. The final integration repeat also removed a
two-second test-client redirect race around the one-use launch token; the
corrected server-restart test passed both alone and inside the 667-test clean
partition.

The independent baseline repairs covered the target-match fixture version,
preparation recovery context, line-ending-stable scenario fixtures, current
browser labels, Windows timezone data, snapshot-contract fixtures, internal
release Python-version isolation, architecture inventory, and registered
repository documentation skills. Architecture, documentation ownership,
lockfile, and diff checks pass on the retirement documentation. The separate
100,000-row track remains deferred and unchanged.
