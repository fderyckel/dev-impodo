# Recipe Phase R7 representative-shape implementation report

**Historical evidence:** This report records the superseded Recipe-first
implementation as it existed on 2026-08-19. ADR-014 and Migration Project
Phases M0 through M7 define the current architecture.

## Outcome

R7 closes the Recipe-first implementation plan with three representative
shapes on the same publication, application, preparation, quality, comparison,
execution, and reconciliation architecture:

- Products exercise scalar fields, governed business identity, target
  references, approved write fields, and no-delete comparison semantics.
- Product plus BOM exercises related parent/child preparation and incoming
  Product/BOM dataset dependencies.
- Opening stock exercises a declared warehouse, the built-in export as-of
  date, target Product/location references, and a fresh expected quantity
  control for every DataVersion.

No shape-specific execution service, alternate mapping contract, magic column
name, or credential path was introduced. Existing row limits remain unchanged.

## Reusable declarations and current values

The missing R7 capability was authoring arbitrary application context. The new
`RecipeParameterDefinition` contract supports bounded, stable string, date,
integer, and decimal declarations. The current declaration set is
content-hashed and stored only in the contained Authoring workspace. A
checksum-pinned additive project migration creates its singleton table, and
the authoring repository records an actor-bound audit event for each save.

Publication compiles each custom name to a portable logical ID such as
`parameter:warehouse`. File Recipes continue to receive the automatic required
`parameter:export_as_of_date`. Custom declarations are limited to controls and
provenance use sites; they do not become unreviewed mapping expressions or Odoo
write authority.

The boundary between meaning and evidence is explicit:

- adding, editing, or removing a declaration changes the Recipe semantic hash
  and therefore requires a new immutable revision and Test qualification;
- changing a declared warehouse value updates only the current DataVersion's
  hash-pinned `RecipeParameterValues`;
- current quantity expectations remain separate hash-pinned
  `RecipeControlValues`; and
- supplying an undeclared parameter fails closed against the selected Recipe
  revision.

## UI continuity and accessibility

The existing Recipe overview gains one bounded **Inputs for each data version**
card for registered Authoring workspaces. It uses the current card, table,
field, action, error, session, origin, and CSRF patterns. Matching and all six
downstream workspace stages remain unchanged.

Automated route coverage verifies persistence, content-hash reload, CSRF-bound
submission, semantic-hash change, accessible region labelling, and accessible
remove actions. A rendered in-app browser smoke test additionally verified:

- the card is exposed as the named **Inputs for each data version** region;
- text fields and both selects have usable accessible names;
- saving `warehouse` renders the typed row and visible status message;
- the removal action is announced as **Remove Warehouse**; and
- the page emitted no browser console error.

The browser fixture used only disposable synthetic local data and contacted no
Odoo or external service.

## Representative shape and volume evidence

`test_recipe_representative_shapes` publishes all three shapes through
`RecipeAuthoringService` and validates the resulting protected-envelope
semantics. Existing readiness coverage exercises BOM splitting, set-wise
relationship resolution, duplicate line blocking, derived Product categories,
and blank related-reference behavior. Preparation-capability coverage retains
the truthful production boundaries:

- 100,000 rows only for a qualifying single-dataset native direct route;
- 50,000 rows for current direct fallback or relationship routes, including a
  10,000 Product plus 40,000 BOM-line admission fixture; and
- 25,000 rows for current derived or materialized routes.

R7 does not reopen or claim the deferred 100,000-row mixed/related objective.
Preparation and relationship execution remain set-wise; Recipe compilation and
application add no Odoo call per source row.

## Security and portability

Custom declarations have a maximum count, bounded names and labels, an
allowlisted type, deterministic ordering, exact content-hash verification, and
a reserved built-in as-of identity. Routes require the existing authenticated
session, same-origin unsafe-request policy, CSRF token, Recipe publication
capability, Authoring DataVersion state, and contained project view scope.
Jinja escaping remains the rendering boundary. No parameter value, credential,
endpoint, database, principal, numeric Odoo ID, source row, or target snapshot
enters reusable Recipe meaning.

The additive DuckDB schema and domain code use the existing cross-platform
repository boundary. Windows-specific DACL, launcher, and installer checks
remain environment-gated on non-Windows hosts; the cross-platform path,
argument, and storage-key tests continue to run in the repository suite.

## Verification

Final verification passed:

- 703 repository tests passed, with 13 environment-dependent tests skipped;
- 78 focused Recipe, representative preparation/readiness, and security tests
  passed, with five Windows-only checks skipped on macOS;
- 21 focused authoring, application, and representative-shape tests passed;
- every changed Python file passed Ruff and bytecode compilation;
- documentation quality and code-documentation inventory checks passed;
- the rendered in-app browser interaction passed with no console errors; and
- the working-tree diff passed whitespace validation.
