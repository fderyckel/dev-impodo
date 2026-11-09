---
audience: developer
kind: plan
status: proposed
---

# Reuse a Recipe when Odoo changes and resolve problems in large files

## Proposed outcome

A data manager should be able to apply the same Recipe in successive migration
environments. If an Odoo developer adds a field and the file already contains
the information, the data manager should be able to add that one mapping and
test a new Recipe version. They should spend their time on the new decision
and carry forward the rules they already reviewed.

When a large file has problems, Impodo should show what it checked, what is
ready for the current stage, and which correction would release the affected
records. A row number helps the data manager find a problem. The field, the
value Impodo checked, and the reason the check failed explain how to fix it.

This proposal is for maintainers reviewing two extensions to the existing
workflow. The first delivery covers file-source Recipes and diagnostics in
local Impodo with local or remote Odoo 19 and 20. All new buttons and messages
below are proposed wording. The current workflow qualification and related
delivery plans retain their own scope and sequencing.

## Add a field and save a successor Recipe

### The data manager's journey

The **Manufacturing** Recipe already maps the **BoMs** worksheet in
`Manufacturing.xlsx`. The sheet includes **Manufacturing note**, which the
Recipe has never used. The test Odoo database now has a writable
**Manufacturing note** field on **Bill of Materials**. The data manager wants
that column to feed the new field on the next run.

1. From the Recipe version or a run using it, the data manager chooses
   **Revise this Recipe**. Impodo shows the exact version being used as a
   starting point. When an eligible Authoring workspace already has that
   mapping and the right source and destination, the action opens it. For a
   closed or diverged workspace, the proposed action starts an Authoring
   draft from the selected version and the chosen compatible source delivery.
   An unrelated draft keeps its own saved work.
2. The data manager chooses which Odoo environment to check. The proposed
   journey leads to **Check for new or changed Odoo fields** and **Apply new
   optional fields** in Authoring. Impodo verifies access and refreshes field
   information for the mapped record types. It shows the new field in the
   relevant table and opens that field in **Match data**. An unused optional
   field can be left out when the existing mapping still passes its checks.
3. The data manager maps **Manufacturing note** to the existing source column
   and reviews a short change summary. In this example it says **Added one
   mapped Odoo field** and **Uses one additional column already in the file**.
   Show the source provider, the intended create and update behavior, and
   how blanks will be handled. The normal mapping preview and validation
   establish the effect of the new rule.
4. The data manager uses **Save a new Recipe version** after the usual
   publication checks. Impodo appends the version with its change summary.
   The test setup can offer that version preselected for a fresh run. An
   existing run continues to identify the version it started with.
5. Where a Production rollout is required, the data manager qualifies the
   revised plan through the existing Test workflow before selecting it for
   Production. Each environment supplies its own Odoo evidence, matches,
   credentials, and approvals. A target that lacks the newly mapped field
   gets a precise compatibility message with the field name and the options
   to check the deployment or select a compatible revision for a new Test.

The smallest useful first case is one optional writable scalar field on a
record type the Recipe already maps. Reuse the existing matching and field
editor controls. Extend the flow to relational providers or new record types
when the existing reference, dependency, and ownership checks can support a
specific case. A field that affects governed identity or a shared target
record calls for explicit review of that changed meaning.

### Current behavior and implementation boundary

Impodo already has an Authoring schema refresh that can carry existing field
matches and matching rules forward when the change consists of optional added
fields. It also has an Authoring command to publish a successor Recipe. The
first implementation task is to qualify that existing path end to end and
make its entry point clear. Starting from a historical version with no
suitable open Authoring workspace is the additional capability proposed here.
Reuse the [Authoring schema service](../../src/impodo/application/schema_workspace_service.py),
[schema route](../../src/impodo/web/routers/schema.py), and
[Recipe publication contract](../developer/contracts/recipe-lifecycle.md).

An applied Recipe has a narrower editing boundary. It may review supported
target value choices and controls under the compiled baseline. Adding a field
provider expands the Recipe's write scope. The new action therefore goes to
Authoring and uses the existing publication service. Test and Production keep
their current target capture, write ownership, and qualification contracts.
A change to the selected cutover plan can affect an older Production run's
execution authority. Its pinned version and recorded evidence still identify
what that run was set up to do.
See the [Integrated Test contract](../developer/contracts/integrated-run-lifecycle.md),
[Cutover plan contract](../developer/contracts/cutover-plan-lifecycle.md), and
[Production contract](../developer/contracts/production-run-lifecycle.md).

The file may have the same headings while the successor Recipe needs more
from it. Recipe compilation stores the logical columns the rules use. Mapping
a previously unused column adds a source requirement to the successor. Show
that change beside the new Odoo field. Rebind from the accepted Data version
through the existing source-binding checks and report an absent or ambiguous
column explicitly. See the
[Recipe compiler](../../src/impodo/application/recipe_compilation_service.py).

Use the existing compatibility and required-field policies when the target
changes in a different way.

| What Impodo finds | What the data manager can do |
| --- | --- |
| An optional writable field was added. | Apply the saved Recipe within its existing scope or open Authoring to map the field for a successor. |
| Odoo added a computed, related, or read-only field. | See that Odoo supplies the field when it is relevant to the change review. The existing field policy determines which fields are writable. |
| Odoo added a required writable field for creates. | Review the captured default or supply the missing value through the existing create-field policy. Access or context gaps stay visible. |
| A mapped field disappeared, changed type, or became read-only. | Open the affected mapping or correct the destination deployment. The compatibility check determines whether the existing revision can be applied. |
| Another Recipe already owns writes to the proposed field. | Resolve write ownership when planning the revised Test. The run planner identifies the competing Recipes and field. |
| The new field needs a source column missing from the accepted file. | Select an eligible provider or accept a corrected delivery. Ambiguous bindings require a decision. |

The new draft uses the selected immutable Recipe revision as a seed. Authorize
the Project action before reading the Recipe or its source evidence. Resolve
its logical datasets and columns against the chosen accepted source, capture
the verified authoring destination, and materialize rules through the normal
compiler and mapping owners. Keep target-specific matches subject to fresh
checks. Reuse an eligible Authoring workspace when its rules match the chosen
baseline and its source and destination are suitable. The most recent open
workspace alone does not establish that match. Define the new draft's owner
through the current Project and Authoring lifecycle before implementing the
historical-version entry point.

Use the current optimistic revision and publication operation for saving.
Review the intended changes before publication, including any difference that
arises while reopening the draft. A failed refresh or save keeps the draft
available for correction. After the mapping changes, the existing evidence
lifecycle determines which checks to run again. Accepted source data remains
available to the draft. A changed Recipe version needs fresh Test and cutover
qualification under the current rollout rules. See the
[evidence lifecycle](../developer/contracts/evidence-lifecycle.md).

## Explain a problem and offer a way forward

### The data manager's journey

Consider a fictional file with 2,400 BoMs. The current check finds that 14
refer to the same final product code, `FP-87`. The scoped lookup completed and
found zero eligible matches in the company **Northwind**. Impodo can show one
problem group for those references and open the affected records on demand.

> **14 BoMs refer to a final product that could not be matched.**
>
> BoM `B-381` on row 892 of **Manufacturing.xlsx**, sheet **BoMs**, uses
> `FP-87` in **Final product code**. The matching rule found no eligible
> product with code `FP-87` in **Northwind**.
>
> **Review product match** · **View affected records**

The primary action opens the existing relationship decision for that field and
exact value. If the lookup was incomplete or access failed, the message says
the check could not finish and offers the corresponding recheck or access
action. A missing source record and an unverified target lookup have different
remedies.

The summary separately counts records ready for this stage, records needing a
decision, records affected through dependencies, and records already set
aside. It states the scope of the current check. For example, 2,386 records
can be ready for comparison while the load still requires another decision.
The action beside the totals uses the actual current readiness policy.

### Information a finding needs

Reuse the current problem group and field review surfaces. Each actionable
finding should answer these questions in the first view or its expanded row
detail.

- **What failed?** Name the record type, field, expected value or relationship,
  and observed reason. Use labels from the source and target when available.
  If a rule changed the input before checking it, show the source and checked
  values when that distinction explains the failure.
- **Where is it?** Show the business record key and the file, sheet or table,
  and original row and column when captured. For CSV, show the source record
  number when the physical line is unknown. For a combined or generated row,
  identify the contributing inputs the saved lineage actually proves.
- **How far does it reach?** Count direct failures and dependent records
  separately. Distinguish physical source rows from prepared records. Use the
  union of affected records when several causes overlap. Keep a separate group
  for the same displayed key in a different company or parent scope.
- **What can I do?** Offer the best eligible action and show its scope. Keep
  secondary choices in the same review. If someone else must fix the source or
  target, specify the field or key to correct, who needs to act, and which check
  the data manager can run afterward.

### Remedies and their scope

The same causes and actions apply to any supported model. A BoM final product,
a contact payment term, and a custom record's parent all use governed
relationship evidence. The underlying reason and the current stage determine
which actions appear.

| Confirmed finding | Proposed next action | Effect to show before confirmation |
| --- | --- | --- |
| A required input is blank or invalid. | **Fix source value** opens the exact cell or contributing source rows. Offer **Fill in value** when a supported provider can supply it with the intended scope. Otherwise guide the operator to accept a corrected delivery and rerun the Recipe. | Identify the value, record, and affected rule. A default may apply to every eligible blank, so show that full population before saving. |
| A complete key has no eligible target match. | **Review match** opens the exact scoped lookup. The data manager can correct the key, explicitly link to another eligible record, or arrange for the related record to be created through an authorized workflow. | Show the selected record by business key and scope. If the choice applies to all occurrences of the source value, show the affected count. |
| Several records match a key. | **Review matches** shows eligible candidates or opens the matching rule when the key needs more detail. | Show the full key and company or parent scope. An incomplete key requires correction. A choice requires sufficient evidence to distinguish the intended record. |
| An incoming parent is excluded or has its own failure. | **Open parent problem** opens the owning finding, with **View affected records** for the dependency group. | Show the direct failure and the affected parents, children, or siblings identified by the dependency checks. |
| A known bad row or complete affected group can be set aside. | **Review rows to use** opens the source-selection decision. **Set aside affected groups for this load** opens the current comparison review when that policy applies. | Preview exactly which source rows or logical groups the action covers, its dependency effects, and which checks need to run again. The stage determines whether the decision changes source inclusion or this load only. |
| Impodo could not verify a value or access to Odoo. | **Check again**, **Review Odoo access**, or a specific instruction for the target owner. Include **Support details** when a cause is unknown. | Identify the incomplete check and what evidence is needed. Leave unverified records visibly unresolved. |
| Odoo definitively rejected a write. | **Review load outcome** opens the confirmed rejection and source trace, then the owning correction or current continuation flow where eligible. | Show the exact known outcome. If only a batch rejection is established, name the batch and keep individual attribution uncertain. |
| The result of a write is unknown. | **Verify in Odoo** opens the existing reconciliation and recovery journey. | Show the unknown outcome and what read-back must establish before any further write is permitted by the recovery policy. |

Actions are suggestions until the data manager reviews and confirms their
effect. An optional relationship can be left blank only where the existing
field and mapping rules allow it. Clearing an input, omitting a field,
excluding a source row, and setting aside a load group each have different
effects. The review names the exact effect and shows the affected records.
Shared scope comes from the governed identity, field, rule, and current
evidence. Two rows with the same error label alone do not establish a shared
correction.

In the first delivery, **Fill in value** and **Review match** should take the
operator to the existing field or value editor when its scope fits. A Recipe
application can use its permitted value choices. A remedy that would change
the pinned rules leads to a corrected delivery or the Recipe revision
journey, according to which decision is needed. If a
single-cell correction still sends operators back to large spreadsheets, a
later slice can trial a small inline correction. Bind it to the exact source
delivery, run scope, row, column, and original value. Preview all affected
derived records and recheck the accepted type and scope before applying it
through a governed correction path. Retain the original source and require an
explicit decision to share the correction with another run. Publishing a
reusable rule for future files is a separate explicit choice.

### Reuse the evidence and correction owners

Use the existing issue projection, quality cause membership, matching detail,
comparison groups, and execution outcomes. Extend the projection of an existing
finding with the smallest required reason, source trace, impact, and action
information. The view explains decisions. The owning workflow validates and
saves them. See the
[quality and quarantine contract](../developer/contracts/quality-and-quarantine.md),
[Final review workflow](../developer/workflow/05-final-review.md), and
[execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md).

- In **Match data**, route to the exact mapping, provider, relationship, or
  **Rows to use** decision. Preserve draft entries when a check or save fails.
- In **Prepare data** and **Final review**, use saved lineage and complete
  cause membership to explain direct and dependent effects. Recheck the owning
  stage after a source or mapping correction. Apply any load-specific set-aside
  decision only after previewing the current comparison's full eligible group.
- In **Load into Odoo**, use the execution journal for known and unknown
  outcomes. Keep continuation under the existing load policy. Exact-record
  repair for known fallout retains its separate scope and acceptance work.
  See [load continuation](20-tax-validation-and-rejected-record-continuation.md),
  [load diagnostics](../developer/contracts/execution-and-reconciliation.md#safe-write-diagnostics), and
  [exact-record repair](06-load-fallout-source-cell-workbook.md).

Read summaries from saved counts and page the affected records through an
authorized detail query. Resolve sample values from permitted source or target
evidence on demand. Keep the cause index, URLs, logs, and routine support
bundles free of raw business values, credentials, and protected target IDs.
Richer custom-rule classifiers and a separately reviewed protected diagnostic
attachment remain deferred.
Preserve source-mode disclosure and spreadsheet formula protections when
linking to an existing workbook. A detail page describes unavailable evidence
honestly when historical runs lack a stored row or column trace.

Use one bounded synchronous local operation per page or command. Release
local database owners before Odoo calls or worker dispatch. Resolve target
matches through the existing scoped lookup, using the verified Odoo major and
shared reference policy. Use bounded queries and paged detail for a large
problem group. When the operator opens or saves a remedy, revalidate the
current source, draft, run, access, and relevant check bindings. If a tab is
stale, return to the updated finding and preserve the operator's draft values
where they can still be reviewed. Repeated saves follow the owning command's
idempotency rules. See the
[local database operation standard](../architecture/code-organization.md#local-browser-database-operation-scopes).

## Delivery sequence

**Current slice · 1. Prove the smallest Recipe change.** The first work item
exercises the existing Authoring refresh and publication flow with one writable
scalar field and an unused column in an accepted CSV. The fixture applies the
successor in a fresh Test with one existing and one new customer. It uses
controlled Odoo 19 and 20 evidence for each access mode. When the browser
retries the same completed publication, Impodo returns the saved revision.
A different stale publication still requires a reload. See the
[one-field revision test](../../tests/integration/web/test_recipe_optional_field_revision.py).

**Revise this Recipe** now opens the exact version from Recipe history or a
run card. It checks the originating Authoring workspace against the saved
rules, accepted data, and verified Odoo destination. Before saving, the
change review shows the new field, source requirement, write behavior, and
blank handling. The saved successor shows that comparison from the selected
baseline. The controlled fixture also checks a newer unrelated workspace, an
older pinned run, and a stale review submission.

Historical draft seeding now provisions a separate Authoring run over the
version’s accepted file delivery. The Odoo check supplies current field
evidence. **Open Match data** loads the saved rules as an editable draft.
Retries recover the draft from the registry and keep later edits. File Recipe
drafts restore saved business checks, custom input declarations, exact
reference lists, and supporting choices verified against the new capture.
When the saved version has advanced checks, the data manager reviews its
historical coverage scope after checking Odoo and approves it for the new
draft. Preparation uses the new approval and the draft's checked matches.
Live qualification remains part of the delivery sequence.

1. **Prove the smallest Recipe change.** Exercise the existing Authoring
   refresh and successor path with one new scalar field and a source column
   already in the file. Deliver the exact-version entry point and change summary
   next. Add draft seeding for closed or diverged Authoring workspaces only
   where the existing workspace path cannot serve the requested revision.
2. **Deliver one complete diagnostic journey.** Start with a missing or
   ambiguous required relationship. Show the record, original source location,
   key, reason, and affected group. Open the existing match or source correction
   and return to the current check. Include a large file and an unrelated
   record type so the design proves both scale and generic behavior.
3. **Apply the same presentation to common findings.** Extend the existing
   views for missing input, invalid value, duplicate identity, and dependent
   records. Connect the appropriate exclusion, comparison group review, and
   load outcome actions. Add a single-value correction path only for a
   demonstrated scope that the current editors cannot address cleanly.
4. **Qualify both improvements together.** Use one Recipe across disposable
   environments with a fresh delivery, a field addition, a missing match, and
   an interrupted load. Build on the current
   [workflow qualification](08-end-to-end-trial-and-scenario-qualification.md#acceptance-criteria),
   [hierarchy fixtures](../../tests/support/hierarchy_cases.py), and
   [manufacturing practice pack](../../scenarios/impodo-practice/manufacturing/v1/README.md).
   Each plan keeps its existing implementation and qualification owners.

## Acceptance and measurement

Run the applicable authoring, Integrated Test, and qualified Production cases
for Odoo 19 and 20, with local and remote access. Report each combination
separately. Distinguish controlled fixtures from disposable live qualification.

| Case | Evidence required |
| --- | --- |
| Same delivery shape, one new optional field. | The data manager maps it with the existing source column. The revised Recipe adds exactly that intended provider and requirement. Existing rule meaning survives. The earlier version still supports its original scope. |
| Historical version and an unrelated open draft. | Starting from the selected version opens or creates the correct Authoring context. The unrelated draft retains its edits. A failed refresh or repeated publication request leaves one recoverable result. |
| Required, read-only, absent, or incompatible field and a write-owner conflict. | Each case uses its owning policy and names the needed decision. Production accepts only the revised qualified plan with fresh evidence and approvals. |
| Large file with missing and ambiguous values. | The review names each cause, original location, checked key and scope, and next action. Identical display values in different scopes remain separate. Multiple causes for one record count once in the combined impact. |
| One source row contributes to several outputs. | The review distinguishes the source row from affected prepared records. A correction or set-aside preview accounts for the complete dependency group and any still-eligible records. |
| Stale browser tab or unavailable evidence. | An outdated finding cannot apply a correction to newer data. A failed refresh or save retains the draft, and the next action lands at the relevant decision. |
| Definitive rejection and lost write reply. | The review reports each outcome separately. Existing continuation and read-back rules determine the next action. Retrying recovery does not duplicate earlier committed records. |

Extend the [optional field tests](../../tests/integration/web/test_mapping_optional_fields.py)
from field discovery through saving a writable mapping and publishing its
successor. A fresh Test must prepare, compare, write, and read back the new
field on the intended records. Seed the destination to exercise both creates
and updates. Use the [Authoring publication tests](../../tests/application/project/test_authoring.py)
and [Production lifecycle tests](../../tests/application/run/test_production_rollout.py)
for version and qualification assertions. For diagnostic fixtures, distinguish
an incomplete compound key, a deliberate optional blank, and a lookup that
could not complete from a confirmed missing match.

Measure time from selecting a Recipe to a usable check result, repeated
decisions, time from a finding to the owning correction, and time until the
operator can see the result of rechecking. Compare a stable repeat run with
an added field and with a dirty delivery. Measure page opens, SQL statements,
schema checks, Odoo call counts, and finding volume on the same fixtures. Use
structural budgets for automated tests. Derive time targets from a measured
baseline with the existing workflow rather than guessing them in this plan.

The first accepted outcome should demonstrate a one-field Recipe revision
and a complete diagnosis-to-correction journey through ordinary entry points.
After implementation is verified, update the owning workflow and contract
documentation, workflow registry, and applicable screenshots with the
behavior that was actually delivered.
