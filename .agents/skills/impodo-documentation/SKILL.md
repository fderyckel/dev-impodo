---
name: impodo-documentation
description: Create, edit, or rewrite Impodo user, developer, architecture, contract, decision, plan, or report documentation using the repository's audience, semantic-language, workflow-ownership, and verification rules.
---

# Impodo Documentation

Use this skill for every Impodo documentation change in
`C:\Users\francois.deRyckel\dev-impodo`.

## Establish the contract

1. Inspect `git status --short` and preserve unrelated work.
2. Read `docs/style-guide.md` completely and apply its plain semantic language
   standard, data-manager-first explanations, and editing workflow.
3. Read `docs/workflow.yml` when the change concerns the implemented browser
   workflow. Identify the owning stage, paired audience page, contracts, code,
   and focused tests.
4. Verify current behavior from the appropriate contract and implementation.
   Do not present planned or remembered behavior as current.
5. Name the intended reader and the decision, task, or understanding that the
   document must support. Do not draft from the code inventory alone.

## Build the human model first

For user documentation and explanations of Impodo's product model, data
lifecycle, or workflow, start from the data manager's perspective. The primary
operator is data-informed but does not need to understand Impodo's internal
architecture.

- State the operator's goal before naming implementation objects.
- When several objects interact, show the smallest useful containment or
  lifecycle model before describing technical boundaries.
- Define each object by what it contains, what it is used for, what it does not
  contain when that prevents confusion, and how it relates to the other
  objects.
- Use a small fictional example, then state what the design means for the data
  manager's work and what they do next.
- Keep source data and evidence, reusable Recipe rules, workspace selections,
  and migration-run results conceptually separate.
- Prefer **Data version**, **Detected file structure**, **Confirmed source
  choices**, **Datasets used by this workspace**, and **Accept Data version**
  over package, catalogue, configuration, projection, and freeze when those are
  merely internal terms.
- Keep canonical ordering, operation identities, adapters, hashes, and recovery
  mechanics out of the normal user path unless their business effect changes a
  decision. Explain that effect before the technical term.

Use the exact current browser label when the reader must find a control. If the
label is technical, explain it immediately; do not document a preferred label
as though it were already implemented.

## Write for understanding

- Use complete, plain-English sentences built around **actor -> action ->
  result**.
- Use concrete verbs. State what triggers behavior, what Impodo does, what
  changes, and how the named objects relate.
- Preserve exact domain terms and code identifiers, but explain their meaning
  before adding implementation detail.
- State ownership, containment, lifecycle, and operator choices explicitly.
- Avoid noun stacks, nominalized shorthand, `X plus Y`, slash shorthand, and
  fragments that require the reader to infer relationships.
- Put one main idea in each sentence. Bullets should normally be complete
  sentences.
- Keep technical precision. Move supporting symbols, evidence boundaries, and
  exceptions into following sentences or structured reference material instead
  of compressing them into the opening statement.

Before accepting a passage, make sure an unfamiliar reader can identify the
trigger, actor, action, result, affected objects, relationships, and practical
reason for the distinction.

## Edit in two passes

1. Write a meaning-first pass that covers the reader's goal, mental model,
   lifecycle, example, practical effect, and next action without depending on
   code symbols.
2. Add a precision pass with the exact labels, domain terms, current or planned
   status, safeguards, evidence boundaries, exceptions, links, symbols, and
   tests that this audience needs.

Move technical inventory that does not help the intended reader into the paired
developer document or a structured reference. Never remove a technical
boundary merely to make the prose shorter.

## Preserve the documentation system

- Write for one audience at a time and keep user and developer material in
  their registered lanes.
- Keep developer documentation human-readable too: explain the responsibility
  and business effect before listing routes, services, repositories, or tests.
- Preserve accurate front matter, local links, workflow labels, code symbols,
  and implemented-versus-planned status.
- When behavior changes, review the owning user and developer pages together
  with affected contracts, architecture, code documentation, screenshots, and
  tests. Do not broaden a prose-only rewrite into unrelated behavior changes.
- Use fictional or sanitized examples. Refresh screenshots only from the
  authenticated current UI when the documented decision point changed.

## Verify

Run the focused documentation checks after an edit:

```powershell
.\.venv\Scripts\python.exe scripts\documentation_quality.py --check
.\.venv\Scripts\python.exe -m unittest tests.test_documentation_quality -v
git diff --check
git status --short
```

Report any omitted browser, screenshot, or broader validation explicitly.
