# Impodo documentation

This index covers the current local browser workflow, the profile-driven
read-only preflight engine, and the future migration product.

Accepted decisions and contracts are normative. Architecture describes the
current composition and boundaries; operations explain how to use it; testing
records evidence; plans and the product vision describe future or historical
delivery. Where an example conflicts with a **MUST**, **MUST NOT**, **SHOULD**,
or **MAY** rule, the normative rule wins.

## Terminology

The browser uses named workflow steps: **Project setup**, **Source data**,
**Select tables**, **Odoo fields**, **Match fields**, and **Review**. The
product vision uses **Stages A–K** for the end-to-end business lifecycle.
Numeric **Phases** describe delivery increments only: Phase 1, Phase 2A,
Phase 2B, Phase 2C.1, and later roadmap phases. Do not use Phase A or Phase B.

## Architecture

- [Architecture overview](architecture/overview.md) — current system context,
  browser and preflight boundaries, component layers, evidence flow,
  performance invariants, and deployment seams.
- [Python code map](architecture/python-code-map.md) — navigation from browser
  and CLI entry points through services, domain behavior, repositories, and
  migration evidence.
- [Security and infrastructure](architecture/security-and-infrastructure.md) — factual
  overview of the local architecture, implemented controls, infrastructure
  requirements, data handling, verification evidence, and current limitations.
- [End-to-end migration product vision](product-vision.md) — complete product
  workflow, mapping architecture, staging, relation handling, executor
  boundary, edge cases, and roadmap.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.

## Active plans

- [Data-quality and staging delivery plan](plans/data-quality-and-staging-plan.md)
  — current integration gap, delivery slices, acceptance requirements, and
  decisions for full-row staging through clean-package certification.
- [100,000-row performance refactor plan](plans/100k-performance-refactor-plan.md)
  — measured path from the current 25,000-row materializing workflow to
  bounded 100,000-row preparation in less than two minutes.
- [Data-quality coverage ledger](plans/data-quality-coverage.md) — current
  status of 24 capability families and the authoritative clean-package gates.
- [Slice 4 normalization review plan](plans/slice-4-normalization-review-plan.md)
  — implemented grouped prepared-value review, immutable decisions, exact
  eligible-dataset freeze, and the gate before read-only Odoo comparison.
- [Slice 5 durable preflight plan](plans/slice-5-durable-preflight-plan.md)
  — implemented adapter from approved durable rows to bounded read-only Odoo
  comparison, protected target evidence, and plain data-manager results.
- [Python code documentation plan](plans/python-code-documentation-plan.md) —
  phased module, class, method, and call-flow documentation for `src/impodo/`,
  organized around migration Stages A–K.

## Contracts

The numbered contracts form the recommended conceptual reading order.

- [Migration project contract](contracts/01-migration-project.md) — project
  lifecycle, registration requirements, source/target evidence, audit, and
  persistence boundary.
- [Browser workspace contract](contracts/02-workspace.md) — source inspection,
  confirmation, dataset freezing, target schema, governed mapping,
  invalidation, validation, and submission.
- [Canonical staging evaluation contract](contracts/03-canonical-staging.md) —
  full-row canonical evidence, complete grouped-row lineage, reconciliation,
  deterministic hashing, and atomic project-scoped DuckDB publication.
- [Profile-driven preflight contract](contracts/04-preflight.md) — strict
  profile, typed preparation, closed Odoo reads, snapshots, classification,
  and portable review evidence.
- [Normalization dry-run governance contract](contracts/05-normalization-governance.md)
  — implemented standalone approval lifecycle, explicit integration status,
  and the boundary between source approval and Odoo authorization.
- [Quality and quarantine contract](contracts/06-quality-and-quarantine.md) —
  integrated data checks, complete source/canonical accounting, immutable
  quarantine evidence, and the eligible-row boundary before Odoo comparison.

## Operations and quality

The numbered runbooks are grouped by reader journey. Browser users start with
01 and use 02 for a local target; expert CLI users read 03 then 04; IT and
release teams use 05 and 06.

- [Local-browser user guide](operations/01-local-browser-user-guide.md) — concise
  walkthrough of the current read-only browser workflow and its limits.
- [Local Odoo runbook](operations/02-local-odoo.md) — local target readiness,
  ownership-aware start and stop behavior, and troubleshooting.
- [Profile authoring](operations/03-profile-authoring.md) — strict YAML datasets,
  business identities, fields, relationships, and validation workflow.
- [Preflight CLI runbook](operations/04-cli.md) — safe profile-driven snapshot
  and offline classification sequence, evidence rules, and exit behavior.
- [Windows workstation readiness](operations/05-windows-workstation-readiness.md)
  — IT provisioning, installation boundaries, Odoo access, and verification.
- [Internal development and release runbook](operations/06-internal-release.md) —
  development setup, authoritative dependency locking, promotion, evidence,
  and installation of an accepted internal bundle.
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
