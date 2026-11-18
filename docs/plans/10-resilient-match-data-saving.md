# Qualify Match data saving on macOS

## Verified on 18 November 2026

Six fresh processes exercised the 1,000-field fixture with two authenticated
editors sharing a workspace. Two obsolete searches yielded HTTP 204. Both
current searches completed, one save advanced the draft once, the stale save
received HTTP 409, and the health request answered during the search burst.
The [raw macOS samples](../developer/reference/mapping-save-macos.json)
include the application build, runtime, request timings, event-loop delay,
queue time and process peak memory.

Three additional trials killed the application process before receipt
reservation, after reservation, and after commit. A replacement process read
the original operation with the same session in each case. The recovered
states were absent, pending and committed respectively. The working draft
advanced only in the committed case.

The [Match data workflow](../developer/workflow/03-match-data.md#operational-diagnostics-and-responsiveness)
owns save recovery. The editor waits for a late receipt and an explicit
retry keeps the same operation and original edits.
The [shared liveness plan](09-responsive-workflow-liveness-and-navigation.md)
owns navigation, health budgets across pages and the connectivity indicator.

## Remaining macOS checks

Run the following on a fixed checkout with Python 3.12, the test dependencies
and a matching Playwright Chromium installation. Use the ordinary user
account and isolated fictional workspaces.

1. Run `python -m pytest -q tests/e2e/test_mapping_save_keyboard.py`.
   Verify that the visible alert receives focus, both recovery buttons work
   by keyboard, the explicit retry saves the draft once, and a stale editor
   can reload the newer draft.
2. Run `python -m pytest -q tests/integration/web/test_diagnostics.py::ServerSupervisorTests::test_spawned_server_reuses_listener_and_session_after_restart`
   in three fresh processes. Verify that the second server accepts the same
   session on the original loopback port. Run
   `python -m pytest -q tests/integration/web/test_diagnostics.py::LauncherDiagnosticTests`
   there as well. Both launcher logging tests bind a loopback socket before
   checking the normal and exceptional shutdown records.

The managed test session denied Chromium's macOS Mach registration and
binding a loopback listener. Retain the revision, runtime and outcomes from
a macOS session that permits these operations. Remove this plan after both
checks pass and the complete repository gate succeeds.
