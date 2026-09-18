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
The source screen now lists writable relationship fields whose related model
is outside the selected scope, using captured metadata. This is guidance;
automatic bounded capture of the related rows is still unfinished.
When selected record types are captured together, publication now rejects
links whose target row was excluded from the frozen set. The destination
matching page supports a first text field and up to two more captured text or
integer fields. Matching, approval, preflight, relationship resolution, and
execution use the same ordered fields. The first field still bounds the
destination read to 1,001 rows; a wide first-field match is blocked.
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

The current scalar composite match does not resolve a parent or company
relationship as an identity component. Such records still need the scoped
identity work above. The current publication check validates only links
between selected record types; it does not yet propose or capture missing
related rows automatically.
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
