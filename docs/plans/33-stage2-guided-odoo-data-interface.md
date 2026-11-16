---
audience: developer
document_type: plan
status: implemented
---

# Give Stage 2 a clear sequence

This proposal helps a data manager see where they are in **Odoo data**, finish
one decision at a time, and find the next action. It reorganizes the existing
interface. It preserves the workflow, available features, saved decisions,
checks, and Odoo access rules. The maintainer approved implementation on
16 November 2026, with concise copy and the existing visual style.

The three substeps, grouped editors, and shorter copy are implemented. Current
instructions belong to the [Odoo data guide](../user/workflow/02-odoo-data.md).
The sections below retain the approved design. Browser visual verification
and replacement screenshots remain pending because the host blocked the
isolated preview. The existing running Impodo session has not been restarted.

## Recommendation

Use three named substeps for file-source Authoring:

1. **Choose records** identifies the Odoo business records in the migration.
2. **Related values** identifies where their linked values come from.
3. **Matching rules** identifies how Impodo finds an existing Odoo record.

Use the same nested sidebar navigation and focused content pattern as Stage 3.
Each substep acts as a tab: selecting it reveals its content, and the other
substeps stay available in the navigation. Keep the existing colours,
typography, buttons, cards, notices, and status treatments. Do not add another
six-stage progress bar or a second competing set of navigation controls.

Display **Step 2 of 3 · Related values** above the active content. This reports
the user's location. Saved evidence determines completion independently.

## What makes the current page difficult

The current template combines record selection, automatic supporting-data
information, relationship choices, Odoo refresh and recovery, field catalogues,
and matching-rule confirmation. A data manager must scroll past unrelated
sections to find the next decision. Selecting many record types makes both
the relationship review and matching-rule cards longer.

This assessment uses the current templates, routes, navigation, and workflow
guides. Existing documentation screenshots provide visual context; they are
not verification of a live workspace or of this proposed layout.

Saving record types already captures their Odoo fields when necessary. A
separate required **Load fields** tab would add an artificial stop. Keep that
capture attached to the existing save action.

## The three substeps

| Substep | Question for the data manager | Existing content placed here | Main action |
| --- | --- | --- | --- |
| **2.1 Choose records** | Which business records am I preparing? | Available record types, search, existing visibility filters, selection, saved read-key status, field-capture status, and discovery or capture recovery. | Keep **Save record types**. After a successful save and capture, open **Related values**, as the current file-source route already opens that review. |
| **2.2 Related values** | Where do the linked values come from? | Existing relationship choices, required and optional distinctions, incoming table selection, reuse choices, checked defaults, Odoo-managed choices, supporting-data preview, saved-choice status, and the existing value-check action. | Keep **Save choices** and its same-page save result. Offer **Continue to matching rules** as navigation when the existing prerequisites allow it. |
| **2.3 Matching rules** | How will Impodo recognize an existing record? | Suggested and confirmed rules, matching fields, all supported scope fields, combined matching, descriptions, field search and catalogues, validation errors, and the complete-set confirmation. | Keep **Confirm suggested matching rules** or **Confirm matching rules**, according to the current state. A successful confirmation continues to Stage 3. |

The related-value review must keep **Check existing Odoo values** available
beside the saved reuse choices. Keep its current availability and authority;
do not make it a new mandatory checkpoint. If no required relationship needs
a decision, show a neutral explanation and allow the existing continuation.
Optional choices must remain accessible without looking like unfinished
required work.

Keep **Odoo access** in its existing sidebar location. Stage 2 should show a
compact access summary and its existing recovery action when needed.
**Company for this load** remains in Stage 3.

## Reduce length inside each substep

Splitting the page alone will not solve a long list of matching-rule cards.
Within **Matching rules**, show the existing horizontal progress counts above
a compact list of the selected record types. Each item shows its saved or
suggested rule and status. Selecting an item reveals its existing editor and
field catalogue. This is a presentation of the current list, not a new
per-record approval process.

Use the same compact grouping for relationships when there are many selected
record types. Keep each group's unresolved required count visible while its
details are collapsed. Open the group containing a validation error and focus
the affected choice. Never collapse the only explanation of a blocker.

Preserve every matching-rule value in the complete confirmation form, including
rules whose editor is not currently visible. The existing confirmation still
confirms the complete set once. Do not submit or confirm only the visible
record type.

Keep available-field tables and technical support information behind their
existing disclosures. Show one primary action group at the end of the active
task; long content may use the existing action-footer treatment. Repeating an
action in that footer must invoke the same command and use the same readiness.

## Show progress honestly

| Display | Meaning |
| --- | --- |
| **Current** | This is the substep being viewed. It does not establish that its work is saved. |
| **Complete** | The current saved evidence satisfies the existing requirements for that responsibility. Suggested matching rules alone do not count as confirmed. |
| **Needs attention** | An existing check requires a correction. The substep names the issue and links to the exact affected control. |
| **Not yet available** | An existing prerequisite prevents the action. Explain that prerequisite beside its disabled control. |

Keep unsaved changes distinct from saved completion. Opening every tab must
not complete Stage 2. A successful field capture must not make the whole stage
look ready while required related choices or matching rules still need work.
Do not introduce persisted substep-completion flags or a new approval.

## Navigation, saves, and recovery

- On first entry, open **Choose records**. On an ordinary return, use the
  first existing requirement needing action, or the requested valid substep.
  Keep completed choices directly accessible for inspection and editing.
- Switching tabs in the open page preserves unsaved form controls without
  implicitly saving them. Existing save buttons remain explicit. Refresh
  continues to use the current saved-state and recovery contracts; this
  proposal does not add autosave or another draft store.
- After **Save choices**, retain the active substep, useful position, and save
  control focus. Validation failures reveal the exact affected group and keep
  the submitted choices. Continue links remain distinct from same-page saves.
- If field capture fails after record types are saved, open **Choose records**
  with the saved selection and **Try loading details again**. Do not ask the
  user to select the record types again.
- Keep **Check for Odoo changes** available with the saved Odoo-detail summary
  in **Choose records**. A pending change opens its existing review and
  confirmation there. Its explicit acceptance and invalidation rules remain
  unchanged. Return links identify the originating substep where applicable.
- An unchanged record selection or Odoo check retains the current dependent
  work. A genuine saved upstream change uses the existing invalidation rules;
  affected substeps must reflect those rules immediately.
- Browser Back, Forward, refresh, opening a substep, and opening help must not
  save scope, read Odoo, check existing values, or confirm matching rules.
- Keep current section links working. A link to a field or error reveals its
  owning panel before scrolling and focusing it. Explicit destinations and
  current validation errors take priority over unrelated remembered position.

Use ordinary navigation links with a current-page indicator, following Stage 3.
Keep keyboard focus visible and controls labelled. On narrow screens, retain
the same order in the existing responsive navigation. Ensure the current
substep and next action remain discoverable. Avoid nested scroll areas that
trap the operator between the page and a list.

## Preserve the other existing journeys

The same template serves several workflows. Give them consistent visual
grouping without imposing file-source Authoring's three decisions on them.

| Existing journey | Proposed presentation boundary |
| --- | --- |
| **Odoo-source capture** | Keep record selection and source-detail recovery together. Preserve the direct successful handoff to capture plans or the record-count check in Source data. Do not add a required related-value or destination matching-rule visit before that handoff. Existing source relationship and capture decisions keep their current owner. |
| **Recipe Test or Production setup** | Group the pinned requirements and the explicit Odoo check clearly. Keep the record types and Recipe rules read-only, the current company prerequisite, and the existing successful redirect. Do not introduce editable scope or Authoring confirmation. |
| **Recipe recovery** | Lead with the missing defaults and the existing corrective check. Keep one primary recovery action rather than making the operator traverse unrelated tabs. |
| **Recipe revision** | Preserve the existing advanced-check review or Match data handoff after the owning checks. |
| **Local Odoo and support fallback** | Keep reconnect actions beside their affected task. Retain the manual unverified field contract under **Support options**, its acknowledgement, and the live-check requirement before confirmation. |

Saved-data entry points and returns must retain their current destination and
read-only or editable authority. Tab navigation cannot widen it.

## Implementation boundaries

Keep the current commands and application services as the decision owners.
Add only a small presentation resolver for allowlisted substep locations and
derive navigation status from existing evidence. Preserve existing URLs and
anchors by resolving them to their owning panel.

The main implementation owners are:

- [Stage 2 template](../../src/impodo/web/templates/workspace_schema.html),
  which can be split into focused template partials.
- [Stage 2 presenter](../../src/impodo/web/presenters/schema.py),
  [navigation presenter](../../src/impodo/web/presenters/navigation.py), and
  [Stage 3 location resolver](../../src/impodo/web/presenters/mapping_journey.py).
- [Schema routes](../../src/impodo/web/routers/schema.py),
  [supporting-choice routes](../../src/impodo/web/routers/supporting_review.py),
  and [supporting review](../../src/impodo/web/templates/_supporting_data_review.html).
- [Stage 2 interactions](../../src/impodo/web/static/schema.js),
  [scope interactions](../../src/impodo/web/static/schema-scope-fields.js), and
  [existing styles](../../src/impodo/web/static/schema.css).

Keep one implementation for these responsibilities. Apply the layout to the
current saved format without introducing another storage format or a parallel
editor. Preserve Odoo 19 and Odoo 20 compatibility and versioned metadata.

Panel selection must not add Odoo requests, source-data scans, or a database
read per record type or relationship. Reuse the authorized page projection.
Keep complete local page and command work in the existing bounded synchronous
operations and close database owners before remote I/O. Bound physical opens,
schema inspections, and SQL for changed GET, POST, and recovery paths.
macOS checks do not establish a Windows speed improvement.

## Verification before implementation delivery

Run focused navigation and rendering checks for first entry, saved return,
direct anchors, invalid view parameters, Browser Back and Forward, refresh,
unsaved tab switches, same-page save, and validation followed by correction.
Check both a small example and many record types and relationships. Inspect
desktop and narrow layouts with fictional data, including keyboard operation.

Verify the saved business result: full scope, every relationship choice, every
matching rule, and scope-field order must agree with the current workflow.
Test unchanged saves, changed upstream choices, capture failure and retry,
pending Odoo changes, and rejected confirmation without losing other values.
Assert that passive navigation does not invoke Odoo or mutate evidence.

Cover both supported Odoo majors, local and remote access, file Authoring,
Odoo-source handoff, Recipe setup and recovery, and Recipe revision wherever
the shared rendering or redirects are affected. Reuse and extend the focused
[Stage 2 scope tests](../../tests/integration/web/test_stage2_scope_selection.py),
[supporting-review tests](../../tests/integration/web/test_supporting_review.py),
[capture-recovery tests](../../tests/integration/web/test_schema_capture_recovery.py),
[main-scope return tests](../../tests/integration/web/test_odoo_main_scope_return.py),
and [complete-route performance tests](../../tests/integration/web/test_stage235_performance.py).

Only the implementation should update the current user and developer guides,
workflow registry, affected code documentation, and current screenshots.
Keep this proposal under `docs/plans/` until then. Run focused behavioral
checks, documentation checks, Python quality after Python edits, and the
complete repository gate before delivery. Report omitted browser, platform,
and hosted checks explicitly.
