# Prefill Stage 2 Odoo matching rules

## Status and decision

**Status:** Implemented; visual and representative live-Odoo qualification
remain, 2026-09-22.

This implementation record is for the product owner and the developers who own
the file-source **Odoo data** stage. Stage 2 now prepares the safest available
matching rule for every supported selected Odoo record type, so a data manager
normally reviews one complete set and confirms it once.

The proposed default is **prefill, explain, and confirm**. Impodo prepares a
rule when captured Odoo evidence or a reviewed Odoo 19 policy supports it. The
data manager remains the actor who confirms the rules. An advanced user can
change any prepared rule before confirmation.

The delivered behavior does not make a suggestion authoritative. It preserves
the product rule that a suggestion is never an approved matching rule until a
person explicitly accepts it.

## Intended outcome

At the end of Stage 2, a data manager should normally see this result:

```text
Selected Odoo record types
|-- 7 matching rules ready to confirm
|-- 1 matching rule needs attention
`-- 0 record types omitted silently
```

The page shows the prepared field and any scope for each record type. The data
manager reviews the summary and selects the proposed **Confirm suggested
matching rules** action once. If one rule is unsuitable, the data manager opens
**Change matching rule**, edits that record type, and then confirms the whole
set.

For example, a fictional migration may select Contacts, Products, Product
variants, and Bills of materials. Impodo can prepare the reviewed reference or
code rule for each supported record type. The data manager checks the four
plain-language descriptions and confirms them together. A specialist who
needs a company-specific contact reference can add Company to the Contact
scope before using the same confirmation action.

## Delivered behavior

The current implementation provides the default-first review while preserving
the existing safety boundary:

- [`business_keys.py`](../../src/impodo/domain/workspace/business_keys.py)
  recommends the reviewed representative Odoo 19 rules, including parent-
  scoped BoM lines and Work Center Usage. It also recommends the only eligible
  Odoo uniqueness constraint when a captured model has exactly one.
- When a model has several eligible uniqueness constraints, Impodo does not
  choose between them. When it has no reviewed rule, Impodo does not guess from
  a field name.
- [`workspace_schema.html`](../../src/impodo/web/templates/workspace_schema.html)
  server-renders each supported recommendation into the form, summarizes the
  complete set, and keeps the advanced editor beside it.
- The authoring governance action requires one confirmed matching rule for
  every captured record type and rejects a stale schema or policy version.
- Prepare data and Final review later prove whether actual source and target
  values are populated and unique. Stage 2 metadata alone does not prove that
  a convention such as Product Internal Reference is unique in the current
  database.

## Meaning of a matching rule

This proposal uses **matching rule** in the browser and **business key** in the
developer contract. They mean the portable field or field combination that
Impodo uses to find zero or one existing Odoo record.

A matching rule is not Odoo's numeric database `id`. It is also not the
External ID that Impodo generates for a new record during loading. Neither of
those target-specific identifiers may become portable Recipe meaning.

Stage 2 chooses the Odoo side of the identity. Stage 3 still maps source
columns to the confirmed Odoo fields. This proposal does not guess that a
source column belongs to an Odoo field and does not move source-to-target
mapping into Stage 2.

## Proposed data-manager experience

### 1. Prepare all supported suggestions after capture

After Impodo captures the selected Odoo details, it derives matching-rule
suggestions locally from that immutable schema snapshot. Opening the page does
not make another Odoo call.

Every selected writable record type receives one visible outcome:

- **Suggested** means Impodo prepared one rule that is ready for review.
- **Needs attention** means more than one defensible rule remains or no safe
  rule is known. Impodo explains what the data manager must choose.
- **Confirmed** means the data manager already accepted a rule for the current
  captured schema.

Impodo must never omit a selected record type from this summary.

### 2. Prefill the form instead of requiring one click per card

When a record type has one recommendation, Impodo server-renders its matching
fields, scope fields, and description into the form. The card says **Suggested
and ready to confirm**. It does not say **Confirmed**.

JavaScript may improve the interaction, but it must not be required to place
the suggested values in the submitted form. Keyboard users and a browser with
scripts unavailable must receive the same proposed rules.

### 3. Make the normal review short

The top of the section shows counts for suggested, confirmed, changed, and
needs-attention rules. Suggested cards are compact by default and show:

- the Odoo record type;
- the proposed field or field combination;
- the optional scope, such as Company or Parent category;
- whether Odoo enforces the rule or Impodo recommends a common convention;
  and
- a warning when blank or duplicate values still need later proof.

The proposed **Confirm suggested matching rules** action saves the complete
reviewed set and continues to **Match data**. It remains one explicit human
confirmation, not automatic approval.

### 4. Keep advanced changes beside the suggestion

Each card keeps **Change matching rule**. It exposes the existing simple field
and scope controls first, followed by the combined-field controls. Changing a
suggested rule marks the card **Changed from suggestion** and keeps the new
draft through a validation error.

An advanced choice is not overwritten when the page is reopened or when a new
recommendation policy ships. Only the data manager can replace a confirmed
rule.

### 5. Stop when a selected record type remains unresolved

Stage 2 becomes **Complete** only when every selected writable record type has
one confirmed matching rule. A model with no safe suggestion remains a visible
attention item and prevents the bulk confirmation from claiming completion.

If Impodo later supports an intentional create-only policy without an existing
record lookup, that policy needs its own explicit contract. An empty card must
not silently mean create-only.

Existing workspaces with valid partial governance remain readable. The stricter
completion rule applies when a data manager next changes the selected model
scope or confirms Stage 2 again; an upgrade must not silently rewrite existing
governance evidence.

## Recommendation policy

The recommendation engine should produce ordered candidates with an explicit
basis. It should select a default only when one candidate is clearly preferred.

### Odoo-enforced rule

Impodo may recommend a captured Odoo unique constraint when all of its fields
are available, portable, writable, and supported matching types. A nullable
constraint carries the existing warning that every incoming key still needs a
value.

When several constraints remain, Impodo may choose one only when a reviewed
model policy names the preferred shape or when one candidate wins an accepted,
documented portability rule without a tie. Otherwise the card shows the
eligible alternatives and remains **Needs attention**. It must not select the
first database constraint by ordering accident.

### Reviewed Odoo 19 convention

A versioned policy may recommend a common Odoo 19 business convention even
when Odoo does not enforce it as unique. Product and Product variant Internal
Reference already use this path. Each rule must name the exact model, fields,
scope, required field contract, reason, and warning.

The first policy qualification should cover the representative migration pack
already used by Impodo documentation and tests: Contacts, Product categories,
Products, Product variants, Bills of materials, Bill of materials lines,
Companies, Units of measure, Countries, Currencies, and Languages. Existing
rules remain in place. A proposed rule such as Contact Reference or Bill of
materials Reference enters the policy only after its field shape, scope, blank
behavior, and duplicate behavior have been verified against the supported Odoo
19 fixture.

This registry is an Odoo-version policy, not a hard-coded substitute for live
metadata. A rule is offered only when the captured model still exposes the
expected compatible fields.

### Custom models

A standard or custom model with one eligible captured unique constraint
receives the same suggestion experience. A custom field named `code`, `ref`,
`external_id`, or similar receives no special trust from its name alone.

Impodo must not guess from translated labels, fuzzy similarity, display names,
or an AI response. A generic `name` field is not a safe default without a
model-specific policy or uniqueness evidence.

### Suggestion categories

The domain result should distinguish these categories without turning them
into approval states:

| Category | Browser meaning | May be prefilled? |
| --- | --- | --- |
| `ODOO_ENFORCED` | Captured Odoo metadata declares the rule unique. | Yes |
| `CURATED_CONVENTION` | A reviewed Odoo-version policy recommends the rule, but current data still needs proof. | Yes, with its warning |
| `MULTIPLE_CANDIDATES` | Several defensible rules remain. | No |
| `NO_SAFE_CANDIDATE` | Captured evidence and reviewed policy support no rule. | No |

The names above are proposed contract vocabulary. They do not replace
`BusinessKeyStatus.CONFIRMED`, which continues to mean that a person accepted
the rule.

## Contract and implementation shape

### Domain recommendation

Replace the single-or-none decision in `recommend_business_key` with a result
that can carry the preferred candidate, alternatives, category, reason,
evidence, warning, and recommendation-policy version. A compatibility wrapper
may retain the existing function while callers migrate.

The function remains pure. Its input is the captured `SchemaModel` and the
versioned Odoo policy. It does not read source rows, target records, a
repository, or a connector.

### Browser draft

`_schema_key_views` prepares the preferred values when no submitted draft and
no confirmed rule exist. A submitted draft always wins so a server validation
error cannot erase the user's change. A confirmed rule always wins so a later
policy version cannot silently replace it.

The form carries the expected schema content hash and recommendation-policy
version. The governance action rejects a page that became stale before
confirmation and asks the data manager to review the current suggestions.

### Confirmed evidence

`SchemaGovernance` remains the authority for the chosen model, key fields,
scope fields, and actor confirmation. Governance evidence should also retain
whether the choice used the suggestion or changed it, and which policy version
produced a used suggestion. This provenance explains the decision but does not
turn a suggestion into portable matching authority on its own.

If the serialized governance contract changes, introduce a versioned,
backward-compatible reader. Do not reinterpret an existing content hash or
rewrite an old confirmation during a storage upgrade.

Recipe publication continues to carry the confirmed portable matching
meaning. It does not need to carry unused alternatives or browser display
text.

### Completeness and invalidation

The application service validates that every selected writable model has one
confirmed rule before it publishes new Stage 2 governance. It must perform
this check even when the browser is bypassed.

The current schema hash remains the primary invalidation boundary. Confirming
changed Odoo details recomputes the draft suggestions and invalidates dependent
work through the existing path. A policy update alone does not invalidate an
already confirmed rule. It affects new recommendations only.

## Delivery increments

### 1. Qualify the recommendation policy — implemented

1. Add a versioned Odoo 19 policy contract beside
   [`business_keys.py`](../../src/impodo/domain/workspace/business_keys.py).
2. Record the exact field and scope shape, rationale, and warning for each
   approved representative model.
3. Test each rule against captured compatible and incompatible schemas.
4. Return alternatives for multiple eligible constraints without choosing by
   incidental order.

This increment exits when every model in the approved representative pack has
either a justified preferred rule or an explicit unresolved reason. Coverage
must not be achieved by weakening the no-guess boundary.

### 2. Add the default-first review — implemented

1. Add the Stage 2 coverage summary.
2. Server-render suggested fields into the form.
3. Collapse ordinary suggested cards and keep attention cards open.
4. Preserve the existing simple and combined advanced editors.
5. Submit all reviewed rules through one confirmation action.

This increment exits when the complete normal path works with keyboard input
and without JavaScript.

### 3. Bind confirmation and completeness — implemented

1. Bind the submitted suggestions to the expected schema and policy version.
2. Record suggested-versus-changed decision provenance.
3. Require one confirmed rule for every selected writable record type on new
   or changed governance.
4. Preserve old valid governance without silent rewriting.
5. Keep current mapping and later-evidence invalidation behavior.

This increment exits when stale, incomplete, duplicate-field, and direct
service submissions fail closed without changing the current governance.

### 4. Qualify the workflow — remaining

1. Run a representative file-source authoring journey with standard,
   extended-standard, and custom models.
2. Prove that no page render or confirmation adds an Odoo call.
3. Capture the normal summary, one convention warning, one advanced override,
   and one unresolved custom model at 1440 by 1024 with fictional data.
4. Update the paired Stage 2 user and developer pages, workflow registry,
   Python code map, screenshots, BPMN, and acceptance documentation only when
   the behavior is implemented.

## Acceptance checks

- Every model in the approved representative pack receives the reviewed
  expected suggestion when its captured schema is compatible.
- A custom model with one eligible unique constraint receives a prefilled
  `ODOO_ENFORCED` suggestion.
- A model with tied candidates or no evidence remains **Needs attention** and
  receives no prefilled field.
- A numeric Odoo `id`, generated External ID, translated label, readonly field,
  unsupported type, or field-name guess can never become a suggestion.
- The normal supported scenario requires one confirmation action, regardless
  of the number of suggested record types.
- One advanced override survives validation failure, page reload, and a later
  policy release.
- A stale schema or suggestion set cannot be confirmed.
- A new governance submission cannot omit one selected writable record type.
- Suggestion rendering and confirmation perform no target-record scan and no
  connector call. Schema capture keeps its existing bounded metadata reads.
- Prepare data and Final review still detect blank, duplicate, missing, and
  ambiguous real values before a load can be approved.
- The existing authorization, actor, invalidation, Recipe portability, and
  Odoo 19 boundaries remain unchanged.

Focused verification should extend
`tests/domain/workspace/test_business_keys.py`, add presenter and route coverage
for the prefilled and unresolved states, and cover governance completeness in
the application-service tests. The end-to-end browser journey should prove the
single-confirmation path and the advanced override.

## Non-goals

- This proposal does not map source columns to Odoo fields.
- It does not use AI or fuzzy matching to invent an identity.
- It does not scan all Odoo records during page rendering.
- It does not treat a convention as proof of uniqueness.
- It does not change the matching, preparation, preflight, loading, or
  reconciliation semantics after a rule is confirmed.
- It does not change the separate Recipe-application setup flow, where a saved
  Recipe already defines its required matching meaning.

## Related documentation

- [User workflow: Odoo data](../user/workflow/02-odoo-data.md)
- [Developer workflow: Odoo data](../developer/workflow/02-odoo-data.md)
- [Optional Recipe publication contract](../developer/contracts/recipe-lifecycle.md)
- [Product vision](../product-vision.md)
