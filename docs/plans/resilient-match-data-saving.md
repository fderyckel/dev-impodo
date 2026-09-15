# Qualify Match data saving on macOS

## Status and remaining scope

**Status:** macOS qualification remains open as of 2026-09-15.

Formula feedback, recoverable saves, mutation receipts, stale-tab handling,
catalogue scheduling, process recovery, diagnostics, and paired documentation
are implemented. Their current contracts and focused tests belong to the
[Match data developer workflow](../developer/workflow/03-match-data.md#operational-diagnostics-and-responsiveness).
The [user workflow](../user/workflow/03-match-data.md) explains the available
controls. Completed delivery records remain in Git history.

This plan retains the missing platform evidence. It does not reopen the
implemented design or change request timeouts without measurement.

## Remaining qualification

Repeat the separate-editor concurrency qualification on macOS using fictional
large catalogues and isolated workspaces:

1. Open concurrent catalogue reads from separate editor identities. Verify
   that the workspace projection gate prevents DuckDB attachment collisions
   and that obsolete searches cannot starve a save or health request.
2. Record cold and warm page time, search latency, mutation latency, event-loop
   delay, queue delay, and peak process memory in fresh runs. Compare them with
   the documented Windows fixture and its current budgets.
3. Exercise committed and uncommitted save timeouts, stale versions, interrupted
   requests, process loss, and restart. Resolve uncertain saves through their
   receipts without automatically repeating a mutation.
4. Verify that invalid formulas remain recoverable drafts and cannot pass
   **Check matches**, including when browser validation is bypassed.
5. Verify visible, keyboard-accessible failure messages and privacy-safe
   diagnostics. Bundles must exclude formulas, full forms, business values,
   credentials, tokens, and raw request details.

Use the current operation and connectivity model documented in the workflow.
The later [navigation and liveness qualification](responsive-workflow-liveness-and-navigation.md)
owns platform p95 measurements for shared pages; do not restore the retired
three-failure heartbeat behavior from the original save delivery.

## Completion evidence

Retain exact revisions, runtime versions, fixture sizes, raw repeat timings,
request counts, memory measurements, and restart outcomes under the testing
workflow. Run the focused mapping, catalogue, receipt, security, and launcher
checks linked from the developer page. Mark this plan complete only after the
macOS separate-editor and restart runs pass repeatedly, then remove it.
