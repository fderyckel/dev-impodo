---
audience: developer
kind: testing
status: current
---

# Code organization Phase 0 baseline

## Decision supported by this baseline

This document gives a human maintainer or coding agent the reproducible checks
that must remain green while Impodo reorganizes production packages and tests.
It records current behavior. It does not make the current package shape the
desired architecture.

Phase 0 protects four kinds of evidence:

1. Accepted owners still keep their existing lifecycle behavior.
2. Test outcomes do not depend on one accidental execution order.
3. The current import graph changes only through a reviewed baseline update.
4. Repository decomposition does not introduce unbounded registry, workspace,
   or Odoo access.

The corresponding execution contract is the
[code organization remediation plan](../plans/code-organization-remediation.md).

## Architecture inventory

Run the inventory check from the repository root:

```bash
.venv/bin/python scripts/architecture_inventory.py \
  --check tests/architecture_phase0_baseline.json
.venv/bin/python -m unittest tests.test_architecture_inventory -v
```

The reviewed snapshot contains 342 production modules and 1,965 runtime
internal import edges. It records one type-only edge. Phase 1 removed the three
application-to-adapter edges and the runtime cycle between
`impodo.inspection` and `impodo.source_worker`. Phase 2 added named
composition, registry-record, preparation-session, and focused-use-case
collaborators without adding a forbidden layer dependency or runtime cycle.
Phase 3 moves the Project, Data version, workspace, Recipe, and Run domain
models, application services, and consumer-owned ports to owner-and-layer
paths. It moves Cutover domain contracts to `domain/cutover/models.py` while
the existing Cutover application services retain their focused names. The
workspace-owned Mapping, Preparation, and Execution application slices now
live below `application/workspace`. Run planning, review, target evidence,
guided Test setup, fresh-data matching, and fresh-data value decisions now
live below `application/run`. The run-owned Odoo requirement query also lives
there and proves that selected Recipe revisions are read in one bulk operation.
Deterministic ordering and collision decisions live below `domain/run`. These
moves preserve the zero-cycle and zero-forbidden-edge baseline.

Phase 2 also splits the two large DuckDB adapters without changing their
public ports. The migration foundation facade assembles owner-specific record
and command components behind one private registry transaction coordinator.
The preparation-session facade assembles direct writing, quality indexing,
normalization, stored-run reading, and cleanup components while preserving one
publication transaction. Run planning and Test setup retain stable facades over
focused use cases. Tests that patch adapter internals now patch the focused
owner module rather than the facade module.

When a remediation slice changes production modules or imports, inspect the
JSON diff. Update the fixture only when the change is intended and the new
result is at least as close to the target dependency rule. The Phase 1
dependency gate now requires zero application-to-adapter edges and zero
runtime cycles.

The inventory resolves relative imports and imports beneath `TYPE_CHECKING`.
It treats type-only edges as dependency-direction evidence while reporting
runtime cycles separately. An unknown nested production package is
unclassified and fails the baseline until a maintainer assigns it a layer.

## Phase 1 dependency direction

Run the direction gate from the repository root:

```bash
.venv/bin/python -m unittest tests.test_architecture_dependency_rules -v
```

The test reads the complete temporary ownership manifest for every remaining
flat production module. It rejects domain imports of application, adapter, or
web modules; application imports of adapter or web modules; runtime module
cycles; and direct concrete-adapter construction outside a composition module
or worker entry point. It prints the exact offending import path or
construction site when a rule fails.

## Reproducible test order

First run the integrated module in its normal order:

```bash
.venv/bin/python -m unittest tests.test_integrated_recipe_runs -v
```

Then run both recorded shuffled orders:

```bash
.venv/bin/python scripts/run_seeded_unittest.py \
  --module tests.test_integrated_recipe_runs \
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
  tests.test_migration_foundation \
  tests.test_project_authoring \
  tests.test_identity_semantics \
  tests.test_workspace_access \
  tests.test_canonical_ownership -v
```

### Data version and source evidence

```bash
.venv/bin/python -m unittest \
  tests.test_data_version_source_packages \
  tests.test_source_snapshot \
  tests.test_source_snapshot_io \
  tests.test_workspace_evidence_storage -v
```

### Workspace, mapping, and preparation

```bash
.venv/bin/python -m unittest \
  tests.test_workspace \
  tests.test_recipe_representative_shapes \
  tests.test_preparation_jobs \
  tests.test_staging_store \
  tests.test_quality -v
```

### Recipe application and integrated Test run

```bash
.venv/bin/python -m unittest \
  tests.test_integrated_recipe_runs \
  tests.test_workspace_journeys -v
```

### Cutover and Production

```bash
.venv/bin/python -m unittest \
  tests.test_cutover_qualification \
  tests.test_production_rollout \
  tests.test_forward_upgrade_compatibility -v
```

## Atomic-operation gates

The remediation plan enumerates which mutations must remain one registry
transaction and which cross-store operations recover through an operation
intent. Run this compact fault and retry set for any transaction-port or
repository decomposition:

```bash
.venv/bin/python -m unittest \
  tests.test_migration_foundation.MigrationFoundationTests.test_fault_injection_replays_each_root_without_duplicates \
  tests.test_project_authoring.ProjectAuthoringTests.test_publication_recovers_after_artifact_store_fault_and_adds_one_recipe \
  tests.test_data_version_source_packages.DataVersionSourcePackageTests.test_freeze_and_projection_recover_after_cross_store_faults \
  tests.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_same_operation_recovers_after_registry_fault_without_duplicates \
  tests.test_cutover_qualification.CutoverQualificationTests.test_qualification_recovers_after_protected_evidence_fault \
  tests.test_production_rollout.ProductionRolloutTests.test_activation_recovers_after_registry_commit_before_workspace_stores \
  tests.test_production_rollout.ProductionRolloutTests.test_activation_retry_reuses_reserved_meaning_before_registry_commit -v
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
  tests.test_migration_foundation.MigrationFoundationTests.test_project_list_is_registry_only_for_one_hundred_projects \
  tests.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_selected_recipe_revisions_use_one_registry_connection \
  tests.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_integrated_progress_reads_registry_without_workspace_open \
  tests.test_integrated_recipe_runs.IntegratedRecipeRunTests.test_review_projection_orders_recipes_without_workspace_open \
  tests.test_workspace_access.WorkspaceAccessTests.test_registry_resolver_is_one_read_and_opens_no_workspace_store \
  tests.test_local_odoo_reader.LocalOdooMetadataReaderTests.test_preflight_capture_batches_models_in_one_rolled_back_shell \
  tests.test_connectors.Json2ConnectorTests.test_schema_constraint_evidence_is_batched_for_all_models \
  tests.test_source_and_planner.PlannerTests.test_record_requests_are_batched_by_model \
  tests.test_execution_service.ExecutionServiceTests.test_configured_create_batch_size_reuses_existing_relation_lookup \
  tests.test_reconciliation_service.Json2ReadbackReaderTests.test_batches_exact_business_keys_in_one_request -v
```

If a deliberate design change increases one bound, update the implementation,
the exact assertion, this table, and the remediation review together. A file
move or repository split alone is not a reason to increase a bound.

## Completion rule

Phase 0 is complete when the architecture inventory, normal integrated order,
both fixed shuffled orders, focused owner groups, atomic-operation set, bounded
I/O set, documentation checks, and `git diff --check` pass. Record any optional
remote Odoo or browser verification that was not run; Phase 0 does not require
a live target because it changes no browser or Odoo behavior.

## Broader web-suite debt

The complete `tests.test_web_app` module is not a Phase 0 gate and is not green
at the reviewed `HEAD`. A clean exported checkout reproduces five assertion
failures and one error across
`test_remote_compare_recovery_matches_the_classified_failure`,
`test_remote_reference_failure_returns_to_matching_without_key_form`, and
`test_transformation_impact_uses_server_filters_and_100_row_pages`. The first
two contracts predate the global read-credential dialog; the third fixture
predates the required source-snapshot `physical_selection_hash`.

Do not attribute those outcomes to a package-only remediation without a clean
baseline comparison. Keep running focused browser contracts for moved assets,
and repair these three contracts before adopting the complete web module as a
required architecture gate.
