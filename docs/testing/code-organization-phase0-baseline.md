---
audience: developer
kind: testing
status: current
---

# Code-organization regression baseline

## Responsibility

This document gives a human maintainer or coding agent the reproducible checks
that protect Impodo's implemented production packages, tests, transaction
ports, and bounded external access. The
[code-organization guide](../architecture/code-organization.md) owns current
placement and dependency rules. This page owns the exact regression commands
and preservation limits.

The baseline protects four kinds of evidence:

1. Accepted owners still keep their existing lifecycle behavior.
2. Test outcomes do not depend on one accidental execution order.
3. The current import graph changes only through a reviewed baseline update.
4. Repository decomposition does not introduce unbounded registry, workspace,
   or Odoo access.

The [completed remediation record](../plans/code-organization-remediation.md)
explains how these gates and the current structure were delivered.

## Architecture inventory

Run the inventory check from the repository root:

```bash
.venv/bin/python scripts/architecture_inventory.py \
  --check tests/architecture/phase0_baseline.json
.venv/bin/python -m unittest tests.architecture.test_inventory -v
```

The reviewed snapshot contains 366 production modules and 2,019 runtime
internal import edges. It records one type-only edge. Phase 1 removed the three
application-to-adapter edges and the former inspection-worker runtime cycle.
Phase 2 added named
composition, registry-record, preparation-session, and focused-use-case
collaborators without adding a forbidden layer dependency or runtime cycle.
Phase 3 moved the Project, Data version, workspace, Recipe, and Run domain
models, application services, and consumer-owned ports to owner-and-layer
paths. It moved Cutover domain contracts to `domain/cutover/models.py` while
the existing Cutover application services retain their focused names. The
workspace-owned Mapping, Preparation, and Execution application slices now
live below `application/workspace`. Run planning, review, target evidence,
guided Test setup, fresh-data matching, and fresh-data value decisions now
live below `application/run`. The run-owned Odoo requirement query also lives
there and proves that selected Recipe revisions are read in one bulk operation.
Deterministic ordering and collision decisions live below `domain/run`. These
moves preserved the zero-cycle and zero-forbidden-edge baseline. Phase 3 also
split artifact, credential, Odoo transport, writer, read-back, and local job
contracts from their concrete adapters. The package root now contains only
`__init__.py` and the `python -m impodo` entry point.

Phase 2 also split the two large DuckDB adapters without changing their
public ports. The migration foundation facade assembles owner-specific record
and command components behind one private registry transaction coordinator.
The preparation-session facade assembles direct writing, quality indexing,
normalization, stored-run reading, and cleanup components while preserving one
publication transaction. Run planning and Test setup retain stable facades over
focused use cases. Tests that patch adapter internals now patch the focused
owner module rather than the facade module.

The 2026-08-28 review incorporated the current mapping-review artifact adapter
and the canonical relationship-dependency domain module. The resulting graph
still has no runtime cycle, forbidden application-to-adapter edge, or
unclassified production module.

When a structural change modifies production modules or imports, inspect the
JSON diff. Update the fixture only when the change is intended and the result
still satisfies the current dependency rule. The dependency gate requires zero
application-to-adapter edges and zero runtime cycles.

The inventory resolves relative imports and imports beneath `TYPE_CHECKING`.
It treats type-only edges as dependency-direction evidence while reporting
runtime cycles separately. An unknown nested production package is
unclassified and fails the baseline until a maintainer assigns it a layer.

## Dependency direction

Run the direction gate from the repository root:

```bash
.venv/bin/python -m unittest tests.architecture.test_dependency_rules -v
```

The test rejects every package-root or otherwise unclassified production module,
domain imports of application, adapter, or web modules, application imports of
adapter or web modules, runtime module cycles, and direct concrete-adapter
construction outside a composition module or worker entry point. The Phase 1
ownership manifest was deleted when the final flat module moved. A failure
prints the exact offending module, import path, or construction site.

## Reproducible test order

First run the integrated module in its normal order:

```bash
.venv/bin/python -m unittest tests.application.run.test_integrated_recipe_runs -v
```

Then run both recorded shuffled orders:

```bash
.venv/bin/python scripts/run_seeded_unittest.py \
  --module tests.application.run.test_integrated_recipe_runs \
  --seed 1729 \
  --seed 20260826
```

The runner starts from sorted test identifiers. It applies a local random
generator with the recorded seed, starts a separate Python process for each
order, and sets `PYTHONHASHSEED` to the same value. A failure therefore reports
an order that another maintainer can reproduce exactly.

Do not replace these checks with one unrecorded random shuffle. A newly found
order-dependent failure should add its reproducing seed or a smaller
process-isolated regression before the fix is accepted.

## Focused owner commands

Run these commands independently when their owner is moved or its ports are
changed. Run all groups for a cross-owner repository or composition change.

### Project and identity

```bash
.venv/bin/python -m unittest \
  tests.integration.duckdb.test_migration_foundation \
  tests.application.project.test_authoring \
  tests.architecture.test_identity_semantics \
  tests.application.workspace.test_access \
  tests.architecture.test_canonical_ownership -v
```

### Data version and source evidence

```bash
.venv/bin/python -m unittest \
  tests.application.data_version.test_source_packages \
  tests.domain.data_version.test_source_snapshot \
  tests.integration.artifacts.test_source_snapshot_io \
  tests.integration.duckdb.test_workspace_evidence -v
```

### Workspace, mapping, and preparation

```bash
.venv/bin/python -m unittest \
  tests.integration.duckdb.test_workspace \
  tests.domain.recipe.test_representative_shapes \
  tests.application.workspace.preparation.test_jobs \
  tests.integration.duckdb.test_staging_store \
  tests.domain.preparation.test_quality -v
```

### Recipe application and integrated Test run

```bash
.venv/bin/python -m unittest \
  tests.application.run.test_integrated_recipe_runs \
  tests.application.workspace.test_journeys -v
```

### Cutover and Production

```bash
.venv/bin/python -m unittest \
  tests.application.cutover.test_qualification \
  tests.application.run.test_production_rollout \
  tests.integration.duckdb.test_forward_upgrades -v
```

## Atomic-operation gates

The remediation plan enumerates which mutations must remain one registry
transaction and which cross-store operations recover through an operation
intent. Run this compact fault and retry set for any transaction-port or
repository decomposition:

```bash
.venv/bin/python -m unittest \
  tests.integration.duckdb.test_migration_foundation.MigrationFoundationTests.test_fault_injection_replays_each_root_without_duplicates \
  tests.application.project.test_authoring.ProjectAuthoringTests.test_publication_recovers_after_artifact_store_fault_and_adds_one_recipe \
  tests.application.data_version.test_source_packages.DataVersionSourcePackageTests.test_freeze_and_projection_recover_after_cross_store_faults \
  tests.application.run.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_same_operation_recovers_after_registry_fault_without_duplicates \
  tests.application.cutover.test_qualification.CutoverQualificationTests.test_qualification_recovers_after_protected_evidence_fault \
  tests.application.run.test_production_rollout.ProductionRolloutTests.test_activation_recovers_after_registry_commit_before_workspace_stores \
  tests.application.run.test_production_rollout.ProductionRolloutTests.test_activation_retry_reuses_reserved_meaning_before_registry_commit -v
```

## Bounded-I/O gates

The following tests are preservation limits, not general performance claims:

| Behavior | Preserved bound | Executable evidence |
| --- | --- | --- |
| List 100 Projects with owner counts. | One registry connection executes one aggregate statement. No Data version or workspace store opens. | `MigrationFoundationTests.test_project_list_is_registry_only_for_one_hundred_projects` |
| Read the selected Recipe revisions for a run. | One registry connection executes three fixed statements for any number of selected revisions. | `IntegratedRecipeRunTests.test_selected_recipe_revisions_use_one_registry_connection` |
| Read integrated-run progress. | Two fixed registry connections execute two statements. No application workspace opens. | `IntegratedRecipeRunTests.test_integrated_progress_reads_registry_without_workspace_open` |
| Build the integrated review projection. | The presenter opens no registry or workspace store. | `IntegratedRecipeRunTests.test_review_projection_orders_recipes_without_workspace_open` |
| Resolve workspace access lineage. | One registry read opens no workspace store. | `WorkspaceAccessTests.test_registry_resolver_is_one_read_and_opens_no_workspace_store` |
| Capture local Odoo preflight evidence. | One rolled-back Odoo shell batches metadata and record requests for all planned models. | `LocalOdooMetadataReaderTests.test_preflight_capture_batches_models_in_one_rolled_back_shell` |
| Capture remote Odoo constraint evidence. | One model query and one constraint query cover all requested models. | `Json2ConnectorTests.test_schema_constraint_evidence_is_batched_for_all_models` |
| Plan Odoo record reads. | Requests are grouped by model rather than emitted per record. | `PlannerTests.test_record_requests_are_batched_by_model` |
| Execute prepared creates. | Configured row batches stay bounded and a shared relationship lookup is reused across all batches. | `ExecutionServiceTests.test_configured_create_batch_size_reuses_existing_relation_lookup` |
| Reconcile exact business keys. | One request contains the complete exact-key batch for a model. | `Json2ReadbackReaderTests.test_batches_exact_business_keys_in_one_request` |

Run the compact preservation set with:

```bash
.venv/bin/python -m unittest \
  tests.integration.duckdb.test_migration_foundation.MigrationFoundationTests.test_project_list_is_registry_only_for_one_hundred_projects \
  tests.application.run.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_selected_recipe_revisions_use_one_registry_connection \
  tests.application.run.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_integrated_progress_reads_registry_without_workspace_open \
  tests.application.run.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_review_projection_orders_recipes_without_workspace_open \
  tests.application.workspace.test_access.WorkspaceAccessTests.test_registry_resolver_is_one_read_and_opens_no_workspace_store \
  tests.integration.odoo.test_local_reader.LocalOdooMetadataReaderTests.test_preflight_capture_batches_models_in_one_rolled_back_shell \
  tests.integration.odoo.test_connectors.Json2ConnectorTests.test_schema_constraint_evidence_is_batched_for_all_models \
  tests.domain.execution.test_planner.PlannerTests.test_record_requests_are_batched_by_model \
  tests.application.workspace.execution.test_service.ExecutionServiceTests.test_configured_create_batch_size_reuses_existing_relation_lookup \
  tests.application.workspace.execution.test_reconciliation.Json2ReadbackReaderTests.test_batches_exact_business_keys_in_one_request -v
```

If a deliberate design change increases one bound, update the implementation,
the exact assertion, this table, and the remediation review together. A file
move or repository split alone is not a reason to increase a bound.

## Focused browser and test-organization gates

Phase 4 replaced the former browser monolith with capability suites. Run the
focused browser and static-ownership gates with:

```bash
.venv/bin/python -m unittest \
  tests.architecture.test_test_organization \
  tests.architecture.test_static_asset_ownership -v
.venv/bin/python -m unittest discover \
  -s tests/integration/web -t . -v
.venv/bin/python -m unittest \
  tests.e2e.test_project_setup_journey -v
```

The integration modules own Project setup, security, local-stack, source,
target, Mapping, Preparation, review, and load browser contracts. The e2e
module protects the complete setup journey. Test support is not discovered and
uses explicit builders plus `tests.support.paths.REPOSITORY_ROOT`, so moves do
not depend on package depth or test order.

All static JavaScript remains framework-free. Run `node --check` for every
file below `src/impodo/web/static` when a browser module changes.

The Phase 4 verification on 2026-08-27 ran 92 focused web tests and the
complete Project setup journey. Repository-root discovery ran 890 tests with
13 expected skips. The integrated-run module ran 26 tests in normal order and
under each recorded isolated seed.

## Preservation rule

The organization baseline remains protected when the architecture inventory,
normal integrated order, both fixed shuffled orders, focused owner groups,
atomic-operation set, bounded-I/O set, browser and test-organization gates,
documentation checks, and `git diff --check` pass. Record any optional remote
Odoo verification that was not run. Organization-only changes do not require a
live target because they do not change Odoo behavior.
