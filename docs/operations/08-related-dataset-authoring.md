# Related-table authoring

## Purpose

Use **Prepare related datasets** when a denormalized source file contains
information that should become more than one Odoo record type. Impodo records
a governed rule and keeps the frozen source unchanged.

The browser supports two shapes:

1. **One field contains reusable values.** Repeated values become one related
   table, while the original rows remain available for mapping.
2. **Several rows describe the same record.** Impodo creates one table with one
   row per group and another table that retains every source row.

These rules currently require frozen file-source datasets. They are expanded
over every source row during preparation, not only over the bounded authoring
preview. Derived or materialized preparation is currently limited to 25,000
physical source rows per project.

## Before starting

- Freeze the intended physical source tables.
- Configure the project's Odoo target.
- For a reusable-value table, load the current Odoo record-type catalogue so
  Impodo can verify the selected target model.
- Agree the business identity of each generated record with the functional
  owner. Similar-looking labels are not automatically the same entity.

Open **Source data**, then **Prepare related datasets**.

## Create a related table from reusable values

Choose **One field contains reusable values** when values from one source field
should become their own Odoo records. For example, a `Product Category` field
can produce a related `product.category` table while the original product rows
retain their category values.

Choose:

- the source table and field;
- the name shown for the new table;
- an existing Odoo record type from the current target-bound catalogue;
- the Odoo display-name field;
- a stable, lowercase source-system namespace; and
- whether blank values stop preparation or are set aside for review.

Select **Preview related records**. Check the sampled source-row count, distinct
value estimate, blank examples, cleaned identities, and proposed External IDs.
The preview is authoring evidence; preparation repeats the rule over the full
frozen dataset.

Select **Create this related table** only when the preview represents the
intended business records. The new table becomes available beside the original
table in **Match data**.

### Identity behavior

The generated identity uses:

- the stable source-system namespace;
- the related Odoo model; and
- the normalized canonical entity path.

Impodo normalizes Unicode, whitespace, case-insensitive identity, and optional
hierarchy paths without rewriting the frozen source value. Parent paths keep
homonymous children separate. For example:

```text
Furniture / Accessories -> entity A
Computers / Accessories -> entity B
```

Do not merge records only because their final labels match.

### Mapping the relationship

Impodo suggests the generated table's target model, trace identity, and display
name. When the captured schema confirms a compatible many2one field, it also
suggests an **Incoming dataset** relationship from the original table to the
generated table.

Review that suggestion against the functional relationship. Missing related
values or conflicting display spellings remain visible as preparation issues;
they are not silently resolved from an arbitrary contributing row.

## Create two tables from repeated rows

Choose **Several rows describe the same record** when repeated information
connects several source rows. Impodo creates:

- one table with one row per group; and
- one table that keeps every source row.

Choose:

- **Table with one row per group**;
- **Table that keeps every source row**;
- **Which field groups rows together?**;
- **Should another field keep identical group values separate?**, when the same
  group value can occur in different contexts;
- **Which field identifies each row within its group?**; and
- whether missing required identity information stops preparation or is set
  aside for review.

Select **Preview the related tables**. Check the group count, retained source
rows, blank identities, duplicate row identifiers, normalization notices, and
example groups. A row identifier must be unique inside its group and additional
separation value.

For example, a bill-of-materials export might use:

| Meaning | Example source field |
| --- | --- |
| Grouping field | BoM reference |
| Additional separation | Company, only when required |
| Row identifier | Line sequence |

Select **Create these separate tables** only when the preview groups the source
rows correctly. The original physical table remains immutable evidence but is
replaced in the effective mapping selection by the generated group and retained
row tables.

### Mapping parent and child records

Map both generated tables to their own Odoo models and business keys. The
one-row-per-group identity uses the grouping value plus any additional
separation. The retained-row identity adds the row identifier.

An Odoo one2many list is owned by its child model's inverse many2one. Map the
child table's many2one relationship to the incoming parent table. Do not map or
write the parent's one2many list directly. Impodo proposes the child-to-parent
relationship only when the selected Odoo models confirm the relation.

## Preparation and loading boundary

Related-table authoring performs no Odoo business-record read or write. Loading
or refreshing the record-type catalogue is one explicit read-only metadata
action; source preview and rule authoring use local frozen evidence.

After authoring:

1. map every generated table and confirm the current mapping revision;
2. prepare the complete supported dataset;
3. review quality, quarantine, normalization, and transformation evidence;
4. compare the approved prepared rows with Odoo;
5. review the exact frozen execution snapshot; and
6. explicitly confirm **Load into Odoo** for the governed disposable target.

The authoring preview is not full-row validation, Odoo comparison, approval, or
permission to write. Downstream preparation, comparison, execution, and
reconciliation retain their own prerequisites and evidence.

## Governance and invalidation

- Related-table plans are immutable revisions stored in the project DuckDB.
- Each plan is hash-bound to the frozen physical source selection.
- Replacing the frozen source selection invalidates the current plan.
- Saving or removing a rule invalidates the current mapping and downstream
  prepared evidence because the effective dataset shape changed.
- Historical plan revisions remain available for audit.
- Generated tables retain lineage to every contributing physical source row.
- Mapping, relationship resolution, comparison, and target access remain
  batched. No Odoo request is permitted inside a source-row loop.

## Authoritative detail

- [Browser workspace contract](../contracts/02-workspace.md)
- [Canonical staging contract](../contracts/03-canonical-staging.md)
- [Local-browser user guide](01-local-browser-user-guide.md)
- [Current capability and remaining work](../plans/remaining-work.md)
