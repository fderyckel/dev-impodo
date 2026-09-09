---
audience: user
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Goal

Test selected Recipe versions with a newer source delivery and the Odoo target
you choose for this Test run. Impodo keeps this work inside the same data
project, but creates a fresh Test data version and a separate working area for
each Recipe. The Authoring sample and saved Recipes remain unchanged.

![The Test setup form selects exact Recipe versions, their required order, and the cutoff for the newer delivery.](../../images/user/03-integrated-test-plan.png)

## Before you start

Your data project needs at least one saved Recipe from an accepted Authoring
data version. Have the complete newer Test delivery and the connection details
for your chosen Odoo target ready. Impodo will collect both as fresh Test
evidence.

## Steps in Impodo

1. Open the data project and select **Test with new data**.
2. Select the exact saved Recipe versions to test.
3. Under **Required order**, select a dependency only when one Recipe must
   finish and reconcile before another can begin.
4. Enter the newer data cutoff and select **Create Test setup**. Impodo opens
   **Fresh data** for this run.
5. Review the exact Recipe versions and their required source tables. Expand a
   table only when you need to see its required columns.
6. Under **Fresh data**, select the complete newer delivery. Select **Add fresh
   files**. You can remove an incorrect file on the same page.
7. Select **Check files and match tables**. Impodo shows the table chosen for
   each Recipe input. If two tables could be right, choose one. If a file is not
   used by the Recipe, remove it and check the files again.
8. Enter the requested **Details for this run** and **Expected totals for this
   delivery**, then select **Use this fresh data**. This accepts the matched
   tables and answers for the Test data version.
9. Under **Check Odoo**, connect the Odoo target for this Test run.
10. Review the Odoo record types, fields, and current supporting values taken from the
    exact selected Recipe versions. You cannot replace them with other Odoo
    choices in this run, and you do not select related tables again.
11. Select **Check this Odoo**. Impodo checks the required fields, refreshes
    the related Odoo values in bounded groups, checks every selected Recipe,
    and creates its separate Recipe work areas when everything is ready.
12. If a Selection or linked-record value differs for this Odoo, Impodo keeps
    **Check Odoo** current and opens **Review values for this Odoo**. Choose an
    Odoo value only for each source value that needs attention. Existing valid
    matches stay collapsed. If every value already matches, select **Confirm
    and continue** without rematching anything.
13. After the target values are confirmed, Impodo takes you to **Review and
    load** and starts preparing the first compatible Recipe. The page updates
    while Impodo works locally.
14. If a card says **Action needed**, open only that card's named review. A
    later Recipe stays waiting until the earlier result is verified.
15. When a card says **Ready for review**, review its prepared rows, exclusions,
    warnings, relationships, and proposed load. **Check changes** remains
    read-only. If Odoo already matches every prepared row, Impodo records that
    verified result and returns to **Review and load** without asking you to
    confirm an empty load. Otherwise, **Confirm and load** remains your
    explicit decision.
16. Review **Verify result**. After successful verification, Impodo starts the
    next compatible Recipe automatically. When every card is verified, review
    and qualify that exact Test run as the Production candidate.

Before creating Recipe work areas, the same Odoo check confirms that the
Recipe order has no cycle and that two Recipes do not claim the same writable
Odoo field. Reordering two conflicting Recipes is not a safe repair; one
Recipe must own that field.

An installed Odoo application may add required fields that were not part of a
saved Recipe. If the current target returns a straightforward create default,
the Recipe continues and its card says that the Odoo target difference was
handled automatically. You can open that note to see the affected field.

Impodo still opens **Review Odoo defaults** when a default selects a linked
record, workflow choice, company-sensitive value, or business amount. The page
shows the exact value and explains why it needs review. Confirm the group to
keep the Recipe unchanged for this run. If Odoo returns no usable default, the
card remains blocked and the Recipe needs a new version with a value provider.
Impodo never guesses from an Odoo choice list.

Target-specific choices follow a separate review. **Review values for this
Odoo** works for any Recipe model that uses a bounded Odoo Selection field or
a Many2one relationship with one governed business key. The page compares the
fresh source values with the Selection options and linked records captured by
**Check this Odoo**. Your decisions belong only to this Recipe application.
They do not change the saved Recipe, and reviewing the page does not make
another Odoo request.

For a linked record identified by several values, Impodo checks the complete
combination, including any company value. For example, category code **SALES**
in company **North** does not match the same code in company **South**.
These exact matches are read-only. If a combination is missing or ambiguous,
correct the data in a new run, or correct Odoo and select **Recheck Odoo**.
Changing the matching rule requires a new Recipe version.

The review keeps you in the run when a check fails. It explains the remaining
problem without opening field-authoring controls. If the Odoo evidence changes
while the page is open, reload the page and review the current choices before
confirming.

![The target-value review shows one French language choice to confirm and one verified English match.](../../images/user/03b-recipe-target-values.png)

The run keeps the Recipe's business checks after you confirm these choices.
The expected totals you accepted on **Fresh data** stay fixed during field review.
Changing field providers, transformations, record identities, or relationships
requires a new Recipe version. Impodo rejects those changes when you confirm
the run's field matches.

An older blocked Test run can select **Check Odoo defaults**. Impodo refreshes
the one shared setup target, verifies that no other field behavior changed,
and rebuilds only applications whose earlier required-field blockers are now
covered. The application continues automatically for straightforward defaults.
Impodo asks you to review only the context-sensitive defaults described above.

## Expected totals for this delivery

Some Recipes check a known amount or quantity. Enter this delivery's expected
total under **Expected totals for this delivery** before you accept the data.
For example, if the customer balances in your delivery should total EUR 125.50,
enter **125.50** for **Opening balance total**. Impodo compares that answer with
the prepared rows using the Recipe's allowed difference.

Each total names its Recipe and source table. Enter separate answers when two
Recipes request totals, even if their labels are similar. A value marked
**Fixed by the Recipe** is read-only. Zero and negative totals are accepted;
missing or invalid numbers prevent acceptance.

![Fresh data asks for the opening balance total and shows the Recipe, table, currency, and allowed difference before acceptance.](../../images/user/03a-fresh-data-control-totals.png)

**Save run details** keeps valid answers while you review the table matches.
It does not accept the files or save unsubmitted table choices.

Once accepted, the totals appear as a summary. Field review cannot replace
them; start a new Test run if a total was wrong. An older setup that saved its
other answers before this feature may request its missing totals once. Its
previous answers stay unchanged.

## What to check

- The Test data version represents one complete, accepted delivery.
- Every selected Recipe version is the intended saved version.
- The required source tables shown under **Fresh data** match the business
  content you expect for each Recipe.
- Dependencies describe real business order, not a workaround for a collision.
- The reviewed Odoo workspace belongs to this data project and target.
- The selected Recipe versions declare non-overlapping writable Odoo fields.

## What Impodo creates

**Create Test setup** creates one draft Test data version, one Test run, and
one shared setup workspace. **Check this Odoo** activates that same run after
you accept the newer delivery and check the chosen Odoo target.
Before you add files, **Fresh data** reads the exact selected Recipe versions
and shows their reusable source requirements. Archiving a Recipe later does
not change the version already pinned to this run.
Each selected Recipe then receives:

- its exact saved version;
- only the logical datasets it needs from the Test data version;
- only its required Odoo models, fields, and supporting lists;
- a freshly checked mapping for this run, or a named issue that prevents its
  automatic confirmation; and
- its own issues and working evidence.

The saved Recipe remains unchanged. No source table or prior workspace is
copied.

The **Fresh data** page explains what the Recipes require and accepts or removes
the new delivery files in the run journey. **Check files and match tables**
compares each safe detected table with the required Recipe columns. A renamed
file can match automatically when its table is the only compatible choice.
Impodo asks only when more than one table could fill the same Recipe input.
Missing inputs, unsafe formula or error tables, and files the Recipe does not
use remain on this page with a clear correction.

The page also asks for details that belong only to this run, such as a stock
date, warehouse, location, or batch reference. The questions and labels come
from the selected Recipe versions, so the same page works for customers,
products, stock balances, and transactional data. If several Recipes use the
same compatible detail, enter it once. The delivery cutoff is already supplied
and appears read-only.

Impodo checks the value type and any saved limit before accepting it. A missing
required value or disagreement between selected Recipes keeps **Fresh data**
current and explains what needs attention. Saved answers belong to the Test
run; they do not change the Recipe or the Authoring workspace. If an earlier
Test delivery was accepted before run details were stored, return to **Fresh
data**, supply the missing details, and continue to **Check Odoo**.
After the details are accepted with the fresh data, they are read-only. Start a
new Test run if an accepted answer needs to change.

**Review values for this Odoo** remains part of **Check Odoo**. It appears only
when a Recipe application cannot use all target-specific values automatically.
The page shows each affected field independently, so the same review works for
contacts, products, transactions, and other Recipe models. If another mapping
problem remains after these values are saved, Impodo directs you to the full
field review instead of treating the target-value decision as complete.

**Review and load** is the visible home from preparation through verification.
It shows the saved Recipe order, one current action, background progress, and
the verified count. A clean Recipe card stays compact. Open its detailed
workspace only to review prepared data, compare changes, confirm a load, or
resolve a named current-data issue. Returning to the run shows the next safe
action.

A card remembers whether preparation finished, Odoo changes were checked, or
verification still needs attention. Restarting Impodo may clear an in-memory
progress message, but it does not discard the saved Recipe application state
or silently repeat an Odoo load.

Reopening a verified Recipe takes you to its saved result. Reopening a Recipe
whose Odoo changes were checked takes you to the load review. Background
percentages update in place, so routine progress does not close expanded details.
The **Fresh data** and **Review and load** navigation links return to the
corresponding pages for this run.

The setup and Recipe workspaces still keep the detailed evidence. Their browser
navigation belongs to the run: setup permits fresh-data and Odoo-check pages,
while an application permits only preparation, review, load, and verification.
A saved or copied setup schema link returns to the run-owned **Check Odoo**
page. The ordinary Authoring workspace keeps its editable Odoo model picker
and six-stage journey.

## Ready and Blocked

**Ready to prepare** means the Recipe was rebound to the current source and
target and its fresh mapping passed the current checks. It does not mean
the integrated run is executed or qualified for rollout.

**Blocked** means the run page names the current difference and the next
action. Typical examples are a missing source column, a changed Odoo field, a
missing supporting list, a required parameter, uncovered values, or a quality
scope that must be reviewed again. A blocked application may still contain a
fresh draft when the issue can be reviewed in that workspace.

If Impodo stops while creating Recipe work areas, return to **Check Odoo** and
retry the saved check. It finishes the original setup and preserves application
work already created. If an older application reports that its saved Recipe
baseline is missing, start a new Test run with the same Recipe version before
changing its run decisions.

## What Complete means

All selected Recipes appear in the validated order, each with a distinct
Recipe work area. The run shows one shared Test data version, one shared
Odoo target review, and the exact Cutover plan version created for the run.
Planning alone does not call the Test result qualified.

## What changes and what does not

Starting setup creates fresh Test identities. Activating the setup creates one
new Recipe work area for each selected version. Neither action changes the
saved Recipes, Authoring data version, Authoring workspace, Odoo data, or
rollout authority. Test credentials belong to the shared Test setup and never
become Recipe content.

## Needs attention

If preparation stops or you reopen Impodo, return to **Review and load**.
Impodo checks whether the current Recipe already has saved work to review.
For example, if the customer balances finished preparing before the progress
page stopped updating, the card returns to **Ready for review**. Select
**Review prepared data** to continue with those saved rows.

![After reopening the run, Customer balances is ready for review and offers Review prepared data.](../../images/user/03c-recovered-recipe-review.png)

An unfinished duplicate review opens its existing decisions. After you approve
that review, preparation continues. If preparation did not save a complete,
current result, the card keeps its existing attention or retry action.
Recovering prepared rows does not load them into Odoo or verify the result.

If planning stops before workspace creation, correct the named missing
dataset, target field, supporting list, dependency cycle, or overlapping field
owner. If a Recipe application is blocked after creation, return to
**Review and load** and open the one card marked **Action needed**. Do not enter its Source
data, Odoo data, or Match data pages and do not save a new Recipe version merely
to hide current-data drift.

## What makes this work stale

A different Test data version, Recipe version, dependency edge, target schema,
supporting-reference version, or credential generation requires a new exact
plan. Earlier Ready status does not transfer.

## Next stage

Complete preparation, comparison, load, and verified read-back from **Review
and load** for each Recipe. Follow dependency order, then
[qualify the integrated Test](qualify-integrated-test.md).

## Related documentation

- [Create a data project](../getting-started.md)
- [Match data](../workflow/03-match-data.md)
- [Prepare data](../workflow/04-prepare-data.md)
- [Developer implementation](../../developer/workflow/07-integrated-test-runs.md)
