# Recipe-first Phase R1 persistence implementation report

## Outcome

Phase R1 completed on 2026-08-19. Impodo now has a first-class Recipe
persistence root around the existing contained project workflow. Every
registered project is represented by an independent Recipe and DataVersion,
while current project URLs and mature workspace screens continue to work
through explicit identity resolution.

This phase deliberately does not add the final Recipe-oriented creation and
history UI. That work begins in Phase R2. Matching remains the established
workspace experience; later Recipe application work should add only the
context and focused drift issues it needs.

## Implemented boundary

- The registry has checksum-pinned migration history and bounded tables for
  Recipe, immutable RecipeRevision manifests, DataVersion lineage,
  applications, qualifications, cutover candidates, intents, and deletion
  targets.
- Registry-only backfill gives every existing project a one-DataVersion Recipe
  without opening the contained project database. New project registration
  creates the same shell.
- Recipe, DataVersion, and workspace project UUIDs are independent. Resolution
  rejects an identifier from one namespace when it is supplied as another.
- Opening a legacy workspace hydrates only business purpose, classification,
  and retention into the Recipe projection, then persists exact Recipe and
  DataVersion linkage locally.
- The exact base project schema remains version 1. A checksum-pinned additive
  workspace migration adds local linkage, seal, and application-draft tables.
- Activating a successor DataVersion seals its predecessor in both the
  registry and its local workspace. Existing mutation services fail closed on
  a sealed workspace.
- Immutable Recipe and qualification payloads are stored beneath a contained
  Recipe root with AES-256-GCM, authenticated context, atomic replacement,
  restrictive permissions, a four-MiB payload cap, and one Recipe-scoped key
  held through the existing secret-store boundary.
- Publication revalidates exact envelope version and fields, both content
  hashes, the semantic field set, forbidden operational keys, embedded UUIDs,
  and portable numeric Odoo identifier rules before storage.
- Publication, DataVersion creation, qualification, cutover selection, and
  Recipe deletion enumeration use optimistic, restart-safe intents. Startup
  recovery deterministically completes committed work or abandons a missing
  protected payload.
- Qualification binds one exact applied application, Recipe revision, Test
  target binding, execution, read-back, and reconciliation evidence. Cutover
  selection accepts that exact qualification but grants no Production
  authority and carries no credential.
- Legacy project deletion remains available only for an unpublished bootstrap
  Recipe with one DataVersion. A reusable or published Recipe is tombstoned and
  its exact project, protected-key, and registry target set is persisted before
  any later destructive executor is allowed to act.

## Compatibility and UI decision

The UI-continuity requirement is selective. Phase R1 resolves existing
`/projects/{workspace_project_id}` routes through Recipe and DataVersion
identity, so the six familiar workspace stages do not need a premature
redesign. Phase R2 may refactor landing, creation, overview, revision history,
and publication because those tasks genuinely move to Recipe ownership.

## Verification

Focused automated evidence covers:

- registry migration, backfill, bounded listing, and identifier confusion;
- encrypted round-trip, path containment, plaintext absence, tamper detection,
  and missing-payload behavior;
- runtime rejection of nonportable Recipe envelopes;
- crash recovery after protected publication and after DataVersion registry
  commit, with no duplicate revision or split lineage;
- exact Test application, qualification, and cutover binding; and
- deletion tombstone recovery and stable target enumeration.

The primary executable evidence is
[`tests/test_recipe_persistence.py`](../../tests/test_recipe_persistence.py),
with the frozen semantic boundary retained in
[`tests/test_recipe_phase_r0_contract.py`](../../tests/test_recipe_phase_r0_contract.py).

## Next phase

Phase R2 is now the sole implementation priority: make **Create Recipe**
provision Recipe plus DataVersion 1, compile the current Customer authoring
workspace into one portable RecipeDefinition, publish immutable revisions, and
present Recipe lineage without unnecessary changes to matching.
