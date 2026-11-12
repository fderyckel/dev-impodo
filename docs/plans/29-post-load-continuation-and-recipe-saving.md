---
audience: developer
document_type: plan
status: partially-implemented
---

# Continue working after a successful load

This proposal lets a data manager inspect a completed load, add a forgotten
field, repeat preparation and comparison, load the reviewed changes, and save
a Recipe after success. The initial implementation supports direct file-source Authoring with standard
data checks and native prepared snapshots. Saved stage browsing and selected
completed-load Recipe saving are implemented independently. Combined or
generated tables, additional business checks, evaluator-only preparation, and
interrupted-attempt recovery still need qualification. The sections below
retain the full approved target behavior.

## Recommended product decision

Preserve every completed load as immutable history. Let the data manager
continue through the normal stages in a new working revision within the same
data project. Reuse the accepted files and saved rules. Provision the next
Authoring run and workspace internally; the operator does not have to rebuild
a project or upload the same files again.

Treat these as three independent actions:

| The operator's goal | Proposed action and result |
| --- | --- |
| Inspect the completed import. | Earlier stages open their saved mapping, preparation, and comparison in a clearly labelled historical view. |
| Change the imported values. | **Continue editing** opens the next working revision at Stage 3. Preparation, comparison, and confirmation establish a new load. |
| Reuse the successful rules. | **Save these rules as a Recipe** publishes eligible rules from the selected completed Authoring load. Another load is unnecessary. |

The first implementation covers file-source Authoring loads against Odoo 19
and Odoo 20 through local and remote access. It applies to every admitted
model and supported field provider. It must not contain a Contact, Product,
BoM, or field-name exception. Existing field policies continue to determine
whether a specific field is writable.

Recipe Test and Production runs retain their pinned Recipe versions and
qualification rules. Their history remains inspectable. **Revise this Recipe**
uses the existing Authoring-draft journey; changing a published Recipe does
not silently edit or reauthorize the run that used it. Odoo-source transfer
continuation requires separate qualification of its captured-source and
destination contracts and is outside the first continuation delivery. Show
that boundary explicitly wherever the action is unavailable.

## Example and normal journey

A fictional import creates 150 supplier records. The file also contains
Payment terms, but the data manager did not map that column. Assume all 150
records need the additional value and none has an unresolved concurrent change.

1. The completed-load page offers **View saved review**, **Continue editing**,
   **Save these rules as a Recipe**, and its review-workbook actions.
2. Selecting Stage 3 opens the saved mapping for inspection. Selecting
   **Continue editing** submits a local command, creates or resumes the next
   working revision, and opens the normal Stage 3 editor. A page GET must not
   create a draft. The editor identifies the completed load it continues.
3. The data manager maps Payment terms. If the captured field information is
   insufficient, the existing Odoo-field refresh leads to the affected choice.
   Valid unrelated mappings remain available.
4. Stage 4 prepares and checks the revised data. Stage 5 performs a fresh
   comparison against the same verified Odoo destination and company context.
5. Stage 5 shows 150 proposed updates and zero creates in this example. It
   shows the field changes, counts, exclusions, and any conflicts. Records
   whose intended values already exist in Odoo count as unchanged.
6. Stage 6 requires a new explicit confirmation, applies the reviewed writes,
   and reads them back. The next verified load gets its own saved review and
   workbook. The earlier load remains available in history.

This is an example of the shared behavior. The same flow must work for another
admitted model, including one whose display name or sequence values repeat.

## Navigation and history

Replace the blanket grey navigation with a distinction between **Saved load**
and **Working revision**. Show the selected load and the current working
revision clearly. Historical Stage 3 and Stage 5 views must never look editable.

| Page or state | Required behavior |
| --- | --- |
| A completed load is selected. | Stage links show saved evidence where it exists. Missing historical evidence gets a specific explanation. |
| A working revision is selected. | The normal stage navigation reflects that revision's current checks. |
| The mapping is edited. | Dependent preparation, comparison, and confirmation become outdated only in the working revision. |
| Stage 5 is opened for a completed load. | Show its exact saved comparison. **View saved review** must reach that comparison rather than redirect to a correction explanation. |
| A workbook is requested. | Identify the selected load and comparison explicitly. Creating or downloading one load's workbook cannot select a newer comparison. |
| A load is running or its outcome is uncertain. | Allow inspection. Resolve the owning load through recovery before authorizing another load in its continuation chain. |

Keep history bounded and paginated. Historical pages read retained evidence;
they do not contact Odoo or recalculate old results. Reloading a page,
restarting Impodo, or following an old link must retain the selected load.

## Record identity and permitted writes

Default a continuation to **Update records from the previous load**. Preserve
the association between each source record and the exact Odoo record verified
by the earlier load. Derive source identity from accepted provenance and the
saved identity policy, independently of the mapping fields being edited.
File position, record labels, and sequence values alone are insufficient.

Bind protected target receipts to the exact destination, model, source
identity, and applicable context. Keep numeric Odoo identifiers outside
portable Recipes and comparison artifacts. Reuse the shared reference and
version policies, including the source-identity namespace when it applies.

An unreadable, missing, ambiguous, or retargeted previous record blocks that
record's update. It must not silently become a create or select a similar
business-key peer. A changed destination requires the ordinary new-target
workflow. Unsupported identity or dataset changes require explicit review
before Impodo can reuse earlier target associations.

Changing a row filter can exclude previous records without deleting them in
Odoo. New records or newly included rows require a separate creation-scope
decision through the existing matching and business-expectation controls.
They cannot appear as implicit creates in the default update continuation.
Existing deletion and ownership boundaries remain in force.

After several cycles, preserve the latest verified target association for
each source record. A later load that changes only a subset must not erase
associations for the remaining records. Retain the immutable receipts that
justify the effective association.

## Compare changes without overwriting later Odoo work silently

For previously mapped fields, compare the previous verified intent, the
current Odoo value, and the newly prepared intent using the existing field
comparison semantics. A rule can change several derived values; assess the
prepared result for each affected record and field.

| What the comparison finds | Proposed default |
| --- | --- |
| A newly mapped field differs from current Odoo. | Propose an update after applying its explicit blank and update policies. Absence from the old mapping is not a blank old value. |
| The revised intent differs from the previous intent, and Odoo still has the previous verified value. | Propose the revised value. |
| Odoo already has the revised value. | Mark the field unchanged. |
| The intent is unchanged, but Odoo changed after the previous load. | Preserve the current Odoo value and show the difference. Reapplying the saved value requires an explicit reviewed choice. |
| Both the revised intent and current Odoo differ from the previous intent and from each other. | Show a conflict and require a reviewed decision before that write can proceed. |
| A field is removed from the mapping. | Leave its Odoo value untouched. |
| A mapped value is blank. | Apply the saved blank policy. Clearing a field must be explicit in the reviewed writes. |

Stage 5 must show added fields, changed fields, unchanged records, conflicts,
and excluded records in data-manager language. Existing shared numeric,
Selection, default, create-only, and relationship policies remain authoritative.
Relationship updates resolve exact eligible related records with fresh evidence;
the parent-field update grants no implicit write authority over those records.
When the comparison proposes no writes, use the existing verified no-change
completion path without requesting write authority.

Before transport, revalidate the exact target bindings and reviewed field
values. An Odoo change after confirmation requires another review. Never reuse
an earlier load's confirmation, write authorization, or execution snapshot.

## Save a Recipe after success

Offer **Save these rules as a Recipe** on the completed-load page and retain
eligible completed Authoring loads in the project page's Recipe-source choices.
Identify the selected load; do not silently choose the most recent open
workspace or a draft that has since changed.

The operator can save a new Recipe or review a new version of an existing one.
If a working revision also exists, distinguish **Rules from this completed
load** from **Current working rules**. The latter must pass normal submission
and publication checks. The completed-load action reads the exact retained
rules used by that load and performs the normal publication eligibility check.
It does not rerun preparation, comparison, or loading merely because the
Authoring workspace is closed.

Extend the current Recipe eligibility contract deliberately: permit publication
from a verified completed Authoring workspace with readable, bound, immutable
inputs. Successful loading alone does not bypass compiler or portability
checks. An older unsupported contract gets an actionable explanation rather
than a partially reconstructed Recipe.

Publish reusable meaning only. Credentials, protected record associations,
numeric Odoo identifiers, approvals, journals, and read-back results remain
outside the Recipe. Record the selected load's provenance separately. Saving
a Recipe must neither reopen the load nor authorize writes.

## Implementation responsibilities and contract changes

The present implementation closes the completed workspace and redirects most
earlier-stage routes in
[the shared route policy](../../src/impodo/web/composition/http_app.py).
[Project Recipe selection](../../src/impodo/web/routers/migration_projects.py)
filters out closed workspaces. The
[correction comparison](../../src/impodo/adapters/polars_correction.py)
requires identical old and corrected writable-field sets. These are the
specific gaps; removing a navigation condition alone does not implement this plan.

1. Add bounded historical read projections for saved stages. Normal mapping
   pages can perform authoring setup, so merely allowing every old mapping
   route is insufficient. Keep historical reads separate from mutation commands.
2. Reuse the existing successor-provisioning and protected-target foundations
   where their contracts apply. Generalize them through shared continuation
   services so newly mapped fields can use normal preparation and comparison.
   Keep a typed origin binding from the draft to the preceding completed load.
3. Create or resume a draft with an idempotent command and optimistic revisions.
   Preserve an existing draft's edits across repeated clicks, multiple tabs,
   failed provisioning, and application restarts. Do not replace unrelated work.
4. Extend the shared final-review and execution owners with explicit continuation
   intent and exact-target evidence. Keep journaling, write-scope validation,
   interrupted-load recovery, and read-back in their existing owners. Do not
   create another generic Odoo client or independent loader.
5. Select Recipe publication inputs explicitly and extend its eligibility
   contract. Keep publication independent of whether another draft exists.
6. Update the owning current documentation, navigation, workflow registry,
   contracts, and screenshots with the implementation. Until then this plan is
   the sole documentation owner for the proposed behavior.

Review the
[execution contract](../developer/contracts/execution-and-reconciliation.md),
[evidence lifecycle](../developer/contracts/evidence-lifecycle.md),
[project lifecycle](../developer/contracts/project-lifecycle.md), and
[Recipe publication contract](../developer/contracts/recipe-lifecycle.md)
with the implementation. Preserve pinned Recipe Test and Production authority.

All local page reads and commands use bounded synchronous database operations.
Release database owners before Odoo calls, background dispatch, and artifact
streaming. Resolve exact targets and relationships in bounded groups. Verify
database connection and SQL budgets for history, draft creation, review,
publication, rejection, and retry paths; this proposal makes no latency claim.

## Delivery sequence and acceptance

Deliver history browsing and Recipe saving first, then continuation through
Stages 3–6. Qualify conflict handling and repeat cycles before offering the
continuation action for writes. Keep explicit new-record creation behind its
own reviewed scope. Preserve the running application during implementation.

| Acceptance case | Required evidence |
| --- | --- |
| A forgotten field is mapped after a verified import. | Preparation and comparison propose updates to the exact earlier targets; a confirmed load verifies the added values without duplicate creates. |
| The same behavior is used for an unrelated model. | The result follows captured metadata and shared policies without model-specific code. |
| The second load changes only some records. | A third cycle still identifies every earlier verified record correctly. |
| The operator inspects Stage 3 or Stage 5 after completion. | The saved evidence and selected-load workbook remain available across refresh and restart. Inspection creates no draft. |
| The operator saves a Recipe after success. | Publication uses the selected completed rules even when another draft has different edits. Saving starts no Odoo write. |
| A new field, removed field, blank, relationship, or create-only field is involved. | The reviewed writes follow the policies described above and retain field ownership. |
| Odoo changed before comparison or after confirmation. | The review exposes the change; transport cannot overwrite an unreviewed value. |
| An earlier target was deleted, became unreadable, or lost its association. | Continuation blocks with an explanation rather than creating a replacement. |
| A load was interrupted or its outcome is unknown. | Recovery resolves the owning load before another cycle can write. |
| Draft creation or Recipe publication is retried concurrently. | The same operation resumes its existing result and preserves saved edits. |
| Old evidence predates the required target receipts. | Browsing and eligible Recipe saving remain independent; continuation explains which evidence is missing. |
| Recipe Test, Production, or an Odoo-source transfer is selected. | The UI follows its qualified owning journey and never bypasses pinned versions or execution authority. |

Run the affected Authoring matrix on Odoo 19 and 20 through local and remote
access. Cover affected Recipe entry paths for both versions, including denial
of unsupported in-place edits. Exercise authorization, source lineage,
deterministic ordering, exact target reuse, freshness, bounded database work,
and crash recovery. Complete the repository gate and focused behavioral tests
on the fixed candidate. Report browser and hosted platform checks separately;
do not dispatch hosted jobs without an explicit instruction.
