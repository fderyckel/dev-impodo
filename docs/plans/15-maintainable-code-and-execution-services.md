---
audience: developer
kind: plan
status: implemented
---

# Maintainable code and focused execution services

## Decision and intended reader

Continue the delivered repository guardrails and use the execution service as
the first focused refactor. The [remaining-work roadmap](00-remaining-work.md)
requires this supporting work to accompany the product change that needs the
boundary. The aim is to make a change easy to locate,
review, test, and recover from as Impodo grows.

This plan is for maintainers and contributors deciding how to organize future
changes. The repository quality commands, pinned Ruff and Basedpyright policy,
dependency repair, and complete delivery gate are current. The execution and
reconciliation extractions described below are implemented and locally verified.
The existing
[code-organization guide](../architecture/code-organization.md) remains the
authority for ownership and dependencies.

Keep the current layered application. Improve boundaries inside the existing
packages before introducing another framework, deployment unit, or generic
service abstraction.

## Implementation on 17 November 2026

The service now delegates shared identity and API scope contracts, structured
snapshot validation, full review projections, durable journal transitions,
payload resolution, relationship completion and recovery classification.
Reconciliation consumes public domain contracts instead of private service
helpers. Public execution use cases retain current approval, target,
credential and recovery checks.

An operation-owned progress accumulator initializes from the durable journal
and updates after successful row transitions. Intermediate publications carry
immutable counters and phase information without journal rows. The browser job
projection consumes those counters directly. An external stop can replace row
states outside the worker; that exceptional boundary reconciles its final
counters from the saved journal once. It does not rebuild intermediate updates.

The repository gate checks newly introduced private imports and obvious runtime
replacement. Size and complexity findings remain advisory. There is no
Basedpyright baseline. Work integrates directly on `main`, with one fixed
candidate checked before delivery and hosted jobs kept manual.

The pre-refactor committed service contained 4,657 physical lines. File size is
supporting evidence; explicit ownership, preserved outcomes and bounded
publication work remain the acceptance criteria. The following sections retain
the design and historical evidence. The verification record distinguishes
local tests from platform and live Odoo qualification.

### Reviewed cohesion decisions

The repository maintainer owns the following advisory findings. These are
cohesion decisions, not exceptions to dependency, typing or write-safety rules.
Review them when a named trigger occurs rather than splitting a closed
algorithm solely to reduce its line count.

| Symbol | Reason to keep the algorithm together | Regression evidence and review trigger |
| --- | --- | --- |
| `ExecutionService` and `_continue_run` | Current approvals, credential bindings and exceptional stop handling remain application authority. | Service, continuation and request-admission tests; review when an entry condition or stop path changes. |
| `LoadWriter.run` | One coordinator owns transport ordering and the decision to stop after an uncertain outcome. Journal transitions, values and relationship completion have separate owners. | Service, transport and journal-transition tests; review when another transport mode or write stage is added. |
| `resolve_identity_crosswalk` | One operation deduplicates the exact nested identity frontier before bounded reads. | Identity lookup and scoped-transfer tests; review when a new identity shape or lookup policy is admitted. |
| `RelationshipCompletion.ensure_projected_receipts` and `apply_deferred_relationships` | Each operation retains its exact receipt checks and journal checkpoints. | Partial-cycle, projected-receipt and unknown-completion service tests; review when receipt or completion semantics change. |
| `row_values` and `_import_relation_value` | Payload construction keeps scalar, protected and reviewed relationship intent in one deterministic field walk. | Local/remote service and scoped-transfer tests; review when another field action or relationship operation is supported. |
| `targeted_recovery_row_ids` and `classify_recovery` | The dependency frontier and closed recovery transition matrix remain inspectable without I/O. | Domain recovery and reconciliation tests; review when evidence, journal states or retry authority changes. |
| `execution_api_scope` and structured validation predicates | Exact frozen capabilities and ordered issue classification remain pure domain decisions. | Identity/hash and validation tests for both supported Odoo majors; review when the snapshot contract changes. |
| `project_load_progress` | One projection preserves the existing phase, count and pause rules. | Load-job and compact-progress tests; review when the browser progress contract changes. |

## Historical evidence and priorities

The 2026-10-28 checkout review supersedes the original guardrail priorities:

- `scripts/check_python_quality.py` and `scripts/check_repository_quality.py`
  are pinned, documented, and run in continuous integration.
- The fallout-workbook application service now depends on its consumer-owned
  port instead of importing the concrete artifact adapter.
- The main execution service has grown to 4,008 physical lines, and
  reconciliation still imports `_identity_domain` and `_portable_key` from
  that service. The shared contract extraction remains open.
- Load progress still publishes an `ExecutionRun` containing the complete row
  tuple at intervals. The compact progress value proposed below remains open.

That review established the extraction order: shared identity and scope,
compact progress, then cohesive write and recovery operations.

The working-tree review on 2026-09-14 found the following conditions. These are
historical point-in-time observations, not current size baselines.

| Observation | Maintenance consequence |
| --- | --- |
| The execution `service.py` has 3,523 physical lines. `_continue_run` occupies 494 lines and `_execution_snapshot_error` occupies 354. | Scheduling, validation, recovery, write coordination, and presentation have accumulated in one place. A change requires understanding several responsibilities. |
| Reconciliation imports `_identity_domain` and `_portable_key` from the execution service. | Private implementation helpers have become shared contracts without a clear owner. |
| Progress publication copies the complete row-reference collection, and the job projection scans the rows again. | Publishing less frequently reduces work, but repeated complete scans can still grow quadratically for a fixed batch size. This is a code-level complexity observation, not a measured latency result. |
| The import inventory contains 447 modules while its reviewed baseline contains 431. | The inventory check fails and needs a review of the actual module and dependency changes. |
| `fallout_workbook_service.py` imports `adapters.artifacts.fallout_workbook`. | The dependency-direction check fails because application code depends on a concrete adapter. |

No runtime monkey patching was found in the reviewed execution service. Its
local callback and `dataclasses.replace()` calls are ordinary Python
composition. The primary concern is responsibility growth and coupling.

The first historical priority was to restore meaningful architecture checks.
That work is complete. The remaining plan starts with the execution contracts
described above.

## Coding practices to adopt

### Give each responsibility a clear home

Name the owner of state and the reason a module changes. Put portable
decisions in the domain, use-case coordination in the application, external
I/O in adapters, and browser wording in presenters. Follow the existing
[placement procedure](../architecture/code-organization.md#placement-procedure).

An application service should expose a small set of related use cases and
coordinate focused collaborators. It should not acquire every helper used by
its workflow. Keep a cohesive algorithm together when splitting it would hide
its invariants. Avoid replacement files named only `helpers`, `utils`, or
`common` that recreate the same unclear ownership.

### Make dependencies and contracts explicit

Inject external capabilities through narrow, consumer-owned ports. Construct
concrete adapters in composition. Extract a shared helper only when its shared
meaning is clear; give it a public name and a capability owner.

Use typed parameters and return values at public boundaries. Prefer named
immutable values for plans, results, and evidence. Use explicit mutable state
only where one operation owns it. A collaborator should receive the values
and ports it needs, rather than the entire service or a generic context object.

Access fields directly on known typed models. Handle older persisted formats
in their versioned decoder, where compatibility can be tested. Avoid spreading
`getattr(..., default)` fallbacks through business code to hide missing fields.

### Keep runtime behavior inspectable

Prohibit production code from replacing methods on existing classes or
instances, altering imported module functions, or changing process-global
behavior to add a feature. Prefer an explicit collaborator, adapter, or
callback. Test doubles should normally use those same boundaries; a scoped
test patch is acceptable when it is restored and the test needs that boundary.

Add a variation where its decision belongs. Repeated feature flags and mode
branches are a review signal that a named policy may be needed. Do not create
a class for every conditional or duplicate a workflow for each Odoo model.

Catch errors at the boundary that can classify or recover from them. Preserve
the distinction between rejection and unknown write outcome. An observation
failure must not silently change the meaning of durable execution evidence.
Write comments and docstrings for invariants and tradeoffs that are not clear
from names and types.

### Treat performance and recovery as part of correctness

Preserve transaction boundaries, deterministic ordering, journal-before-write
behavior, and exact recovery evidence during extraction. Keep current batching
and query bounds unless a separate behavior change justifies a different limit.

Name the expected work in a data-dependent loop. Test call counts and rows
visited where a regression could introduce repeated complete scans or one
lookup per row. Use elapsed-time measurements for representative qualification;
do not make ordinary tests depend on a particular developer machine's speed.

## First refactor: execution

Preserve the public `ExecutionService` use cases while moving responsibilities
to their existing layer and capability. The following table defines proposed
homes, not a requirement to create one class or file for every row.

| Responsibility | Proposed home and boundary |
| --- | --- |
| Authorization, current workspace and target bindings, start and resume entry conditions. | Keep a thin coordinator in `application/workspace/execution/service.py`. |
| Pure snapshot-shape, precision, schedule, and scope decisions. | Use focused functions under `domain/execution`, returning structured issues. Keep workspace and credential lookup in the application. |
| Portable identity keys and shared scope derivation. | Give pure functions explicit public homes under `domain/execution`, reusable by execution and reconciliation. Keep Odoo wire-value encoding in `adapters/odoo`. |
| Preview facts, blocker counts, and bounded group summaries. | Use a focused application query module. Let `web/presenters` own visible labels and guidance. Reuse the existing navigation projection boundary. |
| Prepared create batches, updates, relationship completion, and their journal checkpoints. | Use focused application collaborators under `application/workspace/execution`. Keep one coordinator responsible for ordering and stopping after an uncertain result. |
| Classification of supplied recovery evidence. | Separate pure classification from the application checks that authorize resumption and atomically record recovery. |
| Runtime progress. | Use a typed, compact application progress value and an operation-owned accumulator, consumed by `load_jobs.py`. |

Migrate private helper imports explicitly. Update their callers and tests in
the same change. Avoid forwarding arbitrary attributes or adding permanent
compatibility wrappers merely to make a file move appear smaller.

### Make progress independent of journal size

First extract existing publication behavior without changing it. Then make the
performance change in a separate review:

1. Initialize aggregate counts and component state once from the durable run,
   including when a run resumes.
2. After each successful journal transition, update counts from the previous
   and new row states. A row becoming committed after being partially applied
   must not count as a second create.
3. Publish a compact immutable value containing run identity, phase, counts,
   and the active group. Intermediate updates should not carry all journal rows.
4. Publish terminal state and meaningful phase boundaries immediately. Define
   throttling in the progress collaborator instead of scattering it through
   write logic.
5. Keep the durable journal authoritative. Progress publication cannot authorize
   writes or decide whether a row is safe to retry. A fresh process reconstructs
   progress from the journal.

Migrate callback consumers together. If a temporary compatibility adapter is
necessary, give it an explicit removal step; it must not rebuild the complete
run for every new compact update.

Preserve the existing
[execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md),
including file-source and Odoo-source entry conditions, target and credential
bindings, External IDs, receipt barriers, and recovery after an uncertain call.
Keep persisted formats and semantic hashes stable during structural changes.

## Checks that support daily development

Provide one documented local command that runs the same selected checks as
continuous integration. Pin new development tools and configuration in the
repository. The current
[Python quality gate](../developer/reference/python-code-quality.md) pins Ruff
and Basedpyright, prevents new lint and type debt in `src/impodo`, and runs the
same command locally and in continuous integration. The remaining rows retain
the broader checks that this proposal still needs to compose.

| Check | Proposed enforcement |
| --- | --- |
| Existing dependency direction, runtime cycles, composition ownership, and import inventory. | Require passing results before merge. Review deliberate inventory changes with their code changes. |
| Existing documentation links, workflow ownership, and code-documentation checks. | Require passing results for affected changes and retain repository-wide checks in CI. |
| Formatting and correctness lint rules. | Ruff now enforces its pinned lint rules across `src/impodo`. Repository-wide formatting remains deferred so a mechanical rewrite stays separate from behavior changes. |
| Static typing. | Basedpyright checks `src/impodo` in Standard mode and reports every diagnostic. No baseline or blanket ignores hide existing debt. |
| New imports of another module's private symbols and obvious runtime patching. | The complete gate compares a fixed review base with the candidate and rejects introduced findings. Existing findings remain visible. Static detection supports code review; it cannot prove the absence of all dynamic mutation. |
| Focused behavior, adapter, and bounded-I/O tests. | Require the tests relevant to the changed responsibility. Run broader regression suites for changes to shared execution, persistence, or composition. |
| Complete tests and supported-platform qualification. | Keep full discovery in release validation and run it in merge CI where practical. Keep opt-in live Odoo checks separate and report whether they ran. |

Start with advisory review thresholds of 500 physical lines per production
module, 80 per function, and cyclomatic complexity above 15 in a function.
Report changed code crossing these thresholds and growth in existing oversized
code. These are proposed triage values, not evidence of a defect or targets
for compressing code. Exclude generated code from size triage; review large
test fixtures separately. Calibrate the thresholds after the first refactor.

Hard checks protect behavior and dependency rules. Size findings prompt a
cohesion decision. A documented exception should name the symbol, reason,
responsible maintainer, regression evidence, and review trigger. Do not use a
size exception to waive a forbidden dependency or weaken a write invariant.

## Delivery sequence

Each step should be independently reviewable and pass its relevant checks.
Split a step into smaller delivery candidates when it changes several contracts.
The author owns implementation and evidence; the capability maintainer reviews
ownership and behavior. Assign named people when scheduling the work.

| Step | Deliverable | Completion evidence |
| --- | --- | --- |
| 1. Restore architecture checks — complete. | The fallout-workbook adapter dependency was replaced by a consumer-owned port, and the architecture checks are part of the repository gate. | The inventory and dependency suites pass without weakening their rules. |
| 2. Establish a repeatable check command — complete. | The pinned Python and complete repository quality commands run locally and in continuous integration. | A clean checkout can run the documented commands; deliberate violations fail the gate. |
| 3. Extract pure and display responsibilities — implemented. | Shared identity and scope functions, structured validation and review queries have explicit owners. | Focused tests preserve results, messages, scopes, and hashes. Reconciliation no longer imports private service helpers. |
| 4. Separate progress — implemented. | Compact progress, transition counters and browser consumers have separate owners. | Tests cover resume, partial-to-committed transitions, phase boundaries, errors, external stops and increasing-size row-visit bounds. |
| 5. Reduce execution coordination — implemented. | Scheduled transport coordinates journal, value, identity and relationship collaborators. Pure recovery classification is in the domain. | Failure and recovery tests cover journal-before-transport, stop-after-unknown behavior, batch bounds and duplicate-write prevention. |
| 6. Apply the practice elsewhere — ongoing. | Review the most frequently changed modules with similar coupling and agree the next focused improvement. | New work uses established owners; documented exceptions and architecture failures do not accumulate unnoticed. |

Structural changes and algorithm changes should use separate commits or pull
requests. Keep the service callable throughout the sequence so each step can
be reverted without requiring a data migration. A code revert does not undo
Odoo writes; recovery continues to use the durable journal.

## Review and maintenance routine

For each pull request, explain the problem and resulting behavior, the owner
of the affected state, and any contract or dependency change. Include the
checks run and their results. For execution changes, explain effects on
transactions, retries, query counts, and write batches when relevant.

Add tests for meaningful missing behavior before moving risky code. Retain
tests of public outcomes and failure boundaries. Avoid adding tests solely to
assert a private extraction shape or to raise a coverage percentage.

Record an architecture decision when a change introduces a durable cross-owner
contract, persistence boundary, or dependency exception. Routine extractions
can explain their placement in the pull request. When a public path changes,
update the code map and registered workflow symbols with the implementation.

At a monthly maintenance review, examine the three most active modules with
cohesion concerns, recurring defects, flaky tests, and outstanding exceptions.
Choose a bounded improvement and a responsible maintainer. Prioritize work that
reduces repeated change cost; avoid a repository-wide rewrite or abstractions
for hypothetical future callers.

Review dependency updates regularly through the existing lock and release
workflow. Keep tool versions reproducible, inspect compatibility changes, and
run the affected adapter or platform checks before promotion.

## Acceptance and verification

The first delivery is complete when the architecture checks pass, execution
and reconciliation use explicit shared contracts, and the service delegates
validation, presentation, progress, and cohesive execution operations to their
owners. A presentation or progress-only change should no longer require editing
the write loop. File size reduction is supporting evidence, not the acceptance
criterion.

For the refactor, start with these existing suites:

```console
python -m unittest tests.architecture.test_inventory tests.architecture.test_dependency_rules tests.architecture.test_workspace_navigation_boundaries
python -m unittest tests.application.workspace.execution.test_service tests.application.workspace.execution.test_load_jobs tests.application.workspace.execution.test_reconciliation tests.application.workspace.test_transfer_execution
python -m unittest tests.integration.duckdb.test_execution_repository tests.integration.web.test_load_workflow tests.integration.web.test_transfer_load_routes
python scripts/documentation_quality.py --check
python scripts/code_documentation_inventory.py --check
git diff --check
```

Add the focused tests created by each extraction. Use the architecture and
integration suites for atomic-operation, bounded-I/O, seeded-order, and broader
structural checks.
The [load workflow registry](../workflow.yml) identifies additional affected
adapter and browser suites. Offline tests must not be reported as live Odoo
qualification.

Track whether changes stay within the intended owner, whether architecture
checks remain green, whether exceptions are resolved, and whether related
defects recur. For progress, require one initialization scan and subsequent
work proportional to changed rows and transitions; verify that bound with
instrumented tests at increasing row counts. Record latency measurements
separately with dataset shape and environment.

## Implementation verification record

On 17 November 2026, a fixed candidate based on
`c1db91f449e2f935da4d65f0a55f8cada8c31f21` passed the complete repository gate.
Ruff, Basedpyright, compilation, changed-code maintainability, all 106
architecture tests, documentation checks and whitespace checks passed.

The same candidate passed 411 focused tests and 384 parameterized subtests
covering domain execution, application execution, scoped and aliased transfers,
journal persistence, load and recovery routes, operator-error views, navigation
and preflight. Odoo 19 and 20, local and remote fixtures, Authoring and Recipe
worker paths were included. One third-party AnyIO pytest deprecation warning
was reported; it did not fail the suite.

Progress instrumentation at 100, 1,000 and 10,000 rows requires one initial
journal pass and prohibits subsequent row iteration during normal publication
and terminal completion. Separate tests cover the one-time external-stop
reconciliation, rejected-batch splitting and failed journal writes. Scope and
identity comparisons against the committed algorithms preserved exact values
and semantic hashes for both supported Odoo majors.

The schema contract test was brought into line with the concurrent, explicit
version 8 operator-error migration and now requires its private companion
table. The migration rollback and evidence-preservation tests passed.

This is local fixture qualification. Live Odoo, a browser screenshot capture,
real Windows timing, the Linux server matrix and hosted workflows were not run.
No live session was restarted. The recurring maintenance routine in step 6
continues; this implementation does not qualify a public release.

## Historical proposal verification record

The initial review reran the inventory and dependency suites on 2026-09-14:
four tests ran and the two failures described above remained. They are inputs
to step 1, not failures introduced by this proposal. This documentation change
does not implement the refactor, install tooling, or alter the reviewed baseline.

The documentation-quality and code-documentation commands passed, as did all
seven tests in their architecture suites. The tracked diff passed its whitespace
check. Browser, screenshot, live Odoo, and broader runtime validation were not
run for this proposal because it changes documentation only.

## Related documentation

- [Current code organization](../architecture/code-organization.md)
- [Python code map](../architecture/python-code-map.md)
- [Load into Odoo implementation](../developer/workflow/06-load-into-odoo.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Platform support and testing](../developer/reference/platform-support-and-testing.md)
- [Internal development and release](../developer/runbooks/internal-release.md)
