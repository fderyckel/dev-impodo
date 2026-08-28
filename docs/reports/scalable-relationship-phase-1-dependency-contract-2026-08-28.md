---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 1 dependency contract — 2026-08-28

## Decision supported by this report

Phase 1 establishes one dependency meaning before Impodo adds row-level
scheduling. The data manager still chooses the datasets, source rows, business
keys, and optional relationship fields. Impodo classifies only the safety
constraint that determines whether a relationship must exist when Odoo creates
the owner row.

This result supports starting Phase 2. It does not make Product or bill of
materials loading a special case, add a second planner, raise a related-data
limit, or authorize a Production load.

## Implemented outcome

`DatasetDependencyEdge` is the immutable evidence for one incoming resolver.
It records the owner dataset, dependency dataset, target field, declaration
location, and `hard` or `deferrable` strength. It also identifies a
self-reference without discarding it.

`extract_dataset_dependency_edges` accepts both browser `DatasetMapping`
contracts and compiled `DatasetSpec` contracts. It sorts and deduplicates its
result, so changing source dataset order cannot change edge meaning.

The edge rules are:

- A target identity or target scope resolver creates a hard edge.
- A relationship creates a hard edge when its compiled contract or captured
  Odoo schema says that the field is required during create.
- Every other incoming relationship creates a deferrable edge.
- A target-catalog-only resolver creates no incoming dataset edge.
- A self-reference remains in the result for later row analysis.

The browser compiler normalizes a captured Odoo-required relationship into
`required_on_create`. The final preflight plan therefore cannot treat a field
as deferrable merely because an older browser mapping omitted that flag. New
browser form submissions also preserve the captured required constraint.

## Shared adoption

| Consumer | Current use of the canonical evidence |
| --- | --- |
| Browser mapping validation | The validator supplies captured required fields, checks unknown dependencies, and rejects hard cross-dataset cycles. |
| Profile and compiled-plan validation | `validate_dataset_graph` uses the same extractor and cycle rule. |
| Browser and profile compilation | `CompiledMigrationPlan.dependency_edges` exposes the canonical compiled edges. |
| Preflight planning | `PreflightRequirementPlan` records the edges in its version 3 semantic hash. |
| Execution-snapshot construction | The snapshot aggregates dataset dependencies from the same compiled edges before ordering datasets. |

The earlier dependency mutations inside identity and relationship validators
and the separate snapshot dependency collector were removed. No workspace
version selects between old and new interpretations.

## Cycle boundary

Hard cycles that cross datasets remain invalid. The cycle result is stable
because traversal uses canonical dataset names rather than input position.

A hard self-reference no longer fails only because the owner and dependency
have the same dataset name. Phase 1 retains that edge. Phase 2 must inspect the
actual source rows, order an acyclic hierarchy such as parent categories before
their children, and reject an actual hard row cycle. Until that row scheduler
exists, retaining the edge does not claim that a required same-dataset
hierarchy can complete a load.

This boundary preserves the data manager's flexibility. Impodo does not ask
the operator to maintain a graph or force every optional relationship into a
second pass. It also does not let an operator override an Odoo create
requirement.

## Verification evidence

The following checks passed on 2026-08-28:

- All 218 domain tests passed, with 2 optional tests skipped.
- All 109 selected mapping-form, review, preflight, target-reader, execution,
  and browser integration tests passed.
- Focused dependency tests cover hard, deferrable, identity, scope,
  relationship, self-reference, unknown-target, required cross-dataset cycle,
  compiler, preflight, snapshot-order, and dataset-permutation behavior.
- Python compilation passed for the changed domain and application modules.
- The reviewed architecture inventory includes the new domain module and still
  has no runtime cycle, forbidden application-to-adapter edge, or unclassified
  production module.

The tests use local immutable contracts and recording adapters. Phase 1 did
not require an Odoo API key because it changes no remote query or write shape.
Phase 6 still requires a disposable Odoo 19 database to qualify the captured
schema, bill of materials behavior, permissions, request counts, and read-back
against a real target.

## Reproduction

```console
.venv/bin/python -m unittest discover -s tests/domain -t .

.venv/bin/python -m unittest \
  tests.application.workspace.review.test_preflight \
  tests.application.workspace.review.test_odoo_comparison \
  tests.application.workspace.review.test_odoo_read_failures \
  tests.application.workspace.execution.test_service \
  tests.integration.odoo.test_target_readers \
  tests.integration.web.test_mapping_forms \
  tests.integration.web.test_mapping_workflow \
  tests.integration.web.test_review_workflow \
  tests.integration.web.test_execution
```

## Phase 1 conclusion

The dependency contract is now small, generic, and shared. The implementation
adds no BOM-specific branch and no new aggregate or persistence store. Phase 2
can build row edges and a snapshot-owned schedule from this evidence. If real
row shapes later expose exceptions that this contract cannot express, the
design can be revised from measured cases instead of adding abstraction now.

## Related documentation

- [Accepted scalable relationship dependency plan](../plans/scalable-relationship-dependency-planning.md)
- [Phase 0 dependency baseline](scalable-relationship-phase-0-baseline-2026-08-28.md)
- [Match data implementation](../developer/workflow/03-match-data.md)
- [Load into Odoo implementation](../developer/workflow/06-load-into-odoo.md)
- [Acceptance and test strategy](../testing/acceptance.md)
