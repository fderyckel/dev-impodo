# Practical delivery reset

## Decision

**Status:** Active delivery policy from 2026-08-06.

Impodo will now optimize for completing one useful, reversible migration
before expanding its general governance platform. Existing advanced
capabilities remain available, but they are optional unless the current
migration case demonstrates that they are needed.

The next product outcome is deliberately concrete:

> Move 100–300 representative contacts, product categories, and products into
> a disposable Odoo 19 database, review the exact proposed changes, load them
> once through the native Odoo API, and reconcile every row.

This outcome replaces clean-package certification and broad coverage closure
as the next delivery gate.

## Default product path

```text
register source and disposable Odoo target
-> map the fields and business identities
-> prepare and review the rows
-> read-only preview against Odoo
-> one explicit Load action
-> bounded native API writes
-> automatic read-back and fallout report
```

The ordinary path does not require a coverage-scope revision, reference
bundle, fuzzy-resolution policy, anomaly policy, second approver, signed
manifest, custom Odoo module, restore test, or manually reviewed hashes.
Those facilities are enabled only when the migration or target risk needs
them.

## Controls that remain mandatory

- Registered source bytes are never modified.
- Remote credentials are scoped and never written to portable evidence.
- Every write uses Odoo's ORM/API; there is no direct target SQL.
- The user sees a dry-run preview and explicitly chooses **Load**.
- Creates and updates use stable External IDs or an unambiguous business key.
- Writes use bounded batches and are not blindly retried after an uncertain
  response.
- Every input row ends as verified, failed with a reason, or outcome unknown.
- Read-back reconciliation checks the actual target result.

These controls are implemented automatically wherever possible. They are not
expanded into additional user approvals or lifecycle states unless a real
risk requires that ceremony.

## Optional capabilities

The following implemented capabilities are tools, not prerequisites:

- structural joins, unions, and aggregates;
- governed reference lists and additional domain checks;
- fuzzy candidate review, survivorship, and reviewer corrections;
- advanced anomaly policies and project coverage declarations;
- controlled-profile gateway, dual approval, rehearsal, restore testing, and
  signed execution grants.

A project that does not configure these capabilities follows the existing
canonical staging, quality, normalization, and preflight path unchanged.

## Implementation boundary

The active implementation is limited to:

- one company;
- contacts, product categories, and products;
- create and explicit update;
- simple many2one relationships;
- native Odoo 19 JSON-2;
- a disposable or neutralized local target;
- 100–300 representative rows;
- preview, load, journal, and read-back reconciliation.

Deletes, attachments, accounting entries, stock quantities, users, access
rules, document posting, workflow transitions, generic RPC, production
cutover, hosted workers, and a custom Odoo gateway are parked.

## Delivery rules

- A new abstraction must be required by the active migration fixture or by
  one of the mandatory controls above.
- Prefer one direct use case over a configurable framework.
- Reuse current contracts where they fit; do not redesign completed slices
  before the end-to-end path works.
- Tests should first prove the user-visible path and failure recovery. Add
  broader matrices only after a real case exposes the variation.
- Performance measurements are diagnostics. Optimize only when observed
  behavior prevents the active migration from completing acceptably.
- The coverage ledger records capability breadth but does not block this MVP.

## Short implementation sequence

### P1 — Freeze the practical input

**Status:** Completed 2026-08-06.

Adapt the already approved normalization output and read-only preflight
decisions into one internal execution snapshot. Generate it automatically
from current evidence; do not add a certification screen.

The read-only comparison now writes `impodo_execution_snapshot.json` beside
its technical manifest. The snapshot binds the submitted mapping, compiled
plan, staging, quality, frozen normalization, target fingerprint, protected
target snapshots, and preflight result. It accounts for every compared row and
stores explicit `OMIT`, `SET_NULL`, or `SET_VALUE` intentions only for rows
classified `CREATE` or `UPDATE`. Row hashes, a root hash, and a semantic hash
detect substitution or drift; proposed External IDs contain hashes rather
than business values. Loading the current snapshot rechecks it against the
current report. This is automatic internal evidence, not a new user approval.

### P2 — Preview and load a disposable target

**Status:** Completed 2026-08-06.

The local browser now shows one business-language preview and one explicit
**Load into Odoo** action. A separate JSON-2 writer permits only contacts,
product categories, products, exact business-key lookups, bounded list-create
batches, and one-record updates. It resolves simple many2one dependencies in
dataset order and exposes no generic method, delete, direct SQL, `sudo`, or
caller-controlled context.

Every proposed create/update row is journaled before target I/O and ends as
committed, failed, blocked, or outcome unknown. A lost write response stops the
run without retrying. The browser shows the saved API outcome honestly as a
receipt, not as read-back verification. P2 adds no 60-second or 512-MiB release
gate; runtime and memory remain diagnostics.

### P3 — Reconcile and recover

**Status:** Completed 2026-08-06.

Read the written rows back by External ID or governed business key. Produce a
plain result page and downloadable fallout list. On uncertain responses,
re-match before deciding whether a retry is safe.

The practical browser now verifies committed rows by their journaled Odoo ID
and re-matches uncertain responses with the exact governed business key. The
result is hash-bound to the execution snapshot, target, and completed load run
and is published atomically with one current pointer. The load page reports
verified rows, fallout, unknown outcomes, differing field names, and a concise
recovery action without storing source values or credentials in the report.
Fallout is downloadable as CSV. An uncertain create is marked safe to plan
again only when an exact read-back finds no matching record; Impodo never
automatically replays the write. Verification runs automatically after a load
and can be retried manually if the read connection itself failed.

### P4 — Run the representative migration

**Status:** Next.

Load and review 100–300 sanitized records in a disposable Odoo 19 database.
Fix the concrete product problems found during that run. Do not expand scope
until every row has a clear outcome and a second run is idempotent.

## Definition of done

The practical milestone is complete when a non-technical data manager can:

1. prepare the representative source without configuring advanced governance;
2. understand the preview in business language;
3. explicitly load it into a disposable Odoo target;
4. see every row reconciled or listed with a useful recovery action; and
5. repeat the same migration without creating duplicates.

Production execution remains a later, risk-classified step. The practical
milestone proves that Impodo can complete a migration; it does not claim that
every migration shape or production control is already supported.
