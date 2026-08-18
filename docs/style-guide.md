# Impodo documentation style guide

## Purpose

Impodo documentation helps two primary readers do different jobs:

- a data manager needs a safe, direct path through a migration;
- a developer needs the implementation boundaries, evidence, and tests behind
  that path.

Write for one audience at a time. Link to the paired document instead of
mixing user instructions and implementation detail on the same page.

## Sources of authority

Use one authority for each kind of statement:

1. Accepted [developer contracts](developer/contracts/) define cross-stage
   required behavior.
2. Architecture and decisions explain current boundaries and accepted choices.
3. User documentation explains current browser behavior.
4. Developer workflow documentation maps that behavior to code and tests.
5. Plans describe future work and must be labelled as plans.
6. Reports and testing documents record point-in-time evidence.

If an example conflicts with a contract, the contract wins. If documentation
and code differ, verify the current behavior before changing either one.

## User documentation voice

- Address the reader as **you**.
- Use the exact business labels shown in the browser.
- Explain the goal before the controls.
- Present the normal path first and optional detail afterward.
- Give one obvious next action at the end of a section.
- Explain what an action changes and what remains unchanged.
- Use small fictional examples that can be reviewed by eye.
- Define technical terms only when the reader needs them to make a decision.
- Do not expose class names, repository names, hashes, or transport details in
  the normal path.

Use **Complete**, **Needs attention**, **Current**, and **Not yet available**
with the same meanings as the browser. Do not invent synonyms for workflow
states.

## Developer documentation voice

- Start with the stage responsibility and its non-responsibilities.
- State whether behavior is implemented, partial, or planned.
- Name exact routes, services, methods, evidence, and focused tests.
- Explain entry conditions, exit conditions, invalidation, and recovery.
- Record security, authorization, idempotency, and concurrency boundaries.
- For Odoo integration, state the Odoo 19 API boundary and whether calls are
  read-only or write-capable.
- Identify batching behavior and any N+1 risk. Never document an unmeasured
  performance assumption as a guarantee.
- Prefer a file link plus an exact symbol name over a hard-coded line-number
  link.

## Current and planned behavior

Current workflow pages describe only implemented behavior. Future behavior
belongs in `docs/plans/` and may be linked from a **Current limitations**
section. Do not use roadmap language to make a missing capability sound
available.

## Screenshots and examples

- Use fictional or sanitized data only.
- Include useful alternative text.
- Capture the decision point, not decorative browser chrome.
- Keep counts small enough to review visually.
- Every user page must reference a current PNG under `docs/images/user/`.
- When a route, label, or decision changes, recapture the affected state from
  the authenticated current UI at 1440x1024 with isolated fictional data. Do
  not keep an old image merely because its link still works.

## Required maintenance

When workflow behavior changes, review together:

- the owning user and developer stage pages;
- `docs/workflow.yml`;
- affected developer contracts, architecture, decisions, and plans;
- code docstrings and the Python code map;
- screenshots, examples, and focused tests.

Run the documentation checks listed in [the documentation index](README.md)
before completing the change.
