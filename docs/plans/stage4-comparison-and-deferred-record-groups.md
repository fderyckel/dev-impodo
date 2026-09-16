# Review problem record groups without repeated Odoo reads

## Status and goal

**Status:** Proposed, revised 2026-09-16. The Stage 4 comparison entry point
and reviewed set-aside action described here are not current behavior.

A data manager should be able to discover missing or ambiguous Odoo references
before leaving Prepare data, set aside complete affected record groups, and
load the safe remainder. A review workbook should retain every omitted record
and its reason for later correction. The design should make the check earlier
without adding a second Odoo scan to a normal run.

## Use one comparison from either stage

The current Prepare data calculation is target-independent and checks source
values and incoming relationships. It cannot prove that an `origin='target'`
product key exists in Odoo. The current Final review comparison performs the
authorized, batched Odoo read and saves a report, an execution snapshot, and a
compact summary. That explains why the earlier target-product examples became
visible only at Stage 5.

After Stage 4 has frozen its resolved and normalized prepared values, show the
existing **Compare with Odoo** action there as well. It starts the same
`PreflightService.compare` job and uses its current progress and publication
flow. It starts only when the manager selects the action in Stage 4 or Stage 5.
If a current comparison already exists, show **Open current comparison**
instead of starting another job. It does not run on each source or mapping
edit or when the manager changes pages. If Odoo is unavailable, Prepare data
remains complete and clearly says **Odoo references not checked yet**.

Stage 4 uses the already saved compact summary to show ready and problem
counts. **View problems** opens a bounded page of the existing readiness rows;
the group preview is calculated only when the manager asks for it. Stage 5
opens the same saved comparison for full review. Opening either page, changing
filters, or downloading the workbook does not start another Odoo read. A
changed source, mapping, prepared result, or target binding invalidates that
comparison under the existing
rules. Any freshness check already required before load still applies. When a
new comparison is required, the manager sees why and starts one new job.

This places discovery in Stage 4 without adding a separate target-reference
checker, a second target snapshot, or another read during normal Stage 4 to
Stage 5 navigation. Source-only errors continue to be found by ordinary
preparation. Target-only and Odoo-first relationship failures become visible
as soon as the manager runs the shared comparison.

## Set aside complete groups from saved evidence

The comparison should group isolatable issues by root cause. For each issue,
the preview calculates which parent records, dependent records, and sibling
lines must be set aside together. It displays counts by table and the number
of writes that would remain. For example, one missing component product may
require an entire bill of material and its lines to be omitted. The number of
records omitted can exceed the number of reported errors.

The manager selects **Set aside affected groups for this load** once for the
chosen groups. Impodo records a compact decision bound to the saved comparison,
then derives the reduced report and execution snapshot from the saved prepared
and comparison evidence. Dependency closure, count reconciliation, and numeric
precision validation use local evidence. This action must not make an Odoo
request, run preparation again, or reopen every source row in the browser.
If removing a group would change how a surviving relationship resolves, Impodo
must extend the group or keep the reduced scope blocked. It must never silently
choose a different Odoo record.

Stage 5 shows **Ready with records set aside** only when every remaining write
is safe and at least one write remains. The load page shows exact included and
omitted counts before the existing explicit load action. A stale comparison,
unavailable target, incomplete evidence, invalid mapping, or other run-wide
failure still blocks the load. Missing references and ambiguous matches never
become guesses or silent exclusions.

The existing `TARGET_NUMERIC_PRECISION_LOSS` check should report affected
write records during the same comparison and snapshot-building work, before
the load page. It should use the same captured field digits and exact Decimal
rules. Those records may be considered for group set-aside; Impodo never rounds
or changes quantities implicitly. A Stage 4 check on all prepared values could
overstate the problem because unchanged values are not writes, so the issue
must be based on the intended write set.

## Workbook for later correction

Generate the workbook from the saved full comparison and reviewed reduced
scope. **Review overview** shows the counts prepared, already quarantined,
newly set aside, ready to write, and still blocked. **Deferred issues** has
one row per omitted prepared record with its source table and row, portable
identity, bill of material group, direct or inherited reason, root issue,
suggested correction, and comparison identifier. A record with several
reasons is counted once. Filters and a root-cause summary help the manager
work on a few causes without scrolling through dependent lines.

The workbook is immutable evidence of this run. Editing it does not change
the load. The manager can later correct the source or Match data rule and run
a fresh preparation and comparison. The historical workbook remains
available; no separate project issue database is needed for this first
version. Keep the existing portable-value protections, spreadsheet formula
escaping, artifact integrity checks, and exact source-row accounting.

## Performance and safety acceptance

- A normal run from Prepare data through Final review makes no more Odoo
  metadata or record requests than today's single Final review comparison.
  The comparison is a single active job per workspace and retains batched,
  bounded requests rather than one request per source row.
- Opening Stage 4 or Stage 5, viewing issue pages, accepting set-aside groups,
  and downloading the workbook make zero additional Odoo requests. The compact
  Stage 4 summary uses the existing saved projection. Detailed rows are
  fetched in bounded pages only when opened.
- Group calculation and reduced-scope publication use the existing prepared
  dependency facts and saved comparison decisions. Calculate closure only
  when isolatable issues exist and the manager opens the group preview. Reuse
  that bounded result while the comparison is current; do not recalculate it
  on each page view or build the workbook merely to display a page. If saved
  evidence cannot prove a safe reduced scope, require one explicit fresh
  comparison instead of an implicit retry.
- A changed binding, missing artifact, incomplete source accounting, or target
  freshness failure cannot reuse the old comparison or scope decision. Keep
  the current target and load safety checks; moving the comparison button does
  not relax them.
- A missing incoming-only parent is found during pure preparation. A missing
  target-only or Odoo-first product is found by the shared comparison launched
  from Stage 4. A single problematic bill of material group can be deferred
  while an unrelated group remains loadable, and every omitted prepared record
  appears in the workbook with a direct or inherited reason.
- Measure the full 25,000-record relationship route on Windows. Report Odoo
  request counts, comparison time, group-preview time, page-response time,
  memory, and workbook time separately. Accept the change only if the normal
  Stage 4 to Stage 5 path uses one comparison and navigation remains within
  the existing responsiveness target.

Implement this in two slices. First, expose the current comparison job and
saved compact result at the end of Stage 4, proving unchanged Odoo request
counts and navigation responsiveness. Second, add the local group preview,
reviewed scope decision, reduced snapshot, early precision findings, and
workbook details. Update the paired Prepare data and Final review workflows,
the [quality contract](../developer/contracts/quality-and-quarantine.md),
[preflight contract](../developer/contracts/preflight.md),
[execution contract](../developer/contracts/execution-and-reconciliation.md),
workflow registry, and authenticated screenshots after implementation.
