---
audience: developer
kind: contract
status: current
---

# Optional Recipe publication contract

## Scope

A `Recipe` is a Project-scoped reusable transformation identity. It owns only
its immutable `RecipeRevision` lineage. A Recipe does not own its Project,
DataVersions, runs, workspaces, target bindings, qualifications, or cutover
selection.

A Project can contain zero, one, or several Recipes. Project creation never
creates an empty Recipe shell.

## Eligibility

Only an open Authoring workspace over a frozen Authoring DataVersion and an
Authoring run can publish reusable meaning. An application workspace cannot
publish Recipe meaning. The compiler requires current immutable source
references, a submitted mapping, matching schema governance, current quality
rules, and any referenced preparation, parameter, control, or standard-key
contracts.

Blocked readiness is a read projection. It does not create a Recipe, duplicate
mapping state, or mutate current evidence.

## Portable meaning

A Recipe revision may contain logical source shapes, mapping and
transformation rules, relationships, preparation rules, Odoo model and field
requirements, governed business keys, reusable quality rules, standard
references, parameter definitions, and controls.

It must exclude source rows, file and snapshot identities, current source
hashes, Project and workspace UUIDs, target endpoint or database, credentials,
numeric Odoo IDs, actors, approvals, execution journals, read-back, and
reconciliation evidence. Publication provenance records the physical origin
separately from semantic meaning.

`domain/recipe_envelope.py` validates the exact envelope, its semantic hash,
payload hash, portable fields, forbidden keys, and numeric-ID boundary.

## Atomic publication

First publication performs one restart-safe operation:

1. reserve an operation intent owned by the proposed Recipe ID;
2. store the authenticated immutable envelope;
3. create the Recipe identity and revision 1 in one registry transaction; and
4. commit the operation result.

Successor publication appends the next revision with the Recipe's optimistic
revision. The same operation identity can resume after a cross-store fault but
cannot be reused for different meaning. Semantically identical revisions in
the same Recipe are rejected.

Neither first nor successor publication changes Project, DataVersion, run, or
workspace identity or ownership.

## Read and list boundary

Project overview lists Recipes with one bounded registry query. Reading a
specific revision verifies both the protected artifact hash and logical
payload hash before returning the envelope. List rendering must not open a
workspace, protected payload, or Odoo connection per Recipe row.

## Current application boundary

An integrated Test run applies several exact Recipe revisions inside one
Project-owned MigrationRun. Each receives a separate RecipeApplication and workspace while
the run owns unioned target requirements and target evidence. Application
creates fresh current evidence and cannot publish a Recipe successor. It does
not restore Recipe-owned DataVersions or the superseded `/recipes` Project
shell.

## Verification

- `tests/test_project_authoring.py`
- `tests/test_integrated_recipe_runs.py`
- `tests/test_recipe_representative_shapes.py`

## Related documentation

- [Project lifecycle contract](project-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Integrated Test run lifecycle](integrated-run-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
