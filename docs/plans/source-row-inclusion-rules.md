# Include only matching source rows

## Status and proposed decision

**Status:** Slices 1, 2, 3, and 4 are implemented for the bounded preparation,
browser, and Recipe-application paths. The row-inclusion shape introduced in
mapping contract version 16 is retained by the current contract, semantic
validation, and shared comparison evaluator. Preparation
evaluates the rule before target-oriented transformation, publishes
lineage-only excluded or blocked decisions, and passes only included rows
downstream. A zero-match result blocks the dataset. Match data owns guided
authoring, full-domain counts, bounded row review, and exact confirmation.
Recipe publication stores the logical rule without observed counts. A fresh
application binds every rule column, checks the new Data version, and shows its
exact counts in the run review. Missing or ambiguous columns, zero included
rows, unsafe values, and unconfirmed exclusions block the application. The
native columnar compiler selects the bounded evaluator for this rule until
native parity is implemented. The paired documentation and screenshot refresh
remain planned for Slice 5.

Add one optional, reusable **Rows to use** rule to each mapped dataset. The
rule lets a data manager include only source rows that meet explicit
conditions. Impodo keeps the complete accepted Data version as evidence, but
it prepares, compares, and sends to Odoo only the included rows.

The first delivery supports one bounded condition group. A group can contain
one to eight conditions and can require either all conditions or any
condition to match. It does not accept formulas, SQL, regular expressions, or
arbitrary executable code.

This is a mapping and Recipe capability. It is not a source-file edit and it
is not a quality-check outcome.

## The problem this solves

A source file can contain a broader business population than one Odoo
migration needs. For example, a fictional Product file contains 12,450 rows,
but only 2,103 rows have `Code statut product` equal to `30`.

Today, Impodo accepts the complete table and prepares every row. An Excel
filter only hides rows in Excel; it does not remove them from Impodo's source
reader. Mapping the status code to an Odoo field also changes only that field.
It does not prevent the product record from reaching Odoo.

Editing the file outside Impodo creates a second, manually filtered source
whose relationship to the accepted delivery is harder to prove. A reusable
inclusion rule keeps the business decision visible, reviewable, and portable
across later compatible Data versions.

## Data-manager experience

### Where the control appears

Show **Rows to use** near the top of each dataset editor in **Match data**,
before record identity and field matches. Keep the section visible as a short
summary when it is complete.

The default remains **Use every row**. Existing mappings therefore keep their
current meaning without any action from the data manager.

When the data manager selects **Use only rows that match**, show a guided
condition builder:

```text
Rows to use                                                   Needs check

( ) Use every row
(*) Use only rows that match

Include a row when [all] of these conditions match:

  [Code statut product      ] [is exactly        ] [30        ]
  Compare as: [Text         ]                         [Remove]

  [+ Add another condition]

Save progress, then check the current rows.
```

Show **all** or **any** only when the data manager adds a second condition.
The condition sentence must remain readable without knowledge of Boolean
expressions.

The comparison type controls how Impodo reads both the source value and the
entered comparison value. Offer only types already supported by deterministic
mapping comparisons: **Text**, **Whole number**, **Decimal number**, **Yes or
no**, **Date**, and **Date and time**. For a status code, **Text** is the safe
default because leading zeroes can be meaningful.

### Check and confirmation

The existing **Check matches** action evaluates the row rule before it checks
field mappings. Show the complete result above the field results:

```text
Rows to use                                                   Checked

12,450 source rows     2,103 included     10,347 excluded
0 could not be checked

Rule: Include a row when Code statut product is exactly 30.

[Review rows]                    [Confirm 2,103 rows to use]
```

**Review rows** opens a bounded, filterable result with these views:

- **Included** shows rows that will continue through preparation.
- **Excluded by Recipe** shows rows that will not be prepared, compared, or
  sent to Odoo.
- **Needs attention** shows rows whose condition could not be evaluated
  safely.

Each displayed row names its source location, the relevant source value, the
decision, and the rule sentence. The view must never imply that a browser
page filter changes the saved decision.

The data manager must select **Confirm N rows to use** when at least one row
is excluded. This confirmation binds the checked counts and rule to the exact
mapping revision and Data version. **Confirm field matches** remains blocked
until the row decision and field matches are both current.

For the example, the confirmation sentence is:

> Include 2,103 Product rows where `Code statut product` is exactly `30`.
> Exclude the other 10,347 Product rows from preparation and Odoo loading.

For the data manager, this means the original 12,450-row delivery remains
accepted evidence. The Recipe records the reusable rule, while the current
workspace records that 2,103 rows matched it in this Data version.

### Safe behavior for blanks and unexpected values

For an **is exactly 30** Text condition:

- `30` in a text or numeric source cell matches.
- A blank value does not match and is counted as excluded.
- `20`, `40`, and any other value do not match and are counted as excluded.
- `030`, `30.0`, and ` 30 ` do not match an exact Text comparison.

The review shows distinct excluded values with counts so the data manager can
notice spelling, whitespace, or new status codes. Impodo does not silently
trim, round, or coerce Text comparisons.

If the data manager selects a typed numeric, date, date-time, or Boolean
comparison, an unparseable source value becomes **Needs attention**. It does
not become an ordinary excluded row. Any **Needs attention** row blocks
confirmation.

If zero rows are included, the result is **Needs attention** and cannot be
confirmed. This prevents an accidental empty migration. A future explicit
no-op workflow can address intentional zero-row runs separately.

## Product semantics

### Positive inclusion, not inverted quality logic

Store the rule as a positive statement: include a row when its conditions
match. Every non-matching row is then **Excluded by Recipe**.

Do not implement this feature by exposing `QualityOutcomePolicy.EXCLUDE` in
the current data-check form. Quality rules evaluate prepared Odoo fields and
describe whether a prepared record is trustworthy. A row inclusion rule uses
source columns to decide whether a source record belongs to the migration at
all. Mixing these responsibilities would force excluded rows through field
transformation and would make the UI read as an inverted failure rule.

The existing internal quality `EXCLUDE` value can remain readable for
backward compatibility. New browser and Recipe payloads for this feature must
use the dedicated row-inclusion contract.

### Evaluation order

Impodo performs the work in this fixed order for each effective dataset:

1. Impodo reads the exact accepted source or derived dataset.
2. Impodo verifies that every rule column resolves to one stable current
   source column.
3. Impodo evaluates the row-inclusion conditions against each dataset row.
4. Impodo records an included, excluded, or cannot-evaluate decision with
   source lineage.
5. Impodo applies identities, field transformations, and relationships only
   to included rows.
6. Quality, normalization, Odoo comparison, execution planning, writing, and
   reconciliation receive only included canonical rows.

An excluded row can therefore contain missing target-required values without
creating a target-field error. The row was outside the migration before those
target rules became relevant.

### Complete accounting

The accepted Data version remains unchanged. Preparation must account for
every effective dataset row with this equation:

```text
source dataset rows = included rows + excluded rows + cannot-evaluate rows
```

Only included rows can produce canonical Odoo records. Excluded decisions
remain protected evidence linked to their source coordinates and physical
lineage. They do not carry proposed Odoo values or numeric Odoo record IDs.

Dataset control totals use included rows and label that scope explicitly.
Preparation and final review show the original, included, excluded, blocked,
and quarantined counts separately. **Excluded by Recipe** must not be merged
into **Quarantined** because excluded rows need no correction.

### Related datasets

Each dataset evaluates its own rule. Impodo does not silently cascade a
parent's exclusion into a dependent dataset.

If an included child row refers only to an excluded incoming parent and the
relationship cannot resolve against approved existing Odoo data, the existing
relationship-readiness rule sets the child aside for review. The data manager
can then change either dataset's inclusion rule or the relationship decision.

## Portable contract

Bump the mapping contract after the current version and add
`row_inclusion` to `DatasetMapping`. Older readable contracts project to
`all_rows` without changing their content hash.

The portable meaning should be equivalent to:

```json
{
  "dataset_id": "fictional-products-dataset",
  "target_model": "product.template",
  "row_inclusion": {
    "mode": "matching_rows",
    "join": "all",
    "conditions": [
      {
        "condition_id": "3a6dd1a5-2773-43d1-b610-12038b352c11",
        "source_column_key": "fictional-product-status-stable-key",
        "operator": "equals",
        "comparison_value": "30",
        "value_type": "string"
      }
    ]
  }
}
```

The contract must enforce these rules:

- `all_rows` contains no conditions.
- `matching_rows` contains one to eight conditions and an `all` or `any`
  join.
- Every condition uses a distinct stable identifier and one current stable
  source-column key from the same dataset.
- Operators and comparison values are valid for the selected value type.
- Portable values remain bounded and contain no formulas, code, source
  ordinals, file identities, credentials, or Odoo numeric IDs.
- Condition order is stable and contributes to the mapping content hash even
  though `all` and `any` evaluation is logically order-independent. Stable
  order keeps browser editing and evidence reproducible.

Introduce a shared source-comparison evaluator instead of calling the private
Selection-field evaluator from row inclusion. `SelectionCondition` and the
new `RowInclusionCondition` remain separate contracts but delegate typed
comparison to the same pure function. The row oracle and native columnar
compiler must return identical outcomes and stable reason codes.

## Recipe reuse and invalidation

A published Recipe stores the logical dataset field, operator, comparison
type, comparison value, and join. It does not store source rows or the counts
observed during authoring.

When the Recipe is applied to a later Data version, source binding resolves
each logical rule field to exactly one current stable source column. A missing
or ambiguous field blocks Recipe application. Impodo never substitutes a
similarly named column silently.

Impodo checks the rebound rule against the complete current dataset. The run
review shows the new Data version's source, included, excluded, and
cannot-evaluate counts. When the rule excludes rows, the data manager follows
**Review rows to use** to inspect and confirm the current decision. A different
count does not by itself change the Recipe rule. Zero included rows or any row
that cannot be evaluated blocks the run.

Changing the inclusion mode, condition, source column, operator, comparison
value, type, or join changes the mapping hash. Existing validation, row-impact
confirmation, preparation, quality, comparison, execution, and qualification
evidence then becomes stale through the normal invalidation boundary.

## Implementation ownership

The feature belongs to **Match data**, with evaluation at the start of
**Prepare data**.

| Responsibility | Proposed owner |
| --- | --- |
| Portable dataset rule and serialization | `domain/mapping/contracts.py` |
| Shared typed source comparisons | New `domain/mapping/source_conditions.py` |
| Dataset-rule validation and issue paths | `domain/mapping/validation/` |
| Mapping and Recipe compilation | `domain/compiler/browser_mapping_compiler.py` and `application/recipe_compilation_service.py` |
| Native whole-dataset selection | `domain/compiler/columnar_transformation.py` and `adapters/polars_transformation.py` |
| Protected decisions and count evidence | Canonical-staging repository and artifact owners |
| Browser form parsing | `web/presenters/mapping_forms.py` |
| Dataset-level authoring control | `web/templates/mapping/_dataset.html` and a page-owned browser module |
| Checked-row review | Mapping-impact application, repository, presenter, and route owners |
| Preparation and downstream eligibility | `application/workspace/preparation/preparation_service.py` |

The application layer coordinates publication and invalidation. Domain code
evaluates supplied rows and policies without opening a repository, source
file, browser request, or Odoo connection.

The columnar path must evaluate the rule in bounded native expressions. It
must not add a Python user-defined function, a database query per row, or an
Odoo request during matching or preparation.

## Delivery slices

### Slice 1 - Contract and comparison semantics

- Add the versioned row-inclusion contract with an `all_rows` default.
- Extract one pure typed source-comparison evaluator for field Selection rules
  and row inclusion.
- Add round-trip, old-version compatibility, hash, bound, and malformed-value
  tests.

### Slice 2 - Evaluation, evidence, and accounting

- Evaluate inclusion before target identities and values.
- Publish protected per-row decisions and dataset count reconciliation.
- Pass only included rows into canonical transformation and downstream stages.
- Verify that excluded rows cause no Odoo reads or writes.

### Slice 3 - Browser authoring and review

- Add the **Rows to use** control and strict form-field allowlisting.
- Extend **Check matches** with included, excluded, and cannot-evaluate counts.
- Add bounded row review and the explicit **Confirm N rows to use** action.
- Preserve draft recovery, stale-tab protection, CSRF checks, and
  project-scoped access.

### Slice 4 - Recipe reuse and run review

- Compile the rule into logical Recipe meaning and bind it to fresh Data
  versions.
- Show exact current counts during Recipe application and final review.
- Verify missing-column, ambiguous-column, changed-count, zero-included, and
  relationship-dependency behavior.

### Slice 5 - Documentation and browser verification

- Update the paired **Match data**, **Prepare data**, and **Final review** user
  and developer pages after implementation.
- Update the canonical-staging and quality contracts, workflow registry,
  Python code map, and current screenshots.
- Capture the authenticated decision and review states at 1440 by 1024 with
  fictional data.

## Acceptance criteria

1. A data manager can express `Code statut product is exactly 30` without
   editing the source file or writing a formula.
2. **Use every row** remains the default for every old and new dataset.
3. The full-domain check reports original, included, excluded, and
   cannot-evaluate counts before confirmation.
4. Excluded rows bypass target-field and relationship transformation and
   cannot enter Odoo comparison, request planning, or execution.
5. Every effective source row is accounted for with protected lineage.
6. A missing or ambiguous Recipe column and an unsafe typed comparison fail
   closed instead of becoming silent exclusions.
7. An inclusion-rule edit invalidates all dependent evidence.
8. Recipe publication stores portable logical meaning without source rows,
   physical file identities, or observed counts.
9. Row and native columnar evaluators have parity for text, number, Boolean,
   date, date-time, blank, and invalid values.
10. Preparation performs no repository or Odoo access inside a source-row
    loop and passes the appropriate scale gate.

## Non-goals for the first delivery

The first delivery does not:

- modify, delete, or export a replacement source file;
- rely on Excel hidden-row or worksheet-filter state;
- support nested condition groups, formulas, SQL, regular expressions, or
  user-supplied code;
- filter using current Odoo values or numeric Odoo record IDs;
- provide random sampling, top-N selection, deduplication, or aggregation;
- silently cascade exclusions across related datasets; or
- treat excluded rows as data-quality failures that require correction.
