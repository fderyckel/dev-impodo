# Build hierarchical related records from separate source columns

## Status and decision

**Status:** Implemented in the Source data, Match data, preparation, and Recipe
reuse paths described below.

This design lets Impodo create one related record hierarchy when each
hierarchy level comes from a different column in one accepted source table.
It also makes missing-parent behavior an explicit data-manager
decision.

The implementation extends the existing **Separate combined
information** workflow in Stage 1 and the existing incoming-record relationship
workflow in Stage 3. It does not require the data manager to edit the accepted
file or create an intermediate spreadsheet.

### Generic capability boundary

Product Categories are the motivating example, not the implementation
boundary. The saved rule accepts an arbitrary Odoo target model and display
field. Match data discovers a compatible self-referential many2one on that
model and a compatible many2one on any consumer model from the captured schema.
No runtime branch assumes `product.category`, `parent_id`, or `categ_id`.

The same feature therefore applies to departments and employees, analytic
account trees and their consumers, locations, classifications, and future
models with the same data shape. Qualification includes custom organizational
unit and worker models, with non-category field names, so a category-only
implementation would fail the test suite.

### Evidence from the motivating workbook

In `PLW_Products_v2.xlsx`, Excel column J is `Groupe de modèles d'article`
and column F is `code2`. The accepted rows have these shapes:

| J: model group | F: `code2` | Rows |
| --- | --- | ---: |
| Populated | Populated | 9,819 |
| Blank | Populated | 4,497 |
| Populated | Blank | 1,331 |
| Blank | Blank | 495 |

The workbook contains 541 `code2` values used beneath more than one model
group. A leaf value therefore cannot safely identify a Product Category by
itself; identity and matching must use the complete path.

The 4,497 blank-parent rows must not infer J from other rows. Only 757 of them
have a `code2` that appears beneath one unique populated group elsewhere;
3,443 are ambiguous and 297 are unseen with a populated group. That makes an
explicit fixed-parent, top-level, quarantine, or blocking choice necessary.

## The data manager's goal

Suppose a fictional Products table contains these values:

| Product | Model group | Model code |
| --- | --- | --- |
| P-001 | Finished | M-100 |
| P-002 |  | M-200 |
| P-003 | Components |  |
| P-004 |  |  |

The data manager wants Impodo to prepare Product Categories and Products with
this relationship:

```text
Finished
`-- M-100
    `-- P-001
```

For `P-002`, the data manager must be able to choose one of these meanings:

- **Use a fixed parent value** creates or reuses a reviewed parent such as
  `Default`, creates `M-200` beneath it, and links `P-002` to `Default / M-200`.
- **Make the populated value a top-level record** creates `M-200` without a
  parent and links `P-002` directly to it.
- **Set the row aside** keeps the row for review without guessing a parent.
- **Stop and ask me to correct it** blocks preparation until the source meaning
  is resolved.

A blank parent cannot be represented as a separate Odoo category above
`M-200`. Choosing a blank parent therefore means making `M-200` a top-level
category. Leaving the Product's category relationship blank is a different
decision and does not create `M-200` at all for that Product row.

For `P-003`, **Use the deepest populated level** creates or reuses
`Components` and links the Product directly to that category. For `P-004`,
Impodo should produce no category reference. Stage 3 then decides whether the
Product field stays unset, uses a verified Odoo create default, or blocks the
row.

## Original gap addressed

Before this change, Impodo provided useful parts of this workflow but could not
combine them into the requested result.

| Need | Previous behavior | Gap |
| --- | --- | --- |
| Create related records from repeated values | Stage 1 can extract unique records from one source column. | `DerivedEntityRule` stores only one `source_column_key`. |
| Create a hierarchy | Stage 1 can split one source value such as `Finished / M-100` by a saved parent separator. | It cannot take `Finished` and `M-100` from separate columns. |
| Handle a blank extracted value | Stage 1 can stop or set the affected row aside. | It cannot use a fixed hierarchy level, promote the child to the top level, or emit a deliberate blank consumer reference. |
| Combine columns | Stage 3 can combine two to five columns for one scalar Odoo text field. | The combined value cannot define a generated-dataset identity or a relationship key. |
| Link a Product to an extracted category | Stage 3 suggests the incoming extracted table for a compatible Many2one field. | The suggestion currently carries the one physical source column used by Stage 1. |
| Link a category to its parent | The mapping and execution domains support same-dataset relationships and row-level dependency ordering. | The Stage 3 browser excludes the current table from incoming-table choices and does not suggest the generated parent-path field. |

The single-column hierarchy remains valid. Contract version 5 adds a new closed
rule shape instead of changing the meaning of an existing saved rule.

## Stage 1 experience

The data manager opens **Source data**, then **Separate combined information**.
Under the current reusable-value option, Impodo should offer **Build a hierarchy
from separate fields**.

### Choose the hierarchy

The data manager chooses:

1. The accepted source table.
2. Two to five ordered **Hierarchy level** fields.
3. The name shown for the generated related table.
4. The target Odoo record type and display-name field.
5. The External ID namespace.

For the fictional example, **Level 1** is `Model group` and **Level 2** is
`Model code`. The level order is part of the saved rule and its content
identity.

### Decide what a blank level means

Impodo must show separate decisions because a missing parent, a missing leaf,
and a completely blank path have different business effects.

**When a parent level is blank but a later level is populated** should offer:

- **Stop and ask me to correct it**. This is the safe initial selection.
- **Set the affected rows aside**.
- **Use a fixed parent value**, followed by a required reviewed text value.
- **Make the next populated value a top-level record**.

**When the final level is blank** should offer:

- **Use the deepest populated level**. This is the initial selection.
- **Stop and ask me to correct it**.
- **Set the affected rows aside**.

**When every selected level is blank** should offer:

- **Leave the related value blank**. This is the initial selection.
- **Use one fixed related value**, followed by a required reviewed text value.
- **Stop and ask me to correct it**.
- **Set the affected rows aside**.

The browser must call a fixed value a **fixed value**, not an Odoo default. A
fixed value creates or identifies an actual incoming related record. **Let Odoo
choose** remains a separate Stage 3 decision that omits a Product field only
when the captured target provides a usable create default.

### Preview the result

**Preview related records** should show:

- the selected level order;
- one sample of every blank-shape outcome;
- distinct complete paths and their proposed parent paths;
- rows that will use a fixed value, become top-level records, stay blank, be
  set aside, or block;
- complete-path collisions caused by cleanup or top-level promotion;
- repeated leaf names that remain separate under different parents; and
- the proposed deterministic External IDs.

Counts must say whether they are exact or estimated. A full set-based preview
may be exact only within the current qualified related-data row boundary. The
preparation stage still repeats and checks the rule over every accepted row.

## Generated datasets and values

Stage 1 should create one generated related table and one prepared reference
value on the consumer table. The accepted source remains unchanged.

The generated related table should expose these semantic values:

| Generated value | Purpose |
| --- | --- |
| **Complete path key** | Uniquely identifies one record by its complete normalized hierarchy path. |
| **Name** | Supplies the final display part to the Odoo record's name field. |
| **Parent path key** | Refers to the parent row in the same generated table and stays blank for a root row. |
| **Impodo External ID** | Gives an incoming record a deterministic target identity based on namespace, model, and complete path. |

The consumer table should expose one synthetic **Selected related path key**.
This value is derived from the selected source levels and the reviewed blank
policy. It lets Stage 3 use one portable relationship key without pretending
that a scalar text concatenation changed the accepted source.

For example, when the fixed missing-parent value is `Default`, the generated
evidence is:

| Product | Complete category path | Category name | Parent path | Product's selected path |
| --- | --- | --- | --- | --- |
| P-001 | `finished` | Finished | blank | `finished / m-100` |
|  | `finished / m-100` | M-100 | `finished` |  |
| P-002 | `default` | Default | blank | `default / m-200` |
|  | `default / m-200` | M-200 | `default` |  |
| P-003 | `components` | Components | blank | `components` |
| P-004 |  |  |  | blank |

The table above explains the semantic result. The generated table still has
one row per complete path, and the consumer table still has one row per
accepted Product.

## Stage 3 experience

Stage 3 should present the generated hierarchy before its consumer in
**Recommended matching order**.

### Match the generated related table

For the generated Product Categories table, Impodo should suggest:

1. The generated **Complete path key** as the incoming row identity.
2. The generated **Name** for the Odoo `name` field.
3. The generated **Parent path key** for the Odoo `parent_id` field.
4. **Only another incoming table**, with **This generated table** selected as
   the parent source.

The current-table choice must appear only when the captured Odoo field is a
self-referential Many2one and the source-preparation link supplies a compatible
parent key. The browser must not make arbitrary same-table relationships easy
to create.

When existing Odoo records may be reused, the reviewed Odoo matching rule must
identify the complete category path or the equivalent name-and-parent scope.
Impodo must not match by the final category name alone when that name can occur
under several parents.

### Match the Product relationship

For the Products table, Impodo should suggest the generated **Selected related
path key** for the Odoo Product Category field and the generated Product
Categories table as the incoming source.

If the selected path is blank, Stage 3 should use the existing target-field
decisions:

- **Let Odoo choose** may omit the Product Category only when Impodo has
  verified a usable Odoo create default.
- **Do not fill this field** may omit an optional field.
- A required field with no supported default must block.

The data manager should not use **Combine source columns** for either
relationship. That provider continues to own scalar text values only.

## Portable contract

Increment the source-preparation contract from version 4 and add a closed
hierarchical lookup rule. Its portable meaning should be equivalent to:

```json
{
  "kind": "hierarchical_lookup",
  "output_dataset_name": "product_categories",
  "source_dataset_id": "logical-products",
  "source_level_column_keys": [
    "logical-model-group",
    "logical-model-code"
  ],
  "target_model": "product.category",
  "target_name_field": "name",
  "external_id_namespace": "legacy_erp",
  "missing_parent": {
    "mode": "fixed",
    "value": "Default"
  },
  "missing_leaf": "use_deepest",
  "all_blank": {
    "mode": "emit_null_reference",
    "value": null
  }
}
```

The contract must enforce these rules:

- A hierarchy contains two to five distinct stable source-column keys from one
  accepted dataset.
- The saved order defines path order and contributes to the plan hash.
- Missing-parent modes are `block`, `quarantine`, `fixed`, or `promote`.
- A fixed mode requires one bounded, non-blank text value. Other modes reject
  a hidden fixed value.
- Missing-leaf modes are `use_deepest`, `block`, or `quarantine`.
- All-blank modes are `emit_null_reference`, `fixed`, `block`, or
  `quarantine`.
- A normalized complete path identifies one generated record. A final name
  alone never identifies it.
- The consumer reference, parent reference, External ID, lineage, preview,
  preparation result, and Recipe meaning all use the same path evaluator.
- A legacy `lookup` rule with one `source_column_key` and optional separator
  retains its current meaning.

Recipe publication should replace each stable source-column key with its
logical source-column identity. Recipe application must rebind every selected
level in order and stop when one is missing or ambiguous. It must not guess a
replacement from a similar heading.

## Canonical and blank semantics

The shared evaluator should apply these steps:

1. Read each level in saved order.
2. Normalize Unicode and remove outer and repeated whitespace using the current
   derived-record policy.
3. Treat null, empty, and whitespace-only values as blank.
4. Apply the reviewed missing-parent, missing-leaf, and all-blank decisions.
5. Build the complete canonical path and every required ancestor path.
6. Create one generated row per distinct complete path.
7. Create one consumer reference per accepted source row.

The deterministic External ID should continue to use the namespace, Odoo
model, and complete canonical path. The same final name under two parents must
produce two different records. A promoted root or fixed parent that collides
with an existing complete root path should reuse that exact path and show the
merge in the preview.

Impodo must record whether each path part came from a source column or a fixed
decision. It must retain every contributing physical source-row reference in
protected lineage. It must never write a fallback value into the accepted
source evidence.

## Relationship planning and execution

The generated parent is a same-dataset logical reference from the parent path
key to the complete path key. Its Odoo field is whichever unique compatible
self-referential many2one the captured model exposes. A consumer relationship
is a cross-dataset logical reference from the selected path key to the
generated hierarchy table. In the motivating example, those discovered fields
are Product Category `parent_id` and Product `categ_id`.

Impodo's current generic row scheduler already supports acyclic same-dataset
hierarchies. The implementation should reuse that scheduler so each parent is
created or resolved before its children and every category is available before
its Products. It should not add a Product Category branch to the executor.

A self-parent path, repeated path component, missing parent, ambiguous incoming
path, ambiguous Odoo match, or actual row cycle must produce stable review or
blocking evidence. Impodo must not choose the first matching record.

## Evidence and review

The Stage 3 matching review workbook should name every contributing hierarchy
field and show the reviewed fallback mode. Its transformed-data sheet should
show the generated Product Category reference without replacing the original
source values.

Stage 4 should publish exact row-level evidence for:

- generated roots, children, and complete paths;
- source-provided and fixed path parts;
- promoted values;
- blank consumer references;
- quarantined or blocked source rows;
- parent relationships; and
- Product-to-category relationships.

Changing a level, its order, a blank policy, a fixed value, normalization, or
target model changes the source-preparation hash. Existing mapping,
preparation, comparison, and execution evidence then becomes stale.

## Implementation ownership

| Responsibility | Owner |
| --- | --- |
| Portable Stage 1 rule and path evaluator | `domain/workspace/derived_entities.py` |
| Stage 1 preview and saved-plan service | `application/workspace/derived_entities.py` |
| Stage 1 form and routes | `web/templates/workspace_derived_entities.html` and `web/routers/derived_entities.py` |
| Effective generated and consumer datasets | `domain/workspace/derived_entities.py` |
| Generated-row and consumer-reference staging | `domain/staging/evaluator.py` |
| Stage 3 generated-name and relationship suggestions | `web/presenters/mapping_view.py` |
| Stage 3 self-reference form parsing | `web/presenters/mapping_forms.py` |
| Recipe publication and rebinding | `application/recipe_compilation_service.py` and `application/recipe_application_compilation.py` |
| Generic relationship validation and ordering | `domain/mapping/validation/relationships.py`, `domain/relationship_dependencies.py`, and `domain/execution/dependency_scheduler.py` |
| Matching and prepared-data workbooks | `adapters/artifacts/mapping_review.py` and the existing prepared-data report adapters |

## Delivery slices

### Slice 1: closed rule and shared path semantics

- Add the versioned hierarchical rule and its blank-policy contracts.
- Implement one pure path evaluator used by preview and full staging.
- Add deterministic path, fallback, promotion, collision, and External ID
  tests.

### Slice 2: generated datasets and Stage 1 authoring

- Add ordered hierarchy-level controls and explicit blank decisions.
- Preview generated records, parent paths, consumer paths, and affected rows.
- Add the synthetic consumer path key without changing accepted source data.

### Slice 3: Stage 3 relationships and Recipe reuse

- Suggest the generated name, self-parent relationship, and consumer
  relationship.
- Permit the current generated table only for its governed self-referential
  Many2one.
- Compile and rebind every hierarchy field and policy through a Recipe.

### Slice 4: full preparation, evidence, and qualification

- Materialize the hierarchy and its relationships over the complete accepted
  source.
- Verify generic same-dataset ordering and Product dependency ordering.
- Extend matching and prepared-data review evidence.
- Update current user and developer documentation and refresh affected browser
  screenshots after implementation.

## Acceptance criteria

1. A data manager can build a two-level related-record hierarchy for an
   arbitrary compatible target model from two source columns without editing
   the file. Product Categories and custom organizational units both qualify
   this boundary.
2. A missing parent with a populated child can use a reviewed fixed parent,
   promote the child to a root, be quarantined, or block.
3. A populated parent with a blank child can use the parent itself when the
   data manager selects **Use the deepest populated level**.
4. A completely blank path can remain a blank Product relationship and then
   use the existing Stage 3 target-field decision.
5. The preview distinguishes a fixed incoming category from an Odoo create
   default.
6. Identical leaf names under different parents remain separate records with
   distinct External IDs.
7. The generated category parent relationship resolves through the same
   incoming dataset and produces an acyclic parent-before-child row schedule.
8. The Product relationship resolves through the generated category's complete
   path rather than its leaf name.
9. Existing Odoo matching is exact and complete-path-aware. A missing or
   ambiguous match does not select the first record.
10. Preview, row staging, native preparation, workbooks, Recipe reuse, and
    execution use identical path and blank semantics.
11. A rule edit invalidates every dependent evidence object through the
    existing source-preparation boundary.
12. Preparation adds no Odoo request and no repository lookup inside a source
    row loop.
13. The current qualified 25,000-row related-data boundary remains unchanged
    until a separate scale qualification raises it.

## Non-goals for the first delivery

The first delivery does not:

- infer a missing parent from other rows that happen to use the same child
  value;
- match an existing Odoo category by leaf name alone;
- silently invent the word `Default` or another business category;
- change the accepted source table;
- use Stage 3 scalar concatenation as a relationship identity;
- allow arbitrary formulas inside a hierarchy definition;
- support hierarchy levels from different source datasets; or
- change Odoo's configured create defaults.

## Decision for the motivating case

For a two-column Product Category source, use this initial configuration:

- Choose the model-group field as **Level 1**.
- Choose the model-code field as **Level 2**.
- For a missing Level 1 with a populated Level 2, select **Use a fixed parent
  value** and enter the business-approved category name, such as `Default`.
- For a populated Level 1 with a blank Level 2, select **Use the deepest
  populated level**.
- For both levels blank, select **Leave the related value blank**. In Stage 3,
  select **Let Odoo choose** only if Impodo verifies the intended Odoo Product
  Category default; otherwise review or block those rows.

This configuration preserves every populated model code, distinguishes the
fixed parent from an Odoo default, and makes the remaining blank relationship
visible for a separate decision.

## Related documentation

- [Corrective proposal: accept hierarchy roots and preserve load order](hierarchy-root-null-scope-and-load-order.md)
- [Prepare related tables](../user/guides/related-tables.md)
- [Source data developer workflow](../developer/workflow/01-source-data.md)
- [Match data user workflow](../user/workflow/03-match-data.md)
- [Match data developer workflow](../developer/workflow/03-match-data.md)
- [Scalable relationship dependency planning](scalable-relationship-dependency-planning.md)
