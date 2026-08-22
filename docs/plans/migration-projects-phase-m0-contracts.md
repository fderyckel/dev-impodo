# Migration Projects Phase M0 contracts

## Status and authority

**Status:** Completed architecture-contract phase from 2026-08-22. This phase
made no runtime change. Phases M1 through M3 now implement its clean roots,
source ownership, and Project-first authoring browser.

This document freezes Phase M0 of the [Migration projects and multi-Recipe
cutover implementation
plan](migration-projects-and-multi-recipe-cutover-implementation-plan.md).
[ADR-014](../decisions/README.md#adr-014--migration-projects-coordinate-reusable-recipes-and-cutover-plans)
governs the target ownership model.

Phase M0 itself changed no browser route, database schema, domain class, or
runtime behavior. Its later-runtime statements are point-in-time history;
current Project and Recipe publication contracts describe the implemented M3
workflow.

The deterministic fixture is
[`fixtures/migration-projects/phase-m0/acceptance-contract.json`](../../fixtures/migration-projects/phase-m0/acceptance-contract.json).
The executable gate is
[`tests/test_migration_project_phase_m0_contract.py`](../../tests/test_migration_project_phase_m0_contract.py).

## 1. M0 outcome

Phase M0 freezes these decisions before persistence or service work begins:

- A `MigrationProject` is the business and governance root.
- A Project can contain zero, one, or several Recipes.
- A Project owns each `DataVersion` as one complete source package.
- A `MigrationRun` uses one exact DataVersion and one exact Odoo target
  binding.
- A `MigrationWorkspace` isolates one mapping and execution unit inside a run.
- A `RecipeApplication` binds one exact Recipe revision to one exact
  DataVersion, run, target binding, and workspace.
- A versioned `CutoverPlan` selects the Recipe revisions, dependency order,
  write ownership, and shared controls that must qualify together.
- One Project-level qualification pins the exact integrated Test evidence.
- A Project cutover selection pins that qualification but grants no Production
  write authority.

The fixture describes a fictional legacy-ERP rollout. The Project contains a
Customer Recipe and a Product/BOM Recipe. Both Recipes use the same accepted
Test data package in isolated workspaces and later apply to the same fresh
rollout data package.

## 2. Identity and cardinality contracts

Each aggregate generates and persists its own UUID. No route, repository, or
service may substitute an identifier from another namespace or derive one
identifier from another.

The fixture keeps these identities distinct:

- `project_id` identifies the business migration effort.
- `data_version_id` identifies one immutable source package.
- `migration_run_id` identifies one use of one source package and one target.
- `workspace_id` identifies one isolated technical work area.
- `recipe_id` identifies one reusable migration purpose.
- `application_id` identifies one exact use of one Recipe revision.
- `cutover_plan_id` identifies one versioned integrated plan.
- `qualification_id` identifies one immutable Test qualification.
- `cutover_selection_id` identifies the Project's exact rollout selection.
- `target_binding_id` identifies fresh non-secret target evidence.

The executable cardinality examples contain three different Projects. One has
no Recipe, one has one Recipe, and the full rollout example has two Recipes.
Creating a Project therefore cannot create a Recipe shell as a side effect.

At product level, a Project contains its Recipes. At the domain boundary,
`MigrationProject` and `Recipe` remain separate aggregate roots. A Recipe
stores an immutable `project_id`; a Project list projection may return Recipe
counts and summaries without opening Recipe or workspace storage.

## 3. Project-owned DataVersions

The fixture contains three Project-owned DataVersions:

1. The Authoring DataVersion contains representative data from 15 August.
2. The Test DataVersion contains a fresh complete package from 25 August.
3. The Production DataVersion contains the rollout package from 31 August.

Each package contains the Customer, Product, BOM header, and BOM line logical
datasets. The package hashes differ because each export contains data captured
at a different time. The logical dataset inventory remains compatible, but
Impodo must still create fresh physical bindings, checks, comparisons, and
evidence for every application.

An accepted DataVersion has state `FROZEN`. It has no `recipe_id`,
`workspace_project_id`, or `pinned_recipe_revision`. Several workspaces may
consume different logical datasets from the same package without copying its
mutable state or changing its accepted evidence.

The first release accepts complete replacement packages. A missing row never
means delete. Delta files and inferred deletes require a separate future
contract because they change source completeness, identity, and reconciliation
semantics.

## 4. Retained Recipe semantic envelope

ADR-014 retains Recipe envelope version 2 because it already separates
reusable migration meaning from source, target, and execution evidence. The
executable test loads the current accepted Customer Recipe fixture and proves
that the envelope still has exactly these top-level fields:

```text
recipe_contract_version
semantic_hash
payload_hash
recipe
compatibility_hints
provenance
```

The semantic `recipe` object still has exactly:

```text
contract_versions
source_shape
parameter_definitions
source_preparation
mapping
odoo_target_contract
target_governance
quality
reference_dependencies
control_definitions
```

`semantic_hash` remains the canonical content hash of the semantic `recipe`
object. The semantic object contains logical source bindings, transformations,
target requirements, business keys, relationship rules, quality rules,
parameters, controls, and reusable dependencies.

The semantic object must not contain a Project, DataVersion, run, application,
workspace, target binding, source file, physical source hash, endpoint,
database, credential, principal, mapping artifact, execution result, or numeric
Odoo record identifier. Provenance may identify where Impodo compiled a
revision, but provenance remains outside semantic identity and cannot grant
source or target authority.

The Customer and Product/BOM entries in the M0 fixture identify their exact
Recipe revisions and semantic hashes. They intentionally do not duplicate a
second compiler-acceptance fixture. Later application phases must compile the
real Product/BOM envelope against current mapping contracts before they can
claim that workflow is implemented.

## 5. Run, application, and workspace contracts

One `MigrationRun` references one `project_id`, one `data_version_id`, and one
`target_binding_id`. Every application in that run must reference the same
three identities. A mixed-target run fails with `RUN_TARGET_MISMATCH`.

The run owns target identity, credential-generation evidence, the unioned Odoo
requirements plan, target snapshots, application order, and integrated
readiness. An application references the exact run-level binding. It does not
copy credentials or create one independent Odoo capture for each Recipe.

One application owns one `workspace_id`. That workspace stores the mutable and
immutable evidence needed to bind, prepare, compare, approve, execute,
read back, reconcile, and recover that application. Two applications never
share mutable workspace state, even when they use the same DataVersion and
target binding.

An Authoring workspace can exist without a RecipeApplication. The data manager
uses that workspace to complete one-off work or to publish reusable meaning.
Publishing creates the Recipe and its first immutable revision together; it
does not create or replace the Project or DataVersion.

The architecture requires the run planner to union compatible Odoo 19 model
and field requirements, then perform bounded batch reads. M0 adds no runtime
query path, so it makes no performance claim. Later phases must prove that
Odoo calls do not scale with Recipe count or source-row count and that Project
list rendering does not open workspace databases.

## 6. CutoverPlan validation

One CutoverPlan revision selects one exact revision and semantic hash for each
participating Recipe. It also records dependency edges, write ownership, and
shared controls.

The fixture orders the Customer Recipe before the Product/BOM Recipe through a
`PROJECT_SEQUENCE` edge. This edge records the data manager's deterministic
integrated rehearsal order. It does not invent an Odoo foreign-key dependency.
The plan validator must reject any directed cycle with
`CUTOVER_DEPENDENCY_CYCLE`.

The first release declares write ownership at `(Odoo model, field)` level. Two
selected Recipes cannot own the same pair. The plan validator must reject an
overlap with `CUTOVER_WRITE_COLLISION` before comparison or execution begins.

This policy is intentionally conservative. Two Recipes that intend to update
the same field for apparently disjoint record populations still conflict.
Impodo will not infer that filter predicates are complete or permanently
disjoint. Supporting record-scope partitioning would require explicit merge,
identity, ordering, and reconciliation contracts and remains out of scope.

## 7. Integrated qualification and rollout selection

The fixture qualifies CutoverPlan revision 1 from one Test run. The
qualification pins:

- the exact CutoverPlan identifier and revision;
- both selected Recipe revisions and semantic hashes;
- both Test application identifiers;
- the exact Test run; and
- one immutable integrated evidence hash.

Both Test applications have reached `RECONCILED`. If a Recipe revision,
application, plan revision, or Test run differs, validation fails with
`CUTOVER_QUALIFICATION_MISMATCH`.

The Project cutover selection pins that qualification without identifying a
Production run, DataVersion, target binding, or credential generation. A later
Production run references that selection when it applies the plan to the
rollout DataVersion. The selection has `grants_write_authority: false`. The
Production applications remain `DRAFT_READINESS` until they create fresh
source bindings, target evidence, comparison, approval, and separately
authorized write evidence.

Qualification proves the integrated Test result. It does not promise one
global transaction across Recipes. If a later execution commits one
application and a subsequent application fails, Impodo must preserve the
successful evidence, stop dependants, mark the integrated run incomplete, and
require explicit reconciliation before retry.

## 8. Ownership changes from the current runtime

The current runtime remains Recipe-first. The following table freezes every
persisted or resolved lifecycle field whose authoritative owner changes in the
target. Fields that keep their owner are listed after the table.

| Current field or record | Target owner | Required disposition |
| --- | --- | --- |
| `Recipe.current_data_version_id` | `MigrationRun.data_version_id` | Remove the Recipe pointer. Each run selects one exact Project-owned DataVersion. |
| `Recipe.cutover_candidate_id` | `MigrationProject.project_cutover_selection` | Replace the Recipe candidate with one Project selection that pins a qualified CutoverPlan revision. |
| `Recipe.data_classification` | `MigrationProject.data_classification` | Make the Project authoritative. A Recipe may expose only a derived governance projection. |
| `Recipe.retention_days` | `MigrationProject.retention_policy` | Make the Project authoritative. A Recipe may expose only a derived governance projection. |
| `DataVersion.recipe_id` | `DataVersion.project_id` | Replace Recipe ownership with immutable Project ownership. |
| `DataVersion.workspace_project_id` | `MigrationWorkspace.data_version_id` | Remove the one-workspace pointer. Several isolated workspaces may consume one DataVersion. |
| `DataVersion.pinned_recipe_revision` | `RecipeApplication.recipe_revision` | Remove the package-level Recipe pin. Each application selects one exact revision. |
| `DataVersion.parameter_values_hash` | `RecipeApplication.parameter_values_hash` | Move Recipe-specific values to the exact application. Keep Project package context on DataVersion. |
| `RecipeApplication.workspace_project_id` | `RecipeApplication.workspace_id` | Rename the internal workspace identity and keep it distinct from the business Project. |
| `RecipeApplication.target_binding_hash` | `MigrationRun.target_binding_id` | Let the run own one target binding. Each application references the exact binding and hash. |
| `RecipeApplication.credential_generation` | `MigrationRun.TargetBinding.credential_generation` | Capture credential evidence once per run and project a bounded reference into each application. |
| `RecipeApplication.source_selection_hash` | `DataVersion.source_package_hash` | Let DataVersion own the accepted package. Let the application own only its physical binding hash. |
| `RecipeQualification` | `CutoverPlanQualification` | Retain per-Recipe evidence as a component and qualify the exact integrated Test plan at Project level. |
| `CutoverCandidate` | `MigrationProject.project_cutover_selection` | Replace the Recipe candidate with one exact Project-level qualified plan selection. |
| `RecipeIntent.recipe_id` | Owner-specific operation intents | Use the identifier of the aggregate that owns each operation instead of one universal Recipe coordinator. |
| `WorkspaceResolution.recipe_id` | `WorkspaceResolution.project_id` | Resolve a workspace to Project, DataVersion, run, and optional RecipeApplication identities explicitly. |

`Recipe.recipe_id`, Recipe name, Recipe business purpose,
`Recipe.current_recipe_revision`, Recipe optimistic revision, and immutable
RecipeRevision lineage remain Recipe-owned. DataVersion label, purpose, source
package, source provenance, lineage, and freeze evidence remain
DataVersion-owned. Application bindings, parameter values, focused issues,
compiled mapping evidence, and terminal status remain application-owned.

The implementation must replace these ownership paths through new exact schema
generations. It must not keep old columns as aliases, backfill current
development databases, dual-write both models, or add a compatibility reader.

## 9. Fail-closed acceptance cases

The fixture includes mutations that the executable test applies to a valid
contract. Each mutation must produce its exact issue code:

| Rejected condition | Issue code |
| --- | --- |
| A Recipe points to another Project. | `RECIPE_PROJECT_MISMATCH` |
| One Test application points to another target binding. | `RUN_TARGET_MISMATCH` |
| A reverse edge creates a dependency cycle. | `CUTOVER_DEPENDENCY_CYCLE` |
| The Product/BOM Recipe also claims `res.partner.name`. | `CUTOVER_WRITE_COLLISION` |
| Qualification substitutes another Product/BOM revision. | `CUTOVER_QUALIFICATION_MISMATCH` |
| A cutover selection embeds a Production run. | `CUTOVER_SELECTION_CONTAINS_PRODUCTION_CONTEXT` |

These are target architecture codes. They become runtime API contracts only
when the corresponding implementation phase introduces them.

## 10. Explicit first-release exclusions

M0 freezes these exclusions so later code cannot acquire them accidentally:

- A Recipe cannot move between or be shared across Projects.
- A run cannot combine Odoo target identities.
- Two Recipes cannot merge writes to the same Odoo model and field.
- A source package cannot express delta or inferred-delete semantics.
- Impodo cannot perform unattended rollout.

The first release also does not provide automatic rollback of already
committed Odoo writes. `PRODUCTION` remains an intent label and never bypasses
ADR-010 or the separately reviewed writer policy.

## 11. M0 gate and M1 entry condition

M0 is complete when the fixture and focused test prove:

- distinct identity namespaces;
- zero, one, and several Recipe cardinalities;
- Project-owned complete DataVersions;
- the retained portable Recipe semantic envelope;
- one target binding per run;
- one isolated workspace per application;
- acyclic dependencies and non-overlapping write ownership;
- exact integrated Test qualification;
- no Production authority inherited from qualification; and
- deterministic failure for every rejected mutation.

Phase M1 may introduce the clean Project, DataVersion, run, and workspace
domain roots only while this gate remains green. An intentional change to the
target contract must update this document, the fixture, and the focused test
together.
