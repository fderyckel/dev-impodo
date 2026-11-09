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

A constant existing many2one stores its related model, governed matching rule,
and ordered portable key and scope values. It requires no logical source-column
binding. Each application resolves those values against its own target
evidence.

A source-backed optional many2one may retain explicit `blank_source_values`.
Application rebinds its source column and preserves each decision to produce
no relationship. It must distinguish those decisions from uncovered source
choices and must not resolve them against an existing target record.

A hierarchical lookup preparation stores two to five ordered logical
source-column identities, its arbitrary target model and name field, and all
missing-value policies. Publication replaces every physical hierarchy field
with its logical identity. Application must rebind every level in the saved
order and stops when one is missing or ambiguous; it never substitutes a
similar heading or a model-specific category convention.

Conditional field rules store their ordered conditions and logical source
columns, but not the authoring rule or condition UUIDs. Application generates
fresh deterministic IDs inside the new workspace after rebinding those
columns. The IDs support workspace editing and are not reusable meaning.

Exact source compositions store their dependency-ordered rules, typed key
pairs, join policy, projections, output names, and physical-input use policies
by logical dataset and column identity. Only key and projected physical
columns enter the reusable source shape. Preparation-only inputs remain
required Recipe inputs even though they do not enter target-field mapping.
Application gives each rebuilt rule a deterministic workspace-owned identity,
binds each logical output before rebuilding a dependent rule, and restores the
complete physical-input use policy. Current preparation then evaluates the
fresh rows and blocks cardinality or required-match drift. The Recipe does not
store authoring rule UUIDs, source values, paths, snapshot identities, review
counts, output hashes, or lineage hashes.

`source_preparation_recipe` contract v1 treats `dataset_uses` as an optional
additive field. An older Recipe without that field retains its existing
application behavior. The compiler does not invent a policy for the older
revision, and a current Recipe with explicit policies must restore all of them.

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

### Reviewed successor publication

**Revise this Recipe** starts from the exact saved version selected in Recipe
history or pinned by a run. Impodo authorizes Recipe publication in the Project
before reading that version. The entry checks its originating Authoring
workspace, accepted Data version, verified Odoo destination, and current
compiled rules against the publication evidence. When that workspace is
closed or diverged, **Create Authoring draft** provisions an independent
Authoring run and workspace over the saved version’s accepted file delivery.
The new workspace rebinds the Recipe’s logical source columns and saves an
editable mapping after fresh Odoo field capture. It uses the original target
settings as a starting point and establishes new access evidence.

The exact version has a stable provisioning operation and a mapping seed
receipt. Retries recover the same draft and preserve later mapping edits.
Impodo restores business checks as editable rules and copies custom input
declarations. It reads the matching reference bundle from the original
workspace's immutable history by the saved dataset hashes. It checks saved
supporting choices against the new capture and records a new workspace review
before completing the seed.

For advanced checks, the published quality ruleset hash identifies the
historical coverage scope. The data manager reviews its declarations and
approves them for the draft after Odoo capture. Impodo records a new approval
against the frozen source selection and rebinds any reference lists by
content hash. The ruleset used for preparation carries that approval and
the draft's checked field matches. Preparation rejects an outdated scope
or reference list, and an interrupted seed resumes the same approval.

The browser session binds the review to the selected version, workspace,
accepted delivery, Odoo target, and company context. The review compares the
confirmed draft with the selected version and also shows differences from
the latest version when those versions differ. The save checks the current
working draft, the digest of the reviewed compilation, and the Recipe's
optimistic revision. A retry of the same completed publication returns its
existing result.

The protected provenance of a reviewed successor records its selected
baseline. The saved detail compares the two verified envelopes to show the
same change summary. The version still appends after the latest saved revision.

## Read and list boundary

Project overview lists Recipes with one bounded registry query. Reading a
specific revision verifies both the protected artifact hash and logical
payload hash before returning the envelope. The Project-scoped Recipe page
renders that one verified revision as source requirements, field and
relationship rules, preparation rules, checks, controls, and immutable version
history. List rendering must not open a workspace, protected payload, or Odoo
connection per Recipe row.

A run that needs several exact revisions reads their Recipe identities and
revision rows through one Project-scoped registry connection. It then verifies
one protected envelope per selected revision. This bulk read includes archived
Recipe identities because an existing run keeps its pinned revision.

## Current application boundary

An application may refresh effective Odoo selection choices and rematch source
values against them. The candidate is bound to that application's verified
target and current source and draft. Acceptance publishes the selection
projection and mapping together in the application workspace. It does not
replace the run's frozen target capture, modify the immutable Recipe revision,
or affect another application. Providers, transformations, identities, write
ownership and accepted totals remain subject to the Recipe adaptation guard.

The run confirms its target operation company before activation. The company
ID, protected binding and authoring endpoint are excluded from the Recipe.
Each application workspace projects the run's new company, while portable
relationship keys and explicit blank decisions retain their saved meaning.
After activation the run keeps that company fixed. A different company
requires a fresh run, not a change to historical target evidence.

An integrated Test run applies several exact Recipe revisions inside one
Project-owned MigrationRun. Each receives a separate RecipeApplication and workspace while
the run owns unioned target requirements and target evidence. Application
creates fresh current evidence and cannot publish a Recipe successor. It does
not restore Recipe-owned DataVersions or the superseded `/recipes` Project
shell.

## Verification

- `tests/application/project/test_authoring.py`
- `tests/application/run/test_integrated_recipe_runs.py`
- `tests/integration/web/test_recipe_optional_field_revision.py`
- `tests/domain/recipe/test_representative_shapes.py`

## Related documentation

- [Project lifecycle contract](project-lifecycle.md)
- [Evidence lifecycle](evidence-lifecycle.md)
- [Integrated Test run lifecycle](integrated-run-lifecycle.md)
- [Architecture overview](../../architecture/overview.md)
