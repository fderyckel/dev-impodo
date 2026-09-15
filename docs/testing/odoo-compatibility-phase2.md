---
audience: developer
kind: report
status: current
---

# Odoo compatibility Phase 2: shared version policy

## Result

Phase 2 centralizes Odoo version recognition and operation decisions in
[`domain/odoo/compatibility.py`](../../src/impodo/domain/odoo/compatibility.py).
Odoo 19 remains enabled. Odoo 20 and later majors remain disabled. This work
implements the second slice of the
[dual-support plan](../plans/odoo-19-and-20-support.md).

The policy recognizes the exact reported version, major, series, release
stage, and SaaS prefix. It accepts string-only evidence when no structured
version was supplied. When Odoo supplies `version_info` or
`server_version_info`, the adapters cross-check that tuple against the string.
Malformed or contradictory evidence cannot silently fall back to a `19.`
prefix. The local shell now includes `release.version_info` in its temporary
response. Fingerprints continue to store the original version string.

## Acceptance boundary

| Reported version | Recognition | Current operation decision |
| --- | --- | --- |
| `19.0`, `19.4` | Final Odoo 19 series | Preserve existing acceptance. |
| `19.0+e`, `19.0-20260908` | Odoo 19 with a build suffix | Preserve existing acceptance; this does not qualify a deployment or its installed modules. |
| `19.5a1` | Odoo 19.5 alpha | Preserve the former prefix gate; do not relabel it as final Odoo 20. |
| `20.0`, `20.0rc1`, `21.0` | Recognized major and stage | Block all operations. |
| `saas~19.4`, `saas-20.1` | Recognized SaaS series | Block all operations. |
| Missing, malformed, or conflicting evidence | No verified major | Block dependent operations. |

The Phase 1 baseline froze the existing Odoo 19 acceptance rules. In
particular, the old prefix gate accepted development versions such as
`19.5a1`. Phase 2 preserves that behavior and exposes its alpha stage to later
policy work. Acceptance is not a qualification claim. Explicit prerelease
qualification and final Odoo 20 promotion remain later delivery steps.

## Entry points

Callers request a decision for connection, schema capture, source capture,
comparison, writing, recovery, Recipe use, or Production. The shared decision
does not grant additional Odoo methods, models, fields, or credentials.

- Remote connection status, local readiness, and destination verification use
  the connection decision. Local startup uses a structured `version_blocked`
  flag to stop its own newly launched process after a version rejection.
- Schema discovery and capture, source capture, destination matching, and
  comparison use their operation decisions. Destination matching also rejects
  different or unknown source and destination majors.
- Preflight execution readiness, load validation, and interrupted-load recovery
  use the write or recovery decision. File-source recovery rejects a disabled
  version before accessing its journal or writer.
- Recipe publication, application, supporting references, target evidence,
  and Production evidence use the same recognition and support rules. A Recipe
  target must still match the Recipe's single authored major. A hypothetical
  Odoo 20 Recipe targeting Odoo 20 remains blocked until support is enabled.

The same-major helper only checks equality. It never enables either endpoint.
The later execution slice must still bind and refresh both versions at
preflight, write, and recovery boundaries before Odoo 20 can be enabled.

## Evidence stability

The source-capture and reference-policy constants, connection identity hash,
fingerprint fields, stored snapshot format, and Recipe contract versions are
unchanged. Existing exact-version freshness comparisons remain in place.
Phase 2 adds no persistence migration and no live compatibility hash.

The frozen fixtures and hashes are recorded in the
[Phase 1 baseline](odoo-compatibility-phase1.md). Its pinned upstream release
definitions supply the tuple format used by the recognition tests.

## Verification

Focused tests cover accepted Odoo 19 forms, disabled majors, SaaS versions,
unknown and malformed evidence, conflicting structured evidence, same-major
boundaries, local startup cleanup, Recipe rejection, and recovery rejection
before journal or writer access. Adapter regression tests retain the existing
bounded request contracts.

The validation results below record the completed Phase 2 checks. Live Odoo 20
qualification, browser screenshots, and cross-version migration are outside
this phase.

### Automated checks

Run these commands with the repository's Python 3.12 environment:

```powershell
.\.venv312\Scripts\python.exe -m unittest discover -s tests/integration/odoo -t . -q
.\.venv312\Scripts\python.exe -m unittest `
  tests.domain.test_odoo_compatibility `
  tests.domain.test_odoo_compatibility_baseline `
  tests.application.test_recipe_odoo_version_baseline `
  tests.application.workspace.execution.test_odoo_versions `
  tests.domain.execution.test_reference_policy `
  tests.domain.execution.test_snapshot `
  tests.domain.data_version.test_odoo_capture `
  tests.domain.recipe.test_representative_shapes -q
.\.venv312\Scripts\python.exe -m unittest `
  tests.application.workspace.test_destination_matching `
  tests.application.workspace.test_transfer_preflight `
  tests.application.workspace.test_transfer_execution `
  tests.application.run.test_odoo_requirements `
  tests.application.run.test_production_rollout `
  tests.application.workspace.test_odoo_connection -q
```

These suites passed: 96 adapter tests, 57 policy and evidence tests, and 23
workflow tests. Windows temporary-directory tests required execution outside
the filesystem sandbox, as in Phase 1.

A broader 183-test run covered mapping validation, execution and reconciliation,
preflight, Odoo comparison, workspace storage, and local connection routes.
Of those, 167 passed. Two workspace tests still expect the word `modified`
in a mapping-conflict message that now says the page is out of date. Both the
message and the stale assertions already exist at `HEAD`; Phase 2 changes
neither. Fourteen browser tests stopped during setup because the parent and
its worker observed different application builds. The browser rerun is
recorded separately below.

The local and remote connection route suites passed all 22 tests in an
isolated copy of the checkout. A fresh run in the shared checkout had again
encountered build mismatches while unrelated preparation and recovery files
were being edited. The isolated run used the same Python 3.12 environment and
these modules:

```powershell
python -m unittest tests.integration.web.test_local_stack `
  tests.integration.web.test_target_workflow -q
```

Parsed-code comparison confirmed that all Phase 2 production changes match
the tested copy. Concurrent changes to other execution-preview behavior were
preserved and are outside this report's acceptance claim.

Documentation quality, module documentation, and whitespace checks passed.
The 19-test documentation and structural selection retained four failures
already recorded in Phase 1: the stale architecture inventory, the forbidden
fallout-workbook import, and two mapping asset-size checks. Phase 2 adds one
domain module to the inventory; its 17 import edges obey layer rules and
introduce no cycles. The old inventory has not been regenerated to accept
unrelated dependency debt.

### Live version probes

The adapter's fixed version script and response parser passed against both
existing Phase 1 labs: `19.0` was recognized as final and `19.5a1` as alpha.
The lab layout keeps pinned source and virtual environments in separate
directories, so this probe exercised the script and parser directly. The
normal local-stack containment checks remain covered by adapter tests.

Only the lab PostgreSQL services were started for these reads. Both were
stopped afterward. This probe performed no business-data writes, repeated no
load scenario, and established no new Production or Odoo 20 qualification.
