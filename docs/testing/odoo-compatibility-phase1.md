---
audience: developer
kind: report
status: current
---

# Odoo dual support: Phase 1 baseline

## Result and scope

Phase 1 completed on 2026-09-15. Impodo has a recorded Odoo 19 baseline,
regression fixtures for its policy and saved-evidence identities, an inventory
of version decisions and serialized contracts, and two isolated local test
databases. The development database is pinned to an exact upstream commit
for investigating Odoo 20.

Production integration behavior was not changed. Transfers and Recipes remain
subject to the existing Odoo 19 gates. The implementation target remains
same-major support only: 19 to 19 or 20 to 20. Cross-version migration and
Recipe conversion are deferred.

The tests used the working tree based on Impodo commit
`1e03a8c0524e803a90ceadc25cfdac9d6cb9f030`. Unrelated edits were already
present and were preserved. These results establish a development baseline;
they are not a qualification of a clean release artifact.

The [machine-readable evidence](odoo-compatibility-phase1.json) records the
source commits, complete installed-module versions, selected model field
counts, metadata hashes, scenario counts, timings, and result hashes. It
contains no API key or customer data.

## Observed environments

Both environments use Windows, Python 3.12.10, PostgreSQL 17.6, and Community
Odoo. Each has its own Python environment, PostgreSQL cluster, database,
filestore, configuration, and logs under `.tmp/odoo-compatibility/`.

| Property | Odoo 19 baseline | Odoo 20 investigation |
| --- | --- | --- |
| Upstream commit | `157874aad3aebef5bc9268de6e17530641107e31` | `f582f290ae4e775d0f4fe477ab9d5131f9e48598` |
| Version observed from shell and HTTP | `19.0` | `19.5a1` |
| HTTP listener | `127.0.0.1:18019` | `127.0.0.1:18020` |
| PostgreSQL listener | `127.0.0.1:55419` | `127.0.0.1:55420` |
| Database | `impodo_scenario_v19_baseline` | `impodo_scenario_v20_preview` |
| Requested modules | Base, Contacts, Product, Manufacturing | Base, Contacts, Product, Manufacturing |
| Result | Three live Impodo scenarios passed. | Database initialized; version and metadata observed. |
| Services at completion | Stopped. | Stopped. |

The baseline's principal module versions are `base=19.0.1.3`,
`contacts=19.0.1.0`, `product=19.0.1.2`, and `mrp=19.0.2.0`. The preview's
corresponding versions start with `19.5`. Installed dependencies are listed
in the evidence JSON.

The preview is not final Odoo 20. The exact checked-out
[upstream version definition](https://github.com/odoo/odoo/blob/f582f290ae4e775d0f4fe477ab9d5131f9e48598/odoo/release.py)
declares `19.5a1`. No Impodo preparation or write scenario was run against it.
Replace the investigation pin through review when final Odoo 20 becomes
available, then run the qualification in the support plan.

## Fresh Odoo 19 results

The three committed scenarios ran against the new disposable baseline
database through Impodo's JSON-2 adapters, execution service, journal, and
reconciliation service. The Product scenario ran before the BOM-line scenario
so generated variants existed for relationship resolution.

| Scenario | First creates | First unchanged | Committed writes | Verified rows | Repeat unchanged |
| --- | ---: | ---: | ---: | ---: | ---: |
| Contact | 1 | 0 | 1 | 1 | 1 |
| Products and BOM headers | 9 | 1 | 9 | 10 | 10 |
| BOM lines | 6 | 0 | 6 | 6 | 6 |

All scenarios passed with zero failures, unknown outcomes, partial outcomes,
reconciliation fallout, or differences in the independent target projection.
These live scenarios exercise creation, generated variants, relationship
resolution, read-back, and repeat comparison. They do not establish fresh
live update, failure-recovery, Production, Enterprise, or remote HTTPS
qualification; those remain explicit later gates.

The existing September 4 Contact, Product, and BOM results were also found
in `.tmp/scenario-results/`. They report Odoo 19.0, but do not record the
exact upstream commit. They were used as historical context and were not
substituted for the fresh results above.

Private local artifacts for this run are retained under:

- `.tmp/odoo-compatibility/baseline/` contains each compact scenario result,
  its `evidence/` execution journals, `observed.json`, and server logs.
- `.tmp/odoo-compatibility/preview/` contains `observed.json` and
  `http-version.json`, plus preparation logs and its prepared database.
- Each directory contains `prepared.json` and `resolved-requirements.txt`.
  The baseline API key was generated only for this disposable database and
  expires after one day. Its local file is not part of the committed evidence.

The baseline database now contains the scenario's fictional records. Preserve
the journals. Do not rerun an empty-target scenario against it as though it
were fresh.

## Version-decision inventory for Phase 2

The following are executable decisions, not just text mentioning Odoo 19.
Use this inventory when replacing scattered checks with one shared policy.

| Current owner | Decision that Phase 2 must preserve or explicitly replace |
| --- | --- |
| [Remote connection status](../../src/impodo/web/remote_connection.py) | `mark_checked` requires a version starting with `19.`. |
| [Local readiness](../../src/impodo/adapters/odoo/local_stack.py) | `_probe_odoo` requires `19.`; startup also interprets an “Expected Odoo 19” message prefix. |
| [Local metadata](../../src/impodo/adapters/odoo/local_reader.py) | `_fingerprint` rejects versions outside `19.`. |
| [Schema capture](../../src/impodo/application/schema_workspace_service.py) | Model discovery, supporting-model capture, and schema validation each require `19.`. Refresh also compares exact reported versions. |
| [Source capture](../../src/impodo/application/odoo_source_capture_service.py) | The live capture identity check requires `19.`. |
| [Destination matching](../../src/impodo/application/destination_matching_service.py) | The destination snapshot must report `19.`. |
| [Workspace destination state](../../src/impodo/domain/workspace/workbench.py) | Destination readiness and successful verification both require `19.`. |
| [Odoo comparison](../../src/impodo/application/odoo_comparison_service.py) | Current live fingerprint validation requires `19.`. |
| [Preflight](../../src/impodo/application/preflight_service.py) | Execution-shape readiness requires `19.`; other comparisons retain exact evidence equality. |
| [Execution](../../src/impodo/application/workspace/execution/service.py) | `_execution_snapshot_error` requires `19.` before a load can proceed. |
| [Run target evidence](../../src/impodo/application/run/target_evidence.py) | `RunTargetEvidenceUseCase.read` parses and requires major 19 with live schema evidence. |
| [Production review](../../src/impodo/application/run/production_review.py) | `ProductionRunReviewUseCase.review` requires major 19 and a live capture. |
| [Reader composition](../../src/impodo/web/composition/target_readers.py) | Recipe-run requirements require major 19; reference consumers also parse the version. |
| [Recipe publication](../../src/impodo/application/recipe_compilation_service.py) | The compiler extracts a leading major and stores it in `odoo_target_contract`. |
| [Recipe application](../../src/impodo/application/recipe_application_compilation.py) | `_target_assessment` parses the target major and blocks a different Recipe major. |
| [Reference policy](../../src/impodo/domain/workspace/reference_keys.py) | Standard reference entries and the request default use major 19; authorization compares majors. |
| [Source policy](../../src/impodo/domain/odoo_source_policy.py) | The canonical capture policy specifies major 19. |

Additional parsing consumers include
[relationship validation](../../src/impodo/domain/mapping/validation/relationships.py).
Exact-version evidence comparisons also occur in
[matching order](../../src/impodo/application/workspace/mapping/order_service.py).
Do not replace an exact freshness comparison with a simple “supported major”
check; they serve different purposes.

Two findings need explicit treatment in Phase 2:

1. A `19.` prefix also accepts the observed development string `19.5a1`.
   The current string check does not establish that a build is stable or
   qualified. The new policy needs an explicit development-build disposition.
2. Independently allowing 19 and 20 at each endpoint would allow a
   cross-version pair unless the transfer path also checks source major
   against destination major. Keep both same-major transfer guards and the
   existing single-major Recipe contract in the implementation scope.

UI text and documentation are a separate inventory. In particular,
`scripts/documentation_quality.py` requires the “Odoo 19 and performance”
heading. Change that requirement and all registered pages together when the
new behavior ships. Phase 1 does not relabel the current product as supporting
Odoo 20.

## Serialized-evidence inventory

| Artifact or contract | Current identity | Compatibility work to plan |
| --- | --- | --- |
| `TargetFingerprint` and connection identity | The fingerprint contains reported version and module versions. The connection hash contains mode, URL, and database. | Preserve connection hashes. Treat version and schema freshness separately. |
| Metadata and record snapshots | Canonical snapshot content hashes include the fingerprint. | Preserve historical parsing and hashes. A changed version produces different snapshot evidence. |
| `OdooModelCatalog` and `OdooSchemaCatalog` | Workspace evidence identity contract 2; stored version, policy hash, principal, context, and target evidence. | Plan additive compatibility evidence without rewriting historical captures. |
| Governed-reference policy | Policy version 2; one global hash. | Select policies by major while keeping the Odoo 19 hash stable. |
| Odoo source policy and capture selection | Source policy version 3; capture contract 4. Capture selection currently compares against the global policy hash. | Audit historical selection decoding before adding a second policy. |
| Published Recipe envelope | Envelope 2, target contract 2, current mapping Recipe contract 3. | Retain one target major per Recipe. Keep format-version checks distinct from Odoo-version checks. |
| Mapping | Contract 17; supported historical contracts 12 through 16. | Preserve mapping meaning and schema bindings. |
| Transfer order, review, approval, and preflight | Contract 1 for each; source and destination evidence is bound through hashes. | Identify where an explicit same-major comparison and new compatibility evidence enter these hashes. |
| Execution snapshot | Contract 8; target version, target identity, preflight evidence, and write schedule. | Revalidate before writes and recovery while preserving existing journals. |
| CutoverPlan and qualification | Plan, application qualification, and Integrated Test qualification contract 1. | Keep qualification bound to the intended Odoo major. |
| Production binding | Contract 1. | Preserve activation evidence and require qualification on the Production major. |
| Storage | Workspace engine schema 15; registry schema 7; migration workspace store 2. | Plan explicit forward upgrades only when later slices introduce persisted fields. |

Primary sources for this inventory are
[shared models](../../src/impodo/domain/shared/models.py),
[snapshot serialization](../../src/impodo/domain/odoo/contracts.py),
[workspace evidence](../../src/impodo/domain/workspace/contracts.py),
[capture selection](../../src/impodo/domain/odoo_capture.py),
[Recipe envelope](../../src/impodo/domain/recipe_envelope.py),
[execution snapshot](../../src/impodo/domain/execution_snapshot.py), and
[Production binding](../../src/impodo/domain/run/production.py).

The existing `fixtures/migration-projects/current-contract/customer-recipe-v1.json`
still declares mapping Recipe contract 2; the runtime validator now requires
3. Do not interpret that older fixture as proof of current Recipe acceptance
or silently relabel its Odoo version. The new fixtures in
`fixtures/odoo-compatibility/` are explicitly synthetic regression evidence.

## Preserved identities

The new baseline records these existing identities:

- Odoo source policy:
  `sha256:403be4a671a9e2a25ddee994ff0c337e0b271438a7ae47a60d68f5be3572ac01`.
- Governed-reference policy:
  `sha256:462e13bfba360a4c4ddc1124a8ed3426fad99755bb6d62f98305d6ecf1fe5a11`.

[Domain baseline tests](../../tests/domain/test_odoo_compatibility_baseline.py)
also restore and reserialize a fixed fictional Odoo 19 record snapshot,
preserving its bytes, false value, version, and content hash. They demonstrate
that changing the reported version changes snapshot evidence without changing
the configured connection's identity. Git attributes keep these fixture bytes
stable across operating systems.

[Recipe boundary tests](../../tests/application/test_recipe_odoo_version_baseline.py)
preserve current same-major Recipe assessment and verify rejection of both
cross-major directions even with an identical field projection. They do not
enable Odoo 20 elsewhere in the product.

## Reproduce the local setup

The checked-in [target manifest](../../fixtures/odoo-compatibility/lab-targets.json)
pins both Odoo commits and distinct database names and ports. Separate
Windows/Python 3.12 dependency files record the resolved package versions.
The preparation script installs those versions together with the checked-out
Odoo requirements. These files pin dependency versions; they are not a
release supply-chain attestation with hashes for every downloaded wheel.

Prepare a clean checkout at the manifest's exact commit, then run:

```powershell
.\.venv312\Scripts\python.exe scripts/prepare_odoo_compatibility_lab.py `
  --target preview `
  --source .tmp/odoo-compatibility/preview-src `
  --python C:/path/to/python312/python.exe `
  --postgres-bin C:/path/to/pgsql17/bin
```

Use `--target baseline` with the pinned Odoo 19 checkout for the other lab.
The [preparation script](../../scripts/prepare_odoo_compatibility_lab.py)
checks the source commit and clean checkout, interpreter and PostgreSQL major,
and available ports before installation. It refuses an existing target folder;
it never erases or resets a database. A failed attempt leaves its logs for
inspection. The initialized database contains no demo records or customer
data. PostgreSQL uses trust authentication only within this disposable,
loopback-only lab; do not use this configuration for a shared deployment.

On this workstation both preparations are already complete. To inspect one,
read its `prepared.json`. Before restarting, verify that the source checkout
still matches the recorded commit. Start the matching PostgreSQL cluster and
Odoo with the paths in that receipt. Use the established
[scenario runbook](../developer/runbooks/scenario-qualification.md) for live
commands, substituting the lab port and database. A fresh full repetition
needs a separately prepared empty target and new evidence directories.

For the existing baseline database, inspect retained results or perform a
read-only comparison. Do not discard its journal to force a first-run scenario
to pass. Both labs were stopped after this Phase 1 run.

## Verification

The following focused checks passed on Python 3.12:

- The existing Odoo adapter suite passed 87 tests.
- The Recipe, transfer, execution, capture, Production, and scenario selection
  passed 62 tests.
- The new policy/evidence, Recipe-major, and lab-isolation checks passed
  10 tests.
- Both recorded Python dependency sets passed an offline pip dry-run against
  their respective pinned Odoo requirements.
- Documentation quality, code-documentation checks, and whitespace checks
  passed.

The broader architecture checks were also run. Four tests fail outside this
phase's changes: the stored production import inventory is stale;
`fallout_workbook_service` imports its concrete adapter; `_scalar_catalog.html`
exceeds its 600-line limit; and two mapping JavaScript modules exceed their
size limits. The inventory command reports 448 current production modules
against 431 in its saved baseline. No production import, template, or
JavaScript file was changed by Phase 1. These existing failures are recorded
as baseline debt and must not be described as a green repository-wide gate.

The existing adapter suite command was:

```powershell
.\.venv312\Scripts\python.exe -m unittest discover -s tests/integration/odoo -t . -q
```

Run the new baseline checks with:

```powershell
.\.venv312\Scripts\python.exe -m unittest `
  tests.domain.test_odoo_compatibility_baseline `
  tests.application.test_recipe_odoo_version_baseline `
  tests.integration.odoo.test_compatibility_lab -v
```

The 62-test selection is:

```powershell
.\.venv312\Scripts\python.exe -m unittest `
  tests.domain.execution.test_reference_policy `
  tests.domain.execution.test_snapshot `
  tests.domain.data_version.test_odoo_capture `
  tests.domain.recipe.test_representative_shapes `
  tests.application.run.test_odoo_requirements `
  tests.application.run.test_production_rollout `
  tests.application.workspace.test_transfer_preflight `
  tests.application.workspace.test_transfer_execution `
  tests.integration.scenarios.test_committed_canary `
  tests.integration.scenarios.test_profile_round_trip -q
```

The initial sandboxed adapter run failed to create Windows temporary folders.
The same suite passed when rerun outside that sandbox. No application fix was
needed. Browser screenshots, the entire repository test suite, a remote
HTTPS target, and Enterprise acceptance were not run for this phase.

## Next phase

Implement the shared version policy using the decision inventory above.
Preserve the Odoo 19 baseline and historical hashes, enforce matching majors
for transfer endpoints, and keep the preview explicitly unqualified. Follow
the [dual-support plan](../plans/odoo-19-and-20-support.md) for later read,
write, and final-release qualification.
