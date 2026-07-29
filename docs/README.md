# Impodo documentation

This documentation is the design authority for the read-only Odoo preflight
milestone. Where an example and a normative rule disagree, text marked
**MUST**, **MUST NOT**, **SHOULD**, or **MAY** is authoritative.

The read-only milestone is one part of the larger Impodo migration product.
See [End-to-end migration product vision](product-vision.md) for source
inspection, mapping, staging, approval, loading, and reconciliation.

## Architecture

- [Security and infrastructure approval brief](security-and-infrastructure-approval.md)
  — concise architecture, implemented controls, infrastructure requirements,
  verification evidence, open gates, and reviewer decision.

- [End-to-end migration product vision](product-vision.md) — complete product
  workflow, mapping architecture, staging, relation handling, executor
  boundary, edge cases, and roadmap.
- [Local application and security architecture](local-application-security.md)
  — recommended local browser stack, file/staging hardening, local Odoo
  laboratory, secrets, and eventual on-premise access.
- [Architecture review](architecture-review.md) — current-state evidence,
  fitness assessment, risks, and readiness gates.
- [Read-only preflight architecture](architecture/read-only-preflight.md) —
  boundaries, components, data flow, invariants, comparison semantics, and
  deployment model.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.
- [Implementation plan](implementation-plan.md) — package layout, sequence,
  deliverables, and definition of done.
- [Phase 2B relationship and semantic-validation proposal](phase-2-relationship-semantic-validation-proposal.md)
  — dataset-centric mapping contracts, relationship authoring, semantic
  validation, immutable revisions, delivery slices, and acceptance criteria.
- [Data-quality rules implementation plan](data-quality-rules-implementation-plan.md)
  — governed source corrections, structured formats, rule evidence, package
  quality gates, manager authoring, rollout, and acceptance criteria.

## Contracts

- [Migration project contract](contracts/migration-project.md) — Phase A
  fields, lifecycle, source evidence, persistence, and browser safety boundary.
- [Source catalog contract](contracts/source-catalog.md) — Phase B file
  inventory, bounded preview, candidate types, statistics, and hash binding.
- [Source workspace and semantic-mapping contract](contracts/source-workspace.md)
  — source confirmation, frozen datasets, Odoo schema capture, invalidation,
  governed keys, relationships, semantic validation, and submissions.
- [Profile contract](contracts/profile.md) — how a profile maps,
  types, identifies, resolves, and compares data.
- [Prepared record contract](contracts/prepared-record.md) — the
  environment-independent boundary after source validation.
- [Snapshot contracts](contracts/snapshots.md) — environment
  fingerprint, model metadata, and target-record catalogs.
- [Preflight result contract](contracts/preflight-result.md) —
  classifications, field differences, reference evidence, and portable
  manifest rules.
- [Read connector contract](contracts/read-connector.md) — the intentionally
  narrow interface implemented by fixtures and live Odoo access.

## Operations and quality

- [Local-browser user guide for data analysts and data managers](operations/local-browser-user-guide.md)
  — non-technical, screenshot-led walkthrough with a complete companies and
  contacts relationship-mapping example.
- [Local project browser](operations/local-browser.md) — installation,
  project registration, source inspection, storage, and shutdown runbook.
- [DuckDB on a Windows laptop](operations/duckdb-windows.md) - approved
  user-local DuckDB CLI installation, verification, troubleshooting, and the
  boundary between the CLI and Impodo-managed data.

- [CLI and operating model](operations/cli.md) — commands, artifact flow,
  exit behavior, secrets, and runbook.
- [Examples and edge cases](examples-and-edge-cases.md) — copy-paste runs,
  profile patterns, expected outcomes, failure cases, and current limitations.
- [Acceptance and test strategy](testing/acceptance.md) — test layers, golden
  slice, determinism checks, and acceptance traceability.
- [Glossary](glossary.md) — canonical project terminology.

## Proof-of-concept status

These documents describe one current contract shape. There are no released
contract generations or compatibility guarantees yet. When the shape changes,
the examples, fixtures, generated artifacts, implementation, and documentation
change together.

All source, fixtures, examples, generated review packages, and documentation
belong under `/Users/francois/dev-impodo`. No commit, push, publication, or
deployment is performed by this milestone.
