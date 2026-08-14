---
audience: user
stage: odoo
status: current
---

# Odoo data

## Goal

Choose the Odoo 19 record types and fields needed by the migration, and confirm
how Impodo can identify one existing record.

## Before you start

The project target must be configured. A file-source project also needs frozen
source tables. Know the intended Odoo business records and agree stable
business keys with the functional owner.

## Steps in Impodo

1. Open **Odoo data**. In an Odoo-source project this is shown first as
   **Odoo source data**.
2. Select **Show available Odoo data**.
3. Choose only the record types included in the approved scope.
4. Load the selected Odoo details.
5. Review fields, types, required values, selections, and relationships.
6. For a file-source migration, choose the business key for each writable
   record type and confirm the exact captured schema and matching rules.
7. For an Odoo source, confirm the eligible fields needed by the bounded source
   capture.

Use portable values such as customer reference, internal product reference,
country code, or BoM reference. Do not choose an Odoo numeric database ID as a
portable business key.

![Current Odoo record-type selection bound to a fictional Odoo 19 target.](../../images/user/08-odoo-models.png)

![Current confirmed matching rule for finding one existing Odoo Contact.](../../images/user/08b-odoo-business-keys.png)

## What to check

- The model is the intended Odoo record type, including custom models when
  applicable.
- Required fields and selection choices reflect the connected database.
- For a file source, each business key is expected to find zero or one record,
  never several.
- Linked records can be resolved by an incoming table or approved existing
  Odoo data.
- The scope contains no unrelated business areas.

## What Complete means

For a file source, the selected schema and business-key governance are saved
together and **Match data** becomes available. For an Odoo source, the eligible
schema is captured and you next define and freeze the bounded source-record
selection.

## What changes and what does not

This stage reads and stores target metadata. It does not create or update Odoo
records. Confirming a business key does not prove that every current value is
unique; the later comparison checks the captured target evidence.

## Needs attention

Do not continue when the wrong database, model, inherited field, or business
key is shown. Refresh the available records or recapture the selected details.
If access is unavailable, resolve the Odoo plan, credentials, or permissions
instead of guessing field definitions.

## What makes this work stale

Changing model scope, recapturing fields, or changing a business key
invalidates dependent mapping and review evidence. Recheck the next stages
against the new captured schema.

## Next stage

For a file source, continue to [Match data](03-match-data.md). For an Odoo
source, return to [Source data](01-source-data.md) and freeze the selected
records before matching.

## Related documentation

- [Connect to Odoo on this computer](../guides/local-odoo.md)
- [Developer implementation: Odoo data](../../developer/workflow/02-odoo-data.md)
