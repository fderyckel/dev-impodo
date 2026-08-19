# Recipe Phase R5 Production application implementation report

## Outcome

R5 adds the rollout-day path from a selected cutover candidate to a clean
Production application. The data manager starts with **Run with latest data**,
enters the current parameters and business-control expectations, uploads the
complete latest source package, configures the current Production Odoo target,
and applies the exact selected Recipe revision.

This is a new DataVersion and contained workspace, not a clone of the Test
workspace. Existing matching, preparation, quality, comparison, approval,
load, and reconciliation screens remain the familiar downstream workflow.

## Exact revision and lineage boundary

Production DataVersion creation now requires the Recipe's current cutover
candidate. The lifecycle intent records both the selected candidate identity
and its exact Recipe revision. Registry commit revalidates the Recipe optimistic
revision, current candidate pointer, candidate history row, and pinned revision
before it activates the DataVersion and seals its predecessor.

This deliberately supports the important case where Recipe v3 remains selected
after v4 is published: a Production run pins and reads v3. It never substitutes
the newer current revision. Test runs continue to pin the current published
revision. Authoring DataVersions remain unpinned, and Test or Production
application workspaces cannot publish new reusable Recipe meaning.

## Fresh Production evidence boundary

The Production workspace carries forward only non-secret governance needed to
create the workspace, such as source-system label, owners, classification,
retention, and support-access policy. It starts without:

- source files or source selections;
- Odoo endpoint, database, or connection mode;
- read or write credentials;
- schema, principal, permission, context, or reference snapshots;
- TargetBinding or RecipeApplicationEvidence;
- mapping, preparation, quality, comparison, approval, execution, or
  reconciliation evidence; or
- Test record identifiers or write outcomes.

The Recipe application service now derives the TargetBinding environment from
the active DataVersion. Production therefore creates a
`TargetEnvironment.PRODUCTION` binding from a fresh live schema probe and the
current Production read-credential generation. Test bindings and credential
generations cannot satisfy that clean workspace.

The existing load-confirmation boundary continues to request and probe the
separate write credential against the exact target, readable/writable model
scope, Odoo context, and current execution snapshot. A setup read key is never
accepted as write authority.

## UI continuity

The Recipe overview now presents **Run with latest data** for the selected
candidate and shows Production setup, drift review, and rollout continuation as
contextual next actions. The new start page collects the Production data-version
label, declared Recipe parameters, and control totals while stating which
evidence will be fresh.

The contained workspace remains visually and structurally familiar. Only
environment-sensitive copy changes: target setup, Recipe application review,
and load confirmation identify Test versus Production accurately and explain
that Production read and write credentials are independent.

## Verification

Focused coverage proves:

- a selected older revision remains the exact Production revision after a newer
  semantic revision is published;
- Production DataVersion commit fails without the selected qualified candidate
  or if the candidate changes;
- a new Production workspace has no target configuration, source evidence,
  schema evidence, or TargetBinding;
- the Production browser path collects current parameters and controls and
  leads into the existing latest-file intake;
- application review reads the pinned candidate revision and emits a Production
  TargetBinding against a distinct Production endpoint/database; and
- application workspaces cannot be republished as Recipe revisions.

Final verification passed:

- 688 repository tests passed, with 13 environment-dependent tests skipped;
- every changed Python file passed Ruff;
- bytecode compilation passed;
- documentation quality and code-documentation inventory checks passed; and
- the working-tree diff passed whitespace validation.
