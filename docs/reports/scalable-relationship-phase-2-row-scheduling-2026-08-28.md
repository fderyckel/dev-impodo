---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 2 row scheduling — 2026-08-28

## Decision supported by this report

Phase 2 adds the smallest generic row-level contract needed to distinguish an
acyclic hierarchy from an actual relationship cycle. The data manager keeps
control of the selected datasets, rows, business keys, and optional mapped
fields. Impodo derives only the safe order and the optional fields that must be
finished after create.

This result supports starting Phase 3. It adds no Product or bill-of-materials
branch, no new aggregate, and no new persistence store. It does not raise the
related-data limit or authorize a Production load.

## Implemented outcome

`ExecutionSnapshot` contract version 5 owns the row dependency plan. Each
actionable `ExecutionRow` stores its deterministic ordinal and topological
component. Each incoming relational `FieldIntent` stores its resolved
dependency row identifiers and its `hard` or `deferrable` strength.

The embedded `RelationshipPlan` stores:

- the incoming edge count;
- ordered component membership;
- the exact owner fields that need relationship completion;
- deterministic blocker evidence; and
- a root hash over the relationship plan.

Row hashes include the schedule and field dependency evidence. The execution
snapshot semantic hash includes the complete relationship plan. Contract
version 4 snapshots are not rewritten; they become stale and require a new
**Check changes** result.

## Scheduling behavior

`schedule_dependencies` accepts only stable row identifiers, dense ranks, and
small dependency-edge values. It first uses a stable Kahn pass. It calculates
strongly connected components only for the unresolved residue and uses an
iterative traversal, so a deep hierarchy does not depend on Python recursion.

For each cyclic component, the scheduler:

1. Rejects the component when its hard-edge subgraph contains a cycle.
2. Otherwise chooses one deterministic order that respects every hard edge.
3. Cuts only deferrable owner fields that point backwards in that order.
4. Propagates an unusable dependency blocker to transitive consumers.

An incoming reference to a uniquely reviewed existing row is already
satisfied. An incoming reference to a new row becomes available after its
create receipt. A missing, duplicate, model-incompatible, blocked, or ambiguous
incoming row produces blocker evidence instead of falling back to source order.

## Execution adoption

`ExecutionService` validates the frozen relationship plan before it creates a
journal. It executes topological component layers and groups only independent
rows by dataset and compatible create shape. It omits the exact fields in the
snapshot completion list and applies only those fields after the planned
receipts exist.

This limited adoption is sufficient to make Phase 2 behavior real:

- An eight-row same-dataset hierarchy performs eight ordered create calls and
  zero relationship patches.
- A two-row optional cycle performs two creates and one exact relationship
  patch.
- A hard two-row cycle disables loading before journal or Odoo transport.
- The Product/BOM fixture still performs 64 bounded create calls, zero target
  lookups, and zero relationship patches for 627 rows and 1,125 edges.

Phase 3 still owns bounded existing-target crosswalk revalidation, fuller
receipt persistence between components, and removal of the remaining
per-record lookup risks outside these all-create fixtures.

## Verification evidence

The following checks passed on 2026-08-28:

- All 224 domain tests passed, with 2 optional tests skipped.
- The 70 focused scheduler, snapshot, execution-service, and dependency
  benchmark tests passed.
- Scheduler tests cover input permutation, a 2,000-row hierarchy without
  recursion, optional-cycle cutting, hard-cycle blocking, and transitive
  blocker propagation.
- Snapshot tests cover stable row order, resolved edge evidence, contract
  version 5 round trips, and relationship-plan tamper rejection.
- Execution tests prove exact optional-cycle completion and hard-cycle
  rejection before target I/O.
- Ruff passed for every Phase 2 code and test file.
- The architecture inventory records 366 production modules and 2,019 runtime
  import edges, with no runtime cycle, forbidden application-to-adapter edge,
  or unclassified production module.

One fresh-process benchmark sample on macOS with Python 3.12 recorded the
following current semantics. These measurements are regression evidence, not
the Phase 6 scale qualification.

| Shape | Rows | Edges | Create calls | Relationship patches | Snapshot bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Product and unit | 14 | 12 | 3 | 0 | 17,489 |
| Same-dataset hierarchy | 8 | 7 | 8 | 0 | 11,444 |
| Optional cycle | 2 | 2 | 2 | 1 | 5,180 |
| Product/BOM | 627 | 1,125 | 64 | 0 | 903,653 |

The full 939-test discovery run completed with 922 passing and 13 skipped. It
reported four failures: the architecture inventory needed the intentional
Phase 2 baseline update, two previously known size guards remain in untouched
mapping template and integration-test files, and an unrelated project-setup
journey expects copy that the current source page no longer renders. Focused
Phase 2 tests do not depend on those failures.

## Reproduction

```console
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/domain -t .

PYTHONPATH=src .venv/bin/python -m unittest \
  tests.domain.execution.test_dependency_scheduler \
  tests.domain.execution.test_snapshot \
  tests.application.workspace.execution.test_service \
  tests.performance.test_dependency_execution_baseline

PYTHONPATH=src .venv/bin/python \
  scripts/benchmark_dependency_execution.py \
  --runs 1 --shape all
```

## Phase 2 conclusion

The implementation now distinguishes row order from row cycles without taking
business choices away from the data manager. Acyclic relationships stay in one
write pass, optional cycles have one frozen completion list, and hard cycles
fail closed. The design remains generic enough for Product/BOM dependencies
while keeping the first delivery inside the existing execution snapshot.

An Odoo API key was not needed because Phase 2 changes the frozen planner and
its recording-adapter execution semantics. A disposable Odoo 19 database is
still required in Phase 6 to qualify captured required fields, permissions,
real request behavior, and read-back at representative scale.
