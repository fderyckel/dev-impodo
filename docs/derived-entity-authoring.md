# Related-dataset preparation

**Status:** lookup extraction authoring and parent/child dataset preparation are
implemented in the local browser. Parent/child datasets participate in Mapping.
Full-row canonical staging and export are not implemented.

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

Impodo normalizes Unicode, whitespace, case-insensitive identity, and optional
hierarchy paths. The bounded preview shows deterministic Impodo and Odoo
External IDs without borrowing a contributing product or row identity.

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
- Preview and authoring never contact or write to Odoo.
- Mapping and relationship checks must operate in batches, not one Odoo request
  per source row.

## Delivered boundary

This slice delivers:

- guided preview and persistence for parent/child splits;
- mapping-ready parent and child logical datasets;
- automatic trace-identity defaults;
- model-confirmed child-to-parent relationship suggestions;
- bounded blank, duplicate, normalization, and grouping evidence;
- lookup extraction authoring with deterministic identity previews;
- hash binding, revisions, audit, invalidation, and backward-compatible reading
  of contract-version-1 lookup plans.

It does not yet deliver:

- full-row canonical staging tables;
- full-source post-normalization duplicate and referential-integrity proof;
- a durable cross-project rename or alias registry;
- lookup-derived datasets in the Mapping selector;
- Odoo External-ID matching or creation;
- import files, API writes, rehearsal, or export certification.

Those capabilities belong to the staging, semantic-validation, and controlled
execution slices. They must reuse the governed rules and batch relationship
resolution defined here.
