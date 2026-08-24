---
audience: developer
stage: prepare
status: current
---

# Prepare data

## Responsibility

Prepare data compiles the submitted mapping, transforms every frozen row,
publishes canonical staging and quality evidence, resolves ambiguous source
entities, and freezes required normalization decisions.

It is target-independent and must not contact Odoo.

## Entry conditions

The current mapping revision must have matching validation and submission
evidence. Source, schema, related-dataset plan, and mapping hashes must agree
before any publication begins.

## Implementation flow

`preparation.py` starts and monitors work through `PreparationJobManager`.
Before spawning, the route resolves and captures the Project, DataVersion,
MigrationRun, and MigrationWorkspace identities. The application also captures
the exact application build and workspace schema contract that accepted the
request.
`preparation_worker.py` composes project-only adapters and validates that
captured identity against the Project's immutable linkage; it cannot open the
shared Recipe registry. Before it opens workspace evidence, the spawned worker
must prove that it loaded the same application build and workspace schema
contract. A mismatch returns `IMPODO_BUILD_CHANGED` and requires an application
restart; the browser must not offer a blind retry. The progress page renders
from the same in-memory job snapshot. It therefore does not race either DuckDB
writer.
`PreparationService` selects the supported preparation capability, compiles the
mapping, writes bounded staging batches, publishes quality/accounting evidence,
and records the preparation session.

`resolution.py` applies explicit merge/separate and field-correction decisions
through `ResolutionService`. `normalization.py` handles reviewable value groups
through `NormalizationService`. Both publish new evidence rather than mutating
the frozen source.

## Code references

| Role | Code |
| --- | --- |
| Preparation orchestration | [`PreparationService`](../../../src/impodo/application/preparation_service.py) |
| Background jobs | [`PreparationJobManager`](../../../src/impodo/application/preparation_job_service.py) |
| Process build contract | [`ApplicationBuildContract`](../../../src/impodo/build_contract.py) |
| Running-build request guard | [`BuildConsistencyMiddleware`](../../../src/impodo/web/security.py) |
| Project-only worker wiring | [`create_preparation_worker`](../../../src/impodo/preparation_worker.py) |
| Quality publication | [`QualityService`](../../../src/impodo/application/quality_service.py) |
| Entity resolution | [`ResolutionService`](../../../src/impodo/application/resolution_service.py) |
| Normalization decisions | [`NormalizationService`](../../../src/impodo/application/normalization_service.py) |

## Evidence and state

Prepared evidence includes the compiled plan hash, complete canonical rows,
source-to-canonical lineage, control totals, quality findings, quarantine,
resolution state, normalization decisions, and preparation-session status.
Publication is project-scoped and hash-bound.

For `odoo_pinned_update`, `PreparationService` verifies the one current
protected manifest and bounded origin sidecar against the source binding and
Parquet snapshot before it processes any rows. This verification requires a
constant number of local reads; it does not look up provenance for each row.
The transformation path then uses the same origin-neutral snapshot reader and
canonical staging contracts as a file source. Pinned rows intentionally have
no business identity, so duplicate grouping excludes that empty value. Source
ordinals remain ordinary lineage, while numeric Odoo IDs remain protected.

## Completion and navigation

Preparation progress uses its captured job snapshot for navigation and Recipe
context, avoiding both registry and project database reads while a writer owns
either file. Later stages remain locked while work is active or while required
resolution or normalization decisions remain. Completion requires frozen,
fully accounted prepared evidence for the current bindings.

## Invalidation and recovery

Any source, schema, mapping, compilation, derived-plan, resolution, or
normalization binding change invalidates dependent evidence. A failed or
cancelled attempt retains its status; retry creates a controlled attempt and
must not partially reuse uncommitted tables.

A changed application build or incompatible workspace contract is deterministic
for the running process. The operator must restart Impodo or follow the
workspace compatibility action. Retrying the same job cannot repair either
condition.

Use stage-level transactions and idempotent publication. Never repair a result
by editing DuckDB rows directly.

## Odoo 19 and performance

Preparation makes zero Odoo calls. Direct transformations should remain on the
native columnar path where supported. Python fallback, derived datasets, and
relationship materialization must use bounded batches with measured memory.

Review database writes for repeated single-row `execute` calls and source-row
loops for hidden conversions. New paths must preserve deterministic hash and
lineage parity before being called an optimization.

## Verification

- [`tests/test_preparation_jobs.py`](../../../tests/test_preparation_jobs.py)
- [`tests/test_build_contract.py`](../../../tests/test_build_contract.py)
- [`tests/test_workspace_schema_contract.py`](../../../tests/test_workspace_schema_contract.py)
- [`tests/test_preparation_session.py`](../../../tests/test_preparation_session.py)
- [`tests/test_quality.py`](../../../tests/test_quality.py)
- [`tests/test_normalization.py`](../../../tests/test_normalization.py)
- [`tests/test_preparation_scale.py`](../../../tests/test_preparation_scale.py)
- [`tests/test_web_app.py`](../../../tests/test_web_app.py)

Verify atomic rollback, cancellation, retry, bounded memory, complete
accounting, deterministic hashes, lineage, progress rendering under real
cross-process DuckDB locks, and the appropriate scale gate for each execution
class.

## Related documentation

- [User guide: Prepare data](../../user/workflow/04-prepare-data.md)
- [Canonical staging contract](../contracts/canonical-staging.md)
- [Normalization governance contract](../contracts/normalization.md)
- [Quality and quarantine contract](../contracts/quality-and-quarantine.md)
