# Recipe Phase R4.5 clean-root implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014, current architecture, and
lifecycle contracts own behavior.

## Outcome

R4.5 leaves one Recipe-native product model before Production rollout work
begins. Creating a Recipe now creates its Recipe identity, first authoring
DataVersion, and contained workspace from the first durable write. There is no
standalone-project shell, backfill, setup hydration, or bootstrap-adoption path.

The existing matching and downstream workspace screens remain in place under
their contained `/projects/{workspace_project_id}/...` routes. Recipe list,
creation, overview, qualification, cutover selection, and draft deletion are
Recipe-owned surfaces under `/recipes`.

## Removed architecture

- project-to-Recipe shell backfill and lazy setup hydration;
- pending/abandoned DataVersion bootstrap states and intake markers;
- inert Recipe lifecycle state and intent retry counter;
- standalone project deletion and Recipe deletion intents/target enumeration;
- list and creation aliases at `/projects` and `/projects/new`;
- Phase 0 `ProjectSeries` fixtures, contract test, and superseded plan; and
- old template names and Recipe-deletion selectors carrying project ownership.

## Current recovery model

Initial creation journals the exact Recipe and DataVersion identities before the
contained workspace write and completes their registry registration on restart.
Later DataVersion creation provisions a fresh workspace, records parameter and
control evidence, and then adopts it through one exact intent. Startup retains a
provisional workspace only while an incomplete DataVersion-creation intent
references it; a true orphan is removed.

Recipe publication, DataVersion creation, qualification publication, and
cutover selection remain idempotent cross-store intents. Unpublished draft
deletion is direct and requires both exact Recipe and workspace revisions.
Published Recipe deletion is intentionally outside the current product surface.

Cutover selections are immutable history rows. Recipe stores only the current
selection pointer, so qualifying and selecting a later revision does not erase
the earlier choice.

## Verification

Focused regression covers native creation and distinct identities, schema
migration, protected publication, restart recovery, provisional orphan cleanup,
DataVersion sealing, append-only cutover history, direct Recipe draft deletion,
authorization, existing workspace behavior, and removal of list/create aliases.

Final verification passed:

- 684 repository tests passed, with 13 environment-dependent tests skipped;
- all changed Python files passed Ruff;
- bytecode compilation passed; and
- documentation quality and code-documentation inventory checks passed.
