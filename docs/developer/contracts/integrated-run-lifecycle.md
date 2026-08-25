---
audience: developer
kind: contract
status: current
---

# Integrated Test run lifecycle contract

## Scope

One Project-owned Test `MigrationRun` coordinates several exact
`RecipeRevision` applications over one accepted Test `DataVersion` and one
reviewed, supported Odoo target. Planning materializes those applications and
binds the run to one exact CutoverPlan revision. Qualification accepts only
complete, ordered execution and reconciliation evidence.

The browser creates the Test run in a setup phase. A `TestRunSetupBinding`
pins the selected Recipe revisions and dependency order while the data manager
uploads and accepts the newer Test delivery and reviews the Odoo target chosen
for that Test run. Activation adds the immutable run target and fresh
application workspaces to that same run; it does not create a second Test run.

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

Before any application workspace is provisioned, the planner must:

1. verify Project ownership, an accepted frozen Test package, and exact
   protected Recipe revisions;
2. validate physical source bindings, supplied parameters and controls, Odoo
   19 requirements, and supporting reference versions;
3. reject missing dependencies, duplicate edges, self-dependencies, and
   cycles; and
4. reject two Recipes that claim the same writable Odoo model field.

The earlier setup operation may create only the draft Test DataVersion, draft
run, shared setup workspace, and exact selection binding. It cannot create an
application, target binding, CutoverPlan, approval, or execution evidence.

The write contract has no last-writer-wins or implicit merge rule. A collision
requires a Recipe-boundary or ownership correction.

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

## Browser journey boundary

Canonical workspace ownership determines the browser journey. An Authoring
workspace may expose the six editable stages. A Test setup workspace may
expose fresh-source and shared-target setup only. A Recipe application
workspace may expose preparation, review, load, and verification only; it must
not expose editable source, target-schema, mapping, or relationship-authoring
pages. A stale or crafted URL for the wrong journey must return to the owning
run before the requested route reads or changes child evidence.

Run-owned navigation may redirect to an existing workspace route while the
three-page refactor is delivered. The redirect does not transfer ownership:
shared Odoo refresh remains in the setup workspace, and application evidence
remains in its isolated workspace. This compatibility seam must not create a
second semantic implementation or retain an obsolete application-specific
Authoring path.

## Qualification boundary

`READY` means planning produced compatible fresh application drafts. It is not
execution success or qualification. Integrated qualification requires exact current
evidence from every application, passing Project shared controls, and proof
that each dependency reconciled before its downstream execution began.

One immutable per-application qualification and one integrated CutoverPlan
qualification publish together. Rollout selection is a separate Project
operation. Neither Test qualification nor selection is Production authority.

## Verification

- `tests/test_integrated_recipe_runs.py`
- `tests/test_workspace_journeys.py`
- `tests/test_cutover_qualification.py`
- `tests/test_project_authoring.py`
- `tests/test_data_version_source_packages.py`

## Related documentation

- [Developer workflow](../workflow/07-integrated-test-runs.md)
- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](project-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Cutover plan lifecycle](cutover-plan-lifecycle.md)
