---
audience: developer
kind: plan
status: proposed
---

# Check Odoo rules early and continue past isolated rejected records

## Implementation status on 2 November 2026

Conventional load continuation, rollback isolation, failure budgets, durable
stop, guarded continuation, diagnostic counters and legacy preservation are
implemented with controlled regression coverage. Current behavior belongs to
the execution contract and Stage 6 documentation. Qualification here is fixture
evidence; no live Odoo target was written during this work.

The operator chose to keep Impodo client-only. Stage 3 now explicitly reports
server VAT and other constraints as unchecked, and links authoring to the
existing reviewed row-selection journey to set aside identified rows. No local
VAT profile is enabled, and no Odoo module is installed or required. Qualified
early tax checks and automatic exclusion of newly discovered rule failures
remain proposed in this plan. The optional target-module approach below is
not selected for the current product.

## Decision and operator outcome

Propose a reviewed load policy that sets aside a record rejected for a known
data problem and continues with independent records. The load must retain
every rejection and show an incomplete result until the operator resolves or
explicitly accepts the remaining work. Repeated failures must pause further
uploads, and the operator must be able to stop them. An uncertain write must
still stop subsequent transport and require read-back.

For example, a fictional import contains 100 independent customers. One has
country France and an invalid tax identifier. Under the proposed continuation
policy, 99 customers can be written and verified, while the rejected customer
remains visible with its source trace and a correction action. If another
record depends on the rejected customer, that dependent record is set aside
too. It is not written with a missing relationship.

This document retains the design and remaining tax-check proposal. The current
[execution contract](../developer/contracts/execution-and-reconciliation.md)
continues to govern current loading and recovery.

## Established behavior and problem

Before this implementation, `ExecutionService._continue_run` stopped later writes after a known
`OdooWriteRejected` as well as after `OdooWriteOutcomeUnknown`. It retains
previously committed records and marks subsequent records as blocked. There
is no rollback of earlier successful requests.

The current writer sends bounded create groups through `Model.load`, with
deterministic External IDs. A response with `ids=False` produces a known
rejection; a malformed or lost receipt produces an unknown outcome. This
distinction must remain authoritative. An HTTP success status alone does not
establish that an import batch committed.

Odoo's published JSON-2 contract gives each request its own transaction.
Its published `Model.load` implementation also rolls back its import savepoint
when an import message reports an error. Thus a rejected batch does not prove
that its other records were accepted. Qualify this behavior separately for
the supported Odoo 19 and Odoo 20 adapters before enabling batch isolation.

The existing journal distinguishes committed, failed, blocked, in-flight and
unknown outcomes. Reconciliation independently verifies written records.
Reuse these owners rather than adding a second import queue or parallel
tracking database.

## Make Odoo the validation authority

The target's Odoo rules define accepted values. Impodo should not introduce a
second, independently chosen tax policy. A Stage 3 check must identify which
Odoo rule it implements, which target version and configuration qualify that
rule, and which mapped values were checked.

There is a feasibility boundary. Odoo's `fields_get` describes fields, while
Python constraints are executable methods and database constraints run against
target state. Custom modules can change both. Captured fields alone do not
constitute a complete validation-rule catalogue. Do not label locally checked
records as guaranteed to be accepted by Odoo.

Prefer a qualified, public, side-effect-free target validator where one
exists. Otherwise, implement a bounded profile matching the verified Odoo
rule and its dependencies, with parity tests for each supported major. For
example, published Odoo VAT code uses country-specific overrides or
`python-stdnum`; that dependency is appropriate only where the qualified Odoo
profile uses the same behavior. The current checkout does not declare it.
Review its version compatibility, licensing and packaging before adding it.

Bind the rule profile to target identity, recognized Odoo major, captured
schema, company context and the available module/rule evidence. Verify Odoo 19
and Odoo 20 separately. If metadata access cannot establish whether an override
is present, report that coverage is unverified rather than requiring broader
administrator rights or guessing a standard implementation. Do not expose
private Odoo methods through a generic RPC escape hatch.

An optional Odoo extension could publish a versioned catalogue of explicitly
registered pure validators. It would need separate installation, authorization
and parity qualification. It cannot automatically turn arbitrary custom
constraints into side-effect-free functions. This is an optional later path,
not a prerequisite for the first set of qualified Stage 3 checks.

## Check mapped values in Stage 3

Run the covered value checks when the operator explicitly checks current
matches, using the mapped and transformed logical values. A reviewed VAT rule
binds the tax field and the effective country provider. It must work with
normal, joined and derived datasets and custom field mappings. Do not infer a
country rule merely from a source column named `vat`.

For a French VAT number, check the compact representation, its two-character
key, numeric nine-digit SIREN component, and applicable checksum rules. Retain
support for valid alphanumeric keys and documented exceptions. Do not replace
this with a digits-only regular expression. Distinguish structural validity
from registration validity, which requires an appropriate external service.

For example, fictional value `001234A6789` has a letter inside its final
nine-character component. Adding `FR` cannot repair it. A country mismatch
or an identifier stored in the wrong source field must remain reviewable.

Present **N rows fail checked Odoo rules** with the rule, affected field,
bounded protected value details and source trace. Offer correction through
the source or mapping, or an explicit choice to set aside the listed records.
Do not silently exclude records when a check fails. A reviewed omission must
bind the exact source population, rule results and affected dependencies, and
must update expected counts. Missing required parents remain blocking for
their dependents. Reuse the existing row-population evidence and confirmation
owners rather than introducing an untracked exclusion list.

Reuse bounded logical-source batches and source lineage; do not reload complete
physical tables, recreate the former physical-row cap or open a database per
record. Record the rule version, covered fields, unchecked rules and policy
identity in the evidence. Changes to mapping, source, rule profile or target
context invalidate that result. Preserve the original source value. Never
invent a checksum, erase an identifier, add a country prefix or bypass Odoo
validation without an explicit reviewed transformation.

Stage 3 is the first opportunity, not the final one. Some values depend on
prepared relationships, Odoo defaults, existing target records or the eventual
create/update decision. Repeat applicable checks on actual prepared values in
Stage 4 and target-dependent checks in Final review. Surface changes as early
as their required evidence becomes available. Keep a clear coverage summary:
checked, failed, and not checked. Unknown coverage is not a pass.

Blank identifiers follow captured field requiredness and the reviewed
business rule. Unknown country rules produce an explicit unchecked finding.
A failed or unavailable online validation is distinct from an invalid number.
Online registration checks are a separate, bounded opt-in operation; they
must not become an unannounced external upload of customer identifiers.

## Do not mistake test import for a read-only validator

Published Odoo `base_import.import.execute_import(..., dryrun=True)` executes
import operations and rolls back its database writes. This can exercise more
target constraints than metadata checks, but it still requires the relevant
write privileges and runs create/write hooks. A database rollback alone cannot
prove that arbitrary custom code has no external side effects.

Do not run such imports silently on the live target from Stage 3 or with its
read-only credential. A future exact target rehearsal belongs to an explicit
qualified capability, preferably against an isolated matching target copy.
Document its access, side-effect and parity limits. It does not replace later
load-time checks because the real target can change after the rehearsal.

## Load policy and confirmation

Add a closed policy with two operator choices:

- **Stop at the first rejection** retains the current behavior.
- **Set aside rejected records and continue** permits the qualified cases below.

Bind the selected policy and its version into the existing confirmation and
durable load settings. Every worker and recovery entry must read the same
approved policy. An old journal without the setting retains the stop policy;
never reinterpret an existing partial load. A future default for new imports
can favor continuation only after the complete qualification matrix passes.

Business expectations remain authoritative. If a minimum count is not reached
because records were rejected, the outcome remains incomplete; continuation
does not silently relax the count or approve the result.

## Classify the rejection before continuing

Keep the model-independent rejection parser in the Odoo adapter. Store bounded
reason codes, validated server-reported fields and row ranges, method, batch
identity and message fingerprint. Source values remain in their existing
protected evidence, not operational logs. Recognize vendor validation wording
without assuming that a particular customer, country or field name caused it.

Continue only after the adapter proves a known uncommitted transaction and
classifies a record-level data rejection. Missing required values, invalid
field values and reviewed record constraints are candidates. A structured
reason must distinguish them from access failures and global setup problems.

Always stop for an uncertain outcome, lost or malformed receipt, transport
failure, target or principal drift, changed scope, or authorization failure.
An unclassified rejection remains a stop until its continuation semantics are
qualified. Repeated data errors use the automatic pause described below.

## Pause when failures indicate a wider problem

Continuation is not permission to try every record regardless of results.
Start each model and operation with a small bounded group, retain existing
transaction sizes, and evaluate the failure budget after every settled
transport result and isolated sub-attempt.

Proposed initial limits, subject to qualification, are:

- Pause the complete load after ten consecutive definitive record rejections
  in one model and operation.
- Pause when at least twenty distinct records have settled and the rejection
  rate reaches 20% in the latest fifty settled records for that model and
  operation. Show the actual window size when fewer than fifty have settled.
- Pause after twenty-five additional isolation calls without a successful
  commit, even if batch responses cannot identify individual faulty records.

The first rule catches an entirely failing 20,000-record import after a small
number of confirmed failures. The rate rule catches a widespread problem
interspersed with successes. The isolation-call budget bounds expensive
diagnosis when a server supplies poor row attribution. These are product
defaults to test, not evidence that a particular threshold is universally
optimal. Thresholds and their policy version belong to the confirmed load
settings and cannot change silently during a run.

Count each distinct record result once. Do not count a rolled-back innocent
batch peer as rejected, count repeated attempts twice, or count a dependent
record blocked by one failed parent as a new Odoo rejection. Keep uncertain
outcomes separate; they cause an immediate safety stop, not a rate calculation.

Group causes using validated reason codes, model, operation, trusted field or
constraint identifiers and qualified rule identity when available. Raw error
fingerprints often differ because messages contain record values; they must
not be the grouping key. If only a broad validation code is available, label
the group as repeated validation failures rather than claiming one exact cause.
The no-progress and rate limits still work when cause attribution is weak.

Persist a pause reason, thresholds, distinct counters and the transport
frontier. Present the observed failures, verified successes and remaining
unattempted population. Offer **Review failures**, **Stop further uploads**,
and a guarded **Continue with the next small group** only for known outcomes.
Continuation requires fresh binding/read-back checks and explicit approval;
it does not disable the breaker or retry failed records automatically. Changed
source values or matching rules require a fresh comparison and approval.

For example, ten isolated customers rejected for the same checked field rule
with no successful write pause the load. The other 19,990 customers remain
unattempted. The UI must not claim that all 20,000 customers are invalid.

## Let the operator stop further uploads

Add an authenticated **Stop further uploads** action to progress and failure
views. Persist the request for the exact run through a bounded local command.
Closing the browser is not the stop mechanism. A stop request must survive
page reloads and process restart, and a stopped queue must not restart writes
automatically.

The worker and journal must atomically check the stop request before marking
each new write batch in flight. This includes split batches, updates and
relationship-completion calls. If the request was already in flight when the
stop was accepted, let it finish and durably record its receipt. Show
**Stopping after the current request** while that happens. Do not kill the
request and thereby manufacture an uncertain outcome.

Then stop further writes and reconcile committed records. An uncertain current
response still requires read-back before any continuation. Show **Stopped
with partial results**, with counts for written, rejected, blocked and
unattempted records. Stopping does not undo earlier commits or relax business
expectations. Keep resume and corrected-subset actions distinct.

## Isolate a rejected batch

1. Journal each original batch before transport, preserving stable row
   identities, External IDs and the approved dependency order.
2. On a confirmed rolled-back import rejection, retain the original batch's
   response evidence. A batch-relative range is not a commit receipt for
   other rows in that batch.
3. If the qualified response proves exact rejected rows, set those aside and
   submit the remaining rows in new bounded transport attempts. Otherwise,
   bisect the rejected batch deterministically until the rejected records are
   isolated. Do not mark innocent batch peers as individually rejected.
4. Journal every sub-attempt before its call. Reuse original External IDs and
   unchanged prepared values. Track attempt lineage and retain original
   evidence rather than overwriting it.
5. On an unknown outcome at any depth, stop immediately and use existing
   read-back recovery. Do not split, resend or retry the uncertain rows.
6. Continue independent ready groups after a known isolated rejection, then
   reconcile every accepted write.

Bound additional calls and split depth by the accepted original batch size.
Cancellation and crashes between each journal/transport/receipt boundary must
leave an assessable journal. External IDs help identify writes; they do not
replace absence checks or authorize retrying an unknown outcome.

## Dependencies and relationship completion

Use the frozen dependency graph and receipt requirements. A rejected parent
blocks dependent rows, while unrelated records remain eligible. Preserve
separate reason codes for rejected records and blocked dependents. Never
replace an unresolved relationship with a blank value or silently create a
different supporting record.

For cycles and deferred relationship completion, retain created receipts and
partially applied states. Set aside uncompleted dependent work when a required
member fails. Continue outside the affected dependency group only when its
independence is proved. The outcome must expose partial relationship work;
the group cannot be reported as verified merely because its first creates
committed.

Apply the same distinction to updates and completion writes. Shared updates
must not be split unless the adapter qualifies their transaction and receipt
semantics. Identity or authorization failures continue to stop the load.

## Outcome and correction journey

Show separate totals for verified, rejected, blocked by a dependency, not
attempted, and uncertain records. Keep expected, attempted and verified counts
distinct. Offer a rejected-record view and protected export with source trace,
record identity, target field when known, reason and next action.

Do not enable replay of the original full create plan. A corrected subset
requires fresh target comparison and approval. Previously created records must
be recognized through existing business keys and journal receipts. Preserve
the existing exact-ID correction workflow for changes to committed records.
Use a successor attempt or workspace when current ownership requires it;
do not mutate immutable source or approved execution evidence in place.

## Implementation sequence and verification

1. Add pure rejection and identifier policy contracts, bounded diagnostics and
   the versioned load-setting binding. Keep old journal layouts readable with
   their exact original hashes and stop behavior.
2. Add country-qualified prechecks to the existing preparation/evidence path.
   Expose qualified Odoo checks in Stage 3 and repeat them when actual prepared
   values become available. Verify direct, joined and Recipe inputs without
   extra Odoo writes, and make unchecked coverage visible.
3. Extract batch isolation into a focused application collaborator, injected
   with narrow transport and journal ports. Avoid growing the main execution
   service with another large nested transport loop.
4. Add dependency-aware continuation, automatic failure budgets and durable
   operator stop controls. Cover updates, completion handling, bounded
   progress and the incomplete-result projection.
5. Add the confirmation choice, rejected-record correction journey, current
   documentation and reproducible isolated screenshots with implementation.

Run focused Odoo 19 and Odoo 20 coverage for local and remote access, authoring
and Recipe runs, followed by the complete repository gate. Cover single and
multiple bad rows, rejected batches with innocent peers, partial ranges,
transaction rollback, missing receipts, timeouts at every split depth,
dependencies, cycles, updates, completion writes and exact retry identities.
Include standard and custom models, custom target validation overrides and
unknown rule coverage. Check the 20,000-record all-fail case, interspersed
successes, distinct-count accounting during splitting, weak error attribution,
pause/restart persistence and races between stop requests and transport entry.
Verify that an in-flight result is retained, previously committed records are
never replayed, and a pause cannot bypass changed business expectations.
Measure request count, bounded memory, database opens and schema checks
without wall-clock assertions in CI.

Qualify Odoo-to-Odoo transfers and completed-load correction separately before
enabling the new policy on those paths. They retain strict behavior until their
scope, journal and recovery contracts have corresponding coverage.

No live write was attempted for this proposal. Live Odoo 19/20 transaction and
continuation qualification remains implementation work.

## Primary references

- [French tax authority: identification numbers](https://www.impots.gouv.fr/professionnel/les-numeros-didentification).
- [French tax authority: VAT identification structure](https://bofip.impots.gouv.fr/bofip/1149-PGP.html/identifiant%3DBOI-TVA-DECLA-20-10-20-20220216).
- [Maintained French VAT validator](https://github.com/arthurdejong/python-stdnum/blob/master/stdnum/fr/tva.py).
- [Published Odoo VAT validation](https://github.com/odoo/odoo/blob/19.0/addons/base_vat/models/res_partner.py).
- [Published Odoo field metadata and constraint execution](https://github.com/odoo/odoo/blob/19.0/odoo/orm/models.py).
- [Published Odoo test-import implementation](https://github.com/odoo/odoo/blob/19.0/addons/base_import/models/base_import.py).
- [Published Odoo JSON-2 transaction contract](https://github.com/odoo/documentation/blob/19.0/content/developer/reference/external_api.rst#transaction).
- [Published Odoo import rollback implementation](https://github.com/odoo/odoo/blob/19.0/odoo/orm/models.py).

Public Odoo 19 source establishes the cited upstream behavior. The reported
Odoo 20 failure was identified from its saved response fingerprint. This is
not live Odoo 20 continuation qualification. The implemented continuation has
controlled regression coverage for both majors; no live acceptance run or
Odoo 20 VAT-rule parity is claimed.
