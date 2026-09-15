---
audience: developer
kind: plan
status: proposed
---

# Support Odoo 19 and Odoo 20

## Decision summary

**Status:** Phases 1 and 2 are implemented as of 2026-09-15. The Odoo 19
baseline and isolated prerelease lab are recorded in the
[Phase 1 report](../testing/odoo-compatibility-phase1.md). The shared version
policy and its validation are recorded in the
[Phase 2 report](../testing/odoo-compatibility-phase2.md). Phases 3 through 5
remain proposed. Odoo 20 support has not been enabled or qualified.

**Reader:** Impodo maintainers deciding how to implement, test, and release
dual-version support.

Keep one Impodo application and the existing JSON-2 integration. Add one shared
version policy, small version-specific reference and capture policies, and
qualification against real databases for both releases. Continue to capture
the actual database's models and fields. Introduce adapter differences only
when a verified Odoo change requires them.

Ship this in three steps: preserve Odoo 19 behavior while centralizing version
decisions; enable Odoo 20 for controlled testing; then promote the tested Odoo
20 operations to supported status. Support transfers only within the same
Odoo major version: 19 to 19 or 20 to 20. A Recipe also applies only to its
authored major version. Cross-version transfers and Recipe conversion are
deferred to a separate future proposal.

Here, **same version** means the same major version, 19 or 20. Different builds
within that major still require a qualified series and compatible live schema;
matching major numbers alone does not establish compatibility.

## What this means for a data manager

A data manager should be able to prepare a migration for either version using
the same Impodo workflow. Impodo detects the connected database's version and
shows whether the intended operation is supported, available for testing, or
blocked. A successful connection test establishes identity and access; later
checks establish whether the selected models and operations are usable.

For example, Northwind has a customer Recipe authored and tested on Odoo 19.
It can apply that Recipe to another compatible Odoo 19 database. If Northwind
also adopts Odoo 20, the data manager creates a separate Odoo 20 authoring
workspace, captures its fields, defines its matching rules, and publishes an
Odoo 20 Recipe. An Integrated Test against Odoo 20 qualifies that Recipe for
its intended use. The existing Odoo 19 Recipe remains available for Odoo 19
projects. Selecting it for Odoo 20 produces a blocking version mismatch.

For a direct Odoo-to-Odoo transfer, Impodo records the source version and the
destination version separately and checks that their major versions match
before destination matching. It resolves relationships in the destination
using reviewed identities. A source database's numeric record IDs never
become destination record IDs merely because both servers run the same version.

These are proposed behaviors. Existing browser instructions remain the source
of truth until implementation and qualification are complete.

## Release evidence and assumptions

The official Odoo Experience event runs on September 24–26, 2026. Treat that
as the planning window; the event page alone does not establish the exact
availability of the final Odoo 20 packages.
[Official event information](https://www.odoo.com/event/odoo-experience-2026-9099/page/oxp26-be-practical-information).

At inspection, Odoo's development branch declares version `19.5a1`. Its
version definition also allows SaaS series names. A development or SaaS build
must retain its actual identity; it must not be relabelled as final Odoo 20.
The final Odoo 20 API and model differences remain release qualification
inputs, rather than established changes in this proposal.
[Upstream version definition](https://raw.githubusercontent.com/odoo/odoo/master/odoo/release.py).

JSON-2 was introduced in Odoo 19. It exposes database-specific models and
methods, accepts API-key authentication, and runs each request in its own
transaction. Development documentation continues to describe JSON-2, which
supports the choice of a shared transport, subject to testing the final
release. Odoo's hosted API access also depends on the subscription plan.
[Odoo 19 API source](https://raw.githubusercontent.com/odoo/documentation/19.0/content/developer/reference/external_api.rst),
[development API source](https://raw.githubusercontent.com/odoo/documentation/master/content/developer/reference/external_api.rst).

Odoo has postponed removal of the legacy XML-RPC and JSON-RPC services to
Odoo 22, with the SaaS transition identified as 21.1. Dual support therefore
does not require an emergency protocol migration. Impodo already uses JSON-2.
[Official deprecation change](https://mergebot.odoo.com/odoo/documentation/pull/16765).

## Baseline implementation and required changes

Phase 1 found version assumptions across the integration. Phase 2 centralizes
the runtime gates below; version-specific policies and execution compatibility
evidence remain later work.

| Area | Phase 1 evidence | Proposed change |
| --- | --- | --- |
| Remote API | `Json2ReadConnector` provides named reads and identity probes. `Json2WriteExecutor` provides scoped writes. | Share these adapters between versions and test their request and response contracts on each release. |
| Local access | Local schema reads execute through the selected Odoo installation. Local readiness and fingerprint checks require a version starting with `19.`. | Reuse the shared version policy and qualify each installation's shell and readiness paths. |
| Workflow checks | Schema capture, source capture, destination matching, comparison, preflight, and execution contain Odoo 19 checks. | Route every entry point through the same operation-specific support decision. |
| Target evidence | `TargetFingerprint` stores the reported version and available module versions. The connection hash contains mode, URL, and database. | Keep connection identity stable and bind compatibility evidence separately to schema and run evidence. |
| Recipes | The compiler stores one `odoo_major_version`. Application compilation blocks a different major. | Preserve this contract and qualify separately authored Recipes within each major. |
| Reference models | Country, Language, and Currency policies explicitly describe Odoo 19 and share one policy hash. | Select reviewed policies by major without changing the existing Odoo 19 policy hash. |
| Odoo source data | Capture has a versioned Odoo 19 policy. The transfer workflow separately binds a source and destination. | Add a reviewed Odoo 20 capture policy, validate each endpoint, and require matching majors. |
| Documentation | Workflow headings and documentation checks explicitly require “Odoo 19 and performance”. | Update the checker, audience pairs, labels, and support documentation together when behavior ships. |

Key implementation locations:

- [Remote readers and identity probes](../../src/impodo/adapters/odoo/connectors.py),
  [writer](../../src/impodo/adapters/odoo/writer.py),
  [local reader](../../src/impodo/adapters/odoo/local_reader.py), and
  [local stack](../../src/impodo/adapters/odoo/local_stack.py).
- [Connection testing](../../src/impodo/application/odoo_connection_service.py),
  [remote connection status](../../src/impodo/web/remote_connection.py), and
  [workspace destination state](../../src/impodo/domain/workspace/workbench.py).
- [Schema capture](../../src/impodo/application/schema_workspace_service.py),
  [source capture](../../src/impodo/application/odoo_source_capture_service.py),
  [destination matching](../../src/impodo/application/destination_matching_service.py),
  [comparison](../../src/impodo/application/odoo_comparison_service.py),
  [preflight](../../src/impodo/application/preflight_service.py), and
  [execution](../../src/impodo/application/workspace/execution/service.py).
- [Recipe publication compiler](../../src/impodo/application/recipe_compilation_service.py),
  [Recipe application compiler](../../src/impodo/application/recipe_application_compilation.py),
  [reference policy](../../src/impodo/domain/workspace/reference_keys.py),
  [source policy](../../src/impodo/domain/odoo_source_policy.py), and
  [target reader composition](../../src/impodo/web/composition/target_readers.py).

## Proposed support scope

Support means that a named operation has passed its acceptance checks on a
recorded Odoo build and deployment type. Merely accepting major version 20
does not establish support for every module or custom database.

| Workflow | Initial dual-support objective |
| --- | --- |
| File source to Odoo 19 | Preserve the current preparation, Test, load, and qualified Production behavior. |
| File source to Odoo 20 | Qualify the same supported operations, with Production enabled after Odoo 20 Integrated Test and rollout checks. |
| Odoo source capture | Qualify bounded capture separately for Odoo 19 and Odoo 20. |
| Odoo-to-Odoo transfer | Preserve 19 → 19 and qualify 20 → 20 within the existing transfer scope. |
| Cross-version transfer | Block both 19 → 20 and 20 → 19. Defer these to a separate future proposal. |
| Recipe application | Apply an Odoo 19 Recipe only to Odoo 19, and an Odoo 20 Recipe only to Odoo 20. No cross-version conversion is included. |
| Same-target Odoo round trip | Preserve the current protected-ID update rules and qualify them independently for each major. |
| Production from an Odoo source | Retain the current unsupported disposition; dual-version support does not change that lifecycle contract. |
| Whole-database Odoo upgrade | Outside this proposal. Impodo transfers selected business data; it does not upgrade Odoo modules or migrate an entire Odoo database. |

Start qualification with the deployment types already used by Impodo: local
installations and remote HTTPS databases. Record Community or Enterprise,
installed modules, and exact build information with every live result. Use a
customer-representative Enterprise test database before claiming coverage of
Enterprise features. Qualify Odoo Online and SaaS series explicitly when
needed; major-version parsing alone cannot establish their support.

## Implementation design

### 1. Centralize version recognition and support decisions

Add a small pure domain module under `src/impodo/domain/odoo/` for version
recognition and support policy. It should represent the raw reported version,
major, series, and release stage. Prefer structured `version_info` when
available, cross-check it against the version string, and retain the raw
evidence for diagnosis. Unknown, malformed, or conflicting evidence blocks
dependent operations. Do not infer a version from the URL or a user selection.

All callers request a decision for a specific operation: connect, capture
schema, capture source records, compare, write, or recover. The decision
combines a recognized version with the enabled and qualified operation set.
It does not grant access to additional models, fields, or methods. The current
credential and reviewed execution scope still control those permissions.

Use the same decision in browser routes, application services, workers, CLI
paths, and local-stack readiness. Replace message-text matching in local
startup with a structured version-mismatch reason. Keep connection testing
bounded to identity, version, and authentication; probe business capabilities
only when the relevant workflow needs them.

For the first refactor, freeze the current Odoo 19 acceptance behavior and
regression fixtures. Inventory the Odoo 19 builds actually used before
tightening series rules. Then add an explicit qualification mode for the
recorded development build. Prerelease writes require a disposable test
target; Production remains blocked. Final Odoo 20 becomes supported only
after the corresponding gates pass. Unsupported future majors stay blocked.

### 2. Share transport and isolate proven differences

Continue using JSON-2 for remote reads and writes. Preserve the closed read
methods, scoped writer methods, independent capability objects, response-size
limits, batching, timeouts, and existing write-outcome handling.

Compare the supported operations on both servers: version discovery,
`context_get`, `has_access`, model discovery, `fields_get`, `default_get`,
constraint discovery, `search_read`, `create`, `load`, `write`, External-ID
resolution, generated-record projection, and read-back. Verify the local
metadata scripts as well as the remote adapter.

If an operation differs, contain the adaptation at its adapter or reviewed
policy boundary and add a focused contract test. Do not introduce a second
copy of the migration engine or a general method dispatcher. Add no custom
Odoo add-on unless a demonstrated missing capability makes it necessary and
the separate change is reviewed.

### 3. Use actual schema evidence and versioned reference policies

Continue discovering the effective schema from the selected database. Compare
field presence, type, relation, selection codes, required status, writability,
defaults, business keys, and company scope. A renamed field or altered
selection must produce an actionable incompatibility before preparation or
writing. Structural similarity alone does not prove equivalent ORM behavior.

Maintain separate reviewed Odoo 19 and Odoo 20 reference-policy entries. Pass
the detected major explicitly through every reference consumer, including
mapping validation and Recipe compilation. Remove implicit runtime assumptions
that a missing version means 19. Reuse identical policy content where tests
justify it, while preserving each version's policy identity.

Keep the existing Odoo 19 policy bytes and hashes stable. Adding Odoo 20 must
not change a global hash and invalidate every existing Odoo 19 Recipe or
capture. Apply the same rule to source-capture policies. A deliberate change
to a policy's meaning requires a new revision and explicit revalidation.

### 4. Enforce the same-major rule for transfers and Recipes

Require the captured source major to match the freshly verified destination
major before destination matching, preflight, load preparation, and execution.
Enforce this in application services and workers, as well as the browser.
Checking that each endpoint is independently supported is insufficient:
Odoo 19 and Odoo 20 are both supported targets, but the pair is not supported.
Repeat the check after reconnect and before recovery. A mismatch blocks new
write transport and journaling while preserving existing evidence and
compatible read-back access for investigation.

Keep the existing single-major Recipe target contract. An Odoo 19 Recipe
remains an Odoo 19 Recipe. Author Odoo 20 Recipes against captured Odoo 20
schema and qualify them on Odoo 20. Preserve the current compiler's rejection
of a different target major, even where model and field names happen to match.
Offer compatible Recipes in the selection UI and retain the service-level
check for imported artifacts, stale forms, and direct calls.

Do not add Recipe conversion, a multi-major Recipe contract, automatic field
renaming, or cross-version value adaptation in this delivery. These require a
separate future design if cross-version migration becomes a product goal.

A CutoverPlan qualified against Odoo 19 does not authorize Odoo 20 Production.
Every Recipe in a plan must target the run's major version; mixed-major plans
are blocked. Odoo 20 requires an Integrated Test on Odoo 20 and fresh
Production target, schema, credential, comparison, and approval evidence
under the existing Production lifecycle.

### 5. Bind execution to current compatibility evidence

Keep `target_identity_hash` as the identity of the configured connection. It
currently excludes the Odoo version, so an upgrade at the same address can
leave that hash unchanged. Do not solve that by silently changing existing
connection hashes and credential bindings.

Instead, bind the observed version, selected policy revision, relevant schema
and available module evidence to the run's compatibility assessment. Persist
that assessment with the schema capture and reference it from comparison,
execution, qualification, and Production activation evidence. Separate
observation timestamps from semantic compatibility hashes.

Refresh version evidence before a new load and before recovery. A long-lived
connector's cached fingerprint is insufficient for these checks. If the
server changes major, policy, or relevant schema after review, block new
writes and require fresh capture, compilation, preparation, comparison, and
approval as applicable. If an execution already has journaled attempts,
preserve that journal and reconcile its outcomes before proposing further
writes. Never resume an Odoo 19 execution using Odoo 20 semantics by default.

Treat this as a pre-write freshness check, not proof against an upgrade in
the middle of a multi-request load. Schedule target upgrades outside active
runs. If detectable drift occurs during a run, stop further writes and retain
the journal for reconciliation. Preserve the existing rule that a lost write
response is an unknown outcome, not permission to resend the write.

Version every changed serialized contract deliberately. Existing Odoo 19
artifacts must remain readable with their original hashes. Do not fabricate
missing compatibility evidence when opening an old artifact: require a fresh
assessment before new execution where needed. Use additive storage upgrades
and preserve historical run evidence. An older Impodo binary that cannot read
new storage is not a safe rollback strategy.

## Delivery plan

These are engineering estimates for one developer familiar with Impodo,
assuming accessible test installations. Re-estimate after the Odoo 20 probe.

| Slice | Deliverable and acceptance | Estimate |
| --- | --- | --- |
| 1. Establish the baseline | Record Odoo 19 builds, modules, and representative results. Inventory version checks and serialized evidence. Prepare an isolated, pinned development database for Odoo 20 investigation. | 1–2 days |
| 2. Centralize policy | Introduce version recognition and operation decisions; route all current checks through them. Preserve Odoo 19 behavior, policy hashes, and Recipe fixtures. | 2–3 days |
| 3. Qualify Odoo 20 reads | Capture sanitized metadata and response fixtures, add the Odoo 20 policies, and fix verified adapter differences. Exercise local and remote capture, matching, references, and Recipe assessment. | 2–3 days |
| 4. Qualify execution and version boundaries | Bind compatibility evidence, revalidate before writes and recovery, qualify Odoo 20 Recipes and 20 → 20 transfers, and verify that cross-version use is blocked. | 3–5 days |
| 5. Release and document | Re-run against final Odoo 20, complete the deployment matrix and browser checks, publish evidence and support scope, and enable qualified operations. | 2–3 days |

**Planning range: 10–16 engineering days, roughly 2–4 working weeks**, plus
waiting time for final packages, suitable Enterprise environments, or material
upstream changes. Before release week, aim to complete slices 1 and 2 and
start the development-build probes. Do not promise Production support on the
Odoo launch date before final-build qualification.

Keep the slices reviewable as separate pull requests. Slice 2 can ship while
Odoo 20 remains disabled. Later slices may enable reads before writes, provided
the UI and support matrix state the available operations accurately.

## Verification and release gates

### Automated tests

Run the same connector contract suite for both versions. Use sanitized
fixtures captured from each tested release; changing `19.0` to `20.0` in an
existing mock is insufficient evidence of Odoo 20 compatibility.

Add focused tests for:

- Stable versions, recognized prereleases and SaaS series, malformed versions,
  conflicting version evidence, unknown versions, and unsupported majors.
- Direct browser, CLI, worker, local-reader, and recovery calls that must not
  bypass operation policy.
- Version-specific references, source policies, changed fields and defaults,
  and a missing required model or operation.
- Existing Odoo 19 artifact hashes and Recipe application behavior after
  registering Odoo 20.
- Rejection of Odoo 19 Recipes and Test qualifications on Odoo 20, and the
  reverse direction, even when both schemas have matching field names.
- Rejection of mixed-major CutoverPlans and of direct or imported Recipe
  applications that bypass the selection UI.
- An in-place server upgrade between capture and load, including a cached
  connector and an interrupted execution with unknown outcomes.
- Successful 19 → 19 and 20 → 20 transfer checks, with separate source and
  destination evidence.
- Rejection of 19 → 20 and 20 → 19 before destination matching and again at
  preflight and execution, without new write attempts or Odoo writes.

Extend the existing integration and scenario suites. Wire the version matrix
into the project's automated release process; no checked-in `.github`
workflow directory was present at inspection, so this is new wiring rather
than an assumed existing matrix.

### Live acceptance

Use separate disposable databases and pinned server builds. Keep Odoo 19 and
Odoo 20 configurations, ports, data directories, and credentials separate.
Do not repoint an Odoo 19 test database at Odoo 20 binaries. Record each
server's own Python and PostgreSQL requirements when installing it.

For each supported major, cover:

1. Local metadata capture and remote HTTPS JSON-2 discovery and access checks.
2. Contacts, product categories, products and generated variants, and the
   existing BOM scenarios, including required references and defaults.
3. Creates through the intended ORM and import paths, explicit updates,
   relationship resolution, deferred relationship writes, and exact read-back.
4. An unchanged second preview, rejected writes, lost responses, interrupted
   jobs, and safe recovery without duplicate creation.
5. Recipe publication and application, Integrated Test qualification, and
   Production activation on a different disposable database of that major.
6. Bounded Odoo-source capture and successful 19 → 19 and 20 → 20 transfers.
   Demonstrate that both cross-version directions are blocked, including
   when source and destination schemas appear identical.
7. Representative custom fields, permission restrictions, multi-company
   context, and installed Enterprise modules where those are claimed.

Reuse [remote acceptance](../developer/runbooks/remote-odoo-acceptance.md)
and [scenario qualification](../developer/runbooks/scenario-qualification.md).
The representative remote runner already checks 150 rows, with 125 creates,
20 updates, 5 unchanged rows, read-back, and a repeat unchanged preview.
Parameterize the expected version and record build and policy identities.
Keep throughput measurements per version, with the same workload and request
counts, rather than asserting unmeasured performance parity.

Fast policy and fixture tests should run for every relevant change. Run the
real-server matrix on scheduled integration runs and before a release that
changes Odoo behavior. A scheduled development-branch probe can give early
warning; it cannot replace the pinned stable release gate.

### Promotion criteria and fallback

Promote an Odoo 20 operation only when its final-build live acceptance passes,
the Odoo 19 regression gates remain green, and its supported scope is
documented. The maintainer who owns the release records the tested builds,
modules, deployment types, scenarios, results, and unresolved limitations.

Provide a way to disable new Odoo 20 writes independently of Odoo 19. Preserve
artifact viewing and qualified read-back or recovery access for existing
journals; disabling new loads must not strand outcome investigation. If the
release gate fails, continue shipping Odoo 19 and retain Odoo 20 as explicitly
limited test support until the failed operation is fixed and requalified.

Maintain Odoo 19 as a tested release target throughout the Odoo 20 lifecycle.
Review its retirement when planning Odoo 21, with a separate support decision
and notice. Do not silently remove it when adding the next major.

## Documentation and contract work at implementation time

Update the owning user and developer pages together for setup, source data,
Odoo data, matching, final review, loading, and Production rollout. Update
`docs/workflow.yml`, the architecture documents, code documentation, local
setup instructions, acceptance runbooks, and version-related UI messages.
Capture new screenshots where a label or decision changes.

Replace the required “Odoo 19 and performance” heading with a version-neutral
integration heading in both the documentation checker and all registered
developer pages in one change. Update the style guide and Vale version rule
to describe the actual supported scope. Keep historical evidence labelled
with the release on which it was obtained.

The implementation must update these contracts where the proposed behavior
changes them:

- [Recipe lifecycle](../developer/contracts/recipe-lifecycle.md).
- [Preflight](../developer/contracts/preflight.md).
- [Execution and reconciliation](../developer/contracts/execution-and-reconciliation.md).
- [Production run lifecycle](../developer/contracts/production-run-lifecycle.md).
- The source and transfer responsibilities described in
  [Load into Odoo](../developer/workflow/06-load-into-odoo.md).

## Original proposal validation

This proposal is grounded in the working-tree implementation and the official
upstream sources linked above. It identifies planned behavior separately from
current contracts. Existing unrelated working-tree changes were preserved.

Documentation validation passed: `documentation_quality.py --check`,
`code_documentation_inventory.py --check`, all seven tests in the current
documentation-quality and code-documentation test modules, and
`git diff --check`. The test modules live under `tests.architecture`; the
documentation skill's older test-module path was resolved to that location.

No Odoo server was contacted, no live migration was run, and no runtime
compatibility claim was validated during the original proposal drafting. Browser tests,
screenshots, and the live Odoo matrix belong to the implementation slices.
The installed architecture advisor was unavailable because its Windows
preflight could not start; the proposal therefore has no independent advisor
endorsement.
