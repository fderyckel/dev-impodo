# Impodo documentation

Choose the path that matches your work:

- [User documentation](user/README.md) guides data managers through the
  current browser workflow in business language.
- [Developer documentation](developer/README.md) maps the same workflow to
  routes, services, evidence, invalidation rules, performance boundaries, and
  tests.

Accepted decisions and contracts are normative. Architecture describes the
current composition and boundaries; audience pages explain how to use it; testing
records evidence; the remaining-work plan and product vision describe future
delivery. Where an example conflicts with a **MUST**, **MUST NOT**, **SHOULD**,
or **MAY** rule, the normative rule wins.

## Workflow terminology

**Data project setup** creates the data project, Authoring data version, run,
and workspace before the six workspace stages. A Recipe is optional reusable
rules saved after eligible authoring work. The
[data-manager Concepts page](user/concepts.md) explains these relationships in
the same language as the browser:

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
voice rules, including the
[data-manager-first explanation standard](style-guide.md#data-manager-first-explanations)
and the [two-pass editing workflow](style-guide.md#documentation-editing-workflow).
Repository documentation and its CI checks depend only on tracked content;
local tooling below `.agents/` is intentionally outside that contract.

## Architecture

- [Architecture overview](architecture/overview.md) — current system context,
  browser and preflight boundaries, component layers, evidence flow,
  performance invariants, and deployment seams.
- [Code organization](architecture/code-organization.md) — current ownership,
  package placement, dependency direction, transaction ports, browser assets,
  test structure, and review rules for maintainers and coding agents.
- [Python code map](architecture/python-code-map.md) — navigation from browser
  and CLI entry points through services, domain behavior, repositories, and
  migration evidence.
- [Security and infrastructure](architecture/security-and-infrastructure.md) — factual
  overview of the local architecture, implemented controls, infrastructure
  requirements, data handling, verification evidence, and current limitations.
- [Server fact ownership](architecture/server-fact-ownership.md) — proposed
  PostgreSQL, DuckDB, artifact, local-only, and derived-projection authority
  for the shared-server profile, including cross-store recovery rules.
- [End-to-end migration product vision](product-vision.md) — complete product
  workflow, mapping architecture, staging, relation handling, executor
  boundary, edge cases, and roadmap.
- [Architecture decisions](decisions/README.md) — accepted decisions that
  constrain implementation.

## Process models

- [Current Impodo BPMN models](bpmn/README.md) — BPMN 2.0 overview and detailed
  diagrams for Project/workspace setup and all six implemented browser responsibilities,
  including the current file-source, Odoo-source, and disposable-target
  boundaries.

## Plans

The [remaining-work roadmap](plans/00-remaining-work.md) is the authoritative
priority list. Plans retain only unfinished delivery or qualification work.
Current behavior lives in architecture, contracts, and paired workflow pages.
Completed plans are removed after those current owners and links are verified.

The current delivery sequence is:

- [Linux server deployment architecture](plans/07-vm-deployment-duckdb-worker-management.md)
  defines the approved target, while the
  [detailed shared-server implementation plan](plans/07-linux-server-postgresql-duckdb-implementation.md)
  owns the active Phase 2 Linux container exit gate and the later ordered
  phases.
- [Generic Odoo-to-Odoo migration](plans/05-odoo-to-odoo-migration.md) retains
  Product, bill-of-material, empty-destination, interruption, recovery, and
  measured multi-model qualification beyond the completed Contact vertical.
- [Maintainable code and focused execution services](plans/15-maintainable-code-and-execution-services.md)
  retains the execution and reconciliation boundary refactor after the quality
  and dependency guardrails were delivered.
- [End-to-end trial and scenario qualification](plans/08-end-to-end-trial-and-scenario-qualification.md)
  retains normal lifecycle integration, Product and bill-of-material coverage,
  browser and remote lanes, recovery, and scheduling.
- [Six-stage Authoring workflow user support](plans/01-six-stage-workflow-user-support.md)
  retains root-cause correction, migration intent, and later setup assistance.
- [Preparation efficiency and storage](plans/11-preparation-efficiency-and-storage.md)
  retains bounded correctness work and clean measurements. The 100,000-row
  mixed and related expansion is separately deferred in the roadmap.

Qualification-only follow-up remains in the
[macOS save](plans/10-resilient-match-data-saving.md),
[workflow responsiveness](plans/09-responsive-workflow-liveness-and-navigation.md),
[hierarchy-root](plans/04-hierarchy-root-null-scope-and-load-order.md),
[Match data ordering](plans/12-smart-match-data-ordering.md), and
[row-inclusion](plans/13-source-row-inclusion-rules.md) plans.

Long-term deferred proposals remain in
[Odoo 19 and Odoo 20 support](plans/14-odoo-19-and-20-support.md) for the Odoo 20
Production gate and in
[known-fallout repair](plans/06-load-fallout-source-cell-workbook.md) for an
exact-record repair writer. Listing either plan does not activate it.

Two dated reports remain beside the active server plans because they directly
qualify that program: the
[shared-server Phase 0 baseline](plans/90-2026-10-22-shared-server-phase-0-baseline.md)
and the
[local empty-server container rehearsal](plans/91-2026-10-27-local-empty-server-container-rehearsal.md).
Other completed delivery detail remains in Git history.

## Guides, runbooks, and quality

Data-manager guides live under [user documentation](user/README.md). Technical
setup, CLI, release, and acceptance procedures live under
[developer documentation](developer/README.md).

- [End-to-end local-browser tutorial](user/tutorials/end-to-end-training.md)
  — one complete fictional migration across the current browser workflow.
- [Windows installation](user/installation/windows.md) — step-by-step GitHub
  evaluation and accepted internal release routes.
- [macOS installation](user/installation/macos.md) — step-by-step GitHub
  checkout, local project-data storage, dependency verification, and launch.
- [Local Odoo guide](user/guides/local-odoo.md) — local target readiness,
  ownership-aware start and stop behavior, and troubleshooting.
- [Local Odoo technical runbook](developer/runbooks/local-odoo.md) — process
  ownership, restart safeguards, and technical troubleshooting.
- [Profile authoring](developer/cli/profile-authoring.md) — strict YAML datasets,
  business identities, fields, relationships, and validation workflow.
- [Preflight CLI runbook](developer/cli/preflight.md) — safe profile-driven snapshot
  and offline classification sequence, evidence rules, and exit behavior.
- [Scenario qualification](developer/runbooks/scenario-qualification.md) —
  current profile-driven offline canary and explicitly confirmed local Contact
  round trip, durable evidence, safety gates, and remaining limits.
- [Windows developer setup](developer/setup/windows.md)
  — IT provisioning, installation boundaries, Odoo access, and verification.
- [Internal development and release runbook](developer/runbooks/internal-release.md) —
  development setup, authoritative dependency locking, promotion, evidence,
  and installation of an accepted internal bundle.
- [Remote Odoo 19 acceptance](developer/runbooks/remote-odoo-acceptance.md) —
  opt-in sanitized remote load, read-back, repeat-preview, and throughput
  evidence against a disposable on-premises database.
- [Related-table authoring](user/guides/related-tables.md) — the
  generic browser reference for extracting reusable records or separating
  repeated parent/child rows without editing frozen source data.
- [Developer examples and edge cases](developer/reference/examples-and-edge-cases.md)
  — CLI runs, profile patterns, prepared records, classifications, issue codes,
  and connector cases.
- [Python code quality](developer/reference/python-code-quality.md) — pinned
  Ruff and Basedpyright ownership, baseline policy, local gate, CI enforcement,
  and safe remediation rules for developers and coding agents.
- [Platform support and testing](developer/reference/platform-support-and-testing.md)
  — portable test ownership, host-specific suites, CI lanes, and temporary-file
  rules for Windows, macOS, and Linux server development.
- [Glossary](glossary.md) — canonical project terminology.

## Documentation maintenance

Keep one active authority for each concept. When a contract changes, update
its implementation, fixtures, examples, generated artifacts, tests, and links
together. Label proposals and historical delivery documents explicitly; use
Git history instead of retaining stale architecture summaries in the active
documentation tree.

For workflow changes, update the paired user and developer stage pages and the
workflow registry. Also update the owning module/class/method docstrings and
the [Python code map](architecture/python-code-map.md).

For each conceptual rewrite, first review whether a data manager can understand
the goal, objects, lifecycle, practical effect, and next action. Then perform a
separate precision pass for exact labels, current status, safeguards, evidence
boundaries, links, symbols, and tests. Do not make the user page carry technical
inventory that belongs in its paired developer page.

Run:

```console
python scripts/documentation_quality.py --check --report
python scripts/code_documentation_inventory.py --check
python scripts/code_documentation_inventory.py --missing
python -m unittest tests.architecture.test_documentation_quality tests.architecture.test_code_documentation
```

The workflow and module checks are blocking. The public-symbol list and Vale
style rules are advisory and require semantic review rather than percentage or
readability-score targets. Run `vale docs` to check every documentation lane.
Vale accepts Impodo and Odoo terminology, but it asks writers to explain
implementation terms and internal data-model terms before using them in user
documentation. Treat each Vale alert as a review prompt: correct unclear prose,
preserve exact example data and identifiers, and improve the rule when it
produces a repeatable false positive.
