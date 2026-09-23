# Support data managers through the six-stage Authoring workflow

## Status and decision

**Status:** In progress, 2026-09-23. The first Stage 2 slice of Proposals 1 and
2 and the direct-identity slice of Proposal 3 are implemented. Every remaining
change is still proposed unless a section explicitly marks it as current
behavior.

This plan is for the product owner and the developers who own Impodo's
six-stage Authoring workflow. It proposes how Impodo should help a data manager
make difficult migration decisions earlier, understand why work needs
attention, and return directly to the decision that must change.

The decision is to improve both the process and the browser together. A new
check without a clear correction route leaves the data manager blocked. A new
screen without authoritative evidence can create false confidence. Each
proposal therefore pairs one process change with one interface change.

### Implemented first slice

Stage 2 now derives direct supporting-model dependencies from live captured
Odoo fields and shows **Supporting data Impodo found**. It separates reuse of
existing records, checked defaults, Odoo-managed fields, and related incoming
business data without silently widening the write scope. A user can open the
record-type choices with an incoming model preselected, but must still save
that scope explicitly.

The slice also introduces a shared read-only workflow-issue projection. Stage
2 uses it when a related model is unavailable: the browser states the cause,
owner, affected fields, preserved work, correction route, and recheck. A
required unavailable reuse dependency blocks matching-rule confirmation in
both the interface and application service.

### Implemented second slice

Stage 3 now reuses its explicit bounded Odoo table-order check to prove saved
direct identities against current included source rows and records visible to
the current read user. **Identity health** separates **Suggested**, **Chosen -
not tested**, **Tested on current data**, **Tested - needs attention**, and
**Needs refresh**. It reports aggregate blank, repeated, exact, ambiguous, new,
existing, and blocked counts without publishing key values or numeric Odoo
IDs.

This slice deliberately does not weaken a relational identity into an
unscoped scalar check. A parent-, company-, or other related-record component
remains explicitly not tested until Proposal 6 provides the relationship
simulator. Final review remains authoritative.

This slice does not yet pair dependencies with accepted source tables, derive
recursive Recipe or Odoo-source capture closure, authorize supporting reads,
include optional many-to-one or many-to-many fields without source or Recipe
intent, prove relationship values, or add the issue summary to global
navigation.

The impact score measures the benefit to the data manager, not implementation
cost:

- **10/10** prevents a likely wrong or duplicate Odoo write and avoids major
  rework.
- **8/10** removes a frequent blocker or a long diagnosis loop.
- **6/10** mainly improves speed or clarity without changing migration safety.

## User goal

A data manager should be able to answer six questions before Impodo writes to
Odoo:

1. Did I accept the correct business records, and has Impodo identified the
   supporting Odoo data that those records need?
2. Can Impodo identify one intended Odoo record without using a numeric Odoo
   database ID?
3. Will every child record find exactly one intended parent, and will every
   other populated relationship resolve to exactly one intended record?
4. Does every required field have an explicit source value, approved constant,
   checked Odoo default, or Odoo-managed reason, and can Odoo store every
   prepared number without losing decimal precision?
5. Does the proposed result match the business purpose of this migration?
6. If work stops or needs correction, what is the one safe next action?

For the data manager, this means that Impodo should find identity,
relationship, and required-value risks as soon as enough evidence exists. It
must still repeat the authoritative checks against current prepared data and
fresh Odoo evidence before loading.

## Priority frustrations this plan must resolve

Four recurring failures should determine the first delivery slices and their
acceptance criteria.

### Technical Odoo dependencies must be Impodo's responsibility

In Stage 2, the data manager should choose the business records they intend to
migrate, such as Products or Bills of Materials. They should not need to know
in advance that the current Odoo configuration may also require Product
Categories, Units of Measure, Companies, Products used by Bills of Materials,
or another installed application's related record type.

Impodo should derive that dependency plan from the accepted source structure,
the selected top-level business records, the current captured Odoo schema, and
any applied Recipe. It should classify each related model as:

- incoming business data that the migration intends to create or update;
- an existing Odoo reference that Impodo reads only for matching;
- an Odoo-managed or checked-default dependency that needs no incoming data;
  or
- an unresolved business decision that prevents the workflow from advancing.

The normal path should ask only the remaining business question, such as
**Migrate the Categories in this Data version** or **Reuse existing Odoo
Categories**. Technical model names such as `product.category`, `uom.uom`, and
`res.company` belong under **Support details**. Discovering a supporting model
must never silently authorize Impodo to create or update that model.

### Missing parents must be found before dependent records advance

A Bill of Material Line can depend on its parent Bill of Material. A Bill of
Material can depend on a Product, and that Product can come from another
incoming table or already exist in Odoo. The data manager currently experiences
the same business problem at different times depending on the relationship
origin. An incoming parent can fail during preparation, while a target-only or
Odoo-first parent can remain unresolved until Final review.

Impodo should show **Parent ready**, **Parent missing**, **Parent ambiguous**,
or **Parent set aside** as soon as the current evidence can prove that state.
The message must name the child record type, parent record type, relationship
field, relationship origin, affected record count, and correction route. The
workflow must not allow a dependent stage to look available while a required
parent remains unresolved.

### Decimal-capacity problems must be found before Load into Odoo

The captured Odoo schema already contains the target field's `digits`
metadata. After Stage 4 produces exact prepared Decimal values, Impodo can
compare those values with the captured target capacity without contacting
Odoo. Stage 4 should report potential precision loss across all prepared
values. The shared Stage 5 comparison should then identify the exact intended
writes that would lose precision.

For example, if a prepared quantity is `1.237` and the captured Odoo field can
store only two decimal places, the data manager should see the problem before
the Load page. The correction must remain explicit: either approve a documented
rounding rule in **Match data**, or ask the Odoo owner to increase the field
precision and then refresh **Odoo data**. Impodo must never round silently or
present a unit conversion as a precision fix.

### A blocked stage must always explain the correction journey

A grey or locked stage and a generic **Cannot continue** message are not enough.
Every blocked-stage projection should answer all of these questions:

1. Why is this stage blocked?
2. Which record types, fields, and records are affected?
3. Is the cause in the source data, an incoming relationship, current Odoo
   data, an Odoo field definition, or an interrupted Impodo action?
4. Who owns the correction?
5. Which exact page and decision should the data manager open?
6. Which accepted source data and saved rules remain safe and unchanged?
7. Which check must run after the correction?

The browser should display one primary correction action. It may show
alternative business decisions, such as changing Odoo precision instead of
rounding, but it must explain the consequence of each choice.

## Current workflow and pressure points

Impodo currently separates reusable meaning, complete source-row evaluation,
fresh Odoo comparison, and write verification. That separation is safe, but it
can expose a problem only after the data manager has invested work in later
stages.

| Stage | Current responsibility | Main pressure point |
| --- | --- | --- |
| **1. Source data** | The data manager confirms the exact source tables and accepts them as immutable evidence for the Data version. Odoo-source capture can follow selected relationships. | A missing supporting record type or child table can remain unnoticed. The current Odoo-source review confirms relationship fields as a group; individual edge approval and automatic proposals for missing model types are not implemented. |
| **2. Odoo data** | Impodo captures Odoo 19 models, fields, requirements, selections, relationships, defaults, and optional uniqueness metadata. It now derives direct required supporting relationships and inverse-owned child data, then confirms one matching rule for every selected writable record type. | The first slice does not yet pair optional relationships with accepted source or Recipe intent, authorize arbitrary supporting reads, or prove that current source and target values are populated and unique. |
| **3. Match data** | The data manager defines row inclusion, record identity, scalar values, transformations, defaults, and relationships. Impodo checks the mapping and can produce a review workbook. | This stage contains the highest number of related decisions. Combined identities expose technical controls, and one-to-many guidance does not take the user directly to the child field that owns the relationship. |
| **4. Prepare data** | Impodo applies the confirmed mapping to every accepted row, accounts for every row, resolves source duplicates, evaluates incoming relationships, and freezes normalization decisions. | Preparation is intentionally target-independent. It cannot prove that a target-only or Odoo-first relationship exists in the current Odoo database. |
| **5. Final review** | Impodo reads current Odoo evidence, classifies every eligible prepared row, and produces the final action queue and execution snapshot. | A target-only identity or relationship problem may first become visible here, which sends the data manager back through a long correction loop. |
| **6. Load into Odoo** | Impodo revalidates the reviewed target scope, writes in dependency order, journals each attempted row, and verifies the result through read-back. | The safety controls are strong, but the data manager must still understand the exact business impact and whether an interrupted action is safe to resume. |

The current boundaries are defined by the paired
[user workflow](../user/workflow/01-source-data.md) and
[developer workflow](../developer/workflow/01-source-data.md) pages registered
in [`workflow.yml`](../workflow.yml). Stage 2 implements direct supporting-data
discovery plus prefilled, evidence-labelled matching rules; its qualified
current boundary is documented in the
[Odoo data developer workflow](../developer/workflow/02-odoo-data.md).

## Design rules for every improvement

Every delivery in this plan must preserve these rules:

- Impodo may suggest a choice, but the data manager confirms business meaning.
- The data manager chooses business scope. Impodo derives the bounded technical
  dependency scope from current source and Odoo evidence.
- Automatically discovered supporting models remain read-only unless accepted
  incoming data and an explicit reviewed decision place them in the write
  scope.
- Impodo must not infer identity from a translated label, choose the first
  available Odoo record, or treat a near match as equal.
- Portable mapping and Recipe evidence use business keys and scope. Numeric
  Odoo database IDs remain protected target-bound evidence.
- Opening a page must not trigger an Odoo request. A live check starts only
  after the data manager selects an explicit read-only action.
- Odoo metadata and record requests must be grouped and bounded by model and
  distinct key. No proposal may add a request per source row or browser field.
- An early check provides authoring guidance. **Final review** remains the
  authoritative fresh target comparison, and **Load into Odoo** retains its
  exact confirmation, journal, recovery, and reconciliation boundaries.
- A changed source selection, Odoo schema, matching rule, mapping, prepared
  result, target, or credential identity makes dependent guidance stale. The
  browser must explain what changed and which check must run again.
- A warning, blocker, or set-aside decision must state the cause, affected
  record or group, source or target origin, responsible role, and correction
  route.
- A required parent must be classified as ready, missing, ambiguous, or set
  aside before its dependent records can advance. The message must distinguish
  an incoming parent from a record expected to exist in Odoo.
- Exact prepared Decimal values must be checked against captured Odoo `digits`
  metadata before the Load page. Impodo must not round, truncate, or infer a
  unit conversion.
- The normal browser path uses business labels and one clear next action.
  Technical identifiers and hashes remain under **Support details**.

## Proposal 1: Add one cross-stage issue register

**Delivery status:** First projection contract and Stage 2 issue summary
implemented; persistent cross-stage navigation and remaining stage adapters
are proposed.

**Owning stages:** All six stages.  
**User impact:** **9.5/10**.

### Process improvement

Each stage should continue to own and store its authoritative evidence. A
shared presenter should project current findings into one common issue shape:

- severity and current state;
- business cause and affected record count;
- source, incoming-table, or target origin;
- responsible role;
- owning stage and correction route; and
- evidence revision and freshness;
- work that remains accepted and unchanged; and
- the check required after correction.

This projection must not become a second mutable issue database. When the
authoritative stage evidence changes, the presenter recalculates whether an
issue is current, resolved, or stale.

### Interface improvement

Add a persistent **Migration issues** summary to the workspace navigation. It
should show counts such as **3 Must fix** and **2 Review**. A drawer or dedicated
page should sort root causes before dependent findings.

Every issue should expose one concrete action, such as **Open Product
identity**, **Open Unit relationship**, **Review source row**, or **Prepare data
again**. After the user returns from that action, the register should say which
check must run before the issue can be considered resolved.

A locked navigation stage should expose the same explanation rather than only
showing a disabled link. For example:

```text
Final review is locked
Why: 7 Bill of Material Lines cannot find their Parent Bill of Material
Fix in: Match data -> Bill of Material Lines -> Parent Bill of Material
Your accepted source data and saved field matches remain unchanged
Then: Check matches and prepare data again
```

### Impact justification

Today, the same migration problem can appear as a Stage 3 validation reason, a
Stage 4 quality finding, and a Stage 5 comparison blocker. A common projection
prevents the user from searching six stages and repeatedly interpreting the
same cause. The score is below 10 because this improvement coordinates existing
safety evidence rather than detecting a new class of unsafe write by itself.

### Acceptance checks

- Every current blocking issue remains visible outside a paged or filtered
  field list.
- A direct action opens the owning stage and affected decision without changing
  it.
- A stale issue cannot appear resolved merely because the user visited its
  correction page.
- Every locked stage states why it is locked, what remains saved, the owning
  correction action, and the check that follows the correction.
- A generic **Cannot continue**, **Needs attention**, or **Blocked** label is
  never the only explanation for unavailable navigation.
- The projection contains no source values, target-bound identifiers, or
  credentials unless an existing protected page already authorizes them.

## Proposal 2: Derive supporting Odoo models from business scope

**Delivery status:** Direct required-relationship and inverse-child derivation
from the live schema, plus the Stage 2 review UI, are implemented. Source-table
pairing, optional-field intent, explicit reuse authorization, Recipe closure,
Odoo-source edge proposals, and value-level proof are proposed.

**Owning stages:** Source data and Odoo data.  
**User impact:** **10/10**.

### Process improvement

The data manager should start Stage 2 by selecting top-level business records,
not by assembling a technical Odoo model graph. After the user chooses
Products, Bills of Materials, Contacts, or another business area, Impodo should
build a bounded dependency plan from:

- accepted source tables and generated related tables;
- the chosen top-level business records;
- current Odoo field requirements, relationships, inverse fields, and usable
  defaults;
- reviewed standard-reference policies; and
- an applied Recipe's required portable Odoo contract.

The planner should examine required create-time relationships first. It may
also include an optional relationship when accepted source evidence or a saved
Recipe shows that the migration intends to fill it. It must not recursively
select every model reachable from Odoo, because that would widen scope without
business meaning.

For every discovered dependency, Impodo assigns one role:

- migrate the related records from another incoming table;
- reuse existing records in the destination Odoo;
- let Odoo manage or default the relationship;
- leave an optional relationship empty; or
- ask the data manager one unresolved business question.

Impodo may add a related model automatically to a narrow supporting-read plan
when current policy authorizes the exact key, scope, display fields, and read
purpose. That model does not enter the migration write scope. If accepted
source data contains a table that should create or update the related records,
Impodo proposes that incoming table and asks the data manager to confirm its
business role once.

For Odoo-source capture, the same planner may propose missing related record
types and individual relationship edges. The user confirms the business
capture scope; Impodo handles the technical dependency closure behind it.

### Interface improvement

Replace the normal technical-model checklist with two progressive steps.

First, show **What are you migrating?** with business choices such as Products,
Bills of Materials, Contacts, and Orders. Second, show **Supporting data Impodo
found** only when the user must review a business effect.

For example:

| Your business data | Supporting data | Impodo's proposal |
| --- | --- | --- |
| Products | Product Categories | Migrate the incoming Categories table |
| Products | Units of Measure | Reuse checked existing Odoo Units |
| Bills of Materials | Products | Use the incoming Products table first, otherwise existing Odoo Products |
| Bills of Materials | Company | Use the checked Odoo company context |

Each unresolved row should offer a constrained business action such as
**Migrate incoming data**, **Reuse existing Odoo data**, or **Do not fill this
optional relationship**. The default summary should say **Impodo will handle
this supporting data** when no user decision remains.

The advanced view may expose the complete technical closure and exact Odoo
models. A one-to-many field should be phrased through the child record that
owns the inverse many-to-one link.

### Impact justification

This removes Odoo implementation knowledge from the normal data-cleaning job.
It prevents a user who selected Products or Bills of Materials from learning
about required supporting models only through later blockers. Because this is
a frequent early source of cascading relationship failures, the proposal now
receives the maximum impact score. A correct dependency plan still needs Stage
3 relationship rules and later value-level proof.

### Acceptance checks

- The normal path does not require the user to know or manually select a
  technical supporting model name.
- Selecting Products derives every supporting relationship required by the
  current captured Product schema. Selecting Bills of Materials derives its
  Product, Unit, Company, line, and other dependencies only when the live
  schema and accepted source make them relevant.
- The same algorithm works for compatible custom models; Product and Bill of
  Material examples must not become hard-coded dependency lists.
- Choosing **Reuse existing Odoo data** places the model in an authorized
  supporting-read scope, never the write scope.
- Choosing **Migrate incoming data** requires a compatible accepted table and
  an explicit reviewed write-scope decision.
- A usable checked Odoo default or Odoo-managed inverse relationship does not
  force the user to supply or migrate a redundant table.
- A required unresolved dependency blocks completion with the business reason
  and one correction action. An optional unused dependency does not.
- Proposed incoming additions retain the current source-freeze boundary and
  cannot invent source records that were not accepted in the Data version.
- The dependency walk is bounded, deduplicates cycles and repeated paths, does
  not traverse unrelated optional relationships, and still explains every
  affected business relationship.

## Proposal 3: Prove matching identities against current data

**Delivery status:** Direct scalar identity proof and the Stage 3 Identity
health UI are implemented. Relational key/scope proof, protected duplicate
group review, target-wide blank inventory, and update-versus-unchanged
forecasting remain proposed.

**Owning stages:** Odoo data and Match data.  
**User impact:** **10/10**.

### Process improvement

Impodo should present three separate identity states:

1. **Suggested** means the captured schema or reviewed Odoo policy supports the
   matching rule.
2. **Chosen** means the data manager confirmed its business meaning.
3. **Tested on current data** means an explicit bounded check examined the
   complete key and scope against the current source and target.

Stage 2 can state whether Odoo metadata enforces the selected rule or whether
the rule is a reviewed convention. After Stage 3 binds source columns to the
confirmed target key, one explicit read-only identity check should report:

- blank source and target key components;
- duplicate source key groups;
- duplicate or ambiguous target key groups;
- inaccessible target records; and
- expected create, update, unchanged, and blocked counts when enough mapping
evidence exists.

The current read identity defines what Impodo can observe. The interface must
not present an unmatched key as proof that no access-hidden Odoo record exists;
it states the read-user and company-context boundary instead.

The check must use the complete parent, company, or organizational scope. It
must classify duplicate source rows for later review rather than silently
merging or rejecting them.

### Interface improvement

Add an **Identity health** card to every selected record type. For example:

```text
Product identity: Internal Reference
Basis: Reviewed Odoo convention
Source: 4,320 unique, 3 blank, 12 repeated
Target: 4,011 unique, 2 ambiguous
Expected: 309 new, 4,009 existing, 2 blocked
```

The card should provide **Check this matching rule**, **Review duplicate
groups**, and **Change matching rule**. It should label untested suggestions as
**Not yet checked on current data**, never as safe or unique.

### Impact justification

A wrong identity can update an unintended Odoo record or create large numbers
of duplicates. Proving the complete key earlier gives this proposal the maximum
impact score. Stage 5 must still repeat the authoritative comparison because
the target can change after authoring.

### Acceptance checks

- The check uses no numeric Odoo database ID as portable identity.
- A company-scoped or parent-scoped rule is never tested as an unscoped field.
- Bounded target reads follow the existing distinct-key and model batching
  limits.
- A target or mapping change marks the result **Needs refresh** instead of
  reusing it.
- A tested identity does not authorize a load or replace Final review.

## Proposal 4: Add a required-values and Odoo-default assistant

**Owning stages:** Odoo data and Match data.  
**User impact:** **8.5/10**.

### Process improvement

For every required field used when creating records, Impodo should classify
the current decision as one of these closed choices:

- supplied by a source field;
- supplied as an approved constant;
- supplied by a checked Odoo default;
- managed by Odoo; or
- still missing.

The current bounded `default_get` evidence should remain the source for an
Odoo-default suggestion. Impodo must not use the first selection value or the
first related record as a default. A Many2one default may display a friendly
record label, but its protected Odoo identity remains bound to the current
target, read user, and company context.

A changed schema, principal, company context, or target should require a new
default check. A Recipe may preserve the decision that Odoo supplies a value;
it must not carry the target-specific numeric record ID into another run.

### Interface improvement

Add one **Required values** checklist above the complete field catalogue. Each
item should show the current source of the value, the proposed value when it is
safe to display, its target context, and the number of new records affected.

The user should choose **Accept this Odoo default**, **Map a source value**, or
**Use a fixed value**. A grouped confirmation may accept several identical,
current Odoo-default decisions, followed by one complete mapping check.

### Impact justification

This removes repeated required-field blockers and makes Odoo behavior visible
before preparation. It scores below the identity and relationship work because
the current workflow already captures supported required defaults and can
recover them after a validation blocker.

### Acceptance checks

- No unavailable, blank, malformed, or unsupported default is suggested.
- A checked default remains a warning-bearing human decision.
- Grouped acceptance performs one complete validation after saving all choices,
  not one validation or Odoo request per field.
- The UI distinguishes **Let Odoo choose** from **Odoo manages this field**.

## Proposal 5: Provide a safe mapping starter and coverage score

**Owning stage:** Match data.  
**User impact:** **8/10**.

### Process improvement

Impodo should propose source-to-target field matches only when strong evidence
supports them. Accepted evidence can include a Recipe binding, an exact
technical-name match with a compatible type, an already confirmed generated
table rule, or another versioned policy. A fuzzy display-label similarity must
not create a proposed match by itself.

Suggestions remain a working draft. The data manager reviews their business
meaning, runs **Check matches**, and confirms the exact checked revision. The
normal review order should be record identity, required fields, selection
values, relationships, and then optional scalar fields.

### Interface improvement

Start each table with a mapping coverage summary:

```text
Record identity     Complete
Required values     7 of 8
Selection values    3 need attention
Relationships       4 of 6
Optional fields     18 not mapped
```

Show the evidence and proposed effect for each suggestion. Let the user review
the complete supported set and apply it once, while unresolved or low-evidence
fields remain open individually.

### Impact justification

The starter reduces repetitive configuration and directs attention to blocking
decisions. It scores below the live identity and relationship checks because a
suggestion accelerates work but cannot prove that current values are safe.

### Acceptance checks

- A suggested field match never appears confirmed before **Check matches** and
  human confirmation.
- An advanced user change survives validation failure and page reload.
- No suggestion widens the selected Odoo model or writable field scope.
- Recipe-based suggestions report current value coverage instead of assuming
  that replacement data matches the authoring data.

## Proposal 6: Add a relationship-resolution simulator

**Owning stage:** Match data.  
**User impact:** **10/10**.

### Process improvement

For every Many2one or Many2many mapping, an explicit read-only check should
evaluate the distinct current source keys in bounded batches. It should
classify each populated choice as:

- one existing Odoo record;
- one record in the selected incoming table;
- missing;
- ambiguous;
- a case-only mismatch; or
- an invalid or incomplete compound key.

For **Use Odoo first, otherwise use the incoming table**, the result should
show the exact split between reused target records and incoming records. A
One2many relationship should direct the data manager to the child dataset and
inverse Many2one field that owns the portable link. Impodo must never pretend
that the parent independently writes a child list.

Required parent relationships receive a dedicated readiness check. For
example, Impodo should prove whether each Bill of Material Line resolves to one
incoming Parent Bill of Material and whether each Bill of Material resolves to
one incoming or existing Product. It should retain the relationship origin so
a missing incoming parent is never reported as a missing existing Odoo record,
or the reverse.

This proposal differs from the Stage 2 dependency assistant. Stage 2 decides
which business records, incoming tables, and read-only supporting models
participate; this Stage 3 check proves how the actual mapped values resolve.

### Interface improvement

Add a coverage summary to each relationship card:

```text
8,412 rows checked
7,900 reuse an existing Odoo Category
487 use the incoming Categories table
21 are missing
4 match more than one Category
```

The card should offer **Review 25 unresolved choices**, **Open related table**,
and **Change matching rule**. For One2many, use an action such as **Open Bill of
Material Line -> Parent Bill of Material** rather than a generic explanatory
warning.

For a required parent, the card should also show a compact dependency result:

```text
Parent Bill of Material
1,240 child lines have one parent
7 child lines have no parent
2 child lines match more than one parent
Next action: Review unresolved parent keys
```

### Impact justification

Relationship failures are difficult to diagnose and can set aside a complete
business group. Showing concrete resolution before full preparation prevents
one of the most expensive classes of migration rework, so this proposal
receives the maximum impact score.

### Acceptance checks

- Requests are grouped by related model, matching rule, scope, and distinct
  key; no source-row request loop is allowed.
- Missing, ambiguous, and case-only matches remain different outcomes.
- Every populated required parent key resolves to exactly one incoming or
  existing parent before the relationship can report **Ready**.
- A missing parent message names the child dataset, parent model, affected
  relationship field, origin, and correction route.
- A target-first match reuses the existing record and does not authorize an
  update merely because it won the relationship lookup.
- The check never stores a numeric Odoo database ID in portable mapping or
  Recipe evidence.
- Stage 4 and Final review remain authoritative for complete row and current
  target evidence.

## Proposal 7: Show rule effects beside the edited field

**Owning stage:** Match data.  
**User impact:** **7.5/10**.

### Process improvement

After the data manager checks a rule, Impodo should reuse the current
transformation-impact capability to report affected counts, newly blank
values, invalid results, fallbacks, and representative before-and-after values.
Saving a draft remains separate from checking it. The preview must bind to the
exact checked mapping and accepted source evidence.

The complete matching review workbook remains available for a durable review.
The inline preview should remove the need to create and open a workbook for
each ordinary field correction.

### Interface improvement

Place a compact **Effect on current data** panel inside the edited field card:

| Source value | Prepared value | Rows | Result |
| --- | --- | ---: | --- |
| ` kg ` | `kg` | 1,284 | Changed |
| blank | `PCE` | 32 | Fallback used |
| `unknown` | Not produced | 4 | Must fix |

The sticky action area should continue to distinguish **Unsaved changes**,
**Saved - needs checking**, **Checked - ready to confirm**, and **Confirmed**.

### Impact justification

This makes transformations understandable while the user remembers the rule
they edited. Its score is lower because the optional impact review and matching
review workbook already provide much of the underlying evidence.

### Acceptance checks

- The preview cannot acknowledge a warning or confirm the mapping.
- A new draft makes the previous preview visibly stale.
- Protected Odoo-source business values remain outside portable previews and
  workbooks.
- Preview evaluation makes no Odoo call.

## Proposal 8: Turn Stage 4 findings into root-cause repair plans

**Owning stage:** Prepare data.  
**User impact:** **9/10**.

### Process improvement

Impodo should group complete preparation findings by root cause before it lists
dependent records. For each root cause, it should calculate how many parent,
child, and sibling records became unsafe because of that cause. It should
retain the direct or inherited classification, responsible role, source row,
field label, and owning correction stage.

Stage 4 should also compare every exact prepared Decimal value with the
captured Odoo field's `digits` metadata. This local check reports **Potential
precision loss** because Stage 4 does not yet know which prepared differences
will become writes. It must identify the affected dataset, friendly field,
source rows, prepared decimal scale, captured target capacity, and correction
routes. It must use Decimal semantics and must not round the value.

The plan remains a projection of frozen quality evidence. Selecting a
correction action must not edit accepted source data, mapping evidence, or
prepared rows automatically. After the user corrects the owning input, Impodo
starts one fresh complete preparation attempt.

### Interface improvement

Show root-cause cards such as:

```text
Missing Product P-104
Directly affects 1 Bill of Material line
Sets aside 1 Bill of Material and 18 additional lines
Owner: Product data manager
Next action: Open Product relationship rule
```

The complete affected-record list should remain available through bounded
paging. The main page should focus on the few causes that unlock the largest
number of records.

Show numeric-capacity cards in the same action queue. For example:

```text
Potential precision loss: Quantity
Prepared value 1.237 needs 3 decimal places
Current Odoo field supports 2 decimal places
37 prepared rows are affected
Actions: Adjust the number rule | Review Odoo field precision
```

### Impact justification

The user can fix one cause instead of interpreting many consequential errors.
Current preparation already distinguishes direct findings from inherited
dependencies; this proposal makes the complete impact and correction route
actionable. Moving the first Decimal-capacity warning into preparation also
prevents the data manager from discovering a predictable schema mismatch on
the Load page.

### Acceptance checks

- Equivalent causes use one stable group without discarding any row finding.
- Counts distinguish direct records from records set aside through dependency
  propagation.
- Missing-parent groups distinguish the direct child finding from every parent,
  sibling, or dependent record set aside because of it.
- The local Decimal check uses the exact prepared value and captured `digits`
  metadata and labels its result as potential until the intended write set is
  known.
- The correction link opens the owning source or mapping decision and does not
  mutate it.
- A new preparation recalculates the group from current evidence instead of
  marking the historical issue resolved in place.

## Proposal 9: Expose the same Odoo comparison at the end of Stage 4

**Owning stages:** Prepare data and Final review.  
**User impact:** **9.5/10**.

### Process improvement

After Stage 4 freezes resolved and normalized prepared data, the data manager
should be able to start the existing **Compare with Odoo** action from either
Stage 4 or Stage 5. Both entry points must use the same comparison job, saved
target snapshot, report, and execution snapshot. A normal Stage 4-to-Stage 5
journey must not make two Odoo scans.

When saved comparison evidence proves that a problem is isolated, Impodo may
offer **Set aside affected groups for this load**. The decision must include
the complete parent, child, and sibling closure required to preserve identity
and relationship meaning. Impodo must show every omitted record and reason in
the review workbook. It must never silently drop a row, choose a different
related record, or reduce scope when the remaining result cannot be proven
safe.

The same comparison and execution-snapshot construction should apply the
existing `TARGET_NUMERIC_PRECISION_LOSS` rule to the intended write set. It
should promote the Stage 4 potential finding to an exact blocker only for
records that would actually write a value Odoo cannot represent without loss.
This finding must appear in Stage 5, before **Load into Odoo** becomes
available.

This proposal adopts the detailed safety and performance boundaries in the
[Stage 4 comparison and deferred-record plan](stage4-comparison-and-deferred-record-groups.md)
rather than creating a second target-reference checker.

### Interface improvement

At the end of Stage 4, show one of these states:

- **Odoo references not checked yet** with **Compare with Odoo**;
- **Comparison in progress** with **Open progress**;
- **Current comparison available** with **Open current comparison**; or
- **Comparison needs refresh** with the exact changed input.

For isolatable blockers, **Preview affected groups** should show prepared,
already set aside, proposed omitted, remaining write, and still blocked counts
before the data manager confirms any reduced scope.

The same page should show **Numeric precision checked** and group exact
precision blockers by record type and field. Each group should state the
required decimal capacity, current Odoo capacity, affected intended writes,
and two explicit correction routes:

- **Adjust the number rule** returns to the exact field in **Match data** and
  requires a business-approved rounding method.
- **Review Odoo field precision** returns to **Odoo data** after the Odoo owner
  changes the field, then requires fresh schema, mapping, preparation, and
  comparison evidence.

### Impact justification

The data manager discovers target-only problems earlier and can keep unrelated
safe business groups moving. The score is high because this shortens one of the
longest correction loops and moves exact numeric-capacity blockers before Load
without weakening the single authoritative comparison.

### Acceptance checks

- A normal run performs no more Odoo metadata or record requests than the
  current single Final review comparison.
- Opening pages, filtering issues, accepting a proven group decision, or
  downloading the workbook makes no additional Odoo request.
- Every omitted prepared record remains visible with its direct or inherited
  reason.
- `TARGET_NUMERIC_PRECISION_LOSS` blocks Stage 5 readiness for every intended
  write that cannot fit the captured Odoo field without loss.
- Stage 4 may report all potentially affected prepared rows, while Stage 5
  reports the exact affected write rows. The UI must explain that distinction.
- Neither check rounds a number, chooses a rounding method, or describes a unit
  conversion as an equivalent correction.
- A changed prepared result or target binding invalidates the comparison and
  reduced-scope decision.

## Proposal 10: Add a migration-intent contract and load cockpit

**Owning stages:** Final review and Load into Odoo.  
**User impact:** **9.5/10**.

### Process improvement

Before or during Final review, the data manager should record the business
purpose of the run through closed choices:

- create only, update only, or upsert;
- record types that may change;
- fields and relationships that may change;
- expected or maximum create and update counts; and
- required zero counts, such as zero new Products.

Impodo should compare the fresh Final review result with this intent. A
deviation remains a blocker or explicit review item even when every individual
row is technically valid. The data manager confirms the intent and observed
result together. Stage 6 binds that decision to the exact execution snapshot
and repeats its current freshness and scope checks immediately before writing.

During execution, Impodo should continue to use the durable journal and
reconciliation result as authority. An interrupted outcome must offer an
assessment action before any continuation. The cockpit must never describe an
unknown write outcome as safely retryable.

### Interface improvement

Show **Expected versus found** before load confirmation:

| Business rule | Expected | Found | Result |
| --- | ---: | ---: | --- |
| New Products | 0 | 17 | Must fix |
| Updated Contacts | 900 to 1,100 | 1,024 | Within expectation |
| Deleted records | 0 | 0 | No delete operation planned |

The final page should state what Impodo will and will not change: target
database, record types, create and update counts, fields, relationships,
dependency groups, and absence of delete operations.

During and after execution, use one visible timeline:

```text
Planned -> Submitted -> Accepted by Odoo
        -> Relationships completed -> Verified
```

If the process stops, the timeline should distinguish **Outcome unknown**,
**Assessment required**, **Safe to resume**, and **Verified**. The primary
action should remain **Assess and resume interrupted load** when that is the
current recovery route.

### Impact justification

This catches a migration that is technically consistent but contradicts its
business purpose. It also gives the data manager a trustworthy explanation of
the highest-risk stage, so it receives a near-maximum score.

### Acceptance checks

- Intent is bound to the current prepared result, target, Final review, and
  execution snapshot.
- A count outside the approved rule cannot be hidden by confirming individual
  rows.
- The intent contract does not authorize models or fields outside the captured
  and reviewed write scope.
- Repeated submissions return the current job or recovery state rather than
  starting another load.
- Completion still requires verified reconciliation, not only an accepted Odoo
  response.

## Priorities and delivery sequence

| Priority | Proposals | Reason |
| --- | --- | --- |
| **1 - delivered** | First slice of 1. Issue register and 2. Supporting-model derivation | Remove hidden Stage 2 Odoo knowledge first and establish the shared issue language. Direct schema discovery and the Stage 2 blocker are implemented. |
| **2 - delivered in part** | 3. Identity proof | Direct scalar identities are now proved. Relational scope and protected duplicate-group review remain. |
| **3 - next slice** | 6. Relationship simulator | Reuse proven identities to find missing and ambiguous parents before preparation or Final review, and complete relational identity scope. |
| **4** | 8. Root-cause repair plans; first slice of 9. Shared comparison | Consolidate dependent failures and move exact precision findings earlier without adding a second normal comparison. |
| **5** | Second slice of 9. Reviewed set-aside; 10. Migration intent and load cockpit | Permit a reviewed safe remainder, then bind loading to an explicit business result. |
| **6** | 4. Defaults assistant; 5. Mapping starter; 7. Inline rule effects | Reduce setup effort after the identity, relationship, and shared issue contracts are stable. |

The first two slices establish the issue vocabulary, direct Stage 2 dependency
boundary, and direct Stage 3 identity proof. The next implementation slice
should make relationship checks reuse the same bounded target-read planning
rather than inventing a new connector or comparison path.

The second delivery should expose the existing comparison from Stage 4 before
adding reviewed set-aside decisions. That sequence first proves that the normal
journey still performs one comparison and retains current responsiveness.

## End-to-end acceptance scenario

Use one fictional migration containing Products, Product Categories, Units of
Measure, Bills of Materials, and Bill of Material Lines:

1. Stage 1 accepts Product, Product Category, Bill of Material, and Bill of
   Material Line tables without asking the data manager to identify Odoo's
   technical dependency graph.
2. In Stage 2, the data manager chooses Products and Bills of Materials as the
   business scope. Impodo derives the relevant Product Categories, Units of
   Measure, Companies, Products used by Bills of Materials, and line ownership
   from the accepted source and current Odoo schema. It proposes incoming data,
   existing-Odoo references, or checked Odoo defaults for each dependency. The
   user reviews only unresolved business decisions. Impodo then suggests
   Product Internal Reference and Category Name within Parent Category; the
   identity card explains that both still need a current-data check.
3. Stage 3 maps the complete Category path, Product Unit relationship, Bill of
   Material Line parent, and Bill of Material Product. The relationship
   simulator finds seven lines without a parent Bill of Material, one missing
   Product, and two ambiguous Category paths. Each blocker names its origin and
   opens the exact relationship rule.
4. Stage 4 prepares every row. The missing Product sets aside one Bill of
   Material and its component lines. A Quantity value of `1.237` also receives
   a potential precision warning because the captured Odoo field supports two
   decimal places. The root-cause cards show the affected business groups,
   owners, preserved work, and correction routes.
5. The data manager starts the shared Odoo comparison from Stage 4. It confirms
   which precision findings affect actual writes before the Load stage can
   unlock. One unrelated Bill of Material remains safe, while the affected
   group can be explicitly set aside with every omitted record retained in the
   workbook.
6. Final review compares the reduced result with an intent of zero new Units
   and no Product price updates. Any deviation blocks confirmation.
7. Stage 6 loads the exact approved result. The cockpit shows the dependency
   groups and does not report completion until reconciliation verifies Odoo.

This scenario must also cover one interrupted request. The browser should
return to the saved journal, assess the target, and offer only the safe current
recovery action.

At every blocked step, the acceptance test should assert that the page states
the cause, affected records, owner, correction page, preserved evidence, and
required recheck. A disabled navigation item without that explanation fails
the scenario.

## Implementation and documentation boundaries

Each implemented proposal must update the owning user and developer workflow
pages registered in [`workflow.yml`](../workflow.yml). Review the following
contracts together when their behavior changes:

- [Project lifecycle](../developer/contracts/project-lifecycle.md) and
  [evidence lifecycle](../developer/contracts/evidence-lifecycle.md) for source
  scope and cross-stage freshness.
- [Canonical staging](../developer/contracts/canonical-staging.md),
  [normalization](../developer/contracts/normalization.md), and
  [quality and quarantine](../developer/contracts/quality-and-quarantine.md)
  for complete-row and root-cause behavior.
- [Preflight](../developer/contracts/preflight.md) for shared comparison,
  intent assessment, and reviewed set-aside scope.
- [Execution and reconciliation](../developer/contracts/execution-and-reconciliation.md)
  for load confirmation, recovery, and completion.

Focused verification should extend the owning domain, application, browser,
artifact, security, accessibility, and performance tests rather than relying
only on documentation examples. Each live Odoo check needs request-count tests
that prove batching by model and distinct key. Each browser change needs
authenticated keyboard, focus, narrow-screen, stale-page, delayed-response,
and interrupted-action coverage.

Capture new 1440 by 1024 screenshots with isolated fictional data only after
each decision point becomes current behavior. A proposal screenshot or static
mock-up must not replace authenticated acceptance evidence.

## Non-goals

- The remaining plan does not yet implement the proposal parts that are not
  listed under **Implemented first slice**.
- It does not authorize automatic approval of matching rules, defaults,
  relationship matches, set-aside groups, migration intent, or Odoo writes.
- It does not add numeric Odoo database IDs to portable mappings, Recipes,
  workbooks, or ordinary browser projections.
- It does not raise current preparation or relationship scale limits.
- It does not replace Final review with an earlier advisory check.
- It does not add a second Odoo comparison to the normal Stage 4-to-Stage 5
  journey.
- It does not change the separate
  [generic Odoo-to-Odoo migration plan](odoo-to-odoo-migration.md) or describe
  its Stage 8A and Stage 8B path as part of the six-stage Authoring workflow.

## Related documentation

- [Source data workflow](../developer/workflow/01-source-data.md)
- [Odoo data workflow](../developer/workflow/02-odoo-data.md)
- [Match data workflow](../developer/workflow/03-match-data.md)
- [Prepare data workflow](../developer/workflow/04-prepare-data.md)
- [Final review workflow](../developer/workflow/05-final-review.md)
- [Load into Odoo workflow](../developer/workflow/06-load-into-odoo.md)
- [Odoo data workflow](../developer/workflow/02-odoo-data.md)
- [Stage 4 comparison and deferred record groups](stage4-comparison-and-deferred-record-groups.md)
- [Match data ordering qualification](smart-match-data-ordering.md)
- [Workflow responsiveness qualification](responsive-workflow-liveness-and-navigation.md)
