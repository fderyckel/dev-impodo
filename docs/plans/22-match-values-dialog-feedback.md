# Make Stage 3 value matching responsive and clear

**Status** — Implemented in the Stage 3 dialog. The
[user workflow](../user/workflow/03-match-data.md) explains the controls, and
the [developer workflow](../developer/workflow/03-match-data.md) owns the
request and verification details. The proposal below records the design.

In **Match data**, a data manager opens **Match values** to link source values
to existing Odoo records, such as units of measure, payment terms, or product
categories. The dialog should immediately show whether Impodo is loading choices,
waiting for a decision, or ready for review.

The dialog currently shows a generic loading sentence and uses the same
request for source choices and Odoo choices. A request that stays pending has
no distinct slow or timeout state in the dialog. Refresh also clears the
visible table while it waits. The Tracking screenshot shows a separate problem.
Tracking is an Odoo selection field, and **Choose Source value first** is a
missing prerequisite. The shared form styles can also make the hidden company
label visible, even when the field has no company filter.

## Show one status beside the choices

Reserve one short status line beneath the field name, above the table. Keep
the line visible as the table scrolls. Replace the long success summary with
a match count and show a short warning only when it affects the next decision.

| What Impodo knows | What the data manager sees |
| --- | --- |
| The field needs a source column, a matching rule, or another prerequisite. | Show the next action beside the field before opening the dialog. For example, **Choose a source column first**. Use a link or button to focus that control. |
| The dialog is opening or the user requested a refresh. | Show a spinner and **Loading choices…** or **Refreshing Odoo values…** immediately. Keep existing rows and selections visible during refresh. |
| The request is still pending after a short wait. | Replace the line with **Still waiting for choices…** and keep **Close** available. |
| The choices are ready. | Show **2 of 2 matched**. Give a successful refresh a brief **Odoo values refreshed** confirmation in the same area, then show the count again. Keep the last check time in details. |
| The request failed or the browser stopped waiting. | Show **Couldn’t load choices** or **Loading timed out**, with **Try again**. If refresh failed and the earlier list is visible, say **Showing previous choices** in the same line. |
| The request succeeded with an empty result. | Show **No source values to match** or **No Odoo choices available**, depending on which list is empty. |

Use a polite live region and an accessible busy state. Disable conflicting
actions while a request is pending and keep the close action usable. Use a
bounded browser wait aligned with the endpoint's expected request duration.
The pending state reports that the browser is waiting for a response. It
cannot identify an internal Odoo phase or establish that the server stopped
after a timeout. Retry the original open or refresh action explicitly. Ignore
late responses after closing, reopening for another field, or retrying.

Preserve in-dialog selections through refresh, timeout, and read-key recovery.
When refreshed choices exclude a previously selected record, keep the selection
visible and mark the affected row **Needs review**. Keep technical causes in
support details. **Check matches** is a separate stage action and continues
to use its existing save and recovery flow.

## Show company where it helps identify a record

The dialog's company filter narrows the Odoo choices on screen. **Company for
this load** controls the operating company separately. The selected Odoo
record still carries its company when the matching rule uses one. For example,
**30 Days · Northwind** and **30 Days · Contoso** are two different payment
terms a data manager can distinguish before choosing.

Put a compact **Filter Odoo choices by company** control beside the table
heading only when the matching rule includes company and the returned choices
contain at least two named companies. Start with **All available choices**.
Keep company names visible on company-specific options even when the filter
is hidden. A filter change should narrow the available options and keep
existing selections visible. Enforce the hidden state in the rendered layout
for Tracking and unscoped linked fields.

## Verify before implementation handoff

- Exercise immediate, delayed, failed, timed-out, and empty responses in the
  dialog. Check that the status and close action stay visible at the reported
  viewport and at a narrower width. Cover keyboard use and status announcements.
- Refresh after making an unsaved match. Exercise failure, read-key recovery,
  and retry. Verify that selections survive, a missing saved choice gets
  flagged, and a late response cannot update a different dialog.
- Verify that Tracking and unscoped Unit and Product Category choices have no
  filter. Use a company-scoped Payment Terms fixture to verify option labels,
  filtering, and the saved name and company after reopening. Include a scoped
  list with just one named company.
- Exercise linked-field authoring with Odoo 19 and Odoo 20, using each version's
  existing reference policy. Preserve the request contract, portable choices,
  and existing match and evidence rules.

The current browser entry points are the
[shared dialog](../../src/impodo/web/templates/mapping/_value_match_dialog.html),
[request and selection handling](../../src/impodo/web/static/mapping-value-match.js),
and [company filter](../../src/impodo/web/static/mapping-value-match-company.js).
Extend the existing
[value-match visibility regression](../../tests/e2e/test_value_match_visibility.py).
