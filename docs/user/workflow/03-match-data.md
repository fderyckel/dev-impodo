---
audience: user
stage: match
status: current
---

# Match data

## Goal

Describe how every approved source table becomes an Odoo record, including
identity, values, transformations, and relationships.

## Before you start

Source data must be frozen and Odoo data must be confirmed. Have a functional
decision for every required field, stable identity, selection value, and
relationship.

## Steps in Impodo

1. Open **Match data** and work through one table at a time.
2. Choose whether the table is a reference, create, update, or upsert dataset.
3. Match the source identity to the confirmed Odoo business key.
4. For each writable field, choose its source value, constant, or intentional
   omission.
5. Configure text, number, date, and selection-value preparation where needed.
6. Resolve linked fields using a stable key in another project table or
   approved existing Odoo data.
7. Select **Save progress** before leaving the page.
8. Select **Check matches**.
9. Review transformation effects, including rules that changed no values.
10. Select **Confirm field matches** for the exact checked revision.

![Current source and Odoo identity choices for one fictional customer table.](../../images/user/10-mapping-identity.png)

![Current field-value and cleanup controls for a fictional Contact mapping.](../../images/user/11-mapping-fields.png)

![Current linked-record mapping controls for existing Odoo lists.](../../images/user/12-mapping-relations.png)

![Current read-only report showing the effects of confirmed cleanup rules.](../../images/user/13-rule-effects.png)

## What to check

- Every required Odoo field has a deliberate value or supported default.
- Identity values remain stable across environments.
- Selection labels map to the current Odoo technical choices.
- Many2one, One2many, and Many2many relationships use portable keys.
- Cleanup rules change only the intended values and run in the intended order.
- No field is mapped merely because its name looks similar.

## What Complete means

The exact checked mapping revision is confirmed and the stage shows
**Complete**. A saved draft alone is not complete.

## What changes and what does not

Saving or confirming a mapping stores instructions and review evidence. It
does not edit frozen source data and does not write to Odoo.

## Needs attention

Resolve missing required fields, duplicate target assignments, incompatible
types, unresolved relationships, unexpected selection values, and cleanup
rules with zero matches. Keep a zero-match rule only after explicitly
reviewing why it is intentional.

## What makes this work stale

Changes to source evidence, Odoo fields, business keys, mapping choices, or
transformation rules require a new mapping check and confirmation. A previously
prepared or compared result must not be reused after such a change.

## Next stage

Continue to [Prepare data](04-prepare-data.md).

## Related documentation

- [End-to-end training tutorial](../tutorials/end-to-end-training.md)
- [Developer implementation: Match data](../../developer/workflow/03-match-data.md)
