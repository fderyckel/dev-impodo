---
audience: developer
kind: runbook
status: current
---

# Scenario qualification runbook

## Current capability

`impodo-cli scenario` now provides a governed, profile-driven qualification
slice. It can:

- validate a versioned YAML definition and every contained fixture, profile,
  and target projection without reading a credential or contacting Odoo;
- run source preparation and first comparison against a saved snapshot or a
  live Odoo database on literal loopback;
- run an explicitly confirmed Contact file-to-local-Odoo write through the
  existing `ExecutionService`, scoped `Json2WriteExecutor`, and
  `ReconciliationService`;
- assert reviewed preparation, comparison, reconciliation, independent target
  projection, and repeat-comparison expectations; and
- retain the execution snapshot, journal, and reconciliation result in a
  private evidence directory while emitting a separate count-and-hash-only
  result.

This is not yet the whole browser journey. It does not create the normal
Project, Data version, Recipe application, or workspace records. It also does
not yet provision or reset Odoo, attest a seed fingerprint independently,
capture an Odoo source, run against remote Odoo, schedule a catalogue, or
drive a real browser. Those remain in the phased
[end-to-end scenario plan](../../plans/end-to-end-trial-and-scenario-qualification.md).

## Validate before access

The committed read-only canary can be validated without Odoo:

```powershell
impodo-cli scenario validate `
  --definition .\scenarios\contact-read-only\v1\scenario.yaml
```

Validation rejects unknown properties, embedded credentials, paths outside
the definition directory, symbolic links in fixture sets, missing artifacts,
unbounded Odoo capture plans, inconsistent expected totals, and write
expectations without complete reconciliation and an independent target
projection.

## Run the offline canary

```powershell
impodo-cli scenario run `
  --definition .\scenarios\contact-read-only\v1\scenario.yaml `
  --connector snapshot `
  --snapshot .\scenarios\contact-read-only\v1\target-snapshot.json `
  --output .\.tmp\scenario-results\contact-read-only.json
```

This uses the production profile compiler, source preparation, request
planner, snapshot adapter, and preflight engine. It performs zero writes. A
pass means the actual counts equal the reviewed scenario expectations; it is
not permission to load another database.

## Run the local Contact round trip

Prepare a fresh or otherwise known-empty Odoo 19 database whose name begins
with `impodo_scenario_`. Impodo does not erase, reset, or repair a target to
make the scenario pass. Put the API key in a private file outside the
repository and create a new private evidence directory for this attempt.

```powershell
impodo-cli scenario run `
  --definition .\scenarios\contact-round-trip\v1\scenario.yaml `
  --connector json2 `
  --base-url http://127.0.0.1:8069 `
  --database impodo_scenario_contact_001 `
  --api-key-file C:\private\impodo-scenario.key `
  --evidence-dir .\.tmp\scenario-evidence\contact-001 `
  --output .\.tmp\scenario-results\contact-001.json `
  --confirm-disposable-write contact-round-trip-v1
```

The live path accepts only a literal-loopback URL, a database in the
`impodo_scenario_` namespace, a write-capable definition, its exact scenario
ID as confirmation, and an evidence directory outside the immutable scenario
directory. The key value is never accepted as a command argument and is not
written to the result or ordinary output.

Immediately before the first journal entry, the runner captures Odoo again
and requires the reviewed comparison meaning to remain unchanged. The writer
then journals each transport batch before sending it. If a response is lost,
the compact result is `UNSAFE_TO_CONTINUE`; the journal remains, and another
run using that evidence directory cannot blindly issue the write again.

## Evidence and exit codes

The compact output contains scenario, fixture, expectation, target,
preflight, execution, and reconciliation hashes; expected and actual totals;
phase durations; the final status; and one controlled failure code. It does
not contain source values, target values, the URL, the key, or numeric Odoo
identifiers.

The evidence directory is different. `execution-snapshot.json`,
`execution-journal.json`, and `reconciliation.json` are protected operational
evidence and can contain business intent or numeric Odoo receipts. Restrict
access, retain it for recovery or diagnosis, and never publish it as ordinary
CI output.

| Code | Meaning |
| ---: | --- |
| `0` | Scenario passed, including an expected blocker when declared |
| `3` | Definition, confirmation, path, source, or target-policy input was invalid |
| `4` | A target read failed before the scenario orchestrator could publish a result |
| `7` | The scenario ran but needs attention or is unsafe to continue |

Do not delete an execution journal merely to retry. Inspect the journal and
target, then use the product recovery/read-back contract. Automated recovery
entry and target-provider cleanup remain planned work.
