# Related-dataset preparation

**Status:** lookup extraction and parent/child dataset preparation are
implemented in the local browser. Both become normal Mapping datasets, and
readiness repeats their rules over every frozen source row. Durable canonical
staging and Odoo execution are not implemented.

## Purpose

Legacy exports often flatten related Odoo records into one table. Impodo keeps
the registered file immutable and records a governed preparation rule instead
of asking the user to edit a workbook.

The browser supports two source shapes:

1. **Reusable lookup values.** A product field such as `Product Category`
   produces related `product.category` candidates with identities owned by the
   category model.
2. **One parent with multiple lines.** A repeated key such as `BOMId` produces
   one logical parent dataset and one logical child dataset containing every
   original row.

Both rules remain inside the same migration project as their source and other
related datasets.

## Parent/child browser workflow

Open **Prepare related datasets** after freezing the physical datasets. For a
parent/child source, choose:

- a parent dataset name and child dataset name;
- the repeated parent key;
- an optional company, site, or tenant scope;
- the line key that distinguishes children inside a parent;
- whether missing required keys block or quarantine affected records.

Impodo explains each choice using business language and shows a review screen
before saving. The review includes source rows, parent candidates, retained
child rows, sample groups, blank identities, sample duplicate line identities,
and normalization evidence.

For a BOM-line table, a typical rule is:

| Meaning | Source field |
| --- | --- |
| Parent key | `BOMId` |
| Scope | `dataAreaId` |
| Line key | `LineNum` |

After confirmation, Mapping receives the generated parent dataset first and the
child dataset second. The original physical dataset is still the immutable
evidence but is no longer offered as a third import candidate. Impodo preselects
the governed trace identities and suggests the child-to-parent incoming-dataset
relationship only when the selected Odoo models confirm that relationship.

Direct writes to a parent's one2many field are never proposed. The child
many2one owns the imported relationship.

## Lookup extraction workflow

For a reusable value stored only as a source field, choose the source field,
derived dataset name, target model, stable source-system namespace, blank
policy, and optional hierarchy separator.

Before the target model can be selected, the browser loads the persistent
record types advertised by the project's configured Odoo database through the
same read-only model-catalogue boundary used by schema selection. Preview and
save both reject a technical model name that is absent from that current,
target-bound catalogue.

Impodo normalizes Unicode, whitespace, case-insensitive identity, and optional
hierarchy paths. The bounded preview shows deterministic Impodo and Odoo
External IDs without borrowing a contributing product or row identity.

Review the candidates, then create the related dataset. Mapping keeps the
original dataset and inserts the extracted dataset before it. Impodo suggests:

- the rule's Odoo model for the extracted dataset;
- a canonical source trace identity and the configured display-name field;
- an **Incoming dataset** relationship on a compatible many2one field in the
  original dataset.

For example, one `Product Category` column can create unique
`product.category` candidates while each `product.template.categ_id` value
resolves against those incoming candidates. Readiness performs the extraction,
normalization, deduplication, and relationship resolution over all source rows.
Missing related values and conflicting display spellings block the affected
candidate or source row for review.

## Identity contract

Lookup identity input contains only:

- the stable source-system namespace;
- the related Odoo model;
- the normalized canonical entity path.

Different parents keep homonymous children distinct. For example:

```text
Furniture / Accessories -> entity A
Computers / Accessories -> entity B
```

For parent/child datasets, the user supplies governed business keys rather than
inventing technical IDs. The parent trace identity uses parent key plus scope;
the child trace identity uses parent key plus scope plus line key. Impodo keeps
generated dataset identifiers behind the UI.

## Governance and invalidation

- Preparation plans are immutable revisions stored in the project DuckDB.
- Each plan is hash-bound to the frozen source selection.
- Freezing a new source selection invalidates the active preparation plan.
- Saving or removing a preparation rule invalidates the current mapping.
- Historical plan revisions remain available in project storage.
- Loading or refreshing the available record types performs one explicit,
  read-only Odoo catalogue request. Source-value preview and authoring never
  write to Odoo and do not query Odoo once that catalogue is loaded.
- Mapping and relationship checks must operate in batches, not one Odoo request
  per source row.

## Delivered boundary

This slice delivers:

- guided preview and persistence for parent/child splits;
- mapping-ready parent and child logical datasets;
- automatic trace-identity defaults;
- model-confirmed child-to-parent relationship suggestions;
- bounded blank, duplicate, normalization, and grouping evidence;
- reviewed lookup extraction with deterministic identity previews;
- lookup-derived datasets in Mapping beside their original datasets;
- model, identity, display-name, and compatible many2one suggestions;
- full-row readiness materialization and incoming-dataset resolution for
  lookup-derived records;
- hash binding, revisions, audit, invalidation, and backward-compatible reading
  of contract-version-1 lookup plans.

It does not yet deliver:

- durable canonical staging tables outside the readiness run;
- a durable cross-project rename or alias registry;
- Odoo External-ID matching or creation;
- import files, API writes, rehearsal, or export certification.

Those capabilities belong to the staging, semantic-validation, and controlled
execution slices. They must reuse the governed rules and batch relationship
resolution defined here.
