# Derived-entity authoring

**Status:** implemented browser authoring slice; full-row staging and export are
not implemented.

## Purpose

A legacy source may store a reusable business entity only as a field on another
record. For example, many product rows may contain a `Product Category` label
even though the legacy system has no category table. Odoo represents that
concept as a separate `product.category` record linked from
`product.template.categ_id`.

Impodo must not borrow the product identity for the category. It instead:

1. binds a rule to one frozen dataset and source column;
2. normalizes the source value and, when configured, its hierarchy path;
3. constructs one canonical related entity per normalized path;
4. assigns that entity a deterministic Impodo ID and Odoo External ID;
5. records parent-entity and source-alias evidence in a bounded preview.

The rule remains inside the same migration project as the products and all
other related datasets.

## Browser workflow

After freezing datasets, open **Review derived entities**. For each rule choose:

- the frozen source field;
- a unique derived dataset name, such as `product_categories`;
- the Odoo target model and display field;
- a stable source-system namespace that will be reused in rehearsals and the
  production migration;
- an optional hierarchy separator, such as `/`;
- whether blank source values will block staging or be quarantined.

The browser displays candidates from the bounded source-inspection sample. It
also displays the full-source distinct count already calculated by inspection,
including whether that count is exact. The preview is evidence for authoring;
it is not a full-source transformation result.

## Identity contract

The ID input contains only:

- the stable source-system namespace;
- the related Odoo model;
- the normalized canonical entity path.

It never contains the product ID or another contributing child-row ID. The
same canonical identity produces the same identifiers even if the rule itself
is recreated. Different parents produce different IDs for homonymous children.

Example:

```text
Furniture / Accessories -> entity A
Computers / Accessories -> entity B
```

Both display names are `Accessories`, but their full canonical paths and IDs
remain different. [Odoo 19 product categories support this hierarchy through
`parent_id`](https://github.com/odoo/odoo/blob/19.0/addons/product/models/product_category.py);
stock locations and other hierarchical models use the same general
parent-reference pattern.

Generated IDs remain stable while the governed canonical identity remains the
same. Rename and alias survivorship across separately registered migration
projects still requires the later durable entity-registry slice; the current
preview flags normalized aliases for review instead of claiming that a rename
has been resolved.

## Governance and invalidation

- Derived plans are immutable revisions stored in the project DuckDB database.
- Each plan is hash-bound to the current frozen source selection.
- Freezing a new source selection invalidates the current derived plan.
- Saving or removing a derived rule invalidates the current mapping pointer so
  the mapping cannot remain current after its source-preparation meaning
  changes.
- Historical plan revisions remain available in project storage.
- No authoring or preview action contacts or writes to Odoo.

## Delivered boundary

This slice delivers:

- a generic, model-agnostic derived-entity rule;
- deterministic related-entity and External-ID generation;
- optional parent-path construction;
- blank and malformed-path evidence;
- alias-collision review evidence;
- browser creation, preview, removal, persistence, audit, and invalidation;
- tests proving that identical child names under different parents get
  different IDs and that product IDs are not used.

It deliberately does not yet deliver:

- full-row execution or canonical staging tables;
- a durable cross-project rename/alias registry;
- generated datasets in the mapping relationship selector;
- Odoo External-ID matching or creation;
- import files, API writes, rehearsal, or export certification.

Those capabilities belong to the structural transformation, staging, Odoo
semantic-validation, and controlled-execution slices. They must reuse this ID
contract and must resolve distinct related entities in batches rather than
performing one Odoo request per product row.
