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
The project-local
[Impodo documentation skill](../.agents/skills/impodo-documentation/SKILL.md)
applies those rules when documentation is created or rewritten.

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

- [VM deployment with DuckDB and managed workers](plans/vm-deployment-duckdb-worker-management.md)
  — the proposed internal VM deployment, Microsoft Entra sign-in, retained
  local files, bounded preparation-worker coordination, recovery, and pilot
  acceptance plan.
- [Scalable relationship dependency planning and execution](plans/scalable-relationship-dependency-planning.md)
  — the proposed generic dataset and row dependency planner for hierarchies,
  Product relationships, BOM-shaped migrations, cycle handling, bounded Odoo
  calls, recovery, and scale qualification.
- [Impodo remaining work](plans/remaining-work.md) — the broad forward-looking
  delivery roadmap.
- [Combine source columns into one Odoo field](plans/concatenate-source-columns-matching-rule.md)
  — the implemented design record for the guided, reusable matching rule that
  joins two to five source columns without changing the accepted workbook.
- [Recipe runs in three pages](plans/recipe-run-three-page-ui-refactor.md) —
  the approved plan for applying an existing Recipe to fresh data and an Odoo
  target without repeating the six authoring stages.
- The separate browser-language proposal is active design work. Completed
  delivery history belongs in Git history; current behavior belongs in the
  architecture, contracts, and paired workflow pages.

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
- [Acceptance and test strategy](testing/acceptance.md) — test layers, golden
  slice, determinism checks, and acceptance traceability.
- [Code-organization regression baseline](testing/code-organization-phase0-baseline.md)
  — reproducible import, fixed-order, atomic-operation, bounded-I/O, browser,
  and test-organization gates for the implemented architecture.
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
