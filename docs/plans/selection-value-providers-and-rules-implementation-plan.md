# Selection value providers and conditional rules implementation plan

## Status and authority

**Status:** Core implementation complete; extended evidence and browser
qualification remain open.

**Plan date:** 2026-08-21.

The current Match data workflow now implements the three provider paths,
mapping contract v12, shared evaluation, native compilation, categorical
coverage, and Recipe portability. Per-rule overlap evidence, complete browser
qualification, the maximum-shape scale run, and the current screenshot remain
open acceptance work. The current user and developer workflow pages describe
the behavior that is ready to use.

This scoped proposal does not change the priority order in
[Impodo remaining work](remaining-work.md). Product ownership must adopt the
implementation as current work before a delivery slice begins.

## 1. Decision summary

Impodo will give a data manager three clear ways to fill an Odoo choice field:

1. The data manager can choose one Odoo value for every row.
2. The data manager can match the distinct values from one source column to
   Odoo choices.
3. The data manager can use ordered, plain-language rules that inspect one or
   more source columns and return an Odoo choice.

The page will always distinguish **available Odoo choices** from **source
values**. It will not ask the data manager to choose a source column merely to
see the target choices.

Conditional choice rules will become a first-class, portable value provider.
Impodo will store a declarative rule contract, compile it into bounded local
evaluation, and include its meaning and evidence in mapping and Recipe hashes.
The normal workflow will not store an opaque formula that happens to produce a
choice.

## 2. Product outcome

A data manager who has no Company Type column can select **One choice for every
row**, choose **Company**, and save a complete mapping for
`res.partner.company_type`.

A data manager who has mixed people and companies can select **Decide using
rules** and author a rule such as:

> When VAT Number is not blank, set Company Type to Company. Otherwise, set it
> to Person.

The example explains the interaction. Impodo does not infer that VAT Number is
the correct business classification. The data manager owns that decision.

The rule builder may use more than one source column. It shows complete counts,
bounded samples, overlaps, and unresolved rows before the mapping can be
confirmed.

## 3. Current implementation boundary

Match data currently owns scalar providers, selection-value matching,
categorical coverage, transformation-impact review, immutable mapping
revisions, and submission.

The implemented `ScalarValueSource` supports `source`, `constant`,
`source_with_fallback`, and `odoo_default`. `ScalarFieldMapping` stores those
choices together with optional source, literal, transformation, validation,
value-matching, reference-lookup, and categorical fields.

The current browser already renders **Same value for every row**. When the
target is an Odoo Selection field, it renders the captured Odoo labels and
technical keys in a dropdown. Domain validation rejects a constant whose final
value is not a captured selection key.

The current browser also renders **Review source choices** for every Selection
field. The associated route requires one current source column before it
returns both source and target choices. This wording and dependency incorrectly
combine two different tasks.

Safe formulas can refer to `column_1`, `column_2`, and other source values, but
formula evaluation uses the Python oracle. Categorical coverage treats a
formula-backed selection as unsupported reusable evidence. A formula is
therefore neither the user-centred nor the set-based architecture for this
feature.

The implementation must preserve these current boundaries:

- Editing a mapping does not change the frozen source.
- Reviewing Odoo choices does not contact or write to Odoo.
- Saving a working draft does not authorize preparation or execution.
- A mapping edit invalidates validation, impact review, submission, and all
  downstream evidence.
- Recipe application creates fresh mapping and categorical evidence. It does
  not copy a prior approval.

## 4. Odoo 19 semantics

Odoo Selection fields expose technical keys and business labels. Impodo will
display the business label first and retain the technical key as secondary
support detail. It will store and send only the captured technical key.

Odoo can declare a Selection through a static list, a callable, or an inherited
`selection_add`. Impodo must therefore use the choices in the current captured
Odoo schema. It must not hard-code a list based on an Odoo label or on the
choices from another database.

Odoo 19 defines `res.partner.company_type` with the technical choices `person`
and `company`. The field computes from `is_company`, and its inverse writes
`is_company`. Odoo describes `company_type` as an interface field that business
logic should not use. Impodo may write the interface because its inverse is the
supported write behavior, but one mapping must not intentionally provide both
`company_type` and `is_company`.

The captured public metadata does not describe every semantic alias between
Odoo fields. This plan will not add a display-label exception for Company Type.
If Impodo later enforces known inverse-field conflicts, that behavior must live
in a separately versioned Odoo 19 semantic policy keyed by technical model and
field names. It is not part of the generic conditional-provider contract.

The implementation will verify these Odoo boundaries against the official
[Odoo 19 `res.partner` source](https://github.com/odoo/odoo/blob/19.0/odoo/addons/base/models/res_partner.py)
and the official
[Odoo 19 Selection field contract](https://www.odoo.com/documentation/19.0/developer/reference/backend/orm.html#odoo.fields.Selection).

## 5. Scope

### 5.1 Included behavior

The first release will support conditional providers only for scalar Odoo
Selection fields. A rule can inspect fields from the same frozen source
dataset and return one captured target choice.

Each rule will contain one flat condition group. The data manager chooses
whether every condition or any condition must match. Nested Boolean groups are
not part of the first release.

The initial condition language will support:

- Text conditions can test blank, not blank, exact equality, inequality,
  equality that ignores capitalisation, containment, beginning, and ending.
- Numeric and date conditions can test blank, not blank, equality,
  inequality, less than, less than or equal, greater than, and greater than or
  equal.
- Boolean conditions can test true, false, blank, and not blank.
- Every condition references a stable source-column key and carries an
  explicit typed comparison value when the operator needs one.

Rules run in their displayed order. The first matching rule supplies the Odoo
choice. The provider then uses an explicit **Otherwise** choice or blocks every
row that did not match a rule.

The first release will not allow **leave unchanged** as the conditional
fallback. Omitting a value means something different for create and update
operations, and that ambiguity would weaken review. A future contract can add
an operation-aware omission policy if a separate use case justifies it.

### 5.2 Excluded behavior

This plan does not add:

- arbitrary Python, SQL, JavaScript, regular expressions, imports, or Odoo
  method calls to the rule builder;
- rules that read another dataset, an Odoo record, a relationship catalogue,
  or an external reference package;
- nested condition groups;
- inferred classification from field names, values, or a language model;
- automatic conversion of an existing formula into declarative rules;
- rules for relational fields or non-selection scalar targets; or
- a second preparation or execution path.

## 6. Browser interaction

### 6.1 Provider question

For an Odoo choice field, the first control will ask:

> How should Impodo fill {Odoo field label}?

The choices will be:

- **Do not fill this field**.
- **One choice for every row**.
- **Match values from a source column**.
- **Decide using rules**.
- **Use a source value, or a backup when blank** when this provider remains
  valid for the field.
- **Let Odoo choose** only when the current verified default decision permits
  that behavior.

The existing generic labels remain available for non-selection fields. The
selection-specific wording reduces the amount of technical interpretation that
the data manager must perform.

### 6.2 Available Odoo choices

Every Selection field will show **View available Odoo choices** independently
of the provider. Expanding it will display each business label and technical
key from the current captured schema.

This disclosure reads local schema evidence. It does not invoke the existing
source-value route and does not contact Odoo. When the captured field has no
choices, the disclosure will provide one recovery action: **Refresh selected
Odoo details**.

### 6.3 One choice for every row

Selecting **One choice for every row** will reveal one required dropdown. The
dropdown will display entries such as **Company — company**. It will not accept
free text.

The row summary will say, for example, **Company for every row**. The normal
mapping preview and the complete rule-effect review will show that value as a
provided value.

### 6.4 Match values from a source column

Selecting **Match values from a source column** will reveal the source-column
selector. After the data manager chooses a column, the action will read
**Match source values**.

The existing value-matching dialog will continue to show distinct source
values, counts, and captured Odoo choices. The dialog will no longer be the
place where a data manager merely inspects Odoo choices.

### 6.5 Decide using rules

Selecting **Decide using rules** will show a compact summary in the mapping
row and an **Edit rules** action. The action will open a focused dialog rather
than expanding a large builder inside the horizontally scrollable table.

The dialog will contain:

1. The heading will identify the Odoo field and show its available choices.
2. Each rule will read as a sentence: **When {conditions}, set {field} to
   {choice}**.
3. The data manager can add a condition, add a rule, move a rule up, move a
   rule down, or remove it.
4. The data manager will choose an **Otherwise** value or **Block unresolved
   rows**.
5. **Preview rules** will evaluate the unsaved bounded rule set against the
   frozen dataset without saving it.
6. The preview will show the number of rows resolved by each rule, the number
   resolved by Otherwise, the number that match more than one rule, and the
   number blocked.
7. Each count will open a bounded sample that names the source row and shows
   only the source columns referenced by the rule set.

The preview will explain that displayed order controls priority. When rows
match more than one rule, the first rule wins. Impodo will create a hash-bound
warning that the data manager must resolve or explicitly acknowledge before
submission.

The dialog will support keyboard operation. Moving a rule will have explicit
buttons and announced status; drag and drop will not be the only interaction.
Errors and preview completion will use an `aria-live` status region.

## 7. Mapping contract version 12

### 7.1 Discriminated provider contract

Mapping contract version 12 will replace the optional provider fields inside a
current scalar mapping with one discriminated `ScalarValueProvider` object.
Transforms, validation, comparison, target-field identity, required behavior,
null behavior, and categorical evidence remain properties of the scalar field
mapping.

The provider union will contain:

- `SourceColumnProvider`, which names one stable source column and can contain
  explicit portable value matches;
- `ConstantProvider`, which contains one literal value;
- `SourceFallbackProvider`, which names one stable source column and one
  fallback literal and can contain explicit value matches;
- `ReferenceLookupProvider`, which carries the current immutable reference
  lookup contract;
- `ConditionalSelectionProvider`, which contains one ordered
  `SelectionRuleSet`; and
- `OdooDefaultProvider`, which explicitly omits the field value at runtime.

The union will make invalid combinations unrepresentable. A constant cannot
carry a source column or value matches. A conditional provider cannot also
carry a formula, fallback, reference lookup, or value matches.

### 7.2 Conditional rule objects

The proposed portable objects are:

```text
SelectionRuleSet
  rules: ordered SelectionRule
  otherwise: target technical key or BLOCK

SelectionRule
  rule_id: stable UUID
  join: ALL or ANY
  conditions: ordered SelectionCondition
  target_value: captured Odoo technical key

SelectionCondition
  condition_id: stable UUID
  source_column_key: stable frozen column identity
  value_type: text, integer, decimal, date, datetime, or boolean
  operator: one closed operator from the type-specific list
  comparison_value: typed portable text when required
```

Stable rule and condition identities will preserve review continuity when the
data manager reorders or edits rules. Canonical serialization will retain rule
order because order changes meaning. Conditions inside an `ALL` or `ANY` group
will also retain authored order for display and evidence even though their
logical result is commutative.

Every target choice will use the technical key. Labels remain projections from
the bound Odoo schema and do not participate in portable Recipe meaning.

### 7.3 Bounds

The domain contract will enforce all bounds independently of the browser:

- A conditional provider may contain at most 20 rules.
- A rule may contain at most 8 conditions.
- A rule set may reference at most 20 distinct source columns.
- The existing scalar text bounds will apply to comparison values.
- The browser will encode one rule set in one strict JSON form value whose
  serialized size must remain within the existing 64 KiB form-value limit.
- The complete mapping request remains within the existing 5 MiB request
  limit.

These limits support reviewable business decisions and bound evaluator cost.
The performance qualification in this plan must measure the maximum accepted
shape before the constants become release evidence.

## 8. Validation and categorical coverage

Semantic validation will reject a conditional provider when:

- the target field is not an Odoo Selection field;
- a referenced source column is absent from the bound frozen selection;
- the provider has no rules;
- a rule has no conditions;
- an operator is incompatible with the condition type;
- a comparison value cannot be parsed under the declared type;
- a rule result or Otherwise result is not a captured Odoo technical key;
- the rule set exceeds any contract bound;
- a transform or value match is configured beside the conditional provider;
  or
- the target metadata supplies no captured choices.

Mapping contract version 12 will give a conditional provider an explicit
closed-domain policy. The policy means that every resolved output belongs to
the captured target domain and every source row either resolves or blocks.
It will not pretend that the source itself contains a bounded set of target
values.

`CategoricalCoverageService` will extend its existing one-scan-per-dataset plan.
It will project the union of all columns used by current categorical mappings
and conditional providers, scan each physical dataset once, and evaluate every
supported field through set-based expressions.

The resulting evidence will include:

- the exact source, schema, mapping, evaluator, and rule-set hashes;
- the complete row count;
- the count produced by each rule;
- the Otherwise count;
- the overlap count before priority is applied;
- the unresolved count;
- the distinct final technical keys and their counts; and
- bounded samples for overlaps and unresolved rows.

Adding these facts requires a new categorical-evidence contract version. Old
evidence remains readable with its original hash and cannot satisfy a version
12 mapping submission.

## 9. Shared evaluator and compiler

Impodo will implement one typed conditional semantics layer that is shared by:

- semantic validation;
- unsaved browser preview;
- categorical coverage;
- transformation-impact review;
- bounded preparation;
- native-columnar preparation; and
- Recipe application compatibility.

The Python oracle may remain as a differential test reference. Production
preparation for a supported conditional provider must compile to native Polars
or set-based DuckDB expressions. It must not evaluate a Python rule loop for
every source row.

The columnar contract will add a conditional-selection provider operation. The
compiler will project each referenced source column once and construct an
ordered `when` and `then` expression with an explicit Otherwise result or
blocking sentinel. Adapter code will preserve the same blank, comparison,
parsing, and first-match semantics as the domain evaluator.

If the native compiler cannot represent a declared operator, compilation will
fail closed with one recovery action. It will not silently choose the Python
fallback and reduce the eligible preparation limit.

## 10. Rule-effect evidence

The existing transformation-impact workflow will become the authoritative
review surface for conditional choice rules after the mapping is saved and
checked.

The report will show:

- which rule supplied each displayed proposed value;
- complete counts for every rule, Otherwise, overlap, and unresolved outcome;
- bounded source-row details for changed, blocked, or warning-bearing outcomes;
- a fingerprint for a zero-match rule; and
- a fingerprint for acknowledged overlap priority.

A rule with zero matches remains reviewable because it may represent valid
future Recipe meaning. The data manager must remove it or acknowledge it under
the existing hash-bound warning flow.

The transformation-impact identity already binds source, effective selection,
mapping, schema, and derived-plan hashes. Any rule edit or reorder changes the
mapping hash and invalidates the report, acknowledgement, submission, prepared
data, comparison, approval, and execution readiness.

## 11. Recipe portability and drift

Recipe publication will require the current mapping contract version rather
than a hard-coded check for version 11. A conditional provider will compile to
Recipe meaning with:

- logical source-field bindings for every referenced source column;
- the target model and technical Selection field;
- ordered rules, explicit operators, typed comparison values, and target
  technical keys; and
- the conditional-provider contract and evaluator versions.

Recipe application will bind those logical fields to the new DataVersion. A
missing or renamed referenced column will create a focused source-drift issue.
A removed Odoo target choice will create a focused categorical-drift issue.
Impodo will never replace an unavailable key with a label match or another
choice.

Applying a compatible Recipe will create a fresh mapping draft and fresh rule
coverage. It will not transfer the prior rule preview, overlap acknowledgement,
submission, preparation, comparison, or approval.

## 12. Browser and server boundaries

The server-rendered mapping row will carry the current rule-set JSON and a
plain summary. Client JavaScript will manage only the bounded editor state and
request previews. The server remains authoritative for parsing, validation,
evaluation, and persistence.

The form parser will:

- add exactly one conditional-rule JSON field for each scalar row;
- include that name in the field allowlist;
- reject unknown object keys and unexpected types at every nesting level;
- enforce rule, condition, column, text, JSON, request, and form-field bounds;
- preserve off-page rule providers during partial paged saves; and
- preserve the recoverable working draft after a semantic error.

The existing `/mapping/value-choices` route will continue to serve source-value
matching only after a source column is selected. Captured Odoo Selection
choices will be rendered from local schema evidence.

A new CSRF-protected rule-preview route may accept one unsaved bounded rule set,
the dataset identity, target field, and expected current draft and schema
versions. The route will run only after an explicit **Preview rules** action,
will execute in the thread pool, and will return complete counts with bounded
samples. It will not save a mapping or contact Odoo.

Authorization will use the same Match data capability as mapping edits. Error
responses will not echo unbounded source values, credentials, local paths, or
raw connector errors.

## 13. Performance and N+1 safeguards

The implementation will retain the evidence-lifecycle rule that no connector
call, metadata lookup, or database query may occur inside a source-row loop.

The following gates are mandatory:

- The mapping page will use its captured schema projection. It will not fetch
  Odoo choices once per field row.
- One explicit unsaved preview will scan one required projection from one
  frozen dataset. It will not issue one scan per rule or condition.
- Mapping validation and Recipe application will scan each affected physical
  dataset once across every categorical need.
- Preparation will evaluate rules in the compiled columnar plan. It will not
  build a Python dictionary for every condition and row.
- Rule samples will use bounded projections and will not materialize complete
  rich source rows.
- The maximum rule shape will receive a measured 100,000-row direct-dataset
  qualification. The feature must not silently move that shape to the
  50,000-row Python-fallback boundary.
- Query-count and connector-spy tests will prove that increasing source rows
  does not increase metadata or Odoo request counts.

The qualification will record time, peak memory, compiled route, source scans,
project database queries, and connector calls. A pass requires the native route
and zero Odoo calls during rule preview, coverage, impact review, and
preparation.

## 14. Compatibility and upgrade

Mapping contracts 8 through 11 will remain readable and retain their original
content hashes. Historical revisions and evidence will not be rewritten.

Opening a current version 11 mapping will render its existing providers. Saving
an ordinary source, constant, fallback, reference, or Odoo-default provider will
create a version 12 working draft with the equivalent discriminated provider.
The new mapping hash will require fresh validation and submission.

An existing formula-backed Selection field will remain readable under its
original contract. Impodo will not claim that it is a rule set. Editing or
publishing that field under version 12 will require the data manager to replace
the formula with a supported provider or retain the historical revision without
changing it.

Recipe contract fixtures, required mapping-contract declarations, authoring
checks, application compilation, and compatibility tests will move together.
An older published Recipe remains immutable. Reusing its supported version 11
providers will follow the existing application compatibility rules; adding
conditional meaning requires a new Recipe revision.

## 15. Implementation slices

### Slice 1: Freeze version 12 provider and rule contracts

This slice will add the discriminated provider objects, conditional rule
objects, canonical serialization, bounds, version checks, and backward readers.
It will update deterministic fixtures and reject unknown nested fields.

**Gate:** Every provider round-trips canonically, every semantic change changes
the content hash, reordering rules changes the hash, and contracts 8 through 11
retain their original hashes.

### Slice 2: Add semantic validation and shared rule evaluation

This slice will add typed operators, value parsing, selection-key validation,
first-match semantics, Otherwise blocking, overlap detection, and stable issue
codes. It will add differential tests for the domain evaluator.

**Gate:** Company Type constant and multi-column rule fixtures either resolve
every row to `person` or `company` or produce a precise blocking issue.

### Slice 3: Compile native conditional selection

This slice will extend the columnar provider program and Polars or DuckDB
adapter. It will keep one set of semantics across oracle and native evaluation.

**Gate:** Differential tests pass for null, blank, text, numeric, date,
Boolean, overlap, ordering, and blocking cases. The maximum supported rule set
uses the native route.

### Slice 4: Extend categorical and impact evidence

This slice will scan the union of referenced fields once, publish versioned rule
coverage, add complete counts and bounded samples, and bind zero-match and
overlap acknowledgements.

**Gate:** One dataset scan produces complete results for several ordinary
selection mappings and conditional providers. Editing or reordering one rule
invalidates all dependent evidence.

### Slice 5: Build the progressive browser workflow

This slice will separate Odoo choices from source values, add the provider
wording, build the accessible rule dialog, add explicit preview, and preserve
paged partial saves.

**Gate:** A data manager can complete the constant Company Type path without
selecting a source column and can complete a two-column rule path without
typing a formula or technical key.

### Slice 6: Add Recipe compilation and drift recovery

This slice will publish conditional providers as portable Recipe meaning and
rebind them against fresh source and target evidence during application.

**Gate:** A qualified Recipe applies the same rules to a compatible later file,
while a missing referenced column or unavailable Odoo choice blocks with one
focused recovery action.

### Slice 7: Qualify performance, security, accessibility, and documentation

This slice will run the maximum-shape performance fixture, hostile payload
tests, authorization checks, browser interaction, keyboard and accessibility
checks, complete regression, and documentation maintenance.

**Gate:** Every definition-of-done item passes with recorded evidence.

## 16. Focused verification

### 16.1 Domain and serialization

- `tests/test_mapping_validation.py` will cover provider exclusivity, typed
  conditions, order, bounds, invalid target keys, missing columns, and version
  12 serialization.
- `tests/test_categorical_coverage.py` will cover complete resolution,
  Otherwise, overlaps, unresolved rows, one-scan behavior, and evidence hashes.
- New focused rule-evaluator tests will compare the domain oracle and native
  result for every operator and null case.

### 16.2 Compiler and preparation

- Columnar compiler tests will assert the conditional provider operation and
  input projection.
- Native-adapter tests will cover first-match priority and blocking sentinels.
- Preparation tests will prove that blocked rows become current quality or
  quarantine findings under the existing preparation contract.
- Scale tests will prove native eligibility and record 100,000-row time and
  memory evidence.

### 16.3 Mapping lifecycle and browser

- `tests/test_mapping_forms.py` will cover strict bounded JSON parsing,
  allowed-field enforcement, partial saves, and recoverable invalid drafts.
- `tests/test_web_app.py` will cover independent Odoo-choice review, the
  constant Company Type path, conditional-rule preview, stale versions, CSRF,
  pagination, search, and accessible controls.
- `tests/test_mapping_impact_presenter.py` will cover per-rule counts, samples,
  zero-match warnings, overlap warnings, and invalidation.

### 16.4 Recipe lifecycle

- `tests/test_recipe_authoring.py` will cover portable conditional meaning and
  version 12 publication.
- `tests/test_recipe_application.py` will cover logical source rebinding,
  categorical drift, fresh evidence, and no copied acknowledgements.
- Recipe contract fixtures will cover the new required mapping version without
  rewriting prior immutable revisions.

### 16.5 Security and performance

- Hostile JSON will cover excessive nesting, excessive rules and conditions,
  unknown keys, invalid UUIDs, oversized strings, and malformed types.
- Connector spies will prove that rule work performs zero Odoo calls.
- Repository and scan spies will prove that row count does not create query or
  source-scan growth.
- Browser acceptance will exercise keyboard rule ordering, focus return,
  announced errors, and dialog labelling.

## 17. Documentation and screenshots

After implementation passes, update these current documents together:

- `docs/user/workflow/03-match-data.md` will explain the three choice-field
  paths in business language.
- `docs/developer/workflow/03-match-data.md` will describe mapping contract
  version 12, the shared evaluator, evidence, and performance boundary.
- `docs/developer/contracts/evidence-lifecycle.md` will record conditional
  coverage and invalidation.
- `docs/workflow.yml` will register any new code symbols and focused tests.
- `docs/architecture/python-code-map.md` will register new domain, compiler,
  application, presenter, and route symbols.

The authenticated current browser will supply a new 1440 by 1024 fictional
Match data screenshot. The image must show either the constant Company Type
choice or the conditional-rule decision point. Existing screenshots will not
be relabelled to describe behavior they do not show.

## 18. Blind spots and required decisions

### 18.1 Blank semantics

The contract must define blank once. The proposed meaning is null, an empty
string, or text containing only Unicode whitespace. Other operators compare
the frozen logical value and do not trim or change capitalisation unless the
operator explicitly says so.

### 18.2 Source candidate types

Source candidate types are advisory. A numeric or date condition must parse its
comparison value and each evaluated source value under an explicit invariant
policy. A value that cannot be parsed blocks the row; it does not compare as
text.

### 18.3 Overlap meaning

First-match priority makes the result deterministic, but overlap can hide an
overly broad earlier rule. The implementation must show the complete overlap
count and require a hash-bound acknowledgement if the data manager keeps it.

### 18.4 Derived datasets

Current categorical coverage can warn that it cannot scan some derived source
providers. Conditional rules must not claim Recipe portability for a derived
dataset until the shared source-scan boundary can project that dataset through
a supported set-based artifact. The implementation slice must either support
that boundary or fail closed with the existing focused issue.

### 18.5 Computed inverse fields

Company Type proves that writable business interfaces can be computed fields.
Pinned Odoo-source updates currently allow only stored, non-computed scalar
fields. This plan does not weaken that separate write-approval policy. File
source mappings and pinned Odoo-source updates must retain their different
eligibility rules.

### 18.6 Odoo defaults

For `res.partner`, omitting both `company_type` and `is_company` normally leaves
the default person meaning. The UI must not present that result as an explicit
Company Type decision unless the data manager chooses **Let Odoo choose** under
current verified default governance.

## 19. Approaches explicitly rejected

### Reuse Review source choices for every selection task

Rejected because a constant mapping has no source choices. Odoo choices and
source values are separate objects and require separate controls.

### Put a free-text constant beside an Odoo Selection field

Rejected because labels can be translated and technical keys can drift. The
data manager must choose from current captured choices.

### Generate a safe formula behind the rule builder

Rejected because the formula would become a second hidden source of meaning,
remain on the Python fallback path, and lack first-class categorical coverage.

### Store conditional rules as `ValueMapping` entries

Rejected because a value match translates one distinct value from one source
column. A conditional rule evaluates predicates over several columns and has
ordered priority and fallback meaning.

### Evaluate every rule in a Python source-row loop

Rejected because the cost grows with rows and rules, bypasses the native
100,000-row route, and creates a future N+1-shaped performance defect.

### Hard-code Company Type labels or field-specific UI behavior

Rejected because Selection choices can differ through translation, callable
selection, installed modules, and `selection_add`. The generic workflow binds
technical field names and current captured technical keys.

### Infer Company or Person automatically

Rejected because VAT, company name, email, and parent relationships are not
universal classification rules. Suggestions may explain an example, but only
the data manager can author and confirm business meaning.

## 20. Definition of done

The feature is complete only when:

- the page distinguishes available Odoo choices from source values;
- a data manager can map Company Type to Company for every row without a fake
  source column;
- a data manager can build and preview a rule that uses more than one source
  column without writing a formula or technical key;
- mapping contract version 12 stores one discriminated provider and rejects
  every invalid provider combination;
- every rule output is a current captured Odoo technical key;
- every source row resolves through a rule or Otherwise choice, or blocks with
  precise evidence;
- rule order, overlaps, zero-match rules, and unresolved rows are visible and
  hash-bound;
- browser preview, categorical coverage, transformation impact, preparation,
  and Recipe application use the same typed semantics;
- supported rules compile to the native columnar route and do not introduce an
  Odoo, metadata, source-scan, or database-query N+1 path;
- contracts 8 through 11 remain readable with unchanged historical hashes;
- Recipe publication and application preserve portable rule meaning and fail
  closed on source-column or Odoo-choice drift;
- mapping edits invalidate every dependent approval and execution artifact;
- focused domain, compiler, application, browser, security, accessibility,
  scale, and Recipe tests pass;
- the complete regression suite passes within its recorded environment;
- current user and developer documentation changes only after behavior passes;
  and
- the authenticated browser supplies a current fictional screenshot of the new
  decision point.
