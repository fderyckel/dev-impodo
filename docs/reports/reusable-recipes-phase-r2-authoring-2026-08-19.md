# Recipe-first Phase R2 authoring implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014 and Migration Project
Phases M0 through M7 define the current architecture.

## Outcome

Phase R2 completed on 2026-08-19. The primary browser object is now Recipe:
the landing page lists bounded Recipe projections, **New Recipe** provisions
Recipe plus authoring DataVersion 1, and a Recipe overview owns publication
readiness, immutable revision publication, and Recipe/DataVersion history.

The existing six-stage workspace remains the contained authoring experience.
Matching routes, controls, and interaction structure were not replaced. Recipe
and DataVersion context now surrounds that workspace through navigation and
breadcrumbs.

## Authoring and publication boundary

`RecipeDraft` is a read-only coordination projection. It does not copy mapping,
quality, source, schema, governance, preparation, reference, parameter, or
control drafts. It reports either one ready semantic hash or bounded issues
with one existing authoring surface as the recovery action.

Publication requires:

- a current file-based source/effective selection;
- an exact submitted mapping contract v11;
- current schema governance and captured schema evidence;
- a current mapping-bound quality ruleset; and
- only `UPSERT`, `CREATE`, or `REFERENCE` behavior.

Pinned Odoo updates, stale evidence, missing reference data, ambiguous logical
names, unknown source columns, and other nonportable constructs fail before
protected publication.

The compiler converts current evidence into the exact Recipe definition-v2
shape:

- physical dataset and column IDs become deterministic logical IDs from the
  reusable source shape;
- source file, project, selection, mapping, schema-capture, endpoint, database,
  principal, permission, credential, actor, and timestamp identity stays out
  of semantic meaning;
- source preparation rules, mapping behavior, target requirements and business
  keys, manager-authored quality rules, reference dependencies, reusable
  control definitions, and parameter definitions remain semantic;
- automatic mapping/schema quality families are marked for regeneration;
- DataVersion control expectations remain outside reusable meaning unless the
  control explicitly declares an invariant expectation; and
- absent rows retain `NO_DELETE_INFERENCE` semantics.

Before the Recipe overview shows **Ready to publish**, the compiler runs the
same exact envelope/version/hash/UUID/numeric-ID validator used by protected
publication. Publishing records physical authoring hashes and actor/time only
in integrity-protected provenance, writes the encrypted payload through the R1
intent, and advances append-only Recipe revision lineage. Re-publishing meaning
that already exists in the Recipe is rejected.

## UI scope

The intentionally changed surfaces are:

- Projects became the Recipe landing page, with `/projects` retained as a
  compatible alias for existing bookmarks;
- **New Recipe** became the normal creation action, while legacy
  `/projects/new` POST behavior remains compatible for existing clients;
- the Recipe overview shows authoring readiness, publication, DataVersions, and
  published revisions; and
- project overview terminology now describes the contained DataVersion.

The mature matching screen itself is unchanged.

## Verification

The former `tests/test_recipe_authoring.py` suite proved
that two physically different workspaces, source files, mapping IDs, schema
captures, databases, targets, actors, and timestamps compile to the same
semantic hash when their reusable meaning matches. Changing one reusable field
requirement changes the semantic hash. The test also validates the compiled
envelope with the production parser and exercises Recipe-native browser
creation while confirming the contained project remains a draft workspace.

The R1 persistence/fault suite and full browser suite remain regression gates.

## Next phase

Phase R3 is the sole current priority: create application-specific Test
TargetBindings from current remote target and credential evidence, apply a
published Recipe to representative same-ish data, and surface only source,
target, parameter, control, or credential drift around the existing matching
experience.
