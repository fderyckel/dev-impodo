---
audience: developer
kind: report
status: current
---

# Recipe workflow phase 6: shared setup and lifecycle validation

This report records the implementation and validation of the next phase after
[Production readiness and recovery](recipe-workflow-phase-5-2026-09-09.md).
It helps developers distinguish shared workflow behavior from independent run
evidence and assess the remaining acceptance work.

## Changes

Test and Production now use one Fresh data router and template. `RunSetupService`
selects their separate application owners and supplies Recipe requirements,
saved answers, and logical table matches. Production derives its selection
from the qualified plan. `RunOdooRequirementsUseCase` builds the shared Odoo
scope without per-Recipe network calls. Production checks that scope before
opening readiness for the separate write key.

Production answers have their own immutable `ProductionRunValues` contract.
Registry version 7 adds a dedicated table without rewriting Test JSON or hashes.
The repository checks binding and answer revisions in one transaction, hashes
stored answers, and rejects changed answers after acceptance or activation.
Readiness displays accepted values without editable controls. The compiler uses
those values, and activation checks direct API requests against them.

The operator can select **Save run details** before accepting the files. Valid
answers persist; table choices and partial invalid entries are not autosaved.
The Test route and parameter contracts remain compatible with existing runs.
The sidebar and breadcrumbs use the Recipe journey. Production review stays
locked until activation finishes. A transient `fresh_href` naming error in
the sidebar, reported while this phase was in progress, is fixed and covered
by direct navigation tests for both run purposes.

The stronger lifecycle fixture exposed an actual access-reconnection defect.
Recipe application workspaces inherit schema evidence but do not complete
Authoring registration. `SchemaWorkspaceService.rebind_current_access` now
verifies existing live schema evidence without requiring registration or
reading source data. The run-aware adapter permits the local projection to
update access through its existing guarded transaction, preserving the frozen
run capture and mapping governance. It still rejects a closed workspace, changed target,
fields, permissions, principal, context, or permitted model scope. Initial
schema capture retains its registration and accepted-source requirements.

The mapping browser test module was split into workflow and default-review
modules. The preparation corruption assertion now matches the controlled
columnar-verification message while retaining checks for failed jobs, no
published prepared evidence, and no Odoo read.

The import inventory now contains 428 production modules and 2,500 runtime
import edges, with no cycles or application-to-adapter imports.

## Verification

The authenticated lifecycle test passed in 224.571 seconds. It publishes a
Recipe, accepts Test data with a 125.50 EUR control total, compiles and prepares
the actual application, approves its normalization, compares, loads, and
verifies the result. The resulting Test evidence qualifies the selected plan.
Production then accepts a separate 200.00 EUR delivery and uses the shared
Odoo check before activation.

The same test interrupts activation, restarts with session credentials removed,
resumes without repeating materialization or the write probe, and reconnects
current access. The Production application then completes preparation,
approval, comparison, execution, and read-back reconciliation through the
actual services. Assertions distinguish the Test and Production DataVersions,
targets, totals, and execution identities. Qualification is no longer supplied
by a replacement completed-evidence reader.

The HTTP portion covers authenticated setup, validation failures, readonly
answers, stale revisions, route ownership, and recovery. Approval, comparison,
load, and reconciliation use the application services directly. Only the
external Odoo metadata, identity probes, and record transport are fictional;
the record adapter retains the loaded rows for independent read-back.

The focused checks passed as follows. Some groups repeat architecture tests;
their counts should not be added as a count of distinct tests.

| Check | Result |
| --- | --- |
| Production values, shared Odoo requirements, journeys, and architecture contracts | 36 passed. |
| Forward upgrades, schema reconnection, and architecture boundaries | 30 passed. |
| Fresh data matching and control validation | 13 passed. |
| Existing Test Fresh data, corrupt-source preparation, stored-answer compatibility, and a moved mapping browser case | 4 passed. |
| Actual Test qualification through Production load and reconciliation | 1 passed. |
| Existing Production rollout and recovery cases | All 6 passed. |
| Direct Recipe navigation regression tests | 5 passed. |
| Documentation regression tests | 5 passed. |

The first combined contract run exposed an unregistered identity classification
and an old downgrade fixture that retained the new version-7 table. Both were
corrected and passed in the later contract and compatibility runs. The broader
repository suite was not run.

Current authenticated screenshots were captured with fictional data and
reviewed for the visible controls, readonly totals, and navigation:

- [Test Fresh data and Save run details](../images/user/03a-fresh-data-control-totals.png).
- [Production Fresh data](../images/user/05c-production-fresh-data.png).
- [Production Odoo requirements](../images/user/05d-production-odoo-check.png).
- [Production readiness and accepted values](../images/user/05a-production-readiness.png).
- [Interrupted Production setup](../images/user/05b-production-resume.png).

The workflow documentation checker and whitespace check passed. The final
breadcrumb-only correction has direct navigation coverage and refreshed
browser captures; the full lifecycle result above precedes that label change.

## Limits and next acceptance gate

The lifecycle scenario is a two-row customer balance Recipe with separate
Test and Production databases simulated by in-memory adapters. It is not a
live Odoo rehearsal or representative performance benchmark. Broader scenarios
still need real permissions, companies, relationships, multiple dependent
Recipes, concurrent browser actions, and representative data volumes.

Source acceptance spans the workspace source store and parent DataVersion
store. Answer revision checks do not make the entire browser acceptance action
one atomic database transaction. Concurrent acceptance remains a separate
stress-test boundary. No automatic partial-form or table-choice persistence
was added.

Large-plan activation still runs synchronously in a worker thread. The selected
run can still read its full activation intent for recovery status. No aggregate
speed or memory improvement is claimed without measurements. The next gate is
a controlled Odoo rehearsal followed by realistic timing and peak-memory
measurement, with targeted changes driven by those results.
