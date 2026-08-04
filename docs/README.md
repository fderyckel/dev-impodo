# Impodo documentation

This index covers the current local browser workflow, the profile-driven
read-only preflight engine, and the future migration product.

Accepted decisions and contracts are normative. Architecture describes the
current composition and boundaries; operations explain how to use it; testing
records evidence; plans and the product vision describe future or historical
delivery. Where an example conflicts with a **MUST**, **MUST NOT**, **SHOULD**,
or **MAY** rule, the normative rule wins.

## Terminology

The browser uses named workflow steps: **Project setup**, **Source discovery**,
**Target schema**, and **Governed mapping**. The product vision uses **Stages
A–K** for the end-to-end business lifecycle. Numeric **Phases** describe
delivery increments only: Phase 1, Phase 2A, Phase 2B, Phase 2C.1, and later
roadmap phases. Do not use Phase A or Phase B.

## Architecture

- [Architecture overview](architecture/overview.md) — current system context,
  browser and preflight boundaries, component layers, evidence flow,
  performance invariants, and deployment seams.
- [Security and infrastructure](architecture/security-and-infrastructure.md) — factual
  overview of the local architecture, implemented controls, infrastructure
  requirements, data handling, verification evidence, and current limitations.
- [End-to-end migration product vision](product-vision.md) — complete product
  workflow, mapping architecture, staging, relation handling, executor
  boundary, edge cases, and roadmap.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.

## Plans and delivery history

- [Implementation plan](plans/implementation-plan.md) — package layout, sequence,
  deliverables, and definition of done.
- [Historical delivery Phase 2B relationship and semantic-validation proposal](plans/phase-2-relationship-semantic-validation-proposal.md)
  — the implemented relationship-mapping increment, its contracts, and its
  acceptance criteria; current delivery status is in the product vision.
- [Data-quality rules implementation plan](plans/data-quality-rules-implementation-plan.md)
  — phased delivery of governed corrections, structural transformations,
  entity resolution, Odoo-aware validation, package quality gates, and
  acceptance criteria.
- [Data-quality coverage specification](plans/data-quality-coverage.md) —
  proposed 24-family coverage measure and clean-package release gates.

## Contracts

- [Migration project contract](contracts/migration-project.md) — project
  lifecycle, registration requirements, source/target evidence, audit, and
  persistence boundary.
- [Browser workspace contract](contracts/workspace.md) — source inspection,
  confirmation, dataset freezing, target schema, governed mapping,
  invalidation, validation, and submission.
- [Profile-driven preflight contract](contracts/preflight.md) — strict profile,
  typed preparation, closed Odoo reads, snapshots, classification, and
  portable review evidence.
- [Normalization dry-run governance contract](contracts/normalization-governance.md)
  — implemented standalone approval lifecycle, explicit integration status,
  and the boundary between source approval and Odoo authorization.

## Operations and quality

- [Local-browser user guide for data analysts and data managers](operations/local-browser-user-guide.md)
  — non-technical, screenshot-led walkthrough with a complete companies and
  contacts relationship-mapping example.
- [Windows workstation requirements for Impodo](operations/windows-workstation-readiness.md) —
  IT provisioning requirements, required software, workstation permissions,
  network access, installation boundaries, and verification checks.
- [Internal development and release runbook](operations/internal-release.md) —
  Python 3.12 development mode, dependency locking, evidence-producing
  promotion, and versioned installation of an accepted internal bundle.
- [DuckDB on a Windows laptop](operations/duckdb-windows.md) - approved
  user-local DuckDB CLI installation, verification, troubleshooting, and the
  boundary between the CLI and Impodo-managed data.

- [CLI and operating model](operations/cli.md) — commands, artifact flow,
  exit behavior, secrets, and runbook.
- [Profile authoring](operations/profile-authoring.md) — strict YAML profile
  structure, mapping examples, validation rules, and inspection commands.
- [Derived-entity authoring](derived-entity-authoring.md) — the implemented
  browser slice for related-entity datasets extracted from denormalized source
  fields, plus its execution boundary.
- [Examples and edge cases](examples-and-edge-cases.md) — copy-paste runs,
  profile patterns, expected outcomes, failure cases, and current limitations.
- [Acceptance and test strategy](testing/acceptance.md) — test layers, golden
  slice, determinism checks, and acceptance traceability.
- [Design QA evidence](testing/design-qa.md) — point-in-time visual fidelity
  findings, completed checks, and blocked browser-verification evidence.
- [Glossary](glossary.md) — canonical project terminology.

## Documentation maintenance

Keep one active authority for each concept. When a contract changes, update
its implementation, fixtures, examples, generated artifacts, tests, and links
together. Label proposals and historical delivery documents explicitly; use
Git history instead of retaining stale architecture summaries in the active
documentation tree.
