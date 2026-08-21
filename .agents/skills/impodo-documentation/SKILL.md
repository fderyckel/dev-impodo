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
   standard.
3. Read `docs/workflow.yml` when the change concerns the implemented browser
   workflow. Identify the owning stage, paired audience page, contracts, code,
   and focused tests.
4. Verify current behavior from the appropriate contract and implementation.
   Do not present planned or remembered behavior as current.

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

## Preserve the documentation system

- Write for one audience at a time and keep user and developer material in
  their registered lanes.
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
