---
audience: developer
kind: contract
status: current
---

# Integrated Test run lifecycle contract

## Scope

One Project-owned Test `MigrationRun` coordinates several exact
`RecipeRevision` applications over one accepted Test `DataVersion` and one
reviewed Odoo 19 target. M4 plans and materializes those applications. M5
binds the run to one exact CutoverPlan revision and may qualify only complete
ordered execution and reconciliation evidence.

## Run-owned evidence

The run owns one non-secret `TargetBinding`, one unioned Odoo requirement
plan, one filtered target-schema projection, and one filtered supporting
reference bundle. These objects are captured or selected once for the run.
An application workspace may read only the part required by its Recipe; it
cannot recapture, rebind, or publish independent run target evidence.

The Test DataVersion owns the complete immutable source package. Each
application workspace stores only its selected logical dataset and snapshot
references. It never copies source rows or another workspace database.

## Planning gate

Before any run, application, or workspace is provisioned, the planner must:

1. verify Project ownership, an accepted frozen Test package, and exact
   protected Recipe revisions;
2. validate physical source bindings, supplied parameters and controls, Odoo
   19 requirements, and supporting reference versions;
3. reject missing dependencies, duplicate edges, self-dependencies, and
   cycles; and
4. reject two Recipes that claim the same writable Odoo model field.

The first M4 release deliberately has no last-writer-wins or implicit merge
rule. A collision requires a Recipe-boundary or ownership correction.

## Isolated applications

Every selected Recipe revision receives a distinct `RecipeApplication` and a
distinct `MigrationWorkspace`. Each application pins the Project, Test
DataVersion, run, run target, Recipe revision, selected source datasets,
requirements, and dependency position.

The application compiler creates fresh governance, preparation, mapping, and
quality evidence in that workspace. It may translate a portable text
normalization into the normal scalar mapping transformation, but it must not
copy authoring mappings, current pointers, approvals, journals, credentials,
or source columns.

Source, target, and reference incompatibility prevent mapping creation.
Reviewable current-data quality or categorical issues may retain a fresh
mapping draft while the application remains `BLOCKED`.

## Recovery and status

Provisioning is one restart-safe operation. One registry transaction creates
the run plan and all identities, then isolated stores are initialized and
compiler outcomes are recorded. Exact replay returns the same identities;
changed meaning under the same operation ID fails closed.

Integrated status is a bounded registry projection over the run and its
applications. The run is `READY` only when every application is `READY`.
Opening the run page must not open every application workspace or call Odoo
once per Recipe.

## Qualification boundary

`READY` means M4 produced compatible fresh application drafts. It is not
execution success or qualification. M5 qualification requires exact current
evidence from every application, passing Project shared controls, and proof
that each dependency reconciled before its downstream execution began.

One immutable per-application qualification and one integrated CutoverPlan
qualification publish together. Rollout selection is a separate Project
operation. Neither Test qualification nor selection is Production authority.

## Verification

- `tests/test_migration_project_phase_m4_multi_recipe_runs.py`
- `tests/test_migration_project_phase_m5_cutover_qualification.py`
- `tests/test_migration_project_phase_m3_project_authoring.py`
- `tests/test_migration_project_phase_m2_source_packages.py`

## Related documentation

- [Developer workflow](../workflow/07-integrated-test-runs.md)
- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](project-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Cutover plan lifecycle](cutover-plan-lifecycle.md)
