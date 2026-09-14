---
audience: developer
kind: plan
status: proposed
---

# Maintainable code and focused execution services

## Decision and intended reader

Adopt a small set of enforceable coding practices and use the execution service
as the first focused refactor. The aim is to make a change easy to locate,
review, test, and recover from as Impodo grows.

This proposal is for maintainers and contributors deciding how to organize
future changes. It proposes delivery work; it does not introduce new enforced
rules or change current execution behavior. The existing
[code-organization guide](../architecture/code-organization.md) remains the
authority for ownership and dependencies.

Keep the current layered application. Improve boundaries inside the existing
packages before introducing another framework, deployment unit, or generic
service abstraction.

## Evidence and immediate priorities

The working-tree review on 2026-09-14 found the following conditions. These are
point-in-time observations, not permanent size baselines.

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

The first priority is to restore meaningful architecture checks. Repair the
forbidden adapter dependency through a consumer-owned port and composition.
Review the inventory changes before updating the snapshot. Regenerating the
baseline must not approve a forbidden dependency.

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
repository. The initial command should compose existing checks before adding
new tooling. Tool selection and CI wiring are implementation work under this
proposal; they are not configured by this document.

| Check | Proposed enforcement |
| --- | --- |
| Existing dependency direction, runtime cycles, composition ownership, and import inventory. | Require passing results before merge. Review deliberate inventory changes with their code changes. |
| Existing documentation links, workflow ownership, and code-documentation checks. | Require passing results for affected changes and retain repository-wide checks in CI. |
| Formatting and a small set of correctness lint rules. | Introduce a pinned formatter and linter, initially for the extracted modules. Separate mechanical formatting from behavior changes and expand coverage incrementally. |
| Static typing. | Check new ports and extracted execution modules first, including their boundary contracts. Expand the checked module set without adding blanket ignores to obtain a green result. |
| New imports of another module's private symbols and obvious runtime patching. | Add focused static checks with small, explicit exceptions for reviewed cases. Static detection supports code review; it cannot prove the absence of all dynamic mutation. |
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
Split a step into smaller pull requests when it changes several contracts.
The author owns implementation and evidence; the capability maintainer reviews
ownership and behavior. Assign named people when scheduling the work.

| Step | Deliverable | Completion evidence |
| --- | --- | --- |
| 1. Restore architecture checks. | Remove the fallout-workbook adapter dependency, review inventory drift, and restore the reviewed baseline. | The inventory and dependency suites pass without weakening their rules. |
| 2. Establish a repeatable check command. | Compose existing checks, add the selected formatting and typing scope, and make the command available to CI. | A clean checkout can run the documented command; a deliberate forbidden dependency is rejected. |
| 3. Extract pure and display responsibilities. | Move validation, shared identity and scope functions, and preview presentation to their owning modules. | Focused domain, application, reconciliation, and browser tests preserve results, messages, scopes, and hashes. Reconciliation no longer imports private service helpers. |
| 4. Separate progress. | Extract current publication behavior, then introduce compact progress and migrate consumers in a separate change. | Tests cover resume initialization, partial-to-committed transitions, phase boundaries, errors, and final counts. Instrumentation proves that intermediate publication no longer scans all rows. |
| 5. Reduce execution coordination. | Extract cohesive write and relationship operations, then recovery classification. Keep journal sequencing explicit. | Failure and recovery tests prove journal-before-transport, stop-after-unknown behavior, preserved batch bounds, and no duplicate writes. |
| 6. Apply the practice elsewhere. | Review the most frequently changed modules with similar coupling and agree the next focused improvement. | New work uses established owners; documented exceptions and architecture failures do not accumulate unnoticed. |

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

Add the focused tests created by each extraction. Use the existing
[regression baseline](../testing/code-organization-phase0-baseline.md) for
atomic-operation, bounded-I/O, seeded-order, and broader structural checks.
The [load workflow registry](../workflow.yml) identifies additional affected
adapter and browser suites. Offline tests must not be reported as live Odoo
qualification.

Track whether changes stay within the intended owner, whether architecture
checks remain green, whether exceptions are resolved, and whether related
defects recur. For progress, require one initialization scan and subsequent
work proportional to changed rows and transitions; verify that bound with
instrumented tests at increasing row counts. Record latency measurements
separately with dataset shape and environment.

## Proposal verification record

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
- [Acceptance and test strategy](../testing/acceptance.md)
- [Internal development and release](../developer/runbooks/internal-release.md)
