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

For reviewed Odoo 19 references such as Country, Language, and Currency,
Impodo checks the parent relationship and exact portable key through one
shared policy. It may show the bounded Odoo choices without asking you to add
the supporting record type to the migration scope. A changed relationship,
key, or Odoo field contract blocks the check and returns ownership to this
stage.

### Fill an Odoo choice field

An Odoo choice field now shows **View available Odoo choices** even when the
source file has no matching column. The disclosure shows the business label
and the technical value captured from the current Odoo fields.

Choose the provider that matches the business decision:

- Select **One choice for every row** when every source row must receive the
  same Odoo choice. For Company Type, select **Company** to send the technical
  value `company`; do not create a fake Excel column.
- Select **Use or match one source column** when one source column already
  contains the decision. Select **Match source values** only when its populated
  values need explicit translation to Odoo choices.
- Select **Decide using rules** when the decision depends on one or more source
  columns. Add the most specific rule first, choose whether all or any of its
  conditions must match, and choose the resulting Odoo value. Then choose an
  **When no rule matches** value or keep **Block the row for review**.

Rules run from top to bottom and the first matching rule wins. **Save
progress** to preview the first source row. **Check matches** evaluates the
complete frozen source domain; a row that matches no rule and has no otherwise
choice blocks confirmation. Impodo never invents a Company Type rule from a
VAT number, company name, or another source column. The data manager owns that
classification decision.

### When reusable rules are ready

Complete and submit the Authoring mapping through the normal checks. Return to
the Project overview only after its source, schema, mapping, and quality
evidence is current. If the workspace is eligible, you can save the rules as a
new Recipe or publish a successor revision. Applying those Recipes to later
DataVersions belongs to the next implementation phase.

### When the source is captured from Odoo

The originating Odoo record type is fixed and Impodo prepares updates only; it
does not ask for a business key and cannot fall back to creating records.
Blank or duplicate names are therefore allowed.

Match the captured values you want to transform, then separately select
**Allow Impodo to update this field** for each intended write field. Approval
is off by default. A field can be approved only when its original value was
captured and Odoo metadata identifies it as a safe stored, writable scalar.
Confirming these choices still does not contact or change Odoo.

![Current source and Odoo identity choices inside a fictional Project workspace.](../../images/user/10-mapping-identity.png)

![Current field-value and cleanup controls for a fictional Contact mapping.](../../images/user/11-mapping-fields.png)

![Current read-only report showing the effects of confirmed cleanup rules.](../../images/user/13-rule-effects.png)

## What to check

- Every required Odoo field has a deliberate value or supported default.
- File-source identity values remain stable across environments; captured
  Odoo records use protected target-bound identity instead.
- Selection labels map to the current Odoo technical choices.
- Ordered choice rules use the intended source columns, and every row resolves
  to a current Odoo choice or is deliberately blocked for review.
- Many2one, One2many, and Many2many relationships use portable keys.
- Cleanup rules change only the intended values and run in the intended order.
- Choice rules show how many rows matched, how many rows first-match priority
  selected, and how many rows also matched another rule.
- No field is mapped merely because its name looks similar.

## What Complete means

The exact checked mapping revision is confirmed for this DataVersion and the
stage shows **Complete**. Saving a Recipe is a later, separate action on the
Project overview and is not required for one-off work.

## What changes and what does not

Saving or confirming a mapping stores instructions and review evidence in the
current data version. It does not edit frozen source data, publish a Recipe by
itself, authorize a load, or write to Odoo.

## Needs attention

Resolve missing required fields, duplicate target assignments, incompatible
types, unresolved relationships, unexpected selection values, and rules with
zero matches or overlapping priority. Keep a zero-match rule only after you
review why it is intentional. Keep overlapping rules only after you confirm
that the earlier matching rule should supply the final Odoo choice.

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
