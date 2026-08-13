# Impodo documentation

Choose the path that matches your work:

- [User documentation](user/README.md) guides data managers through the
  current browser workflow in business language.
- [Developer documentation](developer/README.md) maps the same workflow to
  routes, services, evidence, invalidation rules, performance boundaries, and
  tests.
- [Operations](#operations-and-quality) covers local Odoo, expert CLI, IT,
  release, and disposable remote acceptance responsibilities.

Accepted decisions and contracts are normative. Architecture describes the
current composition and boundaries; operations explain how to use it; testing
records evidence; the remaining-work plan and product vision describe future
delivery. Where an example conflicts with a **MUST**, **MUST NOT**, **SHOULD**,
or **MAY** rule, the normative rule wins.

## Workflow terminology

**Project setup** happens before the six registered-project stages:

1. **Source data**
2. **Odoo data**
3. **Match data**
4. **Prepare data**
5. **Final review**
6. **Load into Odoo**

An Odoo-source project presents the first two responsibilities as **Odoo source
data** and **Freeze Odoo records**. The architectural product vision uses
**Stages A–K** to describe the wider business lifecycle; those letters are not
browser navigation labels.

The machine-readable [workflow registry](workflow.yml) owns documentation,
route, template, code-symbol, contract, and focused-test coverage for each
stage. The [documentation style guide](style-guide.md) defines the audience and
voice rules.

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

## Plans

- [Impodo remaining work](plans/remaining-work.md) — the authoritative
  forward-looking roadmap for related/mixed 100,000-row preparation, optional
  clean-package certification, retained remote acceptance, production loading,
  and conditional gateway or hosted work.
- [Odoo source import and round-trip update implementation plan](plans/odoo-source-import-plan.md)
  — scoped proposal for selecting existing Odoo 19 records as immutable
  Impodo source data, transforming them, and applying guarded updates back to
  the same database.
- [High-volume transformation architecture implementation plan](plans/transformation-scale-architecture-plan.md)
  — weighted comparison of four scale architectures, with a phased proposal
  for reducing transformation CPU and memory, extending bounded preparation to
  related Products and BOMs, and retaining governed audit evidence.

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

The numbered runbooks are grouped by operating responsibility. Data managers
start with the [user documentation](user/README.md), use 01 as the complete
training tutorial, use 02 for a local target, and use 08 for generic
related-table authoring. Expert CLI users read 03 then 04; IT and release teams
use 05 and 06. Use 07 only for an explicitly disposable remote acceptance
target.

- [End-to-end local-browser tutorial](operations/01-local-browser-user-guide.md)
  — one complete fictional migration across the current browser workflow.
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
- [Remote Odoo 19 acceptance](operations/07-remote-odoo-acceptance.md) —
  opt-in sanitized remote load, read-back, repeat-preview, and throughput
  evidence against a disposable on-premises database.
- [Related-table authoring](operations/08-related-dataset-authoring.md) — the
  generic browser reference for extracting reusable records or separating
  repeated parent/child rows without editing frozen source data.
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

For workflow changes, update the paired user and developer stage pages and the
workflow registry. Also update the owning module/class/method docstrings and
the [Python code map](architecture/python-code-map.md). Run:

```console
python scripts/documentation_quality.py --check --report
python scripts/code_documentation_inventory.py --check
python scripts/code_documentation_inventory.py --missing
python -m unittest tests.test_documentation_quality tests.test_code_documentation
```

The workflow and module checks are blocking. The public-symbol list and Vale
style rules are advisory and require semantic review rather than percentage or
readability-score targets. When Vale is installed, run `vale docs/user
docs/developer` to apply the repository vocabulary and audience-specific prose
warnings.
