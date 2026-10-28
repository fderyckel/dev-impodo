---
audience: developer
kind: architecture
status: current
---

# Shared-server fact ownership

## Status and decision boundary

**Status:** Accepted on 2026-10-23 as the Phase 0 ownership boundary. It
defines the target shared-server authority boundary. The local Linux trial now
implements the Organization-scoped Project catalogue, immutable cross-store
receipts, fenced Workspace leases, and receipt-projection current pointers in
PostgreSQL. Accounts, remote request identity, the shared worker, and the
operator-facing administration process are not implemented yet.

The current local Windows product remains authoritative through its contained
DuckDB stores, protected artifact stores, Windows Credential Manager bindings,
and loopback launch session. This inventory does not move or dual-write a
local fact. A future local composition may pass an implicit reserved
Organization identity through portable interfaces, but it does not expose
Organization administration to the local user.

The local Linux trial uses PostgreSQL control schema version `4`. Its bounded
reconciliation read verifies receipt hashes and current pointers without
opening DuckDB. Its governed rebuild service accepts only a missing projection
for an already committed data-plane receipt. It never repeats the data-plane
command or derives approvals and Odoo outcomes from display status.

This document refines [ADR-017](../decisions/README.md#adr-017--organization-scoped-postgresql-control-and-duckdb-data-planes)
and the
[Linux server architecture plan](../plans/07-vm-deployment-duckdb-worker-management.md).
This acceptance authorizes Phase 1 composition and request-identity work. It
does not authorize a PostgreSQL schema or data migration ahead of the later
phase gates.

## Authority rules

An authoritative store answers what happened. A projection makes an
authoritative fact discoverable or schedulable and can be rebuilt. A storage
key locates evidence but never replaces its content hash or owner identity.

| Owner | It is authoritative for | It must not contain |
| --- | --- | --- |
| PostgreSQL control plane | Organization, user, session, role and grant revisions, Project discovery, durable jobs, leases, idempotency, network and retention choices, bounded lifecycle, receipt hashes, and repair state. | Source or target rows, complete mapping and Recipe payloads, reports, protected Odoo IDs, or credentials. |
| DuckDB data plane | Detailed migration configuration, revisions, rows, findings, decisions, comparison, execution, reconciliation, and immutable domain evidence. | Server passwords, sessions, Organization membership, or cross-Organization scheduling state. |
| Immutable or protected artifacts | Original uploads, Parquet snapshots, published Recipe packages, reports, protected identifiers, target-bound evidence, and large immutable payloads. | Mutable role, session, job, lease, or current-status state. |
| Local-only control plane | Loopback launch token, local-stack process ownership, local paths, and Windows Credential Manager bindings. | Server tenancy or shared-user policy. |
| Derived projection | Counts, labels, progress, current pointers, and bounded status copied from an accepted receipt. | The only copy of a decision, approval, row, payload, or audit fact. |

The server browser discovers a Project from PostgreSQL. It never opens every
customer DuckDB file and filters afterward. After discovery and authorization,
a worker resolves the Organization-qualified storage key and opens only the
selected Project or Workspace under a current lease.

## Proposed PostgreSQL facts

Every row below has one authority and one failure recovery rule. Detailed
schema design remains a later phase.

| Fact | Command that changes it | Recovery rule |
| --- | --- | --- |
| Organization identity, display label, state, and policy revision | Platform bootstrap or governed platform administration. | Retry the immutable command ID. A duplicate identifier returns the existing matching result; different input conflicts. |
| Customer user and exactly one `organization_id` | Organization administrator creates, disables, restores, or renames an account. | Account identity and email constraints reject conflicting reuse. No partial second membership is possible because membership is scalar. |
| Password verifier, reset nonce, MFA-ready state, session, and revocation | Authentication and Organization administration services. | Store only slow password-verifier output and hashed single-use tokens. Revoke ambiguous sessions; never reconstruct them from browser state. |
| Role definition and immutable role revision | Organization administrator publishes a role revision. | Current pointer advances only after the revision commits. Retry by command ID; never edit an accepted revision in place. |
| Organization-level and Project-level grant revision | Organization administrator changes a grant. | Resolve effective authorization from committed revisions. An expired or superseded receipt is rejected and re-resolved. |
| Project catalogue identity, Organization ownership, label, lifecycle, and visibility | Project create, rename, archive, restore, or access command. | PostgreSQL is the discovery authority. If data-plane creation has not produced a matching receipt, show repair-required and do not advertise readiness. |
| Contained Data version, Migration run, Workspace, Recipe, and cutover catalogue identity | The owning aggregate command publishes an identity and parent binding. | Require the parent Organization and Project in every key. Repair a missing projection from its data-plane receipt; never infer ownership from a filesystem path. |
| Idempotency reservation and semantic input hash | Every state-changing server command before data-plane work. | Same key and same semantic input reuse the operation. Same key and different input returns conflict. Abandoned reservations become retryable by policy without changing their semantic binding. |
| Durable job request and attempt identity | Application service queues allowlisted work; scheduler starts an attempt. | A restart marks a live attempt interrupted after heartbeat expiry. It creates a new attempt and fencing token only when the operation remains retryable. |
| Job transition, bounded progress, cancellation request, interruption, and result receipt hash | Worker and scheduler transitions. | Compare-and-swap the expected transition version. Ignore stale progress and reject terminal-state rewrites. Customer values never enter progress or failure fields. |
| Workspace lease, heartbeat, expiry, release, and fencing token | Scheduler acquires; current worker heartbeats or releases. | Tokens increase monotonically. Expired or lower tokens cannot commit or publish a receipt. A successor does not trust a predecessor's process state. |
| Equal scheduling state and bounded quota counters | Scheduler records active Organizations, runnable work, reservations, and measured use. | Rebuild transient counters from active jobs and leases. Reconcile leaked reservations after expiry; do not fabricate completed work. |
| Approved Odoo network profile and target binding summary | Organization administrator approves a route; authorized data manager binds a Project target. | Store the profile ID, destination allowlist, non-secret target hash, and verification state. Secret material remains in the secret provider. Disable the binding if the profile or credential generation changes. |
| Retention selection and last qualifying activity | Authorized Project administrator selects a policy; governed activity advances the clock. | Revision-check all changes. The default is deletion after 90 inactive days until the user selects another offered policy. |
| Deletion request, execution state, and minimal deletion receipt | Authorized user confirms deletion or the inactivity policy triggers it. | Resume category-by-category deletion until all customer data and recoverable backup copies expire. Retain no storage keys or customer values in the final receipt. |
| Security, account-administration, support-access, and authorization-denial audit event | Authentication, authorization, and administration services. | Append idempotently by event ID. The audit stream never becomes migration-row evidence. |
| Data-plane receipt hash and bounded projection revision | Projection handler accepts a matching immutable receipt. | If absent, replay the same receipt. If the receipt hash differs, quarantine the projection and require repair; do not rerun committed data work. |
| Repair queue and operator-visible repair status | Receipt projection, deletion, restore, or reconciliation detects incomplete cross-store work. | Retry from the authoritative receipt or deletion journal. A repair may advance a projection, never silently change evidence. |

## Current migration foundation inventory

The following classification is exhaustive for the current migration registry
generation `impodo-migration-registry-2026-08-project-root`, version 7. A
current table can be mixed because the local registry presently combines
catalogue control and detailed migration evidence. The target classification
applies to facts, not table names.

| Current table or family | Server authority split |
| --- | --- |
| `schema_version`, `schema_migration` | Remain store-local compatibility metadata. A PostgreSQL schema has its own migration ledger. |
| `migration_project_identity`, `migration_project` | Organization ownership, catalogue identity, visibility, label, lifecycle, optimistic revision, classification, retention choice, and receipt pointers move to PostgreSQL. Detailed migration evidence remains in the Project data plane. |
| `data_version_identity`, `data_version` | Parent identity, bounded lifecycle, version number, labels, and current receipt hash project to PostgreSQL. Source package content and lineage evidence remain in DuckDB and artifacts. |
| `migration_run_identity`, `migration_run`, `migration_workspace_identity`, `migration_workspace` | Parent bindings, lifecycle, labels, and bounded current state project to PostgreSQL. Run plans and Workspace evidence remain in DuckDB. |
| `migration_run_target_setup`, `target_binding` | Approved network-profile binding, non-secret hashes, credential generation, and current verification status belong in PostgreSQL. Base URLs and database names are protected configuration; credentials stay in the secret provider; detailed target evidence stays in DuckDB or protected artifacts. |
| `migration_run_requirement_plan`, `migration_run_target_schema`, `migration_run_reference_bundle` | PostgreSQL keeps a bounded status and content hash. Complete schemas, dependency payloads, reference bundles, and target evidence remain in DuckDB or immutable artifacts. |
| `recipe_identity`, `recipe` | Catalogue identity, Organization and Project ownership, display label, current accepted revision pointer, and lifecycle belong in PostgreSQL. |
| `recipe_revision` | Version, semantic hash, storage key, qualification status, and bounded provenance project to PostgreSQL. The complete portable Recipe package is an immutable artifact; detailed compilation evidence remains in DuckDB. |
| `recipe_application_identity`, `recipe_application` | Parent binding and bounded lifecycle project to PostgreSQL. Applied parameters and detailed outcomes stay in DuckDB. |
| `recipe_application_issue`, `recipe_application_requirement`, `recipe_application_reference_requirement`, `recipe_qualification` | Detailed issues, requirements, reference evidence, and qualification evidence stay in DuckDB. PostgreSQL may project only counts, status, and accepted receipt hashes. |
| `cutover_plan_identity`, `cutover_plan` | Catalogue identity, ownership, label, lifecycle, current revision, and receipt pointer belong in PostgreSQL. |
| `cutover_plan_revision`, `cutover_plan_recipe`, `cutover_dependency`, `cutover_write_ownership`, `cutover_plan_qualification`, `migration_run_cutover_plan`, `project_cutover_selection` | Complete plan, dependency, ownership, qualification, selection, and approval evidence remain in DuckDB or immutable artifacts. PostgreSQL projects the accepted revision, readiness, and receipt hashes. |
| `test_run_setup_binding`, `test_run_parameter_values`, `production_run_binding`, `production_run_values`, `correction_run_binding` | PostgreSQL projects the bounded parent binding, state, and receipt hash. Parameters, production values, correction evidence, and protected values remain in DuckDB or protected artifacts. |
| `project_operation_intent` | Operation ID, idempotency key, semantic hash, state, attempt identity, and receipt hashes move to PostgreSQL. Any detailed input or result payload remains in DuckDB or an immutable artifact. |
| `migration_event` | Split by meaning: security, access, account, support, scheduling, and control-plane repair events belong in PostgreSQL; detailed migration-domain and evidence events stay in DuckDB. Cross-store correlation uses Organization, Project, operation, and receipt IDs. |

Identity tables that contain only one UUID are not copied as separate
PostgreSQL concepts when the owning catalogue row already guarantees the same
identity. They remain useful inside current local DuckDB stores.

## Current Data version store inventory

The current Project-owned Data version store generation is
`impodo-data-version-store-2026-08-project-owned`, version 2.

| Current table | Server authority split |
| --- | --- |
| `schema_version`, `schema_migration` | DuckDB compatibility metadata. |
| `data_version_identity` | DuckDB verifies the exact parent identity. PostgreSQL owns server discovery and stores the matching data-plane receipt hash. |
| `source_package_state` | Complete source intake state, origin, package hash, and freeze evidence remain in DuckDB. PostgreSQL may project only bounded lifecycle and receipt hash. |
| `source_package_file` | File names, hashes, storage keys, sizes, and file evidence remain in DuckDB or the immutable artifact manifest. PostgreSQL does not enumerate customer uploads. |
| `source_package_catalog`, `source_package_configuration`, `source_package_dataset` | Source schema, configuration, and dataset detail remain DuckDB evidence. Counts and completion status may be projections. |
| `source_package_event` | Source intake and freeze evidence remains in DuckDB. Security or access events caused by the command are separate PostgreSQL audit facts. |

Original upload bytes and frozen Parquet source snapshots are immutable artifact
authority. DuckDB retains their owner-qualified manifest and content hashes.

## Current Workspace reference store inventory

The current reference store generation is
`impodo-migration-workspace-2026-08-reference-only`, version 2.

| Current table | Server authority split |
| --- | --- |
| `schema_version`, `schema_migration` | DuckDB compatibility metadata. |
| `workspace_linkage` | PostgreSQL owns server discovery and parent containment. The Workspace store retains the exact linkage used to validate its evidence. |
| `workspace_source_projection`, `workspace_source_dataset` | Detailed source projection, dataset identity, and hashes remain DuckDB evidence. PostgreSQL stores only bounded readiness and the accepted receipt hash. |

## Current Workspace engine inventory

The engine generation is
`impodo-workspace-engine-2026-08-workspace-owned`, version 17. The families
below enumerate every current generation-17 table. Unless a row explicitly
says otherwise, the named tables remain DuckDB data-plane authority.

| Evidence family and current tables | Server projection, if any |
| --- | --- |
| Store metadata: `schema_version`, `schema_migration` | None beyond operational backup compatibility. |
| Cross-store coordination: `cross_store_data_receipt`, `workspace_fence` | DuckDB owns the exact committed receipt and current fencing proof for its bounded data-plane command. PostgreSQL stores and verifies the matching receipt hash, projection receipt, and current pointer. |
| Workspace cache: `workspace_projection_cache` | This is a DuckDB cache of Project/Workspace context. On the server, PostgreSQL is authoritative for the catalogue fields; workers create or refresh the cache from an Organization-qualified receipt. |
| Source intake and selection: `source_file`, `source_catalog`, `source_configuration`, `source_selection`, `source_snapshot_manifest`, `source_snapshot_current` | Source counts, phase status, and snapshot receipt hash only. Source names, values, configuration payloads, manifests, and snapshots stay in the data plane. |
| Odoo source scope: `odoo_capture_selection_revision`, `odoo_capture_selection_current`, `odoo_capture_manifest_revision`, `odoo_capture_manifest_current`, `odoo_model_catalog`, `odoo_schema_catalog`, `odoo_relationship_scope_revision`, `odoo_relationship_scope_current`, `derived_entity_plan_revision`, `derived_entity_plan_current` | Bounded stage readiness, revision number, and receipt hashes only. Models, fields, relationship choices, domains, and protected source links stay in DuckDB or protected artifacts. |
| Mapping authoring: `mapping_working_draft`, `mapping_mutation_receipt`, `mapping_revision`, `mapping_current`, `mapping_validation`, `mapping_submission`, `mapping_row_inclusion_review`, `mapping_row_inclusion_review_row`, `mapping_row_inclusion_current`, `mapping_row_inclusion_confirmation` | PostgreSQL may hold edit revision, lock-free conflict metadata, stage status, and data-plane receipt hash. Complete mappings, row samples, validations, submissions, and confirmations stay in DuckDB. Remote optimistic editing compares a PostgreSQL-visible revision but never copies current field values into the conflict response. |
| Matching-order work: `matching_order_preference`, `matching_order_check`, `matching_order_check_current`, `matching_order_check_attempt`, `matching_order_check_active`, `matching_order_protected_snapshot`, `retired_evidence` | Current progress code and receipt hash only. Preferences, recommendations, protected target evidence, attempts, and retired evidence remain in DuckDB or protected artifacts. |
| Schema governance and supporting lookup: `schema_governance_revision`, `schema_governance_current`, `supporting_lookup_revision`, `supporting_lookup_current`, `recipe_parameter_definitions`, `recipe_quality_seed` | Accepted revision, readiness, and receipt hashes only. Detailed governance, lookup rows, parameters, and quality seed stay in DuckDB. |
| Preparation: `preparation_session`, `preparation_identity_group`, `preparation_lineage`, `preparation_physical_row`, `preparation_impact_row`, `preparation_normalization_group_seed`, `preparation_normalization_finding`, `preparation_direct_identity`, `preparation_relationship_edge`, `canonical_staging_run`, `canonical_staging_row`, `canonical_staging_row_issue`, `canonical_staging_current`, `canonical_prepared_projection` | Job progress, record counts, terminal status, and receipt hashes only. Prepared and canonical rows, lineage, identities, findings, issues, and relationships stay in DuckDB. |
| Prepared and derived artifacts: `prepared_snapshot_manifest`, `preparation_session_snapshot`, `prepared_snapshot_current`, `derived_value_artifact_manifest`, `preparation_session_derived_artifact`, `derived_value_artifact_current` | PostgreSQL may project artifact type, owner, storage key, size, content hash, lifecycle, and receipt hash. Parquet and derived payload bytes remain immutable artifacts; detailed manifests remain DuckDB evidence. |
| Quality and accounting: `quality_ruleset_revision`, `quality_ruleset_current`, `quality_run`, `quality_row_result`, `quality_evidence_projection`, `quality_issue`, `source_accounting_entry`, `source_accounting_link`, `quality_quarantine_entry`, `quality_current`, `readiness_run` | Stage readiness, aggregate counts, and receipt hashes only. Rules, row results, issues, accounting links, quarantine, and readiness evidence remain in DuckDB. |
| Normalization and transformation impact: `normalization_run`, `normalization_effect`, `normalization_group`, `normalization_transition`, `normalization_current`, `transformation_impact_run`, `transformation_impact_row`, `transformation_rule_impact`, `transformation_rule_acknowledgement` | Review status, aggregate counts, accepted revision, and receipt hashes only. Values, effects, groups, transitions, row impact, and acknowledgements remain DuckDB evidence. |
| Coverage and resolution: `coverage_scope_revision`, `coverage_scope_current`, `reference_bundle_revision`, `reference_bundle_current`, `resolution_policy_revision`, `resolution_policy_current`, `resolution_run`, `resolution_candidate`, `resolution_finding`, `resolution_decision`, `effective_row`, `resolution_accounting`, `effective_dataset_reconciliation`, `resolution_current`, `effective_dataset_current` | Bounded phase state, aggregate counts, and receipt hashes only. Scope, reference payloads, candidates, findings, decisions, effective rows, and reconciliation stay in DuckDB. |
| Preflight: `preflight_current`, `preflight_dataset`, `preflight_decision`, `preflight_target_snapshot`, `preflight_transition`, `preflight_execution_projection` | Overall readiness, blocker counts, target-binding hash, and receipt hash only. Per-dataset decisions, snapshots, transitions, and execution projections remain DuckDB or protected evidence. |
| Execution and reconciliation: `execution_run`, `execution_row`, `execution_current`, `reconciliation_run`, `reconciliation_current` | Terminal state, safe counts, and receipt hashes only. Row intents, target results, read-back evidence, failures tied to customer rows, and reconciliation detail remain DuckDB or protected artifacts. |
| `audit_event` | Detailed migration decision and evidence events remain in DuckDB. Authentication, authorization, support access, account administration, lease, scheduling, and repair events are separate PostgreSQL audit facts. Stable actor identity and operation ID correlate them without copying customer detail. |

## Artifact and secret payloads

| Durable payload | Authority | Server rule |
| --- | --- | --- |
| Original upload bytes | Immutable artifact store | Owner-qualified key and SHA-256. Never log a file name or cell value unless the existing evidence contract explicitly permits it. |
| Source and prepared Parquet | Immutable artifact store, with a DuckDB manifest | Publish atomically, verify hash during materialization, and register through a data-plane receipt. |
| Recipe package | Immutable artifact store | Portable payload contains no user, role, Organization grant, credential, or protected target identity. PostgreSQL stores catalogue and accepted hash only. |
| Reports and review workbooks | Immutable artifact store | Access inherits Project visibility. PostgreSQL may store display-safe type, created time, size, key, and hash. |
| Protected Odoo source and target identifiers | Protected artifact/evidence store | Separate capability and target binding remain mandatory. No IDs in PostgreSQL projections, jobs, logs, or portable Recipes. |
| Odoo, database, proxy, VPN, and object-store credentials | Deployment secret provider | PostgreSQL stores a secret reference and generation, never the secret. Local Windows continues to use Credential Manager. |
| TLS private keys and account-password pepper | Deployment secret provider | Never mounted into a data worker unless that process requires the specific secret. |

## Cross-store command protocol

Every server command that may write DuckDB or artifacts follows this order:

1. Authenticate the principal and fix one request Organization scope.
2. Resolve Project visibility and effective capabilities. Freeze an
   authorization receipt with role, grant, policy, and expiry revisions.
3. Reserve an Organization-scoped idempotency key and semantic input hash in
   PostgreSQL. A mismatched reuse stops here.
4. For a Workspace mutation, acquire a lease with a new monotonic fencing
   token. Read-only work may use a bounded snapshot contract instead.
5. Materialize only owner-qualified input artifacts. Perform one bounded
   DuckDB transaction and atomically publish immutable artifacts.
6. Before commit and before publication, prove the current fencing token.
7. Publish a data-plane receipt containing operation identity, input hash,
   evidence hash, artifact identities, and fencing token. It contains no raw
   customer values.
8. In a PostgreSQL transaction, verify the exact receipt hash, advance bounded
   projections, finish the job attempt, and publish a projection receipt. The
   current pointer advances only when this receipt is newer than the pointer it
   replaces.
9. Release the lease. A failed release is repaired by expiry; it does not undo
   committed evidence.

PostgreSQL and DuckDB are not placed in a distributed transaction. The
receipts make partial completion observable and replayable.

## Cross-store failure recovery

| Failure point | Visible truth | Recovery |
| --- | --- | --- |
| Before the idempotency reservation | Nothing accepted. | Safely resubmit the request. |
| After reservation, before lease | Operation is pending; no data-plane receipt exists. | Retry acquisition under the same operation and semantic input. |
| After lease, before DuckDB commit | Lease and attempt are visible; no completed evidence exists. | Let the failed attempt interrupt or expire. A new attempt uses a higher fencing token and resumes only from durable checkpoints. |
| Artifact published but not registered by the DuckDB transaction | Artifact is unreferenced and not a completed result. | Owner-qualified cleanup may delete it after the operation is terminal and no manifest references it. |
| DuckDB committed but receipt publication is incomplete | DuckDB evidence is authoritative but not discoverable as complete. | Reconstruct the bounded receipt from committed operation evidence and hashes; do not repeat the business mutation. |
| Data-plane receipt exists but PostgreSQL projection is missing | Data-plane work is complete; dashboard state is stale or repair-required. | Replay the exact receipt into PostgreSQL and issue the projection receipt. |
| Receipt hash or operation identity differs | Neither candidate is silently preferred. | Quarantine the projection, deny completion, preserve both bounded hashes, and require operator repair. |
| Worker continues after lease expiry | Its token is stale even if the process still runs. | Reject DuckDB finalization and receipt publication; a newer token owns recovery. |
| PostgreSQL projection committed but response was lost | Projection receipt and operation state are authoritative. | The retry returns the existing result for the same idempotency key. |
| Deletion stops between stores | Deletion state remains non-terminal and the Project is unavailable. | Resume deletion from the durable category journal. Complete only after PostgreSQL, DuckDB, artifacts, caches, and backup-expiry actions are acknowledged. |
| Restore contains only one plane | Project is not usable. | Restore to quarantine, validate Organization ownership and all receipt hashes, then expose it atomically in the catalogue. |

## Local behavior that remains separate

The local composition continues to use:

- one implicit trusted local operator and the loopback launch session;
- the current migration registry for Project discovery and local control;
- contained filesystem storage and local DuckDB files;
- Windows Credential Manager for credentials;
- process-local job managers and cooperative cancellation where implemented;
  and
- no role dashboard, session catalogue, Organization switcher, remote edit
  conflict workflow, shared scheduler, server network profile, or server
  deletion worker.

Portable contracts must therefore describe identity, ownership, operations,
jobs, receipts, and retention without importing PostgreSQL, a server identity
provider, `LOCAL_ACTOR`, or a filesystem path.

## Review checklist

- Does each fact have exactly one authority?
- Can every PostgreSQL projection be rebuilt from a named receipt or source?
- Does any proposed PostgreSQL field copy a customer row, mapping payload,
  protected identifier, report, or secret?
- Can the browser discover unauthorized Project existence through a list,
  error, storage key, job, or artifact lookup?
- Does every Workspace writer prove a current fencing token twice: before data
  commit and before receipt publication?
- Can restore and deletion reconcile both planes without claiming completion
  early?
- Does the local composition remain usable without PostgreSQL, Organization
  administration, or remote collaboration behavior?

Approval of this checklist closes the fact-ownership portion of the Phase 0
exit gate. It does not authorize Phase 1 automatically.
