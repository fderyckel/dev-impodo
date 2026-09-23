# Generic Odoo-to-Odoo migration

## Status and outcome

**Status:** Active implementation plan, started 2026-09-17.

A data manager selects the Odoo 19 records to move from one instance to
another. The records may be Contacts, Products, bills of materials,
transactions, or records from an installed custom model. Impodo proposes the
related records needed by the selected data, lets the manager decide what to
reuse, create, or update in the destination, and executes one approved plan.
It verifies the resulting values and relationships in the destination.

The engine must use captured metadata, selected relationship edges, reviewed
identities, and explicit per-model policies. It must not infer a transfer rule
from a model's name. A model with business actions or generated records may
need a separately qualified handler; the generic create/write path must stop
when it cannot prove a safe result.

The first qualification uses two distinct disposable Odoo 19 instances with
the same major version. Odoo 20 and Production cutover have separate gates.

## Current implementation used by this plan

The browser already captures bounded Odoo source tables, stores protected
relationship origins, matches selected models by one to three scalar fields, builds
relationship waves, obtains review approval, runs read-only preflight, and
loads through a journal and read-back. The
[source workflow](../developer/workflow/01-source-data.md) and
[load workflow](../developer/workflow/06-load-into-odoo.md) describe that
current behavior.

The first implementation changes admit finite Odoo `float` values into source
capture and give every selected model an approved reuse-only,
create-if-missing, or upsert policy. Reused records have no update intents.
The source screen presents writable relationships and one-to-many child fields
whose related model is outside the selected scope as a grouped business-scope
proposal. The qualified Product policy preselects recommended supporting
models for review, but the manager still saves the model choice explicitly.
For a supporting model already in that scope, the manager can choose
linked-only capture. A bounded protected closure follows eligible relationships
from root records, resolves only the linked rows, and checks membership again
before atomic publication. The manager confirms all eligible relationship
fields as a group. Per-field edge approval and automatic model-scope mutation
remain.
When selected record types are captured together, publication now rejects
links whose target row was excluded from the frozen set. The destination
matching page supports a first text field and up to two more captured text or
integer fields. Matching, approval, preflight, relationship resolution, and
execution use the same ordered fields. The first field still bounds the
destination read to 1,001 rows; a wide first-field match is blocked.
Matching now reads all destination field definitions and their available
required-field defaults. It blocks a proposed create when required inputs
are neither captured nor automatically safe to leave to Odoo. Preflight
rechecks that coverage. Review of a business-sensitive destination default
is not yet implemented, so such a default remains a blocker.
Models whose destination metadata exposes a Selection field named `state`
now require a qualified workflow handler before generic creates or updates.
Existing destination records can still be reused. This guard does not detect
every possible business action on a custom or standard model.
These changes do not add a model-specific path or allow updates to the
captured source instance. The remaining phases below describe unfinished
capabilities.

On 2026-09-18, a read-only rehearsal used two distinct private Odoo
`19.0+e` demo instances. The matching service classified one sampled Contact
by `name` and `company_type`, then one sampled Product by `name` and
`default_code`. Both identities had one existing destination match and no
matching blocker. This verifies the scalar matching path for those samples;
it does not qualify source capture, creation, updates, relationship loading,
or recovery on live Odoo. The instances also exposed different field counts
for several common models despite sharing the same Odoo version, so the
destination field check remains a required part of each transfer.
One sampled BOM line matched by `display_name` and `sequence` had no
destination match, and the write-field check rejected `display_name` as
incompatible. That combination therefore cannot be treated as a proven
create path. A parent-scoped identity and a writable field plan remain
necessary for this record type.
The full-field rehearsal also found unresolved required create fields on the
demo destination for Contacts, Products, and BOM lines. The sampled Contact
and Product already existed, so they did not need those create inputs; the
BOM line was missing and remains blocked.

| Sampled model | Required destination fields still needing values or reviewed handling |
| --- | --- |
| `res.partner` | `autopost_bills`, `group_on`, `group_rfq` |
| `product.template` | `product_variant_ids`, `service_tracking`, `tracking`, `type`, `uom_id` |
| `mrp.bom.line` | `bom_id`, `product_id`, `product_uom_id` |

This is a check of one selected field surface per model. The exact gaps can
change when the manager captures more source fields or the destination's
modules and defaults change.
The demo source had no sampled `sale.order` identity in its first 20 rows,
so no order transfer was qualified. Its destination metadata does expose a
Selection field named `state`, which activates the workflow-handler guard.

## Phase 1: freeze a complete selected source set

The data manager chooses one or more root record types and a bounded record
filter. Impodo uses captured Odoo metadata to propose relationship edges and
related record types. The manager reviews the proposal before a complete read.

- Extend the closed source value policy to the scalar types needed by the
  selected records. Each type needs a bounded decoder, immutable snapshot
  representation, and round-trip test. The initial `float` addition covers
  numeric quantities and durations without granting source write authority.
- Follow only approved relationship edges. Deduplicate protected source IDs,
  detect cycles, and enforce per-model, total-row, depth, and company bounds.
- Report a missing, inaccessible, or out-of-scope required dependency before
  publishing the source set. Do not silently omit an unselected link.
- Detect source changes during a multi-model capture and require recapture
  when selected values or relationships cannot be shown consistently.

The capture form now accepts one exact-match direct scalar root filter. Its
value is stored in an encrypted, selection-bound project artifact, while the
saved selection carries only the artifact hash. Assessment and capture require
that artifact. Linked-only capture now derives related IDs inside the protected
boundary and enforces row, link, and depth limits before promoting the complete
set. The browser shows model and row counts without exposing source IDs.
Broader root filters, individual edge approval, and automatic selection of
missing model types remain to be implemented. Company access is bound to the
source identity check, but a separate company-specific closure rule remains.

**Implementation status (2026-09-23):** The source page now presents captured
out-of-scope relationships as a business-scope proposal instead of a technical
model warning. It distinguishes supporting data, optional data, destination
configuration, Odoo-managed records, separate processes, excluded history,
and unresolved custom links. The qualified Product policy recommends Product
Categories, Units of Measure, and the Unit of Measure Category when its link is
discovered. The review link preselects only recommendations that exist in the
current model catalogue, while the existing schema-scope POST remains the
explicit save boundary. This first seam does not yet persist individual edge
decisions or silently expand model scope.

On 18 September 2026, a read-only check against the private demo source
retrieved seven Contact samples and found one exact-name match. It verified
the Odoo equality domain without printing the chosen name. This check did not
exercise a complete browser capture with the protected filter.
Another read-only Odoo 19 check sampled five BOMs and resolved all 25 IDs
returned through their `bom_line_ids` field. Five Contacts returned one child
ID through `child_ids`, which also resolved. This confirms the JSON-2
one-to-many response shape for those samples. It does not qualify a complete
linked-only browser freeze or destination load.

**Exit gate:** A selected Contact group, Product group, BOM, or transactional
set produces one reviewable frozen set with its approved dependencies. Every
required edge is present or has a clear blocker. Source reads are bounded and
make no writes.

## Phase 2: classify destination records and links

Impodo matches the frozen source set against the different destination using
one read-only destination key.

- Extend the implemented single-field and scalar composite matching to
  relational scope identities. A Contact
  might use a reference; a Product might use an internal code; a line might
  use its parent identity and sequence. The manager chooses the rule from the
  fields and constraints available for that model. Align this with the shared
  `TargetIdentitySpec` component and scope semantics, then adapt frozen Odoo
  rows to those semantics instead of adding model-name matching rules.
- The manager can choose `reuse_only`, `create_if_missing`, or `upsert` for
  each model in Stage 7. The browser defaults to `create_if_missing`, so
  capturing a supporting model does not automatically make its existing
  records update candidates. Broader identity and preflight checks remain.
- Check destination uniqueness, permissions, required create fields, usable
  defaults, selection values, company scope, and every selected relationship.
- Require a reviewed handler for generated records or model-specific side
  effects. A generic transfer must not duplicate an Odoo-generated child or
  assume that a business workflow action is an ordinary field write.

For relational scope, resolve each protected source parent or company ID to
its reviewed portable identity, then resolve that identity to the destination
record before classifying the child. Carry the relation as a reference into
execution so the destination lookup uses its actual ID at write time. The
current execution snapshot carries scalar scope values, so this requires a
new reviewed resolver contract rather than putting a source or destination
numeric ID into a portable key.

Required destination defaults that select another record, workflow choice,
or company context need a review control bound to exact destination default
evidence. Generated child records need an explicit handler that predicts and
reconciles their post-parent identities before the load is approved.

### Proposal: resolve required create fields inside the transfer

**Implementation status (2026-09-23):** Destination matching now handles exact
scalar and Selection defaults, typed fixed values, and compatible captured
source fields as create-only providers. The browser keeps exact defaults and
fixed values in encrypted Project evidence. The staged execution snapshot
contains only their hashes. The writer opens a fixed value only at the
protected write boundary, while a source-field provider reads the current
frozen source row. Neither provider changes a reused destination record.

An Odoo default now produces a separate protected read-back expectation. The
create request omits that field, then reconciliation reads it and compares the
result with the exact reviewed default. Stage 8A and Stage 8B carry a reviewed
choice only while its field contract, provider, and value hash remain
unchanged. A changed default or provider returns as
`DESTINATION_CREATE_FIELD_DRIFT` before a write. The live Contact qualification
covers `autopost_bills`, `group_on`, and `group_rfq` and removes their
create-field blocker after review without writing to Odoo.

Destination matching also offers a governed existing-record choice for a
required Many2one field when the related record type is selected and the
bounded destination read proves a unique business identity. A Many2one Odoo
default uses that identity evidence when available; otherwise Impodo can still
omit the field and verify the exact protected target ID after creation. The
numeric target ID remains inside encrypted evidence and is resolved only at
the write or read-back boundary.

The data manager can also choose **Use one selected source record** for a
required Many2one field. Impodo lists unique business identities from another
selected source table whose Odoo model matches the required relationship. The
choice applies to every new owner record. If that selected record is missing
from the destination, Stage 6 places its table in an earlier transfer wave. If
it already exists, Impodo reuses it without adding an unnecessary dependency.
A required Many2one field without a usable Odoo default or one of these
governed choices stays named and fails closed.

A required destination field must not leave the data manager at a technical
dead end. After Impodo checks destination matches, it should show **Complete
values for new records** for each record type that has missing destination
records. This proposed control belongs to destination matching because the
decision depends on the exact destination, its current defaults, and the
number of records that Impodo may create.

Impodo should classify every required create field and present one of these
outcomes:

| Destination condition | Impodo action | Data-manager decision |
| --- | --- | --- |
| A compatible captured source field supplies the value. | Use the captured value for each new record. | No extra decision is needed unless the field is business-sensitive. |
| The exact destination supplies a straightforward, non-company-specific default. | Leave the field out of the create request and record that Odoo will supply it. | Show the field under **Handled automatically**. |
| The exact destination supplies a linked record, workflow choice, monetary value, or company-sensitive default. | Show the current value and why it needs review. | Confirm **Use this Odoo default**, choose another supported provider, or change the model policy. |
| Odoo supplies no usable default for a writable scalar or Selection field. | Offer a typed create-only value or a compatible captured source field. | Choose **Set one value for new records** or **Use a source field**. A Selection value must come from the current destination choices. |
| A required Many2one field needs a value. | Resolve an existing destination record by its governed business key or use a selected incoming record type. | Choose the reviewed destination record or incoming relationship. Impodo never asks for or stores a portable numeric Odoo ID. |
| The field is computed, related, read-only, or covered by a qualified Odoo create hook. | Mark the field as managed by Odoo and omit it from the create request. | No value is requested from the data manager. |
| The field participates in an unqualified business action or generated-record workflow. | Explain the missing handler and keep generic creation unavailable. | Choose **Reuse existing records only** or wait for a qualified handler. |

The page should call an incomplete supported choice **Needs decision**, not a
destination-field blocker. Once every create-only field has a current valid
decision, the model can proceed to transfer review. Impodo must still stop
when no legal value exists, when the user has not made a required business
decision, or when the model needs an unqualified business operation. A safe
workflow cannot promise that every installed custom model is generically
creatable.

The choices apply only to rows classified as `CREATE`. They must not overwrite
an existing destination record during `upsert`, and a `reuse_only` model does
not need create-field choices. If a fresh destination check changes the create
count to zero, Impodo keeps the earlier decision as history but excludes it
from the active transfer package.

#### Evidence and execution contract

Add one immutable `DestinationCreateFieldPlan` for the current destination
matching result. Each field decision should bind the Project, Data version,
workspace, source selection, source schema, destination target, destination
schema and defaults, read principal, company context, model policy, field
contract, provider kind, actor, and decision time. The closed provider kinds
should be:

- `source_field`, which copies a compatible captured source value;
- `odoo_default`, which deliberately omits the field so that the exact
  destination applies its verified default;
- `fixed_value`, which writes one typed value to every new record of that
  type;
- `existing_reference`, which resolves one reviewed destination record by a
  governed portable identity;
- `incoming_reference`, which resolves a reviewed record from another
  selected source table and orders that table first when creation is needed;
  and
- `odoo_managed`, which is valid only for captured computed or related
  behavior, a read-only field, or a version-qualified create hook.

The ordinary destination-matching plan and browser projection should retain
technical field names, provider kinds, counts, and evidence hashes. A fixed
business value, an exact Odoo default, and a target-bound Many2one identifier
belong in protected target evidence. Numeric Odoo IDs must not enter portable
matching, review, download, journal, or Recipe artifacts.

For `odoo_default`, the execution snapshot needs two separate meanings. Its
write intent omits the field, while its protected reconciliation expectation
records the reviewed value that Odoo is expected to apply. Read-back must
verify that expected value after creation. For a Many2one default, the
expectation binds the exact reviewed destination record. It also retains the
portable identity when the related type is in matching scope; it never copies
the numeric ID into another target or portable run artifact.

Stage 7 now binds the destination-matching plan hash and lists each create-only
field with its provider kind without including the chosen business value.
Stage 8A should repeat `fields_get` and one
bounded `default_get` request per affected model, then compare the resulting
field contracts and default evidence with the approved plan. Stage 8B should
repeat that check before it stages the execution snapshot. A changed default,
company context, field definition, model policy, or provider invalidates the
decision and returns the user to **Complete values for new records** with the
new current choice. It must perform zero writes and must not report a generic
stale-evidence error when it can name the field that changed.

The compiler should add `fixed_value`, `source_field`, and resolved reference
providers to create intents. It should omit `odoo_default` and `odoo_managed`
fields from the Odoo payload. Reconciliation should read every provided or
expected required field in bounded model-and-field groups. Default capture,
reference resolution, compilation, and read-back must not add a request per
source row.

#### Reuse the shared create-field policy

This work should extend
`domain.mapping.create_field_policy` instead of creating transfer-specific
rules for required fields. Authoring, Recipe application, and Odoo-to-Odoo
transfer should agree on which defaults are automatic, which require review,
and which fields Odoo manages. The transfer workflow needs its own target-bound
decision evidence because it has a separate destination, create count, model
policy, review package, preflight, and execution snapshot.

The first browser increment should reuse the existing **Review Odoo defaults**
interaction for verified defaults. It should then add the typed fixed-value
and governed-reference choices needed when `default_get` has no usable value.
It must not infer a default from the first Selection choice, the first related
record, a similarly named source field, or a model name.

#### Acceptance cases

The implementation is ready for the first transfer scenario when all of these
cases pass:

- The sampled Contact required fields `autopost_bills`, `group_on`, and
  `group_rfq` appear as automatic or reviewable destination decisions. After
  the required review, they no longer prevent an approved Contact create.
- A required Boolean default of `false` remains a usable explicit default; it
  is not mistaken for a missing value.
- A required Selection default displays its current business label and stored
  key, requires review, and becomes stale when the destination choice changes.
- A required Many2one default shows its reviewable business identity when the
  related type is in matching scope. Otherwise it shows the current
  destination default, keeps the numeric ID in protected evidence, and verifies
  that exact result after creation without forcing the related type into scope.
- A required field with no default can use a typed create-only fixed value or
  a compatible captured source field without changing reused destination
  records.
- A required Many2one field can use one reviewed record from another selected
  source table. An existing destination match creates no dependency; a missing
  match adds a hard create-order dependency and compiles to a symbolic incoming
  reference.
- `product_variant_ids` and other proved generated or read-only fields never
  ask the data manager for a source mapping or fixed value.
- An unsupported state transition or generated-record side effect remains a
  named workflow-handler requirement and cannot be bypassed through a field
  default.
- Default reads and reference reads are bounded by model and distinct key,
  and call-count tests prove there is no hidden per-row destination lookup.
- A changed target, principal, company context, schema, default, model policy,
  or source selection invalidates the decision before a journal or Odoo write
  exists.
- Review packages, portable snapshots, reports, and downloads contain no
  target numeric IDs or protected default and fixed values.

The current scalar composite match does not resolve a parent or company
relationship as an identity component. Such records still need the scoped
identity work above. The publication check validates links between selected
record types. Linked-only capture now obtains referenced rows for selected
supporting models. It does not select a missing model type or resolve a parent
or company relationship as an identity component.
At load time, after an earlier wave commits, Impodo rechecks remaining create
identities before each later wave. If Odoo generated a record that the plan
expected to create, the transfer blocks later writes and keeps the journal
for review. It does not silently reuse that generated record; a qualified
handler still has to define how that record and its links are reconciled.

**Exit gate:** The preview accounts for every selected source row and link as
reuse, create, update, or a clear blocker. Matching, ordering, review, and
preflight make no destination writes.

## Phase 3: load and verify one approved plan

The confirmed Stage 8B load uses the approved per-model policies and the
existing journal, writer, recovery, and reconciliation boundaries.

- Create approved missing dependencies before rows that refer to them.
  Resolve destination links from exact existing matches or durable receipts
  for newly created rows.
- Recheck identity, company scope, required fields, and create-key absence
  immediately before writing. Stop later writes after an unknown outcome.
- Apply only supported ORM create and write operations. Stateful transactions
  and business actions need their own qualified operation contract.
- Verify all intended values and links through destination read-back. A
  repeated transfer must not create duplicate rows.

**Exit gate:** Contact, Product, and related-record scenarios load to empty
and partly populated disposable destinations through the browser. An
interrupted load resumes only work that read-back proves safe.

## Phase 4: reduce operator work

The browser starts from the selected root records and shows one dependency
proposal, one set of matching exceptions, and one final load preview. It
remembers approved identity and model policies for later compatible runs
without reusing credentials, source snapshots, or destination results.

**Exit gate:** A data manager can run a second similar migration without
listing all supporting models or rebuilding the same rules. Only ambiguous
matches and business decisions require manual work.

## Phase 5: qualify model breadth, size, and cutover

The current source limit of 10,000 rows per model and destination matching
limit of 1,000 distinct keys per model constrain practical migrations.
Replace large `in` requests with bounded key batches, measure memory and
Odoo calls, and then raise limits by evidence.

Run independent two-instance scenarios for Contacts, Products with generated
variants, BOMs with lines and operations, and one transactional model with
company scope and state constraints. Include partially seeded destinations,
changed source data, access restrictions, changed target keys, rejected
writes, lost responses, recovery, and repeat runs. A scenario may pass with
an expected blocker only when it proves zero writes.

Before Production use, establish the cutover window, backup and restore
checks, source freeze or delta policy, permission set, and post-load
reconciliation owner.

**Exit gate:** Representative migrations pass at the intended size on the
supported platforms. Each supported model class has a recorded field and
operation boundary. Production use begins only after separate cutover
evidence is accepted.

## First vertical scenario

Start with fictional Contacts in source A. Destination B contains one matching
Company and one existing Contact. Another Contact and its reviewed supporting
record are absent. The first pass shows exact reuse and create decisions
before writing. The confirmed load creates the approved missing records and
verifies their fields and links. The second pass proposes no duplicate
creates. An ambiguous Company match blocks before a journal is created.

Apply the same contracts next to Products and a BOM with lines and operations.
The BOM scenario exercises composite line identities and generated Product
variants; it must not add a separate transfer engine or a BOM-only matching
rule.
