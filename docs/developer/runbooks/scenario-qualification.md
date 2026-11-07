---
audience: developer
kind: runbook
status: current
---

# Scenario qualification runbook

## Profile-driven scenario CLI

`impodo-cli scenario` now provides a governed, profile-driven qualification
slice. It can:

- validate a versioned YAML definition and every contained fixture, profile,
  and target projection without reading a credential or contacting Odoo;
- run source preparation and first comparison against a saved snapshot or a
  live local or remote Odoo database;
- combine reviewed fixture columns into one normalized, distinct source
  lookup dataset before mapping;
- run explicitly confirmed Contact, Product, and bill-of-material
  file-to-local-Odoo writes through the
  existing `ExecutionService`, scoped `Json2WriteExecutor`, and
  `ReconciliationService`;
- assert reviewed preparation, comparison, reconciliation, independent target
  projection, and repeat-comparison expectations; and
- retain the execution snapshot, journal, and reconciliation result in a
  private evidence directory while emitting a separate count-and-hash-only
  result.

Remote HTTPS execution is implemented and requires the definition to pin the
exact non-secret target identity hash. The committed edu-ucaps definitions
have passed definition validation but do not yet have a retained live result.

This is not yet the whole browser journey. It does not create the normal
Project, Data version, Recipe application, or workspace records. It also does
not yet provision or reset Odoo, attest a seed fingerprint independently,
capture an Odoo source, schedule a catalogue, clean a remote target
automatically, or drive a real browser. Those remain in the phased
[end-to-end scenario plan](../../plans/08-end-to-end-trial-and-scenario-qualification.md).

## Shared application practice

The [Morrow practice index](../../../scenarios/impodo-practice/README.md)
selects the canonical customer or manufacturing component. These runners use
normal Project, Data version, exact combination, workspace, Recipe,
preparation, comparison, journalled load, reopening and reconciliation services
through an isolated in-process application. They do not add implicit joins to
the profile CLI.

Use `python -m scripts.qualify_practice_customers --help` for customer
combination, staged NET45 schedules and a new incoming immediate term used by
an existing and a new customer. Use
`python -m scripts.qualify_practice_manufacturing --help` for supplied
categories, combined products, explicit product-status stages, separate BoM
headers, component lines and operations. The checkout's `test` extra is
required. The component READMEs own source choices, seed stages and counts.

The manufacturing inputs include a physical six-row header export. Repeated
header cells in the component export remain source evidence; the runner does
not derive header attributes through a related split. Products initially use
Odoo's active default and then receive their source status in separate
company-filtered phases. Preserve the final inactive values and generated
variant references. The existing native Services category is a verified
unowned reference and must remain outside fixture cleanup ownership.

Controlled runs replace only Odoo and exercise the selected LOCAL or REMOTE
contracts. Live runs require an allocated fresh disposable target, a private
descriptor and explicit `--allow-write` before credential access. Customer
runs use one fresh target per exercise and entrypoint; manufacturing runs use
one fresh target for their ordered component journey. Both capture actual
metadata and verify the final major before loading. They retain the target
and private application evidence without starting a service or browser.

For a manual customer baseline, use
`python -m scripts.prepare_practice_target --help`. Manufacturing uses
`python -m scripts.prepare_practice_manufacturing_target --help`: run
`--stage initial`, load the twelve category paths through Impodo, then run
`--stage existing-products` with the saved private `--seed-directory`.
The second setup step validates the target, principal, source binding and
owned ledger before creating the eight initial products. Continue with both
product partitions, both final status phases and both company BoM phases.
These setup commands load no migration and start no service or browser.
The qualification commands do not implement automatic cleanup or
interrupted-write recovery. Inspect retained evidence after a failure and
allocate a fresh target for another attempt.

Read each component's dated receipts for the exact observed Odoo 19/20
release, source/oracle/seed hashes, entrypoint and modes that ran. Controlled
REMOTE is not live HTTPS evidence. The component qualification records all
eight controlled combinations of two majors, two access modes and two
entrypoints separately from four live LOCAL authoring/Recipe journeys across
the two majors. Live REMOTE HTTPS and visual browser checks were not run.
Linux/Windows execution and paused hosted checks must be reported separately.
Native Excel, scale, fault injection and related-split provider cases retain
their own owners; a valid baseline does not qualify those failure paths.

## Maintain a practice case

Use this procedure when a confirmed failure or a new use case belongs in the
shared practice world. The optional `$impodo-practice-maintenance` skill can
guide the work when installed; portable data, commands and tests must not
depend on that local installation.

1. Record the observed trigger, source grain, saved choices, failing boundary
   and safe outcome. Review the relevant history and tests. Keep a suspected
   cause separate from a confirmed reproduction and omit private data.
2. Reuse canonical synthetic keys or add the smallest named source variation.
   Declare initial and intermediate target states, permitted writes, exact
   final business values and dependencies before running the implementation.
   Preserve accepted baseline bytes and existing receipts.
3. Add an actual executable consumer or regression at the failed boundary.
   Demonstrate the original failure, the corrected result and the relevant
   safe-stop behavior through normal services. A description or fabricated
   prepared snapshot cannot establish the application path.
4. Give learners the same files, starting state and decisions. Explain the
   expected rows and values, the review action after an error, and the scope
   of any required target setup.
5. Refresh the owning hashes and retain receipts for each affected major,
   access mode and authoring/Recipe entrypoint. Separate controlled, live and
   visual evidence; record omitted checks and keep operational evidence
   private. Later metadata edits require qualification of that later candidate.
6. Run focused regressions, documentation checks and the complete repository
   gate required by `AGENTS.md`. A failed guardrail blocks delivery unless
   explicitly waived for that delivery. Paused hosted workflows stay paused.

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

## Prepare the PLW manufacturing demo set

The PLW preparation scenario reads the four pinned demo workbooks and checks
the reviewed field rules. It prepares 2,841 Products, 1,942 bill-of-material
headers, 15,142 bill-of-material lines, and 17,328 operations. The expected
result is 37,253 prepared rows with zero source issues.
Two piece-count lines have `BOMQtySerie = 0`. The reviewed demo rule treats
those rows as a quantity per one unit, so their quantity transformation can
complete. That fallback belongs to this demo rule; it does not establish a
general interpretation of zero series quantities in source BOMs.

```powershell
impodo-cli scenario run `
  --definition .\scenarios\plw-manufacturing-preparation\v1\scenario.yaml `
  --connector none `
  --output .\.tmp\scenario-results\plw-manufacturing-preparation.json
```

`--connector none` is accepted only when a scenario stops after preparation.
This run does not contact Odoo or authorize a write. The paired integration
test also checks that every operation belongs to a selected bill of material,
every line's Product exists in the fixture, and each item keeps one demo Work
Center code. A pass confirms source preparation and those file relationships;
it does not establish Odoo matches, Work Center availability, or load safety.

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

## Run the local Product and bill-of-material round trip

Use a disposable Odoo 19 database with Manufacturing installed. The first
scenario derives the `uoms` dataset from the Unit columns in the Product and
bill-of-material fixtures. It creates `PCE` through the normal execution path
when the target does not contain it and reuses the standard `g` Unit.

```powershell
impodo-cli scenario run `
  --definition .\scenarios\product-bom-headers-round-trip\v1\scenario.yaml `
  --connector json2 `
  --base-url http://127.0.0.1:8069 `
  --database impodo_scenario_product_bom_001 `
  --api-key-file C:\private\impodo-scenario.key `
  --evidence-dir .\.tmp\scenario-evidence\product-bom-headers-001 `
  --output .\.tmp\scenario-results\product-bom-headers-001.json `
  --confirm-disposable-write product-bom-headers-round-trip-v1

impodo-cli scenario run `
  --definition .\scenarios\bom-lines-round-trip\v1\scenario.yaml `
  --connector json2 `
  --base-url http://127.0.0.1:8069 `
  --database impodo_scenario_product_bom_001 `
  --api-key-file C:\private\impodo-scenario.key `
  --evidence-dir .\.tmp\scenario-evidence\bom-lines-001 `
  --output .\.tmp\scenario-results\bom-lines-001.json `
  --confirm-disposable-write bom-lines-round-trip-v1
```

The two stages are intentional. Odoo creates each `product.product` variant
from its `product.template`; the second scenario then resolves those variants
by business key. Writer identity rechecks use `active_test=False`, so an
inactive bill of material remains resolvable during execution.

## Remote target boundary

A remote scenario uses the same command with an HTTPS URL and its actual Odoo
database name. `destination.expected_target_hash` must match that exact mode,
URL, and database. The ordinary compact result still omits the URL, database
identity details, API key, and Odoo record IDs.

The current runner does not remove remote records. Before a live remote trial,
prepare a reviewed cleanup operation that is limited to IDs created in the two
execution journals. Delete child bill-of-material lines before headers and
Products, then verify that no scenario business key or generated External ID
remains. Do not delete an existing `PCE` or `g` Unit that compared as
unchanged.

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
