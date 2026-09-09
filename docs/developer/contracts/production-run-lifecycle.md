---
audience: developer
kind: contract
status: current
---

# Production run lifecycle contract

## Scope

This contract governs use of one selected qualified CutoverPlan with a fresh
complete Production DataVersion and a different compatible Odoo 19 target. It
defines setup, activation, evidence separation, execution authority, recovery,
and bounded persistence. The current Production path accepts file-source plans only;
Odoo-source round-trip writes remain unsupported.

## Fresh identities and ownership

A Production rollout creates a new Project-owned `DataVersion`,
`MigrationRun`, and setup `MigrationWorkspace`. These identities differ from
the qualified Test run and its workspaces. The DataVersion owns the complete
immutable latest-data package. The setup workspace holds only its working
source projection, target configuration, and role-separated vault entries.

The setup state grants no Odoo write authority. It pins one current Project
cutover selection, its authenticated qualification, and the exact immutable
CutoverPlan revision.

## Activation boundary

Activation requires all of the following:

- the Production DataVersion is frozen with authentic complete package
  evidence;
- the selected plan, Recipe revisions, dependencies, semantic requirements,
  shared controls, and field-level write owners remain exact;
- current source bindings, delivery parameters, and controls are covered;
- current Odoo 19 schema and supporting references satisfy the plan;
- the Production connection target differs from the qualified Test target;
- read and write credential generations are distinct and target-bound; and
- the write probe proves readable and writable access for the bounded plan
  models in the reviewed company and language context.

Successful activation creates one isolated RecipeApplication workspace per
plan item through the same compiler and six-stage engine used by Integrated
Test. It copies no Test mapping draft, comparison, approval, execution,
read-back, reconciliation, credential, or source row.

The application validates all delivery answers using the Recipe parameter and
control contracts. Export date is derived from the Production DataVersion;
invariant expectations remain fixed by the Recipe. Required delivery totals
must be finite numbers. Browser answers identify the reviewed plan and delivery
so an old form cannot silently apply its fields to different meaning.

## Execution authority and invalidation

Before a Production Odoo writer is constructed, the browser repeats the
dependency guard and Production authority guard. The current selection,
qualified plan, frozen DataVersion, target identity, read principal,
permissions, context, and write principal, permissions, and context must match
activation. Its saved activation operation must also be `COMMITTED`. The current read generation must match the fresh comparison
snapshot. A replacement key for the same evidenced identity can therefore be
used after fresh comparison and a fresh write probe; changed identity or
context requires a new Production setup.

Changed selection or plan meaning requires a new Production run. Credential,
target, schema, parameter, control, comparison, or approval drift invalidates
the evidence it owns. A read key is never substituted for a write key. The
browser rejects the same submitted secret for both roles without persisting a
secret-derived verifier.

Unknown write outcomes retain the existing execution journal and must be
reconciled before retry. Source absence never infers delete, archive, or write.

## Persistence and recovery

`ProductionRunBinding` stores only Project, run, DataVersion, setup workspace,
selection, qualification, plan, target, credential-generation, write-identity,
parameter, control, and activation hashes. Secrets remain in the local vault
under the setup workspace and exact target role.

Setup and activation use separate restart-safe operation intents. Registry
activation is atomic. Application stores are created afterward and compiler
results are committed last. A retry resumes the immutable stored intent after
a registry or store fault. Reusing an operation identity with different
authority or values fails closed.

The active binding marks registry publication, not completion of compiler
materialization. Readiness presentation and application preparation must wait
for the operation's final commit. Project status must distinguish completed
setup from a completed Production run.

New activation intents retain versioned canonical parameter/control answers
and the observed non-secret write identity alongside the existing schema,
reference, and credential-generation evidence. Resume belongs to the original
actor and operation. It revalidates current qualification and plan meaning,
reuses completed mappings, and grants no new Odoo write permission. Browser
resume requires session authentication and CSRF protection; it does not probe
Odoo or restore vault secrets. Older pending intents without reconstructable
inputs direct the operator to fresh setup. Committed older operations remain
readable without migration.

## Performance contract

Project overview reads all Production bindings with one bounded registry
query. Setup completion adds one Project-scoped registry query without loading
intent payloads or opening child stores. Resolving a setup or application credential owner uses one registry
query. Activation performs one plan-level schema/reference review and one
bounded write-identity probe; it must not add an Odoo call per Recipe or per
source row. Each application receives a filtered immutable target projection
and source dataset references without source copying.

## Verification

- `tests/application/run/test_production_rollout.py`
- `tests/application/run/test_production_values.py`
- `tests/integration/web/test_production_readiness.py`
- `tests/application/cutover/test_qualification.py`
- `tests/application/run/test_integrated_recipe_runs.py`

## Related documentation

- [Production rollout developer workflow](../workflow/09-production-rollout.md)
- [Production rollout data-manager guide](../../user/guides/production-rollout.md)
- [Cutover plan lifecycle](cutover-plan-lifecycle.md)
- [Execution and reconciliation](execution-and-reconciliation.md)
