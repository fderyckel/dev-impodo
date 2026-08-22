# Impodo documentation style guide

## Purpose

Impodo documentation helps two primary readers do different jobs:

- a data-informed, non-technical data manager needs a safe, direct path
  through a migration;
- a developer needs the implementation boundaries, evidence, and tests behind
  that path.

Write for one audience at a time. Link to the paired document instead of
mixing user instructions and implementation detail on the same page.
Write for a person who needs to understand or act. Do not optimize prose for
code-symbol extraction or make readers reconstruct the product from internal
architecture terms.

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

## Plain semantic language

Apply these rules to user, developer, architecture, contract, decision, plan,
and report documentation:

- Write complete sentences in plain, direct English.
- Describe behavior as **actor -> action -> result**. Name what triggers the
  behavior, what Impodo does, and what changes as a result.
- Prefer concrete verbs such as **creates**, **stores**, **belongs to**, and
  **uses** over abstract phrases such as **creation as**, **provisioning of**,
  or **application through**.
- When several domain objects appear together, explain their relationship:
  which object creates or contains another, why each object exists, and which
  choice the operator makes.
- Keep exact domain terms and identifiers such as `Recipe`, `DataVersion`,
  `FILE`, and `ODOO`, but introduce them inside an understandable sentence.
- Use one main idea per sentence. Bullets should normally be complete
  sentences.
- Avoid noun stacks, `X plus Y` shorthand, slash shorthand, sentence
  fragments, and compressed architecture language that makes the reader infer
  causality, ownership, or lifecycle.

Technical precision comes from explicit relationships, not compressed
terminology. State the understandable meaning first, then add implementation
details, exact symbols, evidence boundaries, or exceptions in later sentences.

For example, do not write:

> source confirmation as selected tables plus snapshots and hashes.

Write:

> When the data manager confirms the selected source tables, Impodo freezes an
> immutable snapshot and records the hashes that identify the accepted data.

Before accepting prose, confirm that a reader unfamiliar with the
implementation can answer:

1. What triggers this behavior?
2. What does Impodo do?
3. What objects are created or changed?
4. How do those objects relate to one another?
5. Why does the distinction matter?

## Data-manager-first explanations

Use the data manager's perspective in user documentation and whenever a
document explains Impodo's product model, data lifecycle, or operator
workflow. Start from the reader's business situation instead of simplifying an
internal architecture statement one term at a time.

Build the explanation in this order:

1. State what the data manager is trying to prepare, decide, reuse, or verify.
2. Show the smallest useful mental model. A short containment tree or lifecycle
   is useful when several objects or stages depend on one another.
3. Define each object by what it contains, what it is used for, and how it
   relates to the other objects. State important exclusions when they prevent a
   likely misunderstanding.
4. Use one small fictional example with recognizable files, datasets, Recipes,
   or migration stages.
5. Explain the practical consequence with wording such as **For you, this
   means**. Make the next decision or action clear.

For Impodo's core model, keep these distinctions visible:

- A Data version contains one accepted delivery of source data and its source
  evidence. It does not contain Recipe rules, Odoo credentials, or migration
  results.
- A Recipe contains reusable rules that Impodo can apply to suitable Data
  versions.
- A workspace selects the datasets it needs from a Data version. It does not
  own or silently replace that accepted source data.
- A migration run records the outcome of applying selected Recipes and data.

Prefer the data manager's term when an internal term does not help the reader:

| Internal concept | Data-manager wording |
| --- | --- |
| `DataVersion` source package | **Data version** or **one delivery of source data** |
| file catalogue | **Detected file structure** |
| source configuration | **Confirmed source choices** |
| workspace projection | **Datasets used by this workspace** |
| freeze a Data version | **Accept Data version** |

Keep canonical ordering, operation identities, adapters, projections, hashes,
and cross-store recovery out of the normal user path unless the reader must
understand one of them to make a safe decision. When a technical boundary does
matter, explain the business effect first and place the exact term afterward.
For example, explain that retrying an interrupted action does not create a
duplicate before naming idempotency or an operation identity.

Do not invent a browser label to make the prose friendlier. If the current
interface still shows a technical label, give the exact label needed for
navigation and immediately explain its meaning. Describe a preferred
replacement label as a proposal, not as current behavior.

## Documentation editing workflow

Use two distinct writing passes so that precision supports the explanation
instead of replacing it:

1. **Establish the reader's job.** Identify the audience, the decision or task,
   and the current source of authority. Verify current behavior before drafting.
2. **Write the meaning-first pass.** Explain the goal, mental model,
   relationships, lifecycle, example, practical effect, and next action in
   business language.
3. **Add the precision pass.** Restore exact browser labels, domain terms,
   implementation status, safeguards, evidence boundaries, code identifiers,
   and exceptions that this audience needs. Move the remaining technical
   inventory to the paired developer page or structured reference material.
4. **Challenge the result.** Check whether a data manager can say what they are
   working with, what they decide, what Impodo stores or reuses, what remains
   unchanged, and why the distinction matters. Check separately that a
   developer can still find the exact contract, symbol, and test when those
   details belong on the page.
5. **Verify the documentation system.** Review paired pages and affected links,
   screenshots, contracts, workflow registrations, and focused tests. Run the
   repository documentation checks before completing the edit.

The meaning-first pass must not weaken a security, governance, evidence, Odoo
19, performance, or implemented-versus-planned boundary. It changes the order
and clarity of the explanation, not the underlying contract.

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
