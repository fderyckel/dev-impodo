---
audience: developer
kind: implementation-record
status: complete
---

# Phase M3 Project authoring and optional Recipe publication

## Status

Completed on 2026-08-22. M3 switched the browser and local composition to the
Project-first architecture accepted in ADR-014. M4 subsequently added
integrated multi-Recipe Test planning.

## Implemented outcome

The browser now lists and creates real Projects at `/projects` and
`/projects/new`. New project creates a Project, Authoring DataVersion, Authoring
run, and one MigrationWorkspace with four distinct identities. It creates no
Recipe. `/recipes` and `/recipes/new` are absent.

The Project overview is usable with zero Recipes. It opens the contained
authoring engine through `/workspaces/{workspace_id}` and states that the data
manager may complete one-off work without reusable rules.

## Source ownership cutover

File artifacts, Odoo capture values, protected Odoo origin sidecars, and
immutable source snapshots are stored under the DataVersion. After current
authoring choices or a complete Odoo capture are accepted,
`WorkspaceDataVersionSourceService` writes the complete canonical package,
freezes the DataVersion, and creates the workspace's exact dataset projection.

The clean workspace store contains package and dataset references rather than
source files, source rows, catalogues, or a copied DataVersion database.
`WorkspaceMappingSourceProjection` supplies those selected contracts to the
mapping editor and Recipe compiler.

## Recipe publication

`ProjectRecipePublicationService` compiles an eligible workspace without a
Recipe shell. First publication creates one Project-scoped Recipe and revision
1 together. Successor publication preserves the Recipe ID and appends the next
immutable revision.

`ProjectRecipeRepository` reserves an operation intent, stores the protected
payload, commits the registry revision, and resumes safely after injected
cross-store faults. Publication never updates Project or DataVersion identity
or ownership.

## Removed runtime paths

M3 removes the Recipe-root browser router, presenter, templates, list route,
creation route, bootstrap recovery, and Recipe-aware preparation handoff from
the current composition. The active compiler is workspace-only and has no
Recipe-root creation dependency. Superseded Recipe Test, Production,
qualification, and cutover browser surfaces are not compatibility aliases.

## Performance and safety

Project lists and overviews use bounded registry queries and do not open one
workspace database or protected Recipe payload per row. Preparation workers
verify one exact workspace and frozen DataVersion without consulting the
shared registry. Existing Odoo operations remain closed and batched; M3 adds
no Odoo call during Project creation, source projection, or Recipe publication.

## Verification

The focused M3 suite proves:

- a new Project has four distinct roots and no Recipe;
- the Project browser works without `/recipes` aliases;
- file and Odoo acceptance freeze the same DataVersion-owned package boundary
  and project exact references to the workspace;
- completed Odoo capture jobs finalize the DataVersion package, while protected
  origins follow DataVersion artifact ownership;
- first publication preserves Project and DataVersion identity;
- publication resumes after a protected-store fault without duplication; and
- successor publication retains the Recipe identity and Project-owned
  DataVersion.

The M0-M2 and preparation suites remain the regression gate. Representative
Customer, Product/BOM, reviewed reference, and stock Recipe shapes compile
through the workspace-only compiler.

## Next phase

Phase M4 will apply several exact Recipe revisions inside one Project-owned
MigrationRun and plan their unioned Odoo 19 requirements. It must keep
DataVersion and cutover ownership at Project level.
