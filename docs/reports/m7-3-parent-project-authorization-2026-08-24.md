---
audience: developer
kind: report
status: current
---

# M7.3 parent-Project authorization

**Historical evidence:** This dated report records one completed delivery
slice. Current architecture and lifecycle contracts own behavior.

## Outcome

M7.3 makes the Project the enforced authorization boundary for every
authenticated workspace browser request. A caller may supply a workspace UUID,
but Impodo resolves that workspace's verified lineage and checks membership and
capability against the genuine parent Project before route code opens a child
store or external boundary.

At the M7.3 gate, the retained mapping engine still used a temporary
`project_id` alias. M7.4 subsequently removed that persistence and payload
alias, and M7.5 completed the route, view-model, job-key, and test-language
cutover.

## Request boundary

`WorkspaceAccessMiddleware` runs inside the authenticated session boundary and
before every `/workspaces/{workspace_id}` route. It asks
`WorkspaceAccessService` for one `WorkspaceAccessContext` containing the exact
Project, workspace, DataVersion, MigrationRun, and optional Recipe application
identities.

The resolver performs one bounded registry read, verifies the lineage, and
authorizes the requested capability against the parent Project. The middleware
binds that immutable result to the request. Later workspace services reuse it
for their exact capability checks; they do not reopen the registry. Attempting
to change workspace identity within the bound request fails closed.

Missing, malformed, wrong-kind, mismatched, and inaccessible workspace
identities produce the same opaque `404 Workspace not found` result. A caller
who can view the Project but lacks the capability for a specific command
receives a safe `403 Not authorized` result.

## Application and external boundaries

At this gate, the composition root supplied workspace-local application
services with a temporary `WorkspaceScopedAuthorizationPolicy`. It interpreted
the retained workbench argument only as a workspace ID and checked the
requested capability against the verified parent Project before the service
opened its repository. M7.4 subsequently removed this adapter; current
services use the exact workspace authorization port directly.
Project-, DataVersion-, run-, Recipe-, and CutoverPlan-owned services retain
their true Project-scoped authorization policy.

Credential, comparison, reconciliation, source-capture, local-stack, and
protected-evidence routes resolve their exact command capability before the
corresponding vault, artifact, or Odoo action. Protected manifest and workbook
streams carry and rebind the verified context because their response body may
be read after ordinary route dispatch has returned.

Preparation already carried an exact `PreparationWorkspace` packet. M7.3 adds
the same verified `WorkspaceAccessContext` to Odoo capture and confirmed load
jobs. Each manager rejects a packet whose workspace ID differs from the job
key before queueing. The worker binds the packet once around the whole command;
per-row execution never resolves authorization.

## Adversarial gate

The focused authorization tests create two Projects with distinct DataVersion,
run, and workspace lineages. A hosted-style policy permits one actor to use
Project A only. The tests prove that:

- the actor can resolve and use Project A's workspace;
- knowing Project B's workspace UUID does not permit a route or service call;
- denial occurs before the workspace child repository or route boundary runs;
- missing, wrong-kind, malformed, and unauthorized identities return the same
  opaque browser result;
- one allowed request performs one registry resolution and reuses the result
  for later exact capabilities;
- a bound request cannot switch to another workspace; and
- Odoo capture and load managers reject mismatched packets before queueing.

## Verification

The following checks passed on 2026-08-24:

- the then-current identity, workspace-access, canonical-owner, load-job, and
  Odoo-capture-job gate — 25 tests at this slice. The first three modules now
  use `tests.test_identity_semantics`, `tests.test_workspace_access`, and
  `tests.test_canonical_ownership`;
- the confirmed-load background browser test;
- the load-progress browser test;
- the repaired representative mapping browser test;
- the registered local-Odoo recovery browser test; and
- the local manual-schema browser test.

The project-root and local-browser security suite passed eight tests. The
three browser checks for distinct local-stack inspect, start, and stop
capabilities also passed. The documentation-quality script and its five unit
tests passed, as did Python compilation and `git diff --check`.

The first full `tests.test_web_app` run completed 60 tests and exposed stale
M7.2 fixtures rather than an authorization failure. The affected fixtures used
an invalid placeholder source catalogue, bypassed the run-owned target setup,
used a test-only registration event that did not advance the canonical
workspace, and assumed UUID-backed catalogue order. Those fixtures were
corrected and the representative failed scenarios then passed.

A second full browser run did not return before the 15-minute command limit.
It is recorded as a timeout, not as a passing suite and not as a product
failure. The focused M7.3 gate and targeted browser integrations above are the
positive evidence for this slice.

## Subsequent boundary

M7.4 subsequently removed the workspace-engine `project_id` alias, renamed
workspace-owned evidence and persistence to `workspace_id`, split artifact
ownership by its real owner, introduced run-owned shared evidence types,
bumped exact contracts, and rejected old or mixed shapes without mutation.
