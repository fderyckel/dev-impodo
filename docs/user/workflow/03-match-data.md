---
audience: user
stage: match
status: current
---

# Match data

## Goal

Describe how every approved source table becomes an Odoo record, including
identity, values, transformations, and relationships. In authoring, this is
the reusable meaning that can be published as a Recipe revision.

## Before you start

Source data must be frozen and Odoo data must be confirmed. For file sources,
have a functional decision for every required field, stable identity,
selection value, and relationship. Captured Odoo records keep their protected
record identity automatically.

## Steps in Impodo

1. Open **Match data** and work through one table at a time.
2. Choose whether the table is a reference, create, update, or upsert dataset.
3. Match the source identity to the confirmed Odoo business key.
4. For each writable field, choose its source value, a fixed value, or an
   explicit Odoo decision. Use **Let Odoo choose** only when the target
   configuration supplies a default. Use **Odoo manages this field** only for
   a field Odoo creates or maintains itself.
5. Configure text, number, date, and selection-value preparation where needed.
6. Resolve linked fields using a stable key in another project table or
   approved existing Odoo data.
7. Select **Save progress** before leaving the page.
8. Select **Check matches**.
9. Review transformation effects, including rules that changed no values.
10. Select **Confirm field matches** for the exact checked revision.

### When a published Recipe is applied

For a Test or Production data version, first complete the fresh source and
Odoo-data steps, then return to the Recipe overview and select **Apply Recipe**.
Impodo checks current source, target, parameters, control totals, credentials,
relationships, and categorical values before building a fresh mapping draft.

Review that draft on this screen and confirm it through the normal checks. A
renamed used column, a new selection value, or a new many2one value needs an
explicit decision. Applying a Recipe never copies an earlier approval and
never guesses a categorical or linked-record match.

### When the source is captured from Odoo

The originating Odoo record type is fixed and Impodo prepares updates only; it
does not ask for a business key and cannot fall back to creating records.
Blank or duplicate names are therefore allowed.

Match the captured values you want to transform, then separately select
**Allow Impodo to update this field** for each intended write field. Approval
is off by default. A field can be approved only when its original value was
captured and Odoo metadata identifies it as a safe stored, writable scalar.
Confirming these choices still does not contact or change Odoo.

![Current source and Odoo identity choices inside a fictional Recipe data version.](../../images/user/10-mapping-identity.png)

![Current field-value and cleanup controls for a fictional Contact mapping.](../../images/user/11-mapping-fields.png)

![Current read-only report showing the effects of confirmed cleanup rules.](../../images/user/13-rule-effects.png)

## What to check

- Every required Odoo field has a deliberate value or supported default.
- File-source identity values remain stable across environments; captured
  Odoo records use protected target-bound identity instead.
- Selection labels map to the current Odoo technical choices.
- Many2one, One2many, and Many2many relationships use portable keys.
- Cleanup rules change only the intended values and run in the intended order.
- No field is mapped merely because its name looks similar.

## What Complete means

The exact checked mapping revision is confirmed for this data version and the
stage shows **Complete**. A saved or Recipe-built draft alone is not complete.
In authoring, publication from the Recipe overview is a later, separate action.

## What changes and what does not

Saving or confirming a mapping stores instructions and review evidence in the
current data version. It does not edit frozen source data, publish a Recipe by
itself, authorize a load, or write to Odoo.

## Needs attention

Resolve missing required fields, duplicate target assignments, incompatible
types, unresolved relationships, unexpected selection values, and cleanup
rules with zero matches. Keep a zero-match rule only after explicitly
reviewing why it is intentional.

When **Confirm field matches** is unavailable, the reason panel beside the
bottom workflow actions lists every current blocker even if a field search or
page filter hides the affected field. Follow **Match this field**, **Let Odoo
choose**, or the other recovery action shown there, then select **Check
matches** again.

## What makes this work stale

Changes to source evidence, Odoo fields, business keys, mapping choices, field
approvals, or transformation rules require a new mapping check and
confirmation. A previously prepared or compared result must not be reused
after such a change. Changing reusable meaning requires a new Recipe revision
and a new Test qualification before rollout.

## Next stage

Continue to [Prepare data](04-prepare-data.md).

## Related documentation

- [End-to-end training tutorial](../tutorials/end-to-end-training.md)
- [Developer implementation: Match data](../../developer/workflow/03-match-data.md)
