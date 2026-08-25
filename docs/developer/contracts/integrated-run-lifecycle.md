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

Before target capture, the Test setup bulk-reads the exact selected Recipe
revisions and derives one read-only browser projection of required Odoo
models, fields, and Recipe-owned relationship paths. Portable reference tables
remain separate Recipe dependencies. This projection cannot be replaced
through the setup model-scope or generic schema-capture forms. The run-owned
**Check Odoo** route and a copied setup schema URL resolve to the same shared
setup evidence. Ordinary Authoring workspaces remain outside this rule and
retain editable model selection.

**Check this Odoo** is one operation. It captures or compares the unioned Odoo
19 schema, reads all authorized related value sets in one model-and-field
batch, assesses the exact Recipe revisions, and activates the Test run when
the assessment succeeds. Each related model request is limited to 2,001 rows;
more than 2,000 values fails closed. The captured supporting choices contain
portable business keys and labels, never Odoo numeric IDs or credentials.
Target, principal, permission, access-context, Recipe, and completeness
mismatches fail before activation.

A changed schema remains a pending candidate until the data manager confirms
that replacing the current details may retire dependent work. A recoverable
failure retains the submitted activation operation ID. Browser Back, refresh,
or retry therefore cannot create a second set of Recipe work areas. The former
Test activation page and routes do not remain as a second decision or write
path.

The Test DataVersion owns the complete immutable source package. Each
application workspace stores only its selected logical dataset and snapshot
references. It never copies source rows or another workspace database.

Before file intake, the run may read the exact selected Recipe envelopes and
present their logical source tables and columns as one Fresh data projection.
This read must include a Recipe identity that was archived after selection,
because the run pins the exact revision rather than the active Recipe list.
It must use a bounded registry operation rather than one registry query per
Recipe.

After inspection, the run matches the current physical tables to those logical
inputs from the bounded catalogues. Every required header must occur exactly
once after conservative case, spacing, and punctuation normalization. Formula
or error tables are ineligible. One compatible table is automatic; a unique
Recipe-name match may break a tie; every remaining ambiguity requires an
explicit choice. Missing inputs, one physical table claimed by two logical
inputs, overlapping worksheet and named-table selections, and files unused by
the Recipe fail closed.

Acceptance derives a stable dataset name from each Recipe logical dataset ID,
then calls the existing source confirmation, freeze, and DataVersion projection
services. The resulting immutable `SourceSelection` is the physical binding;
the portable Recipe never stores a delivery-specific file or table ID. The
compiler accepts both this logical-ID-derived name and the earlier exact
logical-name convention so saved Recipe evidence remains forward compatible.

## Run-owned parameter values

Each selected Recipe revision declares the values a fresh application may
need. The standard export-as-of date is supplied from the Test DataVersion
cutoff and is read-only. Every other declared value is answered on **Fresh
data** and belongs to that Test run, not to the reusable Recipe.

Compatible declarations with the same logical parameter ID are one business
question. Their type, required status, and constraints must agree before one
answer can be applied to several Recipes. A disagreement, an unknown submitted
parameter, an invalid type, a missing required answer, or a value outside a
saved constraint fails closed before source acceptance or activation.

`test_run_parameter_values` stores normalized Recipe-scoped answers under the
pinned `TestRunSetupBinding`. The record has its own optimistic revision,
content hash, stable actor identity, timestamp, and audit event. The repository
checks the setup ID, Project, run, and editable setup state in the same write
transaction. Accepted answers cannot change after the Test DataVersion is
frozen; an older frozen Test delivery may add a missing answer record once.
The combined Odoo check reads the record once and validates it again against
the exact protected Recipe revisions. Changing these answers never changes a
Recipe revision, Authoring evidence, source snapshot, target snapshot, or
application workspace.

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
quality evidence in that workspace. A clean fresh mapping is checked and
submitted through the normal mapping service before the application becomes
`READY`; automatic preparation never runs from an unchecked draft. A new
validation warning or invalid mapping blocks automatic submission and remains
an application issue. File-based Recipe applications do not inherit the
separate approved-field flag used only for protected captured-Odoo updates.
The run target contract continues to own the fields a Recipe may write. The
compiler may translate a portable text
normalization into the normal scalar mapping transformation, but it must not
copy authoring mappings, current pointers, approvals, journals, credentials,
or source columns.

Source, target, and reference incompatibility prevent mapping creation.
Reviewable current-data quality or categorical issues may retain a fresh
mapping draft while the application remains `BLOCKED`.

A required writable scalar field added by the current Odoo target follows the
same create-field policy as an Authoring workspace. A target-bound
`default_get` value creates a `REVIEW` issue and a fresh mapping disposition;
it does not change portable Recipe meaning. The application remains `BLOCKED`
until the data manager confirms all displayed defaults together. Missing,
relational, malformed, or context-mismatched defaults remain blockers. New
read-only fields are ignored as inputs, while a Recipe-owned write field that
became read-only remains incompatible.

An activated Test run created before default evidence was captured may recheck
the shared setup target. Recovery requires identical target, principal,
permission, context, model, field, selection, and constraint behavior after
create-default facts are excluded. A derived application projection may then
supplement the immutable run target with those defaults. It must reproduce the
original physical binding hash, retain the saved Recipe revision, and stop if
parameters, controls, or any other blocker cannot be reconstructed exactly.

## Recovery and status

Provisioning is one restart-safe operation. One registry transaction creates
the run plan and all identities, then isolated stores are initialized and
compiler outcomes are recorded. Exact replay returns the same identities;
changed meaning under the same operation ID fails closed.

Integrated status is a bounded registry projection over the run and its
applications. The run is `READY` only when every application is `READY`.
Opening the run page must not open every application workspace or call Odoo
once per Recipe.

Preparation and load workers publish only coarse application milestones to
that registry: running, prepared, compared, executed, and reconciled. Detailed
rows, comparisons, journals, and reconciliation remain in the isolated
workspace. Active progress comes from one latest in-memory snapshot per
requested workspace, collected in one pass. A restart may remove the live job
snapshot, but the registry milestone and workspace evidence retain the safe
recovery point. A failed preparation remains the next recoverable application;
it does not unlock a dependent Recipe.

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
remains in its isolated workspace. Fresh file matching and acceptance stay on
the run-owned page. This compatibility seam must not create a second semantic
implementation or retain an obsolete application-specific Authoring path.

`GET /projects/{project_id}/runs/{migration_run_id}` is the canonical
**Review and load** page. It presents applications in the saved dependency
order and exposes one current action. A successful Test activation and a
verified application may enqueue only the first unresolved compatible
application. Downstream application entry fails closed until every earlier
application is reconciled. `GET .../status` reads registry records and job
snapshots only; it never opens an application workspace.

Automatic work ends at preparation. **Check changes** remains a read-only
comparison, **Confirm and load** remains an explicit data-manager decision,
and **Verify result** remains required before the next dependent Recipe can
start. An empty, unblocked comparison may journal a completed zero-row result
without constructing or calling an Odoo writer. It becomes reconciled only
after the unchanged snapshot, target, and empty execution journal match. No
page view or polling request may confirm or repeat an Odoo write.

`GET /projects/{project_id}/test-runs/{migration_run_id}/fresh-data` is the
canonical Test source entry. Run-owned forms may add or remove files only
after verifying that the run owns the draft setup workspace. They must call
the same intake commands as ordinary Authoring; the run journey must not own a
second file-validation, storage, cleanup, or audit implementation. Detailed
physical-table review remains available to ordinary Authoring but is not part
of the Recipe-run journey. The run page must not ask the data manager to
redefine the logical tables or columns already supplied by the selected Recipe
revisions.

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
