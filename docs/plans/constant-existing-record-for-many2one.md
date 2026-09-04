# Use one existing Odoo record for every row

## Status and decision

**Status:** Implemented. This behavior is available in the current browser.

Add a first-class value choice for Odoo many-to-one fields in **Match data**:

> **Use the same existing Odoo record for every row**

The data manager chooses one existing related record by its confirmed business
key. Impodo applies that portable reference to every row in the table. The
rule does not require a source column, does not create or update the related
record, and never stores an Odoo numeric database ID.

The feature belongs to the **Match data** stage. It must work for any supported
many-to-one field, rather than contain special handling for Units of Measure.

## The problem this solves

A source table often omits a value because every row uses the same existing
Odoo choice. Examples include:

- every Bill of Material uses the existing `PCE` Unit of Measure;
- every imported record belongs to one existing Company;
- every price uses one existing Currency; and
- every record uses one existing Warehouse or Category.

The current relationship form requires at least one source column. A data
manager must therefore edit the source file, add a derived constant column, or
misuse an unrelated constant-valued column and translate that value with
**Match values**. Those workarounds obscure the intended rule and make a
Recipe harder to understand and reuse.

Scalar fields already support **Same value for every row**, but many-to-one
fields represent governed references rather than scalar values. Reusing the
scalar implementation would be unsafe because a many-to-one value must be
resolved through the related model and its confirmed business key.

## Data-manager experience

Suppose `plw_bomversion` creates Odoo Bills of Material. Every Bill of Material
must produce `1,000` pieces and use the existing `PCE` Unit of Measure. The
source file does not contain a Unit of Measure column.

The data manager expands **Product Unit of Measure** and chooses:

1. **Fill this linked field using:** Same existing Odoo record for every row.
2. **Find this record by:** Unit of Measure Name.
3. **Existing Unit of Measure:** PCE.

The card then shows this summary:

> `PCE` will be used for all 31 Bill of Material rows.

For the data manager, this means `mrp.bom.product_uom_id` receives the existing
Odoo `PCE` record. Impodo does not alter Product Units of Measure and does not
create another Unit of Measure.

The normal actions remain **Save progress**, **Check matches**, and **Confirm
field matches**. A later Recipe run resolves `PCE` again in its own Odoo
target. It does not reuse the original target's numeric ID.

## Implemented browser behavior

### Choose how the linked value is supplied

Add **Fill this linked field using** at the start of every writable many-to-one
card. Offer these choices:

- **A value from the source** keeps the current source-column, Odoo/incoming
  table, matching-rule, projection, and value-matching controls.
- **The same existing Odoo record for every row** opens the constant-reference
  controls described below.
- **Do not fill this field** leaves the field unmapped. Existing required-field
  validation continues to decide whether that omission is allowed.

Do not offer the constant choice for one-to-many fields. Keep many-to-many
constant lists outside the first delivery because they require list-operation
semantics and multi-record selection.

### Choose the existing record

When the data manager selects the constant choice, hide the source-column,
incoming-table, projection, and **Match values** controls. Show:

- **Find this record by**, which uses a confirmed business key for the related
  Odoo model. A reviewed supporting-record name key may be used when the
  current bounded-reference policy permits it.
- **Existing _record type_**, which is a searchable choice control. The
  browser displays the record label and portable key values, never its numeric
  ID.
- One input for each key or scope component when the selected business key is
  composite. The browser must preserve the confirmed component order.
- **Check this record**, when an exact choice has not yet been verified against
  the current Odoo evidence.
- A summary that names the chosen record and the number of source rows that
  will receive it.

The choice control should reuse the current bounded relationship-choice and
supporting-lookup services. For a large related model, it should request a
bounded search or exact business-key check rather than download every record.
An exact check must return zero, one, or several matches without exposing a
numeric ID in HTML, form data, logs, or portable evidence.

### Required and failure behavior

Keep the current **Compare with Odoo**, **Check only; do not prepare**, **Must
have a value**, and **Required for new records** policies. When Odoo marks the
field as required, the browser and final compiler must preserve that create
requirement even if the author does not select it manually.

Keep the current missing and ambiguous decisions. The safe defaults remain:

- **If missing: stop and ask**; and
- **If several match: stop and ask**.

If the chosen reference later becomes missing or ambiguous, Impodo sets aside
the affected owner row or identity group. Unrelated records may continue to
review. Impodo must never select the first match.

## Portable contract

Bump the mapping contract from version 14 to version 15. Add a closed
relationship value-provider enum and a portable constant-reference object.
The portable provider uses these names:

```python
class RelationshipValueSource(StrEnum):
    SOURCE = "source"
    CONSTANT_EXISTING = "constant_existing"

@dataclass(frozen=True, slots=True)
class ConstantReferenceComponent:
    target_field: str
    value: str

@dataclass(frozen=True, slots=True)
class ConstantBusinessReference:
    key_values: tuple[ConstantReferenceComponent, ...]
    scope_values: tuple[ConstantReferenceComponent, ...] = ()
```

`RelationshipMapping` gains `value_source` and `constant_reference`. Existing
source-driven relationships keep `value_source="source"`.

The proposed portable meaning for the Bill of Material example is:

```json
{
  "target_field": "product_uom_id",
  "kind": "many2one",
  "value_source": "constant_existing",
  "source_column_keys": [],
  "resolver": {
    "origin": "target_catalog",
    "model": "uom.uom",
    "key_mappings": [],
    "scope_mappings": [],
    "value_mappings": [],
    "dataset_id": null,
    "dataset_projection_field": null
  },
  "constant_reference": {
    "key_values": [
      {"target_field": "name", "value": "PCE"}
    ],
    "scope_values": []
  },
  "compare": true,
  "validate_only": false,
  "required": true,
  "required_on_create": true,
  "on_missing": "error",
  "on_ambiguous": "error",
  "operation": "replace",
  "null_policy": "distinct"
}
```

The contract must enforce these rules:

- `constant_existing` is valid only for a many-to-one field.
- It uses `target_catalog` resolution and names the field's exact related
  model.
- It contains no source-column keys, incoming dataset, projection, inline
  value mappings, or list operation other than `replace`.
- Its key and scope fields exactly match one confirmed or policy-approved
  related-model business key, in the confirmed order.
- Every component has one bounded portable scalar value. Empty values and
  oversized values are invalid.
- `source` relationships keep their current requirements and cannot carry a
  constant reference.
- No contract, Recipe, browser form, diagnostic record, report, or prepared
  artifact stores the selected Odoo numeric ID.

Version 14 payloads must continue to decode as source-driven relationships.
Version 15 must reject mixed, incomplete, unknown, or forged provider shapes.
Add explicit version-aware decoding instead of weakening the current closed
field checks for old contracts.

## Preparation, comparison, and loading semantics

The row evaluator and native columnar program must produce the same logical
reference for every applicable source row. For the example, preparation emits
the equivalent of:

```text
BusinessReference(model="uom.uom", key=("PCE",), scope=())
```

Preparation remains local and does not contact Odoo. The reference planner
must deduplicate the repeated constant so comparison and preflight perform one
bounded lookup for `PCE`, rather than one lookup per Bill of Material row.

The current target-first resolution rules remain authoritative:

- exactly one match resolves the relationship;
- no match applies the saved missing policy;
- several matches apply the saved ambiguous policy; and
- the numeric Odoo ID appears only in protected execution and reconciliation
  evidence after the portable reference has been resolved.

A constant existing record creates no incoming-dataset dependency edge. It
does create the same governed target-reference request as a source-driven
target-catalog relationship.

The prepared field lineage should identify the mapping rule as **Same existing
Odoo record for every row** and show the portable display value. It must not
invent a source column or claim that the accepted source contained the value.

## Recipe reuse and invalidation

A saved Recipe keeps the related model, confirmed matching rule, and portable
key and scope values. Applying the Recipe to a later Data version requires no
source-column binding for this field.

The later workspace must check the chosen reference against its own Odoo
target. A renamed, removed, inaccessible, or newly duplicated record returns
the decision to the data manager. Impodo must not substitute a similarly named
record or reuse the original target's numeric ID.

Changing the provider, matching rule, key value, scope value, required policy,
or failure policy changes the mapping content hash. Existing validation,
submission, preparation, comparison, transfer-order, execution, and
reconciliation evidence then becomes stale through the current invalidation
boundary.

## Implementation ownership

| Responsibility | Primary owner |
| --- | --- |
| Versioned provider and portable constant-reference contract | `src/impodo/domain/mapping/contracts.py` |
| Relationship semantic validation | `src/impodo/domain/mapping/validation/relationships.py` and `validator.py` |
| Strict form allowlist and parsing | `src/impodo/web/presenters/mapping_forms.py` |
| Relationship card and constant chooser | `src/impodo/web/templates/mapping/_relationship_catalog.html` and the page-owned mapping JavaScript |
| Bounded choice lookup and evidence | `src/impodo/web/composition/target_readers.py`, `SupportingLookupService`, and the current relationship-choice route |
| Browser mapping compilation and Recipe labels | `src/impodo/domain/compiler/browser_mapping_compiler.py` and `recipe_compilation_service.py` |
| Recipe application and source-binding exemption | `src/impodo/application/recipe_application_compilation.py` |
| Row-level staging oracle and lineage | `src/impodo/domain/staging/evaluator.py` |
| Native constant relationship program | `src/impodo/domain/compiler/columnar_transformation.py` and `src/impodo/adapters/polars_transformation.py` |
| Target-reference planning and resolution | `src/impodo/domain/execution/planner.py` and `src/impodo/domain/preparation/preflight.py` |
| Dependency classification | `src/impodo/domain/relationship_dependencies.py` |
| Mapping review and rule-effect presentation | `src/impodo/adapters/artifacts/mapping_review.py` and the Match data presenters |

Prefer one shared helper that returns the resolver's ordered target key and
scope fields for both source-driven and constant relationships. Do not spread
provider-specific field extraction across validation, compilation, staging,
comparison, and execution.

## Delivery slices

### Slice 1: Contract and semantic validation

- Add the version 15 portable provider and constant-reference objects.
- Preserve strict version 14 decoding with an explicit `source` default.
- Reject mixed providers, wrong relation kinds or models, missing key
  components, ungoverned keys, blank values, and numeric-ID fields.
- Add deterministic serialization, hashing, round-trip, and compatibility
  tests.

### Slice 2: Row and native preparation

- Make the row evaluator emit one constant logical business reference per row.
- Compile the same rule to native constant expressions without a Python user
  function.
- Preserve required-field issues, lineage, target request planning, and
  target-first resolution.
- Prove row/native parity and one-distinct-reference batching.

### Slice 3: Guided browser authoring

- Add the provider choice and progressively disclose only the controls needed
  by the chosen provider.
- Add the searchable existing-record chooser and exact read-only check.
- Preserve strict form-field allowlisting, stale-draft detection, mutation
  receipts, save recovery, and off-screen validation blockers.
- Render saved constant relationships as **Connected** with a plain-language
  summary.

### Slice 4: Recipe reuse, reports, and documentation

- Compile and reapply the portable constant without a source-column binding.
- Show the constant provider accurately in mapping review, impact, preparation,
  and final-review evidence.
- Update the paired Match data user and developer pages, `docs/workflow.yml`,
  the Python code map, and affected contracts after implementation.
- Capture the authenticated browser control at 1440 by 1024 using fictional
  data.

## Acceptance criteria

1. A data manager can fill a many-to-one field with one existing Odoo record
   without adding or repurposing a source column.
2. The browser clearly distinguishes a source-provided relationship from one
   fixed existing Odoo record.
3. The chooser displays business labels and portable key values, never numeric
   Odoo IDs.
4. The saved mapping contains the related model and exact governed key and
   scope values.
5. Version 14 source-driven relationships still load unchanged, while invalid
   version 15 provider combinations fail closed.
6. Row and native preparation produce identical references and issues.
7. Ten thousand owner rows using the same constant produce one distinct target
   lookup request, with no Odoo or repository call inside the row loop.
8. A missing or ambiguous existing record follows the saved policies and never
   resolves to the first result.
9. A Recipe reuses the portable value against fresh Odoo evidence without
   requiring a source column or retaining an old numeric ID.
10. Editing the constant or its business key invalidates all dependent
    evidence through the existing mapping boundary.
11. The mapping review and prepared lineage say that Impodo supplies the same
    existing record; they do not attribute the value to the source file.
12. Focused domain, compiler, browser, preparation, Recipe, artifact,
    performance, documentation, and authenticated-browser checks pass.

## Focused verification

At minimum, extend or add coverage in:

- `tests/domain/mapping/test_compatibility.py`;
- `tests/domain/mapping/test_validation.py`;
- `tests/integration/web/test_mapping_forms.py`;
- `tests/integration/web/test_mapping_workflow.py`;
- `tests/domain/recipe/test_representative_shapes.py`;
- `tests/domain/preparation/test_target_first_relationships.py`;
- `tests/integration/columnar/test_polars_transformation.py`;
- `tests/integration/artifacts/test_mapping_review_workbook.py`; and
- the appropriate preparation-scale test for reference deduplication.

Also run the complete Match data focused package, documentation checks,
`git diff --check`, and the repository's normal test suite before handoff.

## Non-goals for the first delivery

The first delivery does not:

- create, update, rename, or merge the chosen related Odoo record;
- store or accept a numeric Odoo database ID;
- add constant lists to many-to-many fields;
- infer a constant merely because a sampled source column contains one value;
- modify or export a replacement source workbook;
- weaken business-key governance or supporting-reference read limits; or
- make Match data confirmation authorize an Odoo write.
