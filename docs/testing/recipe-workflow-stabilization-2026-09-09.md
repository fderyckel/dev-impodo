---
audience: developer
kind: report
status: current
---

# Recipe workflow stabilization implementation

This report records the first implementation pass following the
[Recipe workflow review](recipe-workflow-review-2026-09-09.md). It is for
developers deciding what is ready to review and what still needs end-to-end
evidence. The changes preserve Project, DataVersion, Recipe, run, and isolated
application ownership. They do not constitute full workflow acceptance.

The next implementation phase is recorded in the
[delivery control expectations report](recipe-workflow-phase-2-2026-09-09.md).
Production readiness and restart recovery are recorded in
[phase 5](recipe-workflow-phase-5-2026-09-09.md).

## Implemented changes

| Problem | Current behavior |
| --- | --- |
| Preparation omitted Recipe business checks. | Both composition roots supply the quality seed repository. Preparation requires the seed for a Recipe application, repairs cached rulesets that omitted its checks, and preserves those checks during run-local rule editing. |
| Target decisions detached quality rules from the mapping. | Mapping confirmation rebinds the same rules after checking that the decision preserves the compiler baseline. Missing or stale evidence blocks preparation. |
| The full matcher could change pinned Recipe meaning. | Submission and application confirmation reject changes to providers, transformations, identities, relationship structure, write ownership, and invariant expectations. The matcher explains the permitted scope. |
| Interrupted activation could omit its CutoverPlan. | The operation commits after the required plan binding exists. Browser recovery finds the original operation and resumes using its saved target evidence. Completed application mappings remain intact. |
| A failed compiler attempt lost its saved draft or appeared complete. | Validation findings keep their specific codes and mapping identity. Incomplete compiler attempts retain the local draft and leave activation pending. |
| Resume navigation used older session progress in preference to later saved work. | Shared application functions own ordering, durable completion, attempt relevance, and resume steps. Comparison and verification reopen their corresponding pages. Reconfirming an unchanged mapping preserves its later milestone. |
| Preparation attempts could advance changed mappings. | New requests carry the mapping hash; the worker checks it before reading source rows, and stale notifications cannot advance the changed application. Direct preparation commands enforce the same dependency order as the run page. |
| Progress repeatedly reloaded the entire page. | Polling updates messages and percentages in place. Changes to state, actions, and issues still refresh the page. Pending Odoo decisions remain visible in the run stepper. |
| Planning repeated Recipe metadata reads. | Review uses one bulk revision read for the selection, with individual protected-envelope verification retained. Activation and focused target-value GET rendering run outside the async request thread. |

The current implementation is described in the paired
[developer workflow](../developer/workflow/07-integrated-test-runs.md) and
[data-manager guide](../user/guides/integrated-test-runs.md). The
[lifecycle contract](../developer/contracts/integrated-run-lifecycle.md)
records the activation and mapping invariants.

## Compatibility and operational limits

Workspace engine version 10 adds a nullable compiler-baseline column through
the existing transactional forward-upgrade mechanism. It does not rewrite
source rows or infer a baseline from mutable work. Older seeds retain their
rules for the original mapping. Changing an older application without a saved
baseline requires a new application of the pinned Recipe.

These fixes do not retroactively prove that earlier prepared or qualified
results evaluated Recipe business checks. Use a fresh Test run to obtain
evidence under the corrected implementation. Existing Odoo execution journals,
confirmations, results, and qualification history are retained.

The import inventory has been refreshed to the current reviewed graph. The
graph has no runtime import cycles or application-to-adapter imports. The
existing test-organization check still fails because
`tests/integration/web/test_mapping_workflow.py` contains 2,096 lines; that
same count is present at the baseline commit and this file was not changed.

## Verification scope

The focused stabilization suite passed 72 tests covering preparation workers,
mapping and quality evidence, activation recovery, schema upgrades, and
architecture and documentation checks. The broader lifecycle suite passed
40 tests covering Test runs, Production rollout, qualification, and target
decisions. A separate run passed the browser journey and five quality-store
tests. These are targeted suites with some overlapping coverage, not a full
repository test run.

Regression coverage includes real DuckDB seed persistence, allowed and
forbidden mapping changes, forward upgrades, actual web/worker composition,
quality evaluation of a violating row with an older cached ruleset, activation
faults before and after materialization, stale attempts, resume precedence,
and stable polling hashes.

Activation fault tests use the real lifecycle repositories and the existing
test compiler substitute. Quality tests exercise real rule evaluation and
real seed persistence, with controlled source and service collaborators.
The browser test exercises the actual request journey but substitutes Odoo
transport and its final activation result. These tests must not be described
as a complete migration against Odoo.

No live Odoo write, authenticated visual screenshot refresh, or end-to-end
throughput and memory benchmark was performed. The resource improvements are
specific reductions in repeated work, not a measured overall speedup.

## Remaining work from the review

1. Collect Recipe control expectations on Fresh data through a typed,
   revision-bound run-value contract. The current validation now retains an
   actionable draft; the full matcher still collects these values.
2. Replace the remaining full-matcher fallback with constrained run decision
   views, including unsupported scoped and composite target matching. The
   submission guard is in place, but the editor still displays authoring
   controls that a Recipe application cannot confirm.
3. Rebuild application milestones from durable workspace evidence when the
   final job notification was lost before a complete app restart. Shared
   ordering now refuses to release a dependency from session evidence alone;
   durable milestone delivery and reconstruction remain separate work.
4. Move the remaining enqueue orchestration and card-action policy out of web
   coordination, and align Production setup with the same run-owned decisions.
5. Add a representative, real-compiler journey through qualification and
   Production review, then measure elapsed time, peak memory, repeated reads,
   and restart behavior. Preserve explicit load confirmation and verified
   read-back throughout that work.

The existing bounded source processing, supporting-value capture, isolated
workspace stores, and qualification boundaries remain the basis for those
next changes.
