# Match data: questions and answers

## Who this tutorial is for

Use this guide when you are the data manager who decides how an approved source
table should become Odoo records. It uses a fictional `contacts` table with
columns such as `Customer ref`, `Name`, `Country code`, and `Company type`.

**Match data** saves reusable instructions and matching evidence. It does not
change the accepted source files, create a Recipe by itself, or write to Odoo.

🟢 **Normal path** means you can continue. 🔵 **Check** marks a useful review.
🟡 **Review** needs a deliberate decision. 🔴 **Stop** means fix the issue; do
not let Impodo guess.

## What must be ready before I start?

Your source tables must be confirmed, and the Odoo record types, fields, and
matching rules must be confirmed in **Odoo data**. Have one stable business
identifier for each file-source table. For example, use `Customer ref`, not a
row number or an Odoo numeric ID.

🔴 A familiar column label is not proof that two fields mean the same thing.
Confirm the business meaning with the functional owner before you map it.

## How do I choose what Impodo should do with a table?

Open **Match data**, select the table, and use **What should Impodo prepare?**.
Choose one of these outcomes:

| Choose this | Use it when | Edge case |
| --- | --- | --- |
| **Create new records or update matches** | A contact may already be in Odoo, and the confirmed business key identifies it. | A duplicate or unclear key still needs review; Impodo does not update the first similar record. |
| **Create new records only** | Every incoming row must create a new record. | Under **When a record already exists**, choose whether to stop for review or leave that existing record unchanged. |
| **Use existing Odoo records only** | The table is a lookup or reference list, not a list to create. | Every used key must resolve to one existing Odoo record. |

![Current Match data screen showing the table outcome, source identity, and Odoo matching-rule controls.](../../images/user/10-mapping-identity.png)

🔵 Notice the two panels in the screenshot: the left panel identifies an
incoming row, while the right panel tells Impodo how to find the same business
record in Odoo.

## How do I identify each incoming row safely?

Under **Which column uniquely identifies each row?**, choose one source column
or a combination. Then choose the confirmed Odoo **Matching rule** and connect
each part to its source column.

For example, a contact can use `Customer ref` on both sides. A document line
may need `Document ref` and `Line number` together.

🔴 Do not use a name alone when two people or products can share it. Do not use
an Odoo numeric ID from one database; it will not safely identify the same
record in another database.

## How can I fill a normal Odoo field?

For each field, use the menu in **Use value from**. The available choices are:

| Choice in Impodo | What it does | Good example |
| --- | --- | --- |
| **Source value** | Sends one selected source column. | `Name` becomes Contact Name. |
| **Same value for every row** | Sends one fixed value. | Every imported contact receives one agreed company. |
| **Source value, or backup when blank** | Uses the source value unless it is blank, then uses the saved backup. | Use `Unnamed contact` only when a blank name is approved. |
| **Let Odoo choose** | Leaves a required field for a captured, usable Odoo create default. | Odoo supplies an agreed default company type. |
| **Do not fill this field** | Leaves an optional field out. | Leave an unused Notes field blank. |

For a captured Odoo source, **Odoo manages this field** is also available for a
field that Odoo creates or maintains. It is not a general shortcut for a
missing required value.

![Current field-value, cleanup, preview, and bottom action controls for a fictional Contact field.](../../images/user/11-mapping-fields.png)

🟡 A backup is a business decision, not an automatic repair. For example, a
blank email should not become a made-up address. Use a backup only when it is
valid for every blank row.

## How do I convert a value without changing the source file?

Select the right **Value type** and the matching preparation choice. Impodo
stores the rule; your registered file stays unchanged.

| Data you have | Use these choices | Check before confirming |
| --- | --- | --- |
| Text | **Remove outer spaces**, **Replace repeated spaces with one**, or **Treat blank values as empty**. | Make sure a blank means empty, not zero or a value to keep. |
| Decimal number | Choose the source **Number format** and, only when approved, decimal places and rounding method. | `1,25` can mean 1.25 in a French export. |
| Whole number | Choose **Whole number**. | A value such as `01` may be an identifier, not a number. Keep it as text when leading zeroes matter. |
| Date | Choose the stated **Input format**. | `03/04/2026` is unclear until you decide between day-first and month-first. |
| Date and time | Choose **Date and time**. The current time zone choice is UTC. | Confirm the source's time-zone meaning before converting times. |
| Yes or no | Choose **Yes or no**. | Check how the source represents false: blank, `No`, `0`, or another value. |

🔴 Never round money, quantities, or percentages merely to make a total look
right. Confirm the source format, unit, and allowed precision first.

## How do I make a product active only when its status code is 10?

Use this rule when a product-status column has an agreed business meaning: a
code of `10` means the product is active, and every other code means it is not
active. For example, a products table can use `Code Statut Produit` to fill
the Odoo **Active** field.

![Current field-value screen showing where you choose the source column and set the result to Yes or no before opening the advanced formula.](../../images/user/11-mapping-fields.png)

🔵 In the screen, first use **Use value from** to choose the status column,
then set **Value type** to **Yes or no**. The advanced calculation is lower in
the same field's preparation controls.

1. Find the product's **Active** field in **Match data**.
2. Under **Use value from**, choose **Source value** and select `Code Statut
   Produit`.
3. Set **Value type** to **Yes or no**.
4. Open **Advanced: formula or custom calculation**.
5. Enter `value == 10` in **Formula**.
6. Pause briefly or leave the Formula box. If Impodo shows **Must fix**, follow
   the correction beside the formula. Use **Go to issue** if the field is no
   longer visible.
7. Select **Save progress**, then select **Check matches**.
8. Select **Review rule effects** and confirm that code `10` produces Yes and
   each other status produces No before you confirm the field matches.

The word `value` means the source value you selected for this field. This
formula produces Yes when `Code Statut Produit` is `10`; it produces No for
`30`, a blank value, or any other status code.

![Current rule-effects screen showing where you review original and prepared values before confirming the mapping.](../../images/user/13-rule-effects.png)

🟡 The rule-effects screen lets you check the prepared result before you
confirm it. Review a small sample of active and inactive products, including
blank or unfamiliar status codes.

🔴 In Odoo, setting **Active** to No archives or deactivates the product. Do
not use this rule until the product owner confirms that every status other than
`10`, including blanks, should make the product inactive. If another status
needs a different outcome, stop and agree that decision before confirming the
mapping.

## How do I clean or check text?

Open **Prepare and check values** for a Text field. Add cleanup steps in the
order they should run. A field can have up to 20 cleanup steps. You can:

- replace text everywhere, only at the beginning, or only at the end;
- remove spaces, dots, hyphens, or slashes only when they sit between digits;
- use **Use phone cleanup** as an editable starting point for international
  numbers;
- use an **Advanced pattern** only when a guided step cannot express the rule;
- choose capitalisation after cleanup; and
- check an exact length, character type, or a custom pattern for the final
  value.

For example, phone cleanup can turn `00352-621.23.45` into `+3526212345`
without removing the internal `00` from an unrelated value such as `120034`.

🟡 **Advanced: formula or custom calculation** is available for a calculated
value. Use it only when you have a reviewed business formula and no guided
choice fits. Keep the formula small, preview it, and make sure the result has
the right type. It cannot access files, networks, Odoo, loops, or imported
code.

## How do I map an Odoo choice field?

For a choice field such as Company Type, use one of three paths:

1. Choose **One choice for every row** when every row receives the same Odoo
   choice.
2. Choose **Use or match one source column** when the source column already
   holds that decision. Open **View available Odoo choices**, then select
   **Match source values** for any source label that differs from Odoo.
3. Choose **Decide using rules** when the Odoo choice depends on conditions.

When using a source column, decide how all populated values are covered:

- **Every final value is already an exact Odoo code** is appropriate only when
  the source truly carries those exact codes.
- **Every populated source choice must be explicitly matched** is safer when
  the source carries business labels such as `Article` and Odoo stores a
  different code.

For example, explicitly match `Article` to the captured Odoo choice **Goods**.
Do not assume that a source label which looks similar is the same choice.

## How do conditional choice rules work?

Choose **Decide using rules**, then add rules in the order you want them to
win. Each rule has one or more conditions. Choose whether **all match** or
**at least one match**, then choose the Odoo choice to use.

One field can have up to 20 rules, with up to eight conditions in each rule.
Keep the rule set small enough that a reviewer can explain why a row received
its choice.

You can compare text, numbers, dates, date-times, and yes/no values. The
available comparisons include blank or not blank, equals, not equals,
case-insensitive equals, contains, begins with, ends with, and numeric or date
comparisons such as greater than.

Example: for Company Type, put “VAT number is not blank → Company” above “VAT
number is blank → Individual.” Select a value under **When no rule matches**
or keep **Block the row for review**.

🟡 Rules run from top to bottom. The first match wins, even if a later rule
also matches. Use **Move rule up** and **Move rule down** to make the priority
visible.

🔴 A rule with no match or an overlap does not prove the classification is
right. Use **Review rule effects** to inspect it, and block unmatched rows when
you do not have an approved fallback decision.

## How do I connect a field to another record?

Open **Connect values to existing Odoo lists** and find the linked field. For
a product category, company, country, or parent record, choose the source
column, where the related record should come from, and its confirmed matching
rule.

![Current linked-record section of Match data. Open the linked field to choose its source, matching location, and matching rule.](../../images/user/12-mapping-relations.png)

Choose the source deliberately:

| Where Impodo finds the related record | Use it when | What happens |
| --- | --- | --- |
| **Only existing Odoo records** | The record already belongs to Odoo. | Each populated source key must find one Odoo record. |
| **Only another incoming table** | The related records arrive in this data project. | Impodo uses the selected incoming table and its own mapping. |
| **Use Odoo first, otherwise use the incoming table** | Odoo may already have some values, while the rest arrive in the project. | One exact Odoo match is reused; it is not updated merely because it won the relationship match. |

For a value that differs only by its label, select **Match values**. This
translates the lookup key only. It does not rename source data or grant
permission to update the related Odoo record.

🔴 Matching is case-sensitive. `KG` is not automatically the same as `kg`.
Review the case difference and match it explicitly only when the business owner
confirms that both identify the same record.

## How do Many2one, One2many, and Many2many fields differ?

| Field kind | What you do in Impodo | Edge case |
| --- | --- | --- |
| **Many2one** | Match one source value to one related record. | Stop when the key is missing or several records match. |
| **One2many** | Map the child table's inverse Many2one field. Do not write the parent list directly. | A bill of materials receives its lines through the child line table. |
| **Many2many** | Supply one or more source values, state the one-character source separator, and choose **Replace the current list**, **Add to the current list**, or **Remove from the current list**. | A blank list must have an agreed meaning; it can be different from removing every existing link. |

For linked fields, choose whether every source key is already an exact business
key or must be explicitly matched. You can also choose whether a missing or
ambiguous match stops the work or asks you to review it.

🟡 Use **ask me to review** only when a person has an agreed way to decide the
case. Use **stop and ask** for a required relationship or any case where a
wrong link would damage the migration.

## What are the support choices for a field?

Open **Support options** when the normal mapping needs an extra instruction.
You can choose whether to compare the field with Odoo, check it without
preparing it, require a value, require it only for new records, and state how
to treat source blanks.

| Blank-value choice | Meaning |
| --- | --- |
| **Keep source and Odoo blanks separate** | A blank source value and a blank Odoo value retain their separate meaning. |
| **Treat blank values as the same** | Treat two blanks as equal for this field. |
| **Ignore blank source values** | Do not use a blank source value as a value to compare. |

🟡 For an update, blank can mean “clear the Odoo field,” “leave it unchanged,”
or “this row is incomplete.” Decide that meaning with the owner before you
choose a blank policy.

## Can I add a total or a business check?

Yes. Open **Check a known total (optional)** for a mapped numeric field. Give
the check a name, expected total, currency or unit, and a tolerance. Impodo
adds the prepared values and compares the result with your stated total. It
does not choose a currency or unit for you.

If **Data checks** is available, you can also add a guided business check:

- require one field when another has a chosen value;
- require exactly one of two fields;
- require the first value not to be greater than the second;
- require two fields to match; or
- require two fields to be different.

Choose whether a failed check asks for review, sets the affected record aside,
or blocks the data. These checks take effect when you next prepare the data.

🔴 A total that agrees does not prove every row is correct. Still review
required values, identities, relationships, and the prepared-data results.

## What changes when my source was captured from Odoo?

Captured Odoo rows are update-only. They stay connected to their protected
captured identity, so you do not choose a business key or a create fallback.
For each field that you intend to write, select **Allow Impodo to update this
field**. It starts off, and confirmation still does not contact Odoo.

🟡 Only approve a field when its original value was captured and it is shown as
a safe writable field. A blank or duplicate display name does not matter here
because the protected capture identity—not the name—identifies the record.

## How do I check and confirm my matching rules?

1. Select **Save progress** to keep your choices. This does not validate or
   write to Odoo. A malformed advanced formula is still preserved and the
   save reports **Saved — needs attention**.
2. Select **Check matches** to check the full frozen source domain.
   **Check matches** is unavailable while a formula shows **Must fix** or is
   still being checked. Correct it beside the field or select **Go to issue**.
3. Select **Create matching review workbook** when an Excel review will help.
   You can create it after a passing check or a check with errors, then select
   **Download matching review workbook**.
4. Optionally select **Review rule effects** to inspect cleanup and
   conditional-choice outcomes before confirmation.
5. Select **Confirm field matches** only for the exact checked revision.

![Current optional rule-effects report showing original values, prepared values, and their result.](../../images/user/13-rule-effects.png)

🔵 The matching review workbook shows the current Match data check. Its red
**Must fix**, amber review, green valid, blue supplied or prepared, and grey
no-action states include written next actions. It is not the prepared-data or
final Odoo comparison workbook.

🔴 When you change a source choice, Odoo field, identity, relationship, or
transformation, select **Check matches** again. Earlier checks, previews, and
confirmation no longer describe the current rule.

## Edge-case desk

| If you see this | Do this |
| --- | --- |
| A source value is blank | Decide whether it is empty, needs a reviewed backup, should be ignored, or must block the row. Do not treat blank as zero. |
| A number uses commas and full stops | Confirm the export's number format before mapping or rounding. |
| A date can be read two ways | Declare the input format. Do not rely on your computer's local setting. |
| A source label resembles an Odoo label | Check the captured Odoo choice or business key and match explicitly when needed. |
| More than one Odoo record matches | Stop or send it to review. Never select the first result. |
| A relationship differs only by case | Treat it as a review case; match it explicitly only after approval. |
| Two conditional rules both match | Reorder them deliberately and inspect **Review rule effects**. |
| An advanced formula shows **Must fix** | Follow the correction beside the formula. Saving preserves the draft, but correct the issue before **Check matches**. |
| A required field has no source value | Map it, use a verified Odoo default, or use an Odoo-managed disposition only when Impodo offers it. |
| You need to join First name and Last name | This guided rule is **not yet available**. Keep the source prepared as one field or use the reviewed advanced calculation while the [combine-source-columns proposal](../../plans/concatenate-source-columns-matching-rule.md) remains a plan. |
| You changed a confirmed rule | Save, check, review again if needed, and confirm the new exact revision. |

## Before I continue to Prepare data

- [ ] Each table has the intended create, update, or reference outcome.
- [ ] Each source identity and Odoo matching rule represents the same business
  record.
- [ ] Every required field has a deliberate source, fixed value, verified Odoo
  default, or Odoo-managed decision.
- [ ] Each choice and relationship is exact or explicitly matched.
- [ ] Missing, ambiguous, blank, and case-different values have the intended
  outcome.
- [ ] Any calculation, cleanup, total, or business check has been reviewed.
- [ ] I selected **Save progress**, **Check matches**, and **Confirm field
  matches** for the current rules.

Continue to [Prepare data](../workflow/04-prepare-data.md).

## Related documentation

- [Match data workflow guide](../workflow/03-match-data.md)
- [Developer implementation: Match data](../../developer/workflow/03-match-data.md)
- [End-to-end training tutorial](end-to-end-training.md)
