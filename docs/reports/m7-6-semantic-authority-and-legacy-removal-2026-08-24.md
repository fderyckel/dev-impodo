---
audience: developer
kind: report
status: current
---

# M7.6 semantic authority and legacy removal

**Historical evidence:** This dated report records the completed M7 closure.
Current architecture and lifecycle contracts own behavior.

## Outcome

M7.6 completes the semantic cutover. A Project is the governed migration
effort. It may contain no Recipe, one Recipe, or several Recipes. The Project
owns its complete DataVersions, runs, workspaces, and CutoverPlan. A Recipe
owns reusable immutable rule revisions. A MigrationWorkspace owns mapping,
preparation, review, and execution evidence for one bounded purpose.

Current code, tests, browser routes, persistence schemas, architecture,
contracts, workflow registration, and forward-looking plans now use those
owners explicitly. No Recipe acts as the Project root, and no workspace uses a
Project identity alias.

## Removed implementation history

The closure removed the retained mapping upgrader, accepted-version lists,
old combined-control payload, workspace Project aliases, unlinked-workspace
creation vocabulary, and obsolete Recipe-application workspace schema module.
Current mapping, normalization, quality, Recipe envelope, source snapshot,
prepared snapshot, and derived-value evidence contracts accept only their
exact current versions. Retired payloads and storage generations fail closed;
Impodo does not upgrade them in place.

Milestone-bound test modules, classes, fixtures, operation keys, temporary
roots, and launch tokens were renamed for durable capabilities. Tests that
prove retired payloads are rejected remain current contract tests; they do not
provide a compatibility path. Stale `WorkspaceState.project_id` reads found by
the Production tests were removed from the planning service, browser route,
and fixtures.

Registry, DataVersion, MigrationWorkspace, and workspace-engine databases now
declare exact capability-named generations. Earlier development generations
are intentionally incompatible and follow the existing development reset or
preservation path instead of being silently rewritten.

## Current documentation authority

ADR-014, the architecture overview, code map, lifecycle and evidence
contracts, paired workflow pages, glossary, product vision, BPMN guide, and
`docs/workflow.yml` describe the implemented Project-first model. ADR-013 and
dated implementation reports are labelled as historical evidence rather than
current authority.

`docs/plans/remaining-work.md` now contains only unfinished or explicitly
deferred work. Completed M7 delivery detail was removed. The separately owned
`browser-language-and-concept-help-proposal.md` remains untouched because it is
active design work, not stale implementation history.

The existing concept-help pages already explain the current relationships, so
M7.6 made no visible concept-help change and did not require replacement
screenshots.

## Bounded access and Odoo behavior

Workspace authorization resolves one bounded lineage row and authorizes the
parent Project before a child store opens. Verified background packets reuse
that result. The semantic closure adds no per-dataset, per-source-row, or
per-Odoo-record lookup; executable tests retain the one-read guard against
N+1 authorization access.

M7.6 adds no Odoo call, generic RPC method, or write surface. Existing Odoo 19
reads and guarded writes retain their exact target, credential, approval,
execution, and reconciliation boundaries.

## Verification

The following checks passed on 2026-08-24:

- Python compilation for `src` and `tests`;
- the repository documentation quality gate and all 7 documentation and code
  orientation tests;
- all 8 integrated multi-Recipe tests;
- all 102 exact mapping, Recipe envelope, connector, source/prepared snapshot,
  and Odoo-source boundary tests;
- all 33 Project foundation, DataVersion source-package, Project authoring,
  and exact workspace-schema tests;
- all 28 identity, canonical ownership, parent authorization, and workspace
  evidence-storage tests;
- all 5 integrated qualification tests;
- all 25 source-snapshot I/O and execution-snapshot tests;
- all 10 normalization tests, with the opt-in 25,000-row scale probe skipped;
- all 5 Production rollout scenarios across the final focused reruns.

The initial broad snapshot command encountered the known Windows temporary
directory access gate before product assertions. The same affected suites
passed outside that sandbox gate. `git diff --check` passed. The final scoped
semantic scan found no M0-M7 delivery identifiers in current source or tests;
the remaining `M2` matches are Excel cell addresses, and M5 references in
acceptance evidence identify Apple hardware.

## Closure

M7 is complete. Future changes must use the durable Project, Recipe,
DataVersion, MigrationRun, MigrationWorkspace, and CutoverPlan meanings. New
compatibility behavior requires an explicit architecture decision; it must not
reintroduce an alias or silently reinterpret retired evidence.
