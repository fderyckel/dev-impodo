# Recipe-first Phase R3 Test application implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014, current architecture, and
lifecycle contracts own behavior.

## Outcome

Phase R3 completed on 2026-08-19. A data manager can start a clean Test
DataVersion from one published Recipe revision, provide representative
replacement files and fresh parameter/control values, connect a remote Test
Odoo server with a separately supplied read credential, review only current
drift, and compile the reusable meaning into a fresh mapping workspace.

The existing Match data editor remains the review surface. Recipe application
adds a focused pre-application page and a small status banner; it does not add
a parallel matching or execution engine.

## Exact application boundary

`RecipeApplicationService` requires one published revision pinned by the
current Test DataVersion. It binds source tables by exact logical name and
used columns by exact source name. Reordered columns are accepted, new unused
tables or columns are informational, and a renamed used column needs one
explicit DataVersion-only physical override.

The service checks only the Recipe's required Odoo models, fields, types,
relations, selection codes, write use, business keys, and newly required
fields. An accepted `TargetBinding` records non-secret endpoint/database
identity, credential-generation hash, storage class, principal, permission,
context, live-schema, reference, probe, environment, actor, and time evidence.
The API key never enters Recipe, TargetBinding, URLs, forms after submission,
or DuckDB evidence.

Applying a compatible Recipe:

- rebuilds supported lookup, parent/child, join, union, and aggregate
  preparation rules against current physical IDs;
- confirms fresh target governance from the Recipe business-key contract;
- compiles scalar, identity, relationship, transformation, categorical,
  control, reference, and target-disposition meaning into a new
  `MappingWorkingDraft`;
- scans current categorical domains set-wise before saving that draft;
- stages only reusable manager-authored quality rules against the exact fresh
  mapping hash, while automatic mapping/schema rules regenerate through the
  existing quality workflow after mapping confirmation; and
- writes protected immutable `RecipeApplicationEvidence` plus bounded local
  and registry projections.

Advanced quality checks that depended on an authoring-time approved scope are
not silently reused. They block with an explicit recovery action until current
scope evidence is established in a later Recipe revision.

## Drift and recovery

Missing used structure, stale overrides, undeclared or missing parameters,
missing controls, uncovered choices such as `German` or `LUX`, incompatible
Odoo fields, target identity changes, missing references, and stale credential
generations block only the current application. They do not mutate the
published Recipe revision.

Edited Test parameter values optimistically update the exact DataVersion hash
before local input evidence advances. Categorical blockers found during apply
remain visible on later review. Immutable application evidence is created only
after an accepted TargetBinding exists and always records its exact ID/hash.

## UI scope

The intentionally changed surfaces are:

- Recipe overview adds **Test on Odoo**, **Complete Test setup**, and
  **Apply Recipe** actions according to current DataVersion state;
- Test DataVersion creation gathers declared parameters and provisions a clean
  file workspace without copying server or credential evidence;
- target setup explicitly labels a Test Recipe rehearsal and explains that its
  server and credential are DataVersion-local;
- application review collapses compatible reused rules and shows only focused
  source, target, parameter, control, reference, quality, and credential drift;
  and
- Match data keeps its current editor and shows that the Recipe-built fresh
  draft is loaded.

## Verification

`tests/test_recipe_application.py` (removed by M7; retained in Git history)
proves exact same-ish source binding, renamed-column override behavior,
target/credential invalidation, parameter pinning, persistent categorical
blocks, preparation rebuilding, fresh mapping creation, protected evidence,
quality-seed hashing, and DuckDB round trips. The Recipe persistence, quality,
authoring, documentation, and browser suites remain regression gates.

The final repository gate ran 684 tests in 200.614 seconds with 13 intentional
platform or opt-in scale skips and no failures. Documentation workflow and
module-orientation checks also passed.

## Next phase

Phase R4 is the sole current priority: use the existing preparation,
comparison, explicit execution, read-back, and reconciliation path against the
Test TargetBinding, then publish immutable qualification evidence and select
one exact successful Recipe revision as the cutover candidate.
