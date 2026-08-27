# Combine source columns into one Odoo field

## Status and proposed decision

**Status:** Proposed. This feature is not implemented as a guided matching
rule.

Add **Combine source columns** as a first-class value choice in **Match data**.
The rule lets a data manager select two to five source columns, arrange their
order, choose the text placed between populated values, and decide what Impodo
does when one of the selected values is blank.

The first delivery applies only to scalar Odoo text fields. It does not change
the accepted source data, create an intermediate spreadsheet, or write to
Odoo.

## The problem this solves

A source table can keep parts of one business value in separate columns while
Odoo expects one field. Common examples include:

- `First name` and `Last name` becoming Odoo `Name`;
- `Street number` and `Street name` becoming Odoo `Street`; and
- `Product family` and `Product code` becoming one display reference.

Today, a data manager can edit the workbook before uploading it. That creates
another version of the source outside Impodo and makes the preparation rule
harder to review and reuse.

Impodo also has an advanced safe-formula control that can join columns. For
example, `strip(concat(column_2, " ", column_3))` can join two values. This
keeps the change inside Impodo, but the data manager must identify columns by
ordinal and author a formula. The rule is therefore difficult to discover,
review, and explain when it becomes part of a reusable Recipe.

## Data-manager experience

Suppose a fictional `contacts` table contains these columns:

| First name | Last name | Email |
| --- | --- | --- |
| Ada | Mensah | ada@example.test |
| Luis |  | luis@example.test |

The data manager maps the Odoo **Name** field and chooses **Combine source
columns** from the field's value-choice list. They then:

1. Select **First name** as the first source column.
2. Select **Last name** as the second source column.
3. Choose **Space** as the separator.
4. Choose **Skip blank values**.
5. Keep **Remove spaces at the start and end of each value** selected.

The bounded preview shows `Ada Mensah` for the first row and `Luis` for the
second row. It does not show `Luis ` with a trailing space.

For the data manager, this means the accepted source remains unchanged while
the combination becomes visible, checked, and reusable as part of the
mapping. The next action is **Save progress**, followed by the existing
**Check matches** and **Confirm field matches** actions.

## Proposed browser rule

### Value choice

Add this option to the existing scalar value choices:

> **Combine source columns** — Build one text value from two or more columns.

When the data manager selects it, show these controls:

- **Source columns** is an ordered list with two required entries. The data
  manager can add up to five entries, remove optional entries, and move entries
  up or down.
- **Place between values** offers **Nothing**, **Space**, **Comma and space**,
  **Hyphen**, and **Custom text**. Custom text is limited to 20 characters.
- **When a selected value is blank** offers **Skip blank values** and **Block
  the row for review**. **Skip blank values** is the default.
- **Remove spaces at the start and end of each value** is selected by default.

The control shows a bounded sample immediately after the data manager saves
the draft. The sample names every contributing source column and shows the
proposed Odoo value.

### Blank-value behavior

Impodo treats `null`, an empty cell, and a value containing only whitespace as
blank for this rule.

With **Skip blank values**, Impodo removes blank parts before inserting the
separator. If every selected part is blank, the combined result is `null`.
The field's existing required-value rule then decides whether the row can
continue.

With **Block the row for review**, any blank selected part creates a row issue.
Impodo does not produce a partial value for that row.

This proposal deliberately excludes a mode that preserves empty positions.
That mode would produce hard-to-see leading, trailing, or repeated separators
and would make review less reliable.

### Cleanup and validation order

Impodo performs the work in this fixed order:

1. Read the selected source values in the saved order.
2. Remove surrounding whitespace from each value when that choice is enabled.
3. Apply the selected blank-value behavior.
4. Insert the separator only between retained values.
5. Apply the existing guided cleanup and final-value validation rules.
6. Convert an empty final result to `null`.

The advanced formula control is unavailable for a field that uses **Combine
source columns**. This keeps one visible rule responsible for constructing the
value. Existing casing, whole-value whitespace cleanup, find-and-replace, and
final text validation remain available after the combination.

## Proposed portable contract

Increase the mapping contract version when this feature is implemented. Add
`concatenate` to `ScalarValueSource` and add one closed configuration object to
`ScalarFieldMapping`.

The portable meaning should be equivalent to:

```json
{
  "target_field": "name",
  "value_source": "concatenate",
  "concatenation": {
    "source_column_keys": [
      "fictional-first-name-key",
      "fictional-last-name-key"
    ],
    "separator": " ",
    "blank_handling": "skip_blank",
    "trim_parts": true
  },
  "value_type": "string"
}
```

The contract must enforce these rules:

- The rule contains two to five distinct current source-column stable keys
  from the same dataset.
- The saved array order defines the concatenation order and contributes to the
  mapping content hash.
- The separator contains no more than 20 characters.
- `blank_handling` is either `skip_blank` or `block_row`.
- The target is a supported Odoo scalar text field.
- A concatenation provider cannot also carry a single source provider,
  constant, fallback, conditional Selection rules, inline value matches,
  reference lookup, or formula.
- The existing maximum rule-output length and captured Odoo field limits still
  apply. A row that exceeds a limit receives a stable blocking issue rather
  than a truncated value.

The provider should use stable source-column keys in portable evidence. Source
column names and ordinals remain display information and must not define the
Recipe rule.

## Preview, evidence, and reuse

The existing mapping lifecycle remains unchanged:

1. Saving the rule creates recoverable draft work.
2. **Check matches** validates every selected source key and the complete
   frozen source domain.
3. **Review rule effects** can show bounded input parts and the combined
   proposed value.
4. **Confirm field matches** binds the exact valid revision.
5. Recipe publication stores the rule as portable reusable meaning.

The optional effect report should distinguish these outcomes:

- all selected parts contributed;
- one or more blank parts were skipped;
- all selected parts were blank; and
- the row was blocked because a required part was blank or the output was too
  long.

Changing a source column, its order, the separator, blank handling, part
trimming, or a later cleanup rule creates a new mapping hash. Prior validation,
impact, submission, preparation, comparison, and execution evidence then
becomes stale through the existing invalidation boundary.

When a Recipe is applied to a fresh Data version, every selected stable source
column must resolve through the existing Recipe source-binding rules. A
missing or ambiguous column blocks application and returns ownership to the
fresh-data or matching decision. Impodo must not guess a replacement column
from a similar label.

## Implementation ownership

The feature belongs to the existing **Match data** stage.

| Responsibility | Proposed owner |
| --- | --- |
| Portable provider and serialization | `domain/mapping/contracts.py` |
| Shared row semantics | `domain/mapping/scalar_values.py` |
| Semantic checks and issue paths | `domain/mapping/validation/scalars.py` |
| Native whole-dataset program | `domain/compiler/columnar_transformation.py` |
| Native execution | `adapters/polars_transformation.py` |
| Browser form parsing | `web/presenters/mapping_forms.py` |
| Guided field control | `web/templates/mapping/_scalar_catalog.html` and its page-owned browser module |
| Display labels and Recipe compilation | `domain/compiler/browser_mapping_compiler.py` |
| Optional rule-effect evidence | `domain/staging/transformation_impact.py` and `application/workspace/mapping/transformation_impact.py` |

The shared scalar evaluator remains the row-level oracle. The columnar
compiler must produce equivalent native expressions and must not use a Python
user-defined function for the supported rule. No preview, validation, or
preparation path may query Odoo or a repository once per source row.

## Delivery sequence

### Slice 1 — Contract and row semantics

- Add the versioned portable provider and reject mixed provider shapes.
- Implement ordered concatenation, blank handling, trimming, separator
  insertion, and stable row issues in the shared evaluator.
- Add round-trip, hashing, invalidation, output-limit, and edge-case tests.

### Slice 2 — Native preparation and Recipe reuse

- Compile the provider to a native columnar operation with parity against the
  row oracle.
- Carry all contributing source keys into source lineage and Recipe
  requirements.
- Verify the rule against later compatible Data versions and fail closed when
  a required column is missing or ambiguous.

### Slice 3 — Guided authoring and review

- Add the ordered source-column control, separator choice, blank choice, and
  bounded sample.
- Extend the optional effect report with concatenation outcomes.
- Preserve draft recovery, strict form-field allowlisting, and complete
  off-screen validation blockers.

### Slice 4 — Documentation and browser verification

- Update the paired user and developer **Match data** pages only after the
  feature is implemented.
- Update `docs/workflow.yml`, the Python code map, relevant docstrings, and the
  current screenshots when the browser decision point changes.
- Verify the authenticated browser at 1440 by 1024 with fictional source data.

## Acceptance criteria

The feature is complete when all of these statements are true:

1. A data manager can combine two source columns without editing Excel and
   without writing a formula.
2. The preview makes source order, separator behavior, and blank handling
   visible before confirmation.
3. Blank parts never create an unexplained leading, trailing, or repeated
   separator.
4. The rule round-trips through portable mapping and Recipe payloads without
   using source ordinals or Odoo numeric IDs.
5. The row evaluator and native columnar execution return identical values and
   stable issue codes for populated, partially blank, fully blank, oversized,
   and non-text source values.
6. A rule edit invalidates every downstream evidence object that depends on
   the mapping hash.
7. A later Data version with a missing or ambiguous contributing source column
   blocks safely instead of guessing.
8. Preparation performs no Odoo call and no repository read inside a source-row
   loop.
9. Focused domain, compiler, browser-form, workflow, impact, Recipe, and
   documentation checks pass.

## Non-goals for the first delivery

The first delivery does not:

- combine values from different datasets or related Odoo records;
- build a relationship key or a dataset identity from the combined display
  text;
- accept arbitrary expression code, spreadsheet formulas, loops, or Odoo
  methods;
- add per-part prefixes, suffixes, date formats, or number formats;
- split one source column into several Odoo fields; or
- modify or export a replacement source workbook.

Those needs should be evaluated separately after the guided two-column use
case has real usage evidence.

## Interim Impodo workaround

Until the guided rule is implemented, a data manager can keep the source file
unchanged by using the current advanced formula control:

1. Map either contributing column as the field's source value.
2. Open **Advanced: formula or custom calculation**.
3. Enter a formula such as `strip(concat(column_2, " ", column_3))`, using the
   column references shown beside the control.
4. Select **Replace repeated spaces with one** and **Treat blank values as
   empty** when the source can contain blank or space-only cells.
5. Save, check, preview, and confirm the mapping through the normal workflow.

This workaround is suitable only when the displayed column references and the
preview have been checked carefully. The proposed guided rule removes that
ordinal-based authoring burden.
