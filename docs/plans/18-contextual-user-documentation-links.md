---
audience: developer
kind: plan
status: active
---

# Link every relevant user guide from the place where a data manager needs it

## Status, reader, and decision

**Status:** The page-level and reverse-coverage foundation was implemented on
2026-10-29. Every registered workflow template now resolves a static guide,
the authenticated **Help and guides** page lists every current user document,
release builds use version-matched GitHub links, and documentation checks fail
for an unowned guide or workflow template. Decision-level anchors beside the
most difficult controls remain proposed.

This plan is for the product owner and the developers who maintain Impodo's
browser workflow and user documentation. It defines how a data manager should
reach the right user guide without leaving their current decision to search the
documentation index.

The decision is to make documentation coverage work from the user guide back
to the interface. Every current user guide that explains an Impodo page, step,
or decision must name at least one in-product help location. A new eligible
user guide is incomplete until Impodo has a question-mark link to it. A new
browser decision is incomplete when its existing guide cannot be reached from
the place where the data manager may need help.

The first delivery should add one stable page-level question-mark link to each
primary setup and workflow page. Later deliveries should add local links only
beside decisions for which the page-level guide is too broad. This keeps help
easy to find without covering forms and tables with icons.

## Data-manager outcome

When a data manager feels blocked, they should be able to select a subtle
question mark beside the page title or the decision they are reviewing. Impodo
should open the exact, version-matched GitHub user guide in a new browser tab.
The current Impodo page, its scroll position, and any unsaved form values
should remain available in the original tab.

For example, a data manager who is unsure how to configure a Many2one field
should not need to search the documentation tree. They should select the help
link beside the relationship decision and arrive at **How do I connect a field
to another record?** in the Match data questions-and-answers guide.

## Current foundation and gap

Impodo already has a useful contextual concept-help pattern. Selected project,
Test, and Production headings show an information control that opens a local
dialog. The full **Concepts** page provides a no-JavaScript fallback. This
pattern proves the visual language, keyboard behavior, focus return, and
minimum touch target.

The current pattern does not provide systematic access to the GitHub user
guides. The six Authoring stages, supporting guides, and detailed Match data
questions do not share one documentation-link contract. A documentation page
can therefore be added or renamed without any check that a data manager can
reach it from Impodo.

`docs/workflow.yml` already pairs the implemented setup, Authoring stages,
integrated Test, qualification, and Production pages with their owning user
documents and templates. The implementation should extend that ownership
model instead of creating an unrelated help catalogue.

## Interface contract

### One predictable page-level location

Every primary browser page should place its page-level question mark in the
main heading row, immediately after the visible `h1`. It should remain close
to the title instead of floating at the far edge of a wide screen.

```text
Stage 3 of 6 · Match data
Match your data to Odoo  [?]
```

The page-level link should open the guide that explains the goal, normal path,
completion state, and recovery path for that page. A status badge can remain at
the right side of the heading. The help link should not compete visually with
the primary action or status.

### Add local help only at difficult decisions

A page can also show a local question mark after a section heading or field
group label when the page-level guide would make the data manager search
through a long document.

```text
How should Impodo find the related record?  [?]
[ Use Odoo records first                     v ]
```

Local links should target a stable section anchor. They should be used for
decisions such as source-table composition, business identities,
relationships, row inclusion, normalization, comparison blockers, recovery,
and transfer ordering. They should not appear in every table row, beside
ordinary text fields, or beside controls whose label and nearby explanation
already answer the likely question.

### Direct-link behavior

Selecting a documentation question mark should:

- open the exact GitHub Markdown page, and the exact section when the link is
  local to one decision;
- open a new tab with `target="_blank"` and `rel="noopener noreferrer"` so an
  unsaved Impodo form remains intact;
- use a descriptive accessible name such as **Open the Match data guide on
  GitHub in a new tab**;
- show the guide title in a tooltip on pointer hover and keyboard focus;
- remain a normal link when JavaScript is unavailable; and
- perform no mutation, form submission, database read, Odoo call, or tracking
  request.

The hit target should be at least 44 by 44 CSS pixels even when the visible
question-circle icon is smaller. The link needs the same visible keyboard focus
style as current concept help. On narrow screens, it should remain beside the
heading or wrap directly below it before the status badge. It must not overlap
page actions or force horizontal scrolling.

### Keep one meaning for the question mark

The completed design should not make identical question marks behave
differently. Documentation question marks should always open documentation.
The existing concept dialogs should therefore move to a clearly labelled
**Concepts** or information control, or their question marks should be migrated
to the matching GitHub concepts anchors. The local concepts page can remain as
an offline, authenticated reference, but it should not make the external-help
question mark ambiguous.

## Link to documentation that matches the running build

A released Impodo build should link to the matching Git tag on GitHub, not to
whatever happens to be on `main` later. A source-checkout development build
may use its commit identity or `main` when no release tag is available. The
public repository base, documentation reference, document path, and anchor
should be combined by one shared URL builder.

For example, a released link would have this shape:

```text
https://github.com/fderyckel/dev-impodo/blob/<release-tag>/docs/user/workflow/03-match-data.md#choose-which-source-rows-belong-to-the-migration
```

The URL builder must accept only reviewed repository-relative user-document
paths and anchors from the help registry. It must never copy a workspace ID,
project ID, query string, source value, Odoo value, credential, or current page
URL into the GitHub link.

If the configured repository is private or the browser has no network access,
the normal GitHub sign-in or offline behavior may appear in the new tab. Impodo
should not delay page rendering to test GitHub availability. The global
**Concepts** page can continue to provide the core product model locally.

## Documentation-led coverage contract

The implementation should add one machine-readable help registry. Each target
should contain:

- a stable help identifier;
- the repository-relative path below `docs/user/`;
- an optional stable section anchor;
- the visible guide title;
- the accessible link name; and
- whether the target is page-level, decision-level, global, or pre-launch.

Templates and presenters should refer to a help identifier instead of
constructing GitHub URLs themselves. The renderer should reject an unknown
identifier rather than produce a best-effort link.

The documentation checks should enumerate every current Markdown page below
`docs/user/` and require one of these outcomes:

1. **Contextual:** At least one rendered question-mark link owns the guide.
2. **Global:** The authenticated Help area owns a guide that is useful across
   several pages rather than at one decision.
3. **Pre-launch:** The page explains installation or another task that must be
   completed before an Impodo page exists. The registry records the reason and
   the global Help area still links to it for later reference.

An eligible page must not be silently excluded. A pull request that adds a
user guide without one of these classifications should fail the documentation
gate. A removed or renamed heading should fail when a registered anchor no
longer exists. A help identifier with no rendered placement should also fail.

The same checks should verify that every `user_doc` in `docs/workflow.yml` has
at least one page-level owner. This preserves the existing workflow ownership
instead of making the help registry a second description of the stages.

## Initial guide-to-interface map

The following map defines the first complete coverage target. The exact
template list can change during implementation, but the data-manager location
and owning user page should remain clear.

| User documentation | Primary question-mark location | Additional local locations |
| --- | --- | --- |
| `docs/user/getting-started.md` | Data projects, create-project, project overview, and initial workspace setup headings | Data version acceptance and workspace creation decisions |
| `docs/user/concepts.md` | Global **Concepts** or Help area and the existing data project, workspace, Recipe, Test, Cutover plan, and Production concept locations | Matching concept anchors for the object beside the control |
| `docs/user/workflow/01-source-data.md` | **Source data**, file inspection, dataset choice, and Odoo source-freeze page headings | File-reading options, source-table choice, related capture scope, and accepted Data version decisions |
| `docs/user/workflow/02-odoo-data.md` | **Odoo data** or **Select data to download** page heading | Record-type scope, business identity, supporting data, and schema refresh decisions |
| `docs/user/workflow/03-match-data.md` | **Match data** page heading | Identity health, relationship health, matching order, row inclusion, transformation review, validation, and interrupted-save recovery |
| `docs/user/workflow/04-prepare-data.md` | **Prepare data**, preparation progress, resolution, and normalization headings | Root-cause findings, normalization choices, retry, and stale-evidence explanations |
| `docs/user/workflow/05-final-review.md` | **Final review**, comparison progress, issue review, and deferred-scope headings | Compare with Odoo, blocker causes, affected groups, stale comparison, and reconnect decisions |
| `docs/user/workflow/06-load-into-odoo.md` | **Load into Odoo**, load progress, correction, destination, transfer review, transfer preflight, and transfer load headings | Write confirmation, dependency order, recovery, correction, reconciliation, and verification decisions |
| `docs/user/guides/local-odoo.md` | Odoo connection and reconnection dialogs | Read access, write access, schema refresh, and Final review reconnect explanations |
| `docs/user/guides/related-tables.md` | **Create additional tables** or derived-entities heading | Join, hierarchy, reusable-value extraction, and repeated-row split decisions |
| `docs/user/guides/integrated-test-runs.md` | Integrated Test setup, fresh data, target matching, run progress, and review headings | Run controls, shared Odoo values, Recipe order, recovery, and completion checks |
| `docs/user/guides/qualify-integrated-test.md` | Integrated Test qualification heading | Evidence selection and qualification decisions |
| `docs/user/guides/production-rollout.md` | Production activation and new Production run headings | Latest-data acceptance, target confirmation, approval, and stale evidence |
| `docs/user/tutorials/match-data-questions-and-answers.md` | A secondary help link on the **Match data** page | Direct anchors beside each advanced mapping, transformation, identity, and relationship decision |
| `docs/user/tutorials/end-to-end-training.md` | Data projects Help area and first-project empty state | A subtle **Follow a fictional example** link; do not place this long tutorial beside every stage action |
| `docs/user/guides/scenario-trials.md` | Data projects Help area and first-project empty state | A subtle **Trial your files** link when no project exists |
| `docs/user/README.md` | Persistent authenticated Help entry point | Fallback index when no more specific guide applies |
| `docs/user/installation/windows.md` | Global Help area, classified as pre-launch | Show the Windows link without adding it to workflow pages |
| `docs/user/installation/macos.md` | Global Help area, classified as pre-launch | Show the macOS link without adding it to workflow pages |

The primary page-level icon should always choose the stage or task guide. A
secondary tutorial must not replace the normal guide. For example, the Match
data heading should open `workflow/03-match-data.md`; a relationship-specific
icon can open the relevant question in the questions-and-answers tutorial.

## Implementation sequence

### Delivery 1: Stable page-level help — implemented

1. Add the reviewed help-target model, version-aware GitHub URL builder, and a
   reusable template macro.
2. Add one page-level question mark to setup and the six Authoring stage
   headings, including their progress and recovery variants.
3. Extend documentation checks so every workflow `user_doc` and every
   registered anchor is valid.
4. Preserve current route orchestration. Rendering a help link must use static
   registry data and must not add a repository open, DuckDB connection, schema
   check, or Odoo call.

### Delivery 2: Difficult decisions — proposed

Add local anchored links to the decisions that data managers most often need
to understand before they can continue safely:

1. source-table and related-table composition;
2. Odoo scope and business identity;
3. Match data identities, relationships, row inclusion, and transformations;
4. preparation causes and normalization;
5. Final review blockers and deferred groups; and
6. load order, confirmation, recovery, correction, and verification.

Review each screen at normal and narrow widths. Remove a local icon when the
same help is already obvious from the page-level guide and nearby text.

### Delivery 3: Supporting guides and reverse completeness — implemented

Add contextual links for integrated Test, qualification, Production, local
Odoo, related tables, and the two tutorials. Add the global authenticated Help
area for the documentation index, trial guide, and pre-launch installation
pages. Turn on the complete reverse-coverage check for every current page below
`docs/user/`.

## Accessibility and usability acceptance

The implementation is complete only when all of these statements are true:

- A keyboard user can reach every question mark in reading order, see a clear
  focus indicator, and understand its destination from the accessible name.
- A screen-reader user hears the guide name, the fact that GitHub opens, and
  the fact that it opens in a new tab.
- A touch user receives at least a 44 by 44 CSS pixel target without an
  oversized visible icon.
- A data manager can return to the original Impodo tab with unsaved values,
  scroll position, and open form sections preserved by the browser.
- A page never displays more local help icons than the number of genuinely
  different decisions that need separate documentation anchors.
- The icon does not become a substitute for a clear label, nearby instruction,
  validation message, correction route, or disabled-state explanation.
- The page-level link remains visible in empty, blocked, complete, progress,
  stale, and recovery states.

## Verification and guardrails

Each implementation delivery should add focused tests that prove:

- every help identifier resolves to an existing current user-document page;
- every registered anchor matches a heading in that page;
- every eligible user page has a contextual or global in-product owner;
- every pre-launch exception has a recorded reason and a global Help link;
- rendered links use the configured version-matched GitHub reference and do
  not contain request or workspace data;
- external-link, keyboard, tooltip, and accessible-name attributes are
  present;
- setup and workflow pages retain their existing bounded database and Odoo
  call counts; and
- the heading and local-link layouts work at the repository screenshot width
  of 1440 by 1024 and at a representative narrow viewport.

When implementation changes a documented decision point, update the owning
current user and developer pages, `docs/workflow.yml`, screenshots, and focused
browser tests in the same change. Until that implementation exists, this plan
is the sole documentation owner for the proposed behavior.

## Non-goals

This proposal does not introduce a chatbot, full-text documentation search,
remote support telemetry, or generated advice based on project data. It does
not send migration state to GitHub. It does not replace clear browser copy,
correction routes, or the existing local Concepts reference. It also does not
make every paragraph or control a help target; the design remains deliberately
subtle and decision-focused.
