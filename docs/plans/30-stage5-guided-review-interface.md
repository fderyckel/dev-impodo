---
audience: developer
document_type: plan
status: approved
---

# Give Stage 5 a clear sequence

This proposal reorganizes the interface for data managers who need to know
where they are, what has been checked, and what to do next. It preserves the
existing workflow and its decisions. The maintainer approved implementation
on 12 November 2026 with the explicit requirement to keep the existing CSS,
colours, and design. The interface now uses the existing components; the
earlier concept mockup is not an application stylesheet or a visual specification.
The current workflow guides own the implemented labels and actions. Browser
visual verification and refreshed screenshots remain pending when preview
access is unavailable.

## Recommendation

Use the Stage 3 navigation pattern: named substeps in the existing sidebar,
one focused page at a time, and one primary next action. For the ordinary
prepared-data comparison, use three substeps:

1. **Prepared data** explains the records available for comparison.
2. **Compare with Odoo** owns comparison setup, progress, and recovery.
3. **Review results** brings proposed changes and their findings together.

Keep preparation approval in Stage 4 and final load confirmation in Stage 6.
Do not introduce another approval, required checklist, or completion state.
Substeps are locations in the interface. Existing evidence and checks continue
to determine what actions are available.

An Odoo-to-Odoo destination transfer has a different existing workflow. Give
it the same visual structure with **Transfer order**, **Transfer contents**,
and **Approve transfer**. Do not insert an Odoo comparison into its Stage 5.

## What currently makes orientation difficult

This assessment comes from the current templates, navigation, routes, tests,
and accepted contracts. Existing documentation screenshots supplied visual
context. The live browser could not be inspected because computer-use access
was unavailable. The findings below are source-backed observations, not a
claim that a particular current workspace was reproduced.

| Observation | Consequence for a data manager |
| --- | --- |
| Ordinary Stage 5 navigation exposes only **Review and compare**. Stage 3 exposes named pages and tasks. | Progress, recovery, findings, and result inspection do not have distinct visible locations. |
| The summary mixes preparation status, quality records, comparison results, business expectations, saved evidence, exports, and readiness. | The user must work out which result applies to the action they want to take. |
| Comparison and load actions appear in several conditional sections, including a second transition near the bottom. | Scrolling can make the same review feel like several competing routes. |
| Successful comparisons can open Stage 6 directly, while attention results return to a filtered Stage 5 list. | The destination changes with the result, without a consistent explanation of the transition. |
| A combined **Set aside** count includes preparation exclusions and accepted final-review omissions, but its link targets only the preparation quarantine filter. | A user can open a count and fail to find every record counted there. |
| After accepting a reduced load, original comparison findings and reduced load totals remain visible in different sections. | A user can mistake retained findings for unresolved blockers on the remaining load. |
| Preparation approval, issue correction, group previews, expectation editing, and workbooks lead to different pages. | Without parent context and a return destination, each side task can feel like leaving the sequence. |

The primary source owners are
[`workspace_summary.html`](../../src/impodo/web/templates/workspace_summary.html),
[`navigation.py`](../../src/impodo/web/presenters/navigation.py),
[`preflight.py`](../../src/impodo/web/routers/preflight.py), and
[`mapping_journey.py`](../../src/impodo/web/presenters/mapping_journey.py).

## Ordinary comparison: three substeps

| Proposed location | Main question | Content | Primary action |
| --- | --- | --- | --- |
| **5.1 Prepared data** | Which records will be compared? | Current preparation and approval status; counts by table; exclusions by filters; records held back by a data issue; declared totals; concise causes and exact correction links. | **Continue to comparison** when existing prerequisites are satisfied. Otherwise show the existing action that resolves the actual prerequisite. |
| **5.2 Compare with Odoo** | Has this prepared data been checked against the intended Odoo destination? | Verified destination and company context; preparation status; existing explicit comparison action; current attempt, progress, outcome, and recovery. | **Compare with Odoo**, **View comparison progress**, or the applicable recovery action. A completed saved result offers **Review results**. |
| **5.3 Review results** | What would happen in Odoo, and what needs my decision? | Comparison totals; field differences and linked records; expected versus found; findings; accepted set-aside scope; existing load availability and handoff. | The existing next action for the result: inspect blockers, review the remaining load, continue to Stage 6, or inspect the saved outcome. |

Prepared data is an orientation page, not another preparation approval.
An approved Stage 4 review can still start comparison directly. The user does
not have to revisit substep 5.1 or click through every substep.

Use **Not yet compared** before a comparison exists. Never display zero
comparison findings as evidence that Odoo has been checked. An old report must
be labelled **Previous saved comparison** when it no longer applies to the
current inputs.

Keep the existing clean-result transition to Stage 6 **Check changes**.
At that destination, show a concise transition cue such as **Comparison
complete · Stage 6: Check changes** and a link back to the saved Stage 5 results.
Do not add an extra Stage 5 stop merely to make the sequence look complete.
Preserve Recipe Test completion when the existing no-change path completes
and verifies a run automatically.

## Review results: one place for findings and changes

Start with a compact outcome sentence and a horizontal count summary.
For example, **108 records compared; 4 need attention**. Keep the current
record outcomes distinct from findings: one record can have several findings.

Within this substep, provide three focused views:

- **Needs attention** shows current findings, their business effect, affected
  columns and records, and a precise action. Select this view when the current
  result requires intervention. A warning that does not block loading must
  say so beside its action.
- **Proposed changes** shows new records, updates, unchanged records, field
  differences, related-record outcomes, and expected versus found. Start with
  all tables and allow the existing table and status filters. Do not require
  a separate acknowledgement for every table or record.
- **Records outside this load** separates filter exclusions, preparation data
  issues, and records omitted by an accepted Stage 5 set-aside decision. Show
  each available count with a destination that accounts for those same records.

Use a single detailed owner for each finding. The preparation page may show
its count and a short cause, but should link to the existing detail rather
than repeat the complete expanded card. Existing checks remain responsible
for identifying causes and dependencies; this layout does not merge or
recompute their evidence.

When a group preview is accepted, lead with **Current load after your
set-aside decision** and the existing remaining-load totals. Keep the original
report available under **Original comparison**. Retained findings do not mean
that the remaining load is still blocked. Conversely, accepting omissions
does not mean the underlying data problems have been corrected.

The set-aside preview remains an explicit existing decision. It must retain
linked-record effects, writes remaining, surviving blockers, and stale-preview
rejection. Whole-comparison failures cannot become selectable row omissions.
Reduced counts must still satisfy the saved business expectations.

## Keep location and next action visible

The existing application sidebar remains the main navigation. Nest the three
substeps under Stage 5 rather than adding a second application sidebar or a
second six-stage progress bar. On narrow screens, collapse that list into one
labelled current-substep control.

Every Stage 5 page should identify the stage, substep, and applicable scope.
A record detail might read **Stage 5 · Review results / Products / Source
row 24**. Give comparison progress, finding details, expectation editing,
affected groups, and workbook jobs the same parent location.

Show current position separately from business status. **Current** identifies
the location. **Complete**, **Needs attention**, and **Not yet available**
come from existing saved evidence and action availability. Never mark all
substeps complete because the final page was visited.

Use one primary action area in a consistent location. Remove competing
primary cards and repeated expanded blockers. On a long list, an action footer
may repeat that same action; it must not offer a different interpretation of
readiness. Explicit section links take priority over remembered scroll.

Navigation, Browser Back, refresh, and opening a detail must not start a
comparison, approve preparation, accept omissions, build a transfer package,
or grant write authority. Step and filter parameters select a view only.
Keep existing URLs and anchors usable through their correct parent substep.

## Make corrections feel like a returnable side task

Before leaving, say where the action goes. For example, **Edit Category match
in Stage 3** is more useful than **Fix issue**. Link to the exact table, field,
or saved choice when the existing correction action supports it.

Retain the originating result, table, filter, page, and useful position as
navigation context. Offer **Return to Final review** from the destination.
If changes invalidate preparation or comparison, return to the first required
existing action and explain why: **Matching rules changed. Prepare the data
again, then compare with Odoo.** Do not silently restore a stale green result.

Keep the previous saved result separate from the latest comparison attempt.
A failed attempt must say whether a previous result is still current, whether
no new report was saved, or whether the save outcome needs recovery. Reopening
progress resumes the existing attempt. Only the existing explicit retry starts
another attempt when permitted.

## Keep optional tools available without making them required

Keep the two Excel actions separate:

- **Download rows needing attention** is the correction worklist. Preserve
  availability before comparison and while blocked. Its scope is all current
  saved findings across tables and pages, regardless of the visible filter.
- **Review workbook** records the full proposed load when its existing
  readiness conditions permit creation. Show its local job and recovery
  status beside this action.

Offer these actions in a consistent secondary **Downloads** area, with the
worklist also available beside findings. A failed count request must retain
the existing download behavior. Do not invent a failed or incomplete stage
because an optional workbook has not been created.

Keep Recipe saving in a separate optional reuse area. Follow its current
eligibility checks. Post-load continuation and expanded Recipe-saving behavior
belong to the separate
[continuation proposal](29-post-load-continuation-and-recipe-saving.md).

## Odoo-to-Odoo destination transfer

This branch already has Stage 5 transfer-order and transfer-review pages.
It performs local review work here; its fresh destination preflight belongs
to Stage 6. Reuse the presentation component, with labels that match the work.

| Proposed location | Existing responsibility retained |
| --- | --- |
| **5.1 Transfer order** | Build and inspect the existing transfer order, cycles, dependencies, and creation waves. |
| **5.2 Transfer contents** | Review existing record-type policies and build the exact review package. Inspect its records, selected fields, relationships, and reconciled totals. |
| **5.3 Approve transfer** | Review the package summary and retain the existing explicit exact-package approval, checkbox, and note. Continue to Stage 6 destination preflight when approved. |

This is a presentation split of current responsibilities. Preserve the
Stage 2 related-data constraints, Stage 4 destination matching, exact-package
binding, stale approval rejection, and separate Stage 6 confirmation.
Source-mode comparison against captured records also retains its own supported
outcomes; it must not acquire a create fallback or destination-transfer
permissions from this interface.

The source owners are
[`workspace_transfer_order.html`](../../src/impodo/web/templates/workspace_transfer_order.html),
[`workspace_transfer_review.html`](../../src/impodo/web/templates/workspace_transfer_review.html),
and [`transfer_review.py`](../../src/impodo/web/routers/transfer_review.py).

## Implementation and acceptance

Implement the shell and view selection first, then move existing panels into
their owning substeps. Reuse existing bounded presenters, services, and action
endpoints. Read only the active panel and necessary status summaries; do not
fetch complete record lists for hidden views or introduce a new database
connection cache. Measure any changed page-read scope under the existing
performance rules.

Update current workflow documentation and screenshots only with the verified
implementation. This plan alone changes no current workflow documentation,
runtime behavior, checks, or running session.

Acceptance must demonstrate the following:

- A data manager can identify the current substep and next action without
  scrolling on first entry, after refresh, and after returning from a detail.
- Preparation already approved in Stage 4 is not approved again in Stage 5.
  Direct entry from Stage 4 retains the current comparison action.
- Every count opens records that account for that same count. Global counts
  remain distinct from filtered counts and from finding counts.
- All existing ready, blocked, set-aside, no-write, and saved-load branches
  remain available under their existing checks. Clean comparisons retain
  their Stage 6 transition and Recipe no-change completion remains intact.
- Progress, interrupted attempts, old results, stale results, reconnect, retry,
  and uncertain save outcomes have clear and accurate parent context.
- Exact upstream correction and return preserve useful location without
  claiming outdated preparation or comparison is current.
- Existing group preview, acceptance, expected-versus-found reassessment,
  correction exports, and workbook behavior remain unchanged.
- Odoo-source transfer preserves its current order validation, review-package
  construction, approval, and subsequent destination preflight.
- Browser navigation and view selection make no unexpected Odoo requests and
  create no business decisions. Protected values remain protected.

Run focused route, presenter, and browser coverage for affected Authoring and
Recipe paths on Odoo 19 and Odoo 20, with local and remote access. Verify the
initial viewport, narrow layout, keyboard operation, Browser Back, direct
anchors, and saved scroll behavior. Run the complete repository gate before
delivery. Hosted checks remain subject to the maintainer's existing manual-only
policy and require an explicit dispatch request.
