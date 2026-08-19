# Recipe-first Phase R0 contracts

## Status and authority

**Status:** Completed on 2026-08-19.

This document freezes Phase R0 of the
[Recipe-first test-to-production implementation
plan](reusable-recipes-and-data-versions-implementation-plan.md). It is the
active contract authority for Recipe persistence and workflow implementation.
[ADR-013](../decisions/README.md#adr-013--recipe-is-the-aggregate-root-and-target-bindings-are-application-specific)
governs the architecture.

The superseded `ProjectSeries`, `series_id`, edition, series-owned endpoint, and
credential-copy contracts and fixtures have been removed and are not
implementation inputs.

## 1. Phase boundary and UI continuity

Phase R0 freezes architecture, payloads, hashes, fixtures, bounds, and recovery
actions. It adds no runtime route, database table, browser control, or visible
workflow change. The existing project screens remain the contained workspace
experience until later phases add Recipe navigation around them.

The implementation uses UI continuity as a design constraint, not a ban on
necessary refactoring:

- preserve the interaction model and visual structure of mature screens when
  Recipe behavior does not require a change; in particular, the matching
  phase should remain largely intact and receive Recipe/DataVersion context
  around it rather than a parallel matching experience;
- refactor landing, creation, overview, target binding, qualification,
  rollout, and history surfaces where Recipe ownership genuinely changes the
  user's task;
- initially resolve current project routes through an internal
  Recipe/DataVersion adapter rather than renaming every page;
- introduce Recipe and DataVersion context coherently in navigation,
  breadcrumbs, headings, and history while retaining compatible project URLs
  during the transition;
- do not expose `MigrationProject`, workspace IDs, hashes, credential
  generations, or intent states as primary product concepts; and
- require explicit user actions at the same existing write-approval and load
  boundaries.

The active deterministic fixture is
[`fixtures/recipes/phase-r0/acceptance-contract.json`](../../fixtures/recipes/phase-r0/acceptance-contract.json).
Its executable gate is
[`tests/test_recipe_phase_r0_contract.py`](../../tests/test_recipe_phase_r0_contract.py).

## 2. Identity and ownership

### 2.1 Recipe

`Recipe` is the aggregate root and the stable operator-facing identity. It
owns:

- `recipe_id`, name, business purpose, classification, and retention policy;
- optimistic revision;
- current immutable RecipeRevision pointer;
- current DataVersion pointer;
- optional CutoverCandidate pointer; and
- bounded RecipeRevision, DataVersion, application, and qualification
  projections.

Recipe does not own a fixed source file, Odoo endpoint, database, API key,
principal, target snapshot, comparison, approval, or execution.

### 2.2 RecipeRevision

`(recipe_id, version)` identifies one immutable semantic revision. Version is a
positive integer and parent version is either absent for version 1 or exactly
one prior version. Publishing appends; it never mutates a published payload.

Recipe v3 in the fixture represents the data manager's fine-tuned and
Test-qualified Customer recipe. The revision number expresses lineage; the
semantic hash expresses exact reusable meaning.

### 2.3 DataVersion and workspace

`DataVersion` identifies one exact source package for `AUTHORING`, `TEST`, or
`PRODUCTION`. It has an independently generated `data_version_id` and owns one
existing contained `MigrationProject` through a separately generated
`workspace_project_id`.

Recipe, DataVersion, and workspace IDs are disjoint UUID namespaces. No route,
repository, or service may infer one ID from another. Each application must
carry all three identities explicitly.

Persisted DataVersion states are:

| State | Meaning | Allowed next state |
| --- | --- | --- |
| `ACTIVE` | Current mutable contained workspace | `SEALED` |
| `SEALED` | Exact source and workspace evidence fixed | none |

Activating a successor seals its predecessor. A provisional workspace never
replaces the current pointer until its exact DataVersion-creation intent commits.
Startup retains a provisional workspace only while such an incomplete intent
references it and otherwise removes it.

## 3. RecipeRevision envelope version 2

The stored envelope has exactly:

```text
recipe_contract_version
semantic_hash
payload_hash
recipe
compatibility_hints
provenance
```

`semantic_hash` is `content_hash(recipe)` over canonical JSON. `payload_hash`
is `content_hash(envelope_without_payload_hash)`. Reads verify payload
integrity before parsing nested values and recompute semantic identity.

The semantic `recipe` object has exactly:

```text
contract_versions
source_shape
parameter_definitions
source_preparation
mapping
odoo_target_contract
target_governance
quality
reference_dependencies
control_definitions
```

It contains deterministic logical IDs and portable business meaning. It must
not contain UUIDs, project/data-version/mapping/binding IDs, source artifacts
or snapshots, endpoint/database identity, connection-target hash, credential
generation or value, principal/permission/context evidence, actors,
timestamps, target snapshots, execution results, or numeric Odoo IDs.

Changing transformations, mapping, identity, relationships, value matches,
quality rules, references, parameters, controls, or Odoo requirements changes
the semantic hash. Changing source file, origin workspace, actor, timestamp,
Test/Production server, database, API key, or credential generation does not.

Compatibility hints and provenance are payload-integrity protected but remain
outside semantic identity. They cannot authorize a source or target binding.

## 4. Parameters and controls

`RecipeParameterDefinition` has logical ID, label, type, required flag,
validation constraints, and allowed use sites. Values that legitimately vary
between data exports must be declared. An undeclared mutable constant requires
a new RecipeRevision.

Each DataVersion supplies a fresh content-hashed `RecipeParameterValues` with:

```text
contract_version
data_version_id
values
source
reason
actor
confirmed_at
content_hash
```

The value hash binds RecipeApplicationEvidence. Reusing the Recipe with a new
valid value set does not change Recipe semantic identity.

RecipeRevision stores reusable control definitions. Each DataVersion supplies
fresh expected values unless a definition explicitly declares an invariant.
Test totals never silently become Production totals.

## 5. Odoo target and credential contracts

### 5.1 OdooTargetContract

The semantic OdooTargetContract freezes the required Odoo major version,
applications, technical models/fields, field types and relations,
required/readonly/write-use semantics, required selection codes, custom
capabilities, ordered business keys and scope, reference dependencies, and
approved write fields.

It contains no endpoint, database, credential, principal, permission, probe,
or snapshot. Unrelated target schema additions do not affect compatibility.

### 5.2 TargetBinding

TargetBinding version 1 contains:

```text
contract_version
target_binding_id
environment
endpoint
database
connection_target_hash
credential_role
credential_generation
credential_storage_class
principal_hash
permission_hash
context_hash
schema_dependency_hash
reference_snapshot_hashes
probe_status
probed_at
captured_by
content_hash
```

Endpoint and database are non-secret operational identity. Credential values
remain only in the governed project secret store. API keys, passwords, tokens,
and other secret material are forbidden from Recipe, registry, application,
qualification, intent, log, URL, error, and export payloads.

Test and Production always have distinct bindings. Read and write roles always
have distinct binding evidence even if an operator supplies the same secret
text. No credential-copy intent exists between environments.

`connection_target_hash` is the canonical hash of non-secret endpoint and
database identity. TargetBinding `content_hash` covers the entire object except
its `content_hash` field.

### 5.3 Rotation

Changing a credential generation on the same connection target creates a new
TargetBinding and requires:

1. a new probe;
2. new principal, permission, and context evidence;
3. refreshed schema and reference evidence;
4. invalidation of comparison and load-readiness projections bound to the old
   generation; and
5. a fresh comparison before write authority can be established.

Rotation does not mutate prior application evidence and does not change Recipe
semantic or source-selection hashes. `ApplicationReadinessProjection` records
whether immutable evidence remains current. The fixture rotates the Production
read credential after an accepted comparison and before load, then proves
re-probe, refresh, and re-comparison occur first.

## 6. Application, qualification, and cutover

### 6.1 RecipeApplicationEvidence

Version 1 immutably binds:

- application ID, Recipe ID/revision/semantic hash;
- DataVersion and workspace IDs;
- source artifact and effective source-selection hashes;
- parameter-values hash;
- exact TargetBinding ID/hash;
- target-contract assessment and logical-binding hashes;
- compiled MappingDefinition ID/content hash;
- comparison hash, terminal status, timestamp, and content hash.

Every new source package or TargetBinding produces new application evidence.
The compiled MappingDefinition remains the exact evidence-bound runtime input;
it does not become the reusable Recipe.

### 6.2 RecipeQualificationEvidence

Qualification version 1 is immutable and must bind the exact Test:

- Recipe revision and semantic hash;
- application and TargetBinding evidence hashes;
- preparation, quality, control, and comparison hashes;
- execution, read-back, and reconciliation hashes;
- bounded outcome and findings;
- qualifying actor and timestamp; and
- qualification content hash.

Only a successful Test execution with successful read-back, reconciliation,
and controls may become `TEST_QUALIFIED`. Qualification proves the tested
logic; it grants no Production access or write authority.

### 6.3 CutoverCandidate

CutoverCandidate version 1 pins Recipe ID/revision/semantic hash and exact
qualification ID/hash under optimistic Recipe revision. It includes selecting
actor and timestamp. It contains no Production endpoint, database, credential,
TargetBinding, DataVersion, source, comparison, approval, or execution.

Production must create fresh source, parameters, binding, compatibility,
mapping, comparison, approval, execution, read-back, and reconciliation
evidence around that pinned Recipe revision.

## 7. Cross-store intents and recovery

Version 2 freezes these idempotent intent kinds:

- `RECIPE_PUBLICATION`;
- `DATA_VERSION_CREATION`;
- `QUALIFICATION_PUBLICATION`;
- `CUTOVER_SELECTION`.

Every intent carries operation ID, Recipe ID, expected optimistic revision, and
state plus only the exact identifiers required by that operation. No intent
contains `series_id` or secret material.

An unpublished Recipe draft is deleted directly through the Recipe aggregate
after its exact Recipe and workspace revisions are validated. Published Recipe
deletion is outside the current product surface.

The frozen focused recovery outcomes are:

| Failure | Code | Single recovery action |
| --- | --- | --- |
| Credential rotated after comparison | `TARGET_BINDING_STALE` | Re-probe, refresh target evidence, and recompare |
| Production schema incompatible | `RECIPE_TARGET_INCOMPATIBLE` | Fix target or publish and requalify a new revision |
| Used source column renamed | `RECIPE_SOURCE_COLUMN_MISSING` | Confirm exact column binding |
| New categorical value | `MAPPING_SOURCE_VALUE_UNMATCHED` | Map value and publish a new revision |
| Stale cross-store operation | `RECIPE_REVISION_CONFLICT` | Reload Recipe |
| Unknown remote write outcome | `EXECUTION_OUTCOME_UNKNOWN` | Reconcile before retry |
| Qualification evidence mismatch | `QUALIFICATION_EVIDENCE_MISMATCH` | Requalify exact revision |

## 8. Bounds

The initial bounds are:

| Item | Limit |
| --- | ---: |
| Recipe payload | 4 MiB |
| Reference packages, aggregate | 64 MiB |
| Logical datasets | 100 |
| Logical columns | 10,000 |
| Semantic nodes | 10,000 |
| Parameter definitions | 100 |
| Parameter value payload | 64 KiB |
| DataVersions per Recipe | 1,000 |
| RecipeRevisions per Recipe | 1,000 |
| Applications per DataVersion | 100 |
| Qualification findings | 2,000 |
| Application overrides | 10,000 |
| Application issues | 2,000 |
| Examples per issue | 20 |
| Target candidate keys per batch | 10,000 |

These are metadata and evidence bounds. They do not raise current preparation
row limits or reopen the deferred 100,000-row work.

## 9. Deterministic acceptance gate

The Customer fixture proves:

- Recipe v3 semantic identity excludes source and environment evidence;
- Recipe, DataVersion, and workspace IDs are independent;
- the Test and rollout CSVs have different bytes, dates, rows, parameters, and
  expected control totals;
- Test and Production endpoint/database identities differ;
- Test qualification pins only exact Test evidence;
- the CutoverCandidate pins that qualification but no Production authority;
- Production uses new binding/application/comparison evidence and separately
  established write evidence;
- Production credential rotation invalidates only dependent readiness,
  preserves Recipe and source identity, and forces fresh comparison;
- German and Luxembourg refinements in Recipe v3 apply to rollout data;
- absent rows never infer deletion; and
- intents, recovery actions, hashes, and bounds are deterministic.

Phase R1 may begin only while this executable contract remains green. Any
intentional contract change must update this document, the active fixtures,
and the focused test together.
