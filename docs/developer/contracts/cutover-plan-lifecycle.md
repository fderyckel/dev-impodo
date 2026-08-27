---
audience: developer
kind: contract
status: current
---

# Cutover plan lifecycle contract

## Scope

This contract governs Project CutoverPlan meaning, Test qualification,
rollout-candidate selection, persistence, and performance. The separate
[Production run lifecycle](production-run-lifecycle.md) governs use of that
selection with fresh rollout evidence.

## Ownership and meaning

A `CutoverPlan` belongs to one Migration Project and has immutable revisions.
One revision pins exact Recipe revisions, an acyclic dependency graph,
field-level write ownership, unioned Odoo requirements, and all Project shared
controls. A one-Recipe Project uses the same one-item plan.

The plan excludes source rows, DataVersion identity, Test run identity, target
identity, credentials, approvals, execution evidence, and Production
authority. Those are evidence for one plan use, not reusable plan meaning.

## Revision and qualification

Unchanged meaning reuses the current plan revision. Any change to a selected
Recipe revision, dependency, write owner, unioned requirement, or shared
control appends a new revision. A new revision has no qualification. Earlier
qualification remains immutable history and never transfers.

Integrated Test qualification requires one frozen complete Test DataVersion,
one exact run target, complete current evidence from every selected Recipe
application, dependency-order proof, and passing shared controls. Individual
application evidence cannot substitute for the integrated result.

## Selection and Production boundary

Qualification records evidence. Project cutover selection separately records
which exact qualified plan revision is the intended rollout candidate. Only a
qualification for the current plan revision can be selected.

Neither record authorizes Production. Production requires a fresh complete
DataVersion, independent target and credential bindings, comparison, approval,
execution, and reconciliation.

## Persistence and recovery

Full qualification evidence is immutable, canonical, authenticated, and
application encrypted under a Project-scoped key. The registry retains bounded
identity, count, hash, actor, time, and storage projections.

Plan binding, qualification, and selection use separate restart-safe operation
intents with optimistic Project revision checks. Exact replay returns the same
result. Reusing an operation identity with changed meaning fails closed.

## Performance contract

Project and run status pages use registry projections and open no application
workspace. An explicit qualification review may read each selected application
once. It performs no Odoo call and must not issue repository or Odoo work per
source row.

## Verification

- `tests/application/cutover/test_qualification.py`
- `tests/application/run/test_integrated_recipe_runs.py`

## Related documentation

- [Integrated Test run lifecycle](integrated-run-lifecycle.md)
- [Execution and reconciliation](execution-and-reconciliation.md)
- [Production run lifecycle](production-run-lifecycle.md)
- [Developer workflow](../workflow/08-integrated-qualification.md)
- [Data-manager guide](../../user/guides/qualify-integrated-test.md)
