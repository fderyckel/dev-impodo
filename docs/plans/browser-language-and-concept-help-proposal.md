# Browser language and concept help proposal

## Status and authority

**Status:** Implemented on 2026-08-23. The canonical language, shared Concepts
page, contextual dialogs, Project/workspace and run-language audits,
documentation, and current visual evidence are in place.

**Proposal date:** 2026-08-23.

The current Project and Recipe lifecycle contracts remain authoritative. This
proposal defines how the browser can explain those contracts to a
data-informed, non-technical data manager without exposing the internal object
model as the normal working language.

## 1. Decision summary

Impodo should combine three changes:

1. The browser should use one canonical term and one canonical meaning for each
   core product concept.
2. A permanent **How Impodo organizes your migration** page should explain the
   complete mental model in plain language.
3. A small number of question-mark help links should open focused dialogs at
   the first important use of a concept. Each link should still open the full
   concepts page when JavaScript is unavailable.

The normal page must remain understandable without opening help. The dialogs
provide reassurance and a deeper explanation; they must not carry information
that the data manager needs in order to make a safe decision.

## 2. Reader and outcome

The primary reader is a data manager who understands the migration but does
not need to understand Impodo's persistence architecture.

After this work, the data manager should be able to explain:

- what a data project keeps together;
- what one data version represents;
- what work happens in a workspace;
- when saving a Recipe is useful and when it is unnecessary;
- why Test and Production use fresh data, access, approvals, and results; and
- what the next browser action changes and what it leaves unchanged.

## 3. Current findings

The current browser has a strong underlying ownership model, but its visible
language sometimes asks the data manager to translate internal terms.

The review covered all 37 HTML templates under
`src/impodo/web/templates`. The first pass found these examples:

| Current pattern | Why it creates friction | Proposed direction |
| --- | --- | --- |
| `DataVersion` appears on the Project overview, while nine templates use **data version**. | One concept appears as both a code identifier and a business term. | Use **data version** in all normal browser copy. Reserve `DataVersion` for developer and support material. |
| The templates alternate among **source package**, **data package**, and **data version**. | The data manager cannot tell whether these names describe the same object. | Use **one delivery of source data** when introducing the idea, then use **data version** consistently. |
| The Project overview says a workspace “belongs to DataVersion.” | The wording implies ownership that the architecture does not define. The Project contains both objects; the workspace uses one data version. | Say that the workspace **uses Data version 1**. |
| The browser alternates between **save a Recipe** and **publish a Recipe**. | Two verbs make one user action appear to have two different meanings. | Use **save** in the normal browser path. Reserve **publish** for developer contracts and support details. |
| **Authoring workspace**, **application workspace**, **Recipe application**, and **Cutover plan** appear without a nearby explanation. | These terms are meaningful only after the reader understands the Project, data version, workspace, Recipe, and run relationships. | Explain the base model first. Add contextual help only at the first decision where an advanced term matters. |
| The existing glossary is developer-oriented. | Terms such as canonical typed atom, semantic hash, and target fingerprint do not answer the data manager's immediate questions. | Keep the developer glossary. Add a separate data-manager concepts page in the browser and user documentation. |

Impodo already uses native HTML dialogs for local Odoo checks, confirmations,
and value matching. The new help pattern can reuse the established visual and
keyboard conventions, but it should use a shared generic component instead of
adding one JavaScript handler per term.

## 4. Canonical browser language

The browser should adopt the following display-language contract.

| Browser term | Meaning for the data manager | Terms to keep out of the normal path |
| --- | --- | --- |
| **Data project** | One migration effort. It keeps the source deliveries, workspaces, optional Recipes, and migration runs together. | `MigrationProject`, aggregate root, Project-owned lineage |
| **Data version** | One complete delivery of source data that Impodo accepts and keeps unchanged. | `DataVersion`, source package, data package, package hash |
| **Workspace** | The working area where the data manager prepares one data version for one use. A workspace uses the accepted data; it does not replace or own it. | `MigrationWorkspace`, projection, workspace store |
| **Recipe** | Optional reusable preparation, matching, relationship, and checking rules. A Recipe does not contain source rows, Odoo access, approvals, or migration results. | semantic envelope, compiled meaning, payload hash |
| **Recipe version** | One saved version of a Recipe. A later change creates another version instead of changing the saved version in place. | `RecipeRevision`, optimistic revision |
| **Test run** | One coordinated rehearsal that applies selected Recipe versions to an accepted Test data version and one reviewed Odoo target. | `MigrationRun`, union requirement plan |
| **Recipe work area** | The separate workspace where one selected Recipe is applied during a Test or Production run. Use **Recipe application** only in optional detail when the exact domain term helps. | application projection, application workspace identity |
| **Cutover plan** | The reviewed Recipe versions, order, write ownership, and controls proved by the Test run. Selecting it does not authorize Production. | `CutoverPlanRevision`, semantic requirement set |
| **Production run** | A fresh use of the selected plan with the latest source data and separate Odoo access, comparison, approval, load, and verification. | production binding, activation generation |

The casing should also be deliberate. Normal sentences should use **data
project**, **data version**, and **workspace** as common nouns. Impodo should
retain **Recipe**, **Test**, and **Production** as named product concepts. The
browser should use **Cutover plan** consistently rather than alternating with
**Cutover Plan**.

## 5. Recommended concept model

The full concepts page should introduce the smallest accurate model before it
explains Test and Production:

```text
Data project
|-- Data versions: complete deliveries of source data
|-- Workspaces: working areas that each use one data version
|-- Optional Recipes: reusable rules, without source rows or access
`-- Runs: Test or Production outcomes created from selected data and rules
```

A short fictional example should make the model concrete:

> The Customer migration project starts with a small Authoring data version.
> You prepare it in a workspace and save a Customer Recipe because the same
> rules will be useful again. The Test run uses a separate Test data version.
> The Production run later uses the latest customer export and separate Odoo
> access. The Recipe supplies reusable rules, but it supplies none of the Test
> or Production data, credentials, approvals, or results.

The page should end the first explanation with this practical consequence:

> **For you, this means:** You can complete one migration without saving a
> Recipe. Save a Recipe only when you expect to reuse the preparation and
> matching rules with another delivery of suitable data.

## 6. Contextual help pattern

### 6.1 Placement

Impodo should place a help link beside a heading or the first important use of
a concept. It should not place a question mark after every repeated term.

The initial slice should add only these three entry points:

1. **Data projects** on the Project list should open the complete Project,
   data-version, workspace, and Recipe model.
2. **Authoring workspace** on the Project overview should explain how the
   workspace uses Data version 1 and why Recipe publication is optional.
3. **Recipes** on the Project overview should explain what a Recipe saves and
   what remains with the data project.

Later entry points should be added only where a term introduces a real
decision, such as the first Integrated Test plan or the first Cutover plan
selection. A page should normally show no more than one help entry for the
same concept.

### 6.2 Interaction and accessibility

Each question-mark control should be a real link with:

- a fallback destination such as `/concepts#data-version`;
- `aria-haspopup="dialog"` and `aria-controls` for the enhanced dialog;
- an accessible name such as **Explain data versions**;
- a question-circle icon that is hidden from assistive technology; and
- a comfortable pointer target even when the visible icon remains small.

JavaScript should enhance the link by opening a native `<dialog>`. The dialog
should have a unique labelled heading, a visible close button, Escape support,
and reliable focus return to the opening link. Closing the dialog should not
change the page, the project, or any evidence.

Hover-only tooltips are not sufficient. They are difficult to use with touch,
keyboard navigation, magnification, and screen readers, and they do not provide
enough room for the relationship model.

### 6.3 Dialog content

Each dialog should answer the same five questions in the same order:

1. What is this?
2. What does it contain or use?
3. What does it not contain when that distinction prevents confusion?
4. How does it relate to the other core concepts?
5. What does this mean for the data manager's current decision?

The dialog should provide one secondary link, **See all concepts**, and one
obvious closing action. It should not introduce workflow buttons that compete
with the page's main action.

## 7. Proposed copy for the three current examples

| Location | Proposed normal page copy | Help topic |
| --- | --- | --- |
| Project list | **Data projects** — “A data project keeps one migration effort together. It contains each delivery of source data, the workspaces used to prepare it, and any Recipes you decide to save.” | **How a data project is organized** shows the four-part concept model. |
| Project overview | **Authoring workspace** — “This workspace uses Data version {{ number }}. You can complete this migration once without saving a Recipe.” | **Why this workspace uses a data version** explains that the Project contains both and that accepting data does not save reusable rules. |
| Project overview | **Recipes** — “A Recipe saves reusable preparation, matching, relationship, and checking rules. The data project continues to keep its data versions and workspaces; saving a Recipe does not copy or move them.” | **What a Recipe saves** lists the reusable rules and the excluded data, access, approvals, and results. |

These sentences deliberately remain useful when the data manager never opens
the help dialog.

## 8. One source for every explanation

The concepts page and contextual dialogs should render from one reviewed
content registry. Each concept entry should provide:

- a stable slug;
- the canonical display term;
- a short page explanation;
- a fuller dialog/page explanation;
- important containment and exclusion statements;
- a fictional example; and
- related concept slugs.

The implementation may represent this registry as immutable presentation data
in a dedicated presenter module. It should not store this static language in a
database, open a Project workspace, call Odoo, or fetch one explanation per
help icon. The Project list must retain its current bounded query behavior and
must not introduce an N+1 read.

The normal HTML should use a shared Jinja macro or partial for the help link and
dialog markup. One generic JavaScript listener should enhance every registered
help link.

## 9. HTML language review method

After the initial help pattern is approved, the browser copy review should
cover every visible string in all 37 templates. The review should classify
each string as one of these types:

- a core concept;
- a workflow action;
- a status;
- a safety or evidence boundary; or
- optional support detail.

For each template, the reviewer should confirm:

1. The page states the data manager's goal before it names architecture
   objects.
2. Each object uses the canonical browser term and expresses the correct
   ownership or use relationship.
3. The same action uses the same verb. In particular, **accept** changes a data
   version from draft evidence to an immutable delivery, **save** creates a
   reusable Recipe or Recipe version, **compare** remains read-only, and
   **load** performs the explicit Odoo write.
4. The page uses the exact workflow states **Complete**, **Needs attention**,
   **Current**, and **Not yet available** where those states apply. Internal
   states such as `DRAFT`, `REGISTERED`, and `FROZEN` should not appear without
   a business explanation.
5. One sentence carries one main idea and identifies the actor, action, and
   result.
6. The page explains what its main action changes and what remains unchanged.
7. Optional support details contain the technical terms that the data manager
   does not need in the normal path.
8. Help remains sparse, keyboard-accessible, and useful without becoming a
   second navigation system.

The review should record findings in a deterministic inventory with the
template, visible phrase, concept category, problem, approved replacement, and
review status. This inventory will make later language drift visible instead
of relying on a one-time rewrite.

## 10. Delivery slices

### Slice 1: Approve the semantic contract

- Approve the canonical browser terms and casing in section 4.
- Approve **save** as the user verb and **publish** as the developer term.
- Approve the full-page plus contextual-dialog pattern.
- Review the fictional example with a data manager before implementation.

### Slice 2: Build the shared help foundation

- Add the static concept registry and shared Jinja components.
- Add a global **Concepts** link that remains available with or without an
  active Project.
- Add the `/concepts` page with stable anchors for deep links.
- Add one generic dialog enhancer in `app.js` and shared styles in
  `workflow-pages.css`.
- Add the three initial help links and the revised copy from section 7.

### Slice 3: Audit the Project and workspace workflow

- Review `base.html`, Project list, Project creation, Project overview, and the
  six workspace-stage templates first.
- Replace `DataVersion`, source package, and data package in normal browser
  copy with the approved data-manager language.
- Align save, accept, compare, and load verbs without changing the underlying
  operations.

### Slice 4: Audit Integrated Test and Production

- Explain Recipe work areas, Cutover plans, qualification, and fresh
  Production evidence only after the base model is stable.
- Keep the rule that Test evidence does not grant Production authority.
- Verify that each run page explains shared run evidence without exposing the
  internal persistence model.

### Slice 5: Synchronize documentation and visual evidence

- Add a data-manager concepts page under `docs/user/` and link it from the
  setup guide.
- Update the paired setup and run developer pages with the shared content and
  performance boundaries.
- Register the new shared route and documentation in `docs/workflow.yml`.
- Refresh affected user screenshots from isolated fictional data.

## 11. Expected implementation surface

The likely implementation will affect:

- `src/impodo/web/templates/base.html`;
- `src/impodo/web/templates/project_list.html`;
- `src/impodo/web/templates/project_business_overview.html`;
- a new concepts page and a shared concept-help partial;
- `src/impodo/web/static/app.js` and `workflow-pages.css`;
- a shared concepts presenter and a read-only concepts route;
- setup and browser-focused tests;
- `docs/user/getting-started.md`, a new data-manager concepts page, the paired
  developer pages, `docs/workflow.yml`, and current screenshots.

This work should not change domain objects, saved Projects, DataVersions,
Recipes, mappings, evidence, Odoo credentials, or Odoo read/write behavior.

## 12. Verification and acceptance gates

The initial implementation is ready only when:

- every initial help link works as a normal deep link without JavaScript;
- JavaScript opens the correct native dialog and returns focus on close;
- keyboard and screen-reader names describe the concept instead of saying only
  “question mark”;
- the page remains understandable without opening the dialog;
- the full page and the dialogs render from the same reviewed concept content;
- no normal browser copy uses `DataVersion`, `MigrationProject`,
  `MigrationWorkspace`, source package, or data package;
- Project-list and overview query-count tests prove that concept help adds no
  per-row read and no Odoo call;
- current CSRF, session, auto-escaping, and local-only data boundaries remain
  unchanged;
- focused setup and browser tests pass;
- the documentation quality check, documentation tests, and `git diff --check`
  pass; and
- affected pages receive keyboard, narrow-screen, and 1440x1024 visual review.

## 13. Decisions adopted

The implementation adopts these three decisions:

1. Use the recommended hybrid of a permanent concepts page and sparse
   contextual dialogs.
2. Adopt **data project**, **data version**, **workspace**, and **Recipe** as the
   canonical browser terms, while keeping the implementation class names out
   of the normal path.
3. Use **save** for the data manager's Recipe action and reserve **publish** for
   developer and support material.

Implementation evidence is recorded in the
[browser language review](../reports/browser-language-review-2026-08-23.md),
the [data-manager Concepts guide](../user/concepts.md), and
`tests/integration/web/test_concept_help.py`.

## Related current authority

- [Documentation style guide](../style-guide.md)
- [Architecture overview](../architecture/overview.md)
- [Project lifecycle contract](../developer/contracts/project-lifecycle.md)
- [Recipe publication contract](../developer/contracts/recipe-lifecycle.md)
- [Project setup user guide](../user/getting-started.md)
- [Project setup developer guide](../developer/workflow/00-project-setup.md)
