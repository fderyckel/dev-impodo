---
audience: developer
kind: plan
status: proposed
---

# One practice dataset for users and coding agents

## Proposed decision

Create one versioned, fully fictional business dataset that people can use to
learn Impodo and coding agents can use to investigate realistic migrations.
Give each exercise its own starting state, decisions, expected results, and
recovery instructions. Both audiences use the same input bytes and business
expectations.

Consolidate ownership of shared business data. Keep small, focused fixtures
where they explain a test or protect an accepted contract. A one-row unit
test, a learner exercise, and a disposable Odoo qualification run have
different jobs even when they use contacts or products.

The first implementation owns the customer inputs in the
[current Morrow customer pack](../../scenarios/impodo-practice/v1/README.md).
Its 124 source rows drive customer enrichment, staged payment terms, and a
same-load incoming-term regression through normal application services.
Customer enrichment and all three payment-term stages have passed live Odoo
19 and 20 checks, with independent read-back and unchanged repeat comparisons.
The pack records authoring and Recipe evidence separately.

The [current manufacturing owner](../../scenarios/impodo-practice/manufacturing/v1/README.md)
contains twelve CSVs and 236 rows for supplied categories, combined products,
separate BoM headers, component lines and operations. It preserves the original
five baseline CSVs and adds a separate header export plus a reordered delivery.
Its source, target seed, business answers and recorded qualification scope
have one canonical owner. Native Services remains an existing unowned reference.

The [remaining proposal assets](24-shared-practice-data/README.md) contain two
CSVs and fourteen rows: eight accounts and six phone patches. Supplier repair,
optional mappings, generated hierarchy, the related-split provider and the
eighteen fault variants remain separate proposals. The supplied-header main
exercise does not qualify deriving headers from repeated component rows.
Small contract fixtures and existing consumed scenarios retain their own inputs.
The detailed original delivery design below is retained as proposal history;
use the canonical manifests for current physical files and counts.

## What the review found

Three agents reviewed the existing assets, implemented workflows and
regressions, and the design of a common dataset. The review also listed the
accessible local Codex history, including archived chats, and the accessible
ChatGPT history. The available lists contained 80 Codex chats and 21 ChatGPT
chats. There were 62 other chats associated with this Impodo checkout.
Recent turns from those 62 chats were inspected, with deeper review of the
failure investigations used below. This is not a claim that every turn in
every historical chat, attachment, other execution host, or unavailable
conversation was reviewed. The ChatGPT listing did not identify an Impodo
migration investigation.

The [scenario catalogue](24-practice-scenario-catalogue.md) records the
exercise scope and acceptance requirements.

The main findings are:

- `examples/` contains tiny input files. Some use custom asset models and
  cannot serve as a stock-Odoo tutorial without additional modules.
- `fixtures/` also contains compatibility and contract evidence. Those files
  are not interchangeable with sample migration data.
- `scenarios/` contains runnable definitions with pinned inputs and expected
  results. Several manufacturing workbooks are exact byte duplicates.
- The Contact scenarios prepare one row. The Product and BOM-header scenario
  prepares ten rows, and the BOM-line scenario prepares six. These are useful
  canaries but do not reproduce a demanding data-manager journey.
- The PLW preparation scenario covers 37,253 rows. It checks preparation,
  not target matching, loading, or read-back. Its provenance needs an explicit
  publication review before treating it as public practice data.
- Many of the richest cases already exist in integration tests, qualification
  scripts, and chat investigations. They are difficult for a learner or a new
  agent to discover as one coherent business exercise.

Folder similarity therefore points to missing ownership and discoverability,
as well as some duplication. Deleting all but one folder would lose useful
distinctions.

## The common business story

Morrow Assembly makes small assembly kits in two fictional operating
companies, NORTH and SOUTH. Its old system exports customers, party details,
addresses, products, product details, categories, bills of materials, and
operations separately. It also exports accounts and payment terms.

The data manager must combine the right records, preserve identifiers such
as leading-zero codes, clean a few deliberate source conventions, and connect
records to the correct company and parent. Some records already exist in the
destination. Others must be created before their dependants can load.

The original proposal contained 250 physical rows across 17 CSV files.
After customer promotion, seven proposed files and 126 rows remained.
The current owners now contain 22 CSVs and 360 rows across both components;
only the two account and phone-patch files remain local proposal inputs.
The following table preserves the original delivery design:

| Delivery | Purpose | Physical rows |
| --- | --- | ---: |
| Baseline | Learn source preparation, relationships, hierarchy, and manufacturing with a small inspectable delivery. | 182 |
| Second delivery | Reuse reviewed rules with changed values and a source language choice outside the current mapping scope. | 62 |
| Customer patch | Update a narrow selection of existing records while forbidding creates. | 6 |

These are source-file totals. Joining three customer tables does not create
three times as many customers. Separating repeated BOM headers from lines
does not create one BOM per physical row. Each exercise must state its own
prepared, included, excluded, created, updated, unchanged, and verified counts.

The business expectations and seed projection describe intended values by
business key. The current customer runner translates its reviewed subset
using the verified Odoo major and captured metadata. Proposed extensions still
need their own adapters and qualification. These files are not Odoo database
dumps, portable numeric Odoo IDs, or `SnapshotConnector` snapshots.

### Complexity that teaches a useful decision

| Data shape or situation | What the person or agent must demonstrate |
| --- | --- |
| Three partial customer exports share a compound identifier. | Join at the same record grain, preserve lineage, and choose between similarly named columns deliberately. |
| Product descriptions and business codes live in separate tables. | Combine one product with its details without multiplying products. |
| The category tree repeats a leaf name under different parents. | Match complete ancestry; distinguish source row identity from destination business identity. |
| BOM headers repeat across component rows. | Produce one header and several lines; keep operations in their own table so a join does not multiply lines by operations. |
| A component appears twice at different sequences. | Preserve both legitimate lines instead of deduplicating by component alone. |
| A subassembly is used by another BOM. | Resolve products and BOM relationships in the intended dependency order. |
| Existing and incoming payment terms are mixed. | Reuse unique existing terms and create missing terms before both new and existing contacts that use them. |
| Two companies use similar reference labels. | Keep company scope separate from names and from the selected operation company. |
| Leading zeroes, decimal commas, blank values, zeroes, dates, and `UNKNOW` occur. | Apply explicit reviewed transformations; keep blank, zero, ignored, cleared, and unavailable meanings distinct. |
| A second delivery changes business values. | Reuse a Recipe without carrying old target IDs, decisions about unknown new values, or stale comparisons into the new run. |

Payment terms include separate schedule-line intent. Matching a term by name
alone does not establish that its payment schedule is correct. The accounts
exercise also needs metadata qualification before using a hierarchy on a
particular target. Do not invent a common Odoo field alias for either major.

## One dataset, two ways to use it

### A data manager learns through progressive exercises

The draft pack's [walkthrough](24-shared-practice-data/README.md) defines the
business decisions and small subsets to inspect. The proposed published
experience should offer these exercises in order:

1. **Prepare customers.** Upload the three customer files, combine them using
   the complete key, review the result, save it, and reopen it. Check a small
   named set of records rather than trust the total alone.
2. **Connect shared values.** Match countries, languages, company-scoped
   choices, and payment terms. Review the mixture of existing and incoming
   terms. Apply the `UNKNOW` cleanup only where the exercise calls for it.
3. **Prepare products and categories.** Try the supplied category hierarchy.
   In a separate workspace, derive a hierarchy from product paths and compare
   the intended category relationships. The two routes must not create two
   sets of categories in the same trial.
4. **Prepare manufacturing.** Separate BOM headers and lines, retain repeated
   components at different sequences, and connect operations. Review the
   effect of excluding one header on its lines.
5. **Compare, load, and verify.** Use a disposable target with the reviewed
   seed. Explain each create, update, unchanged record, and set-aside group.
   Check final values and relationships, then require a no-write repeat.
6. **Reuse and repair.** Apply the Recipe to the second delivery. Then try the
   six-row patch with a zero-create expectation and deliberate blank handling.

An exercise that ends after source preparation can teach data shaping without
permitting target writes. Live comparison and loading require an available
qualified Odoo target. The current offline scenario canary remains a separate
way to exercise saved-snapshot comparison; the draft seed projection does not
make this whole pack runnable offline today.

### A coding agent receives an investigation, not an answer to copy

Give the agent the exercise ID, input delivery, exact target seed, Odoo major,
allowed stopping point, and relevant symptoms. Keep the independently
reviewed expected output available to the evaluator. The agent should use
normal authoring, Recipe, preparation, comparison, execution, and recovery
services. It must not update internal DuckDB rows to produce a passing state.

For each investigation, require the agent to report:

- The reproducible trigger and the evidence that supports its diagnosis.
- Whether the cause is bad source data, a deliberate business decision, an
  Impodo regression, target drift, unavailable access, or an uncertain write.
- Which rows and dependent groups are affected, and which may still proceed.
- The corrected behavior, focused regression, exact read-back, and repeat
  comparison where the exercise includes writes.
- Passed, failed, skipped, and unexecuted checks for each affected version and
  access mode.

The new-payment-term exercise must traverse real preparation, comparison,
saved snapshot reopening, and loading. A test that manually constructs an
old relationship representation would repeat the weakness identified in
**Find payment term load failure**. It must check both a contact CREATE and
an existing-contact UPDATE that depend on the same incoming term.

## Failure variants remain separate from the clean delivery

The pack supplies exact-keyed draft source mutations. Apply one named variant
to a copy of the baseline. Keep the original immutable and identify the
variant and resulting bytes in the run evidence. Do not modify data in place
and continue to call it the baseline.

Source variants should cover duplicate join keys, missing join matches,
duplicate business identities with distinct source row IDs, a missing parent,
an excluded parent, a hierarchy cycle, a root sentinel such as `0`, an invalid
date, an unresolved selection, and a missing Recipe column.

Transport and access variants need a controlled target or adapter. They must
not be encoded as implausible CSV values. These variants include:

- A known batch rejection with innocent peer rows.
- A response lost after a commit, with read-back required before another write.
- A recorded receipt followed by a worker interruption.
- A server-side change to a submitted value, producing a read-back mismatch.
- A changed company or permission context, and a key-only renewal with
  unchanged access.
- A broad lookup that lacks a needed value but an exact lookup can resolve.
- A rejected save, a failed supporting-choice refresh, and reopening a saved
  combination or mapping without losing choices.

A known rejection may permit isolation and independent continuation under
the existing contract. An uncertain outcome must stop for recovery. A blank
dropdown after an access failure does not prove that a record is missing.
Likewise, a rejected batch does not establish that every row in it is bad.

The catalogue also includes a diagnostic-only investigation: give an agent a
sanitized failure bundle and ask it to explain what is known about the writes.
The [execution contract](../developer/contracts/execution-and-reconciliation.md#safe-write-diagnostics)
defines the current diagnostic fields and their privacy boundary.

## Proposed repository ownership

The final maintained suite should put its scenario YAML files directly in one
versioned root. The current scenario loader resolves contained paths relative
to the definition's directory, so this layout shares data without weakening
path validation:

```text
scenarios/impodo-practice/v1/
    scenario-contacts.yaml
    scenario-products.yaml
    scenario-manufacturing.yaml
    data/
    deliveries/
    variants/
    profiles/
    expected/
    exercises/
    manifest.json

fixtures/
    odoo-compatibility/
    migration-projects/
    golden/

tests/support/
    focused test builders and controlled fault adapters
```

The same files in `data/` serve learners, browser qualification, and automated
scenarios. A scenario definition selects its subset and pins the manifest,
profile, seed, and expectations. Native Excel-date behavior will need a small
generated XLSX companion whose values match the same reviewed case; a text
date in CSV does not establish native Excel-date coverage.

Keep `fixtures/` for compatibility snapshots, accepted contract artifacts,
and small independent test oracles. Keep focused test builders close to tests.
Keep scale generators and isolated screenshot capture tools in their current
tooling lane unless a consumer audit supports a move.

After consumers move, retire `examples/` as a separately maintained business
dataset. Replace its educational role with links to the published practice
exercises. Do not redirect a minimal unit test to the entire 250-row pack.
Do not delete a custom-model golden case merely because the public practice
suite uses standard models.

Existing scenario `v1` bundles retain their bytes and identities. A replacement
suite is a new versioned definition. Avoid `../` fixture references, symlink
sharing, silent hash rewrites, or flattening compatibility snapshots into
sample business data.

## Qualification and promotion

| Stage | Required result before moving on |
| --- | --- |
| 1. Review the draft | A data manager and an engineer agree on joins, business keys, payment schedules, blanks, expected fields, and each source defect. Check row totals, referential integrity, and file hashes. |
| 2. Compile focused scenarios | Add reviewed profiles and version-specific seed adapters. Validate contained paths and pinned artifacts. Keep the tiny canaries. |
| 3. Prove the real lifecycle | Exercise normal Project, Data version, workspace, and Recipe creation and reopening. The current profile-driven scenario runner does not yet establish this lifecycle. |
| 4. Qualify the target matrix | Record independent Odoo 19 and Odoo 20 results, each with local and remote access, for Authoring and Recipe paths where affected. Controlled adapter results and live results remain separate. |
| 5. Publish the learner path | Verify the walkthrough against the current browser, capture approved fictional screenshots, and update owning current documentation only with the implemented suite. |
| 6. Migrate old consumers | Switch one consumer at a time, run its focused tests and complete gate, then remove only assets with no remaining owner. Retain a mapping of old paths and replacement exercise IDs. |

For a successful load, acceptance requires the exact intended values and
relationships, correct dependency order, reconciliation, and an unchanged
repeat comparison. Totals alone can miss a consistently wrong mapping.
For an expected blocker, acceptance requires the named condition and no
unauthorized writes, with dependent consequences explained.

Recipes and Odoo-to-Odoo transfer remain same-major exercises. Cross-major
Recipe conversion, whole-database upgrades, unrestricted source graph capture,
and Odoo-source Production are outside this proposal's supported path.
Target provisioning, seed attestation, remote cleanup, and browser lifecycle
automation are not capabilities supplied by these draft files.

Every run records the application revision, dataset and expectation hashes,
Odoo major and module set, access mode, Authoring or Recipe path, initial seed,
actual outcomes, and retained evidence references. Use different numeric Odoo
IDs in a second seeded target to expose accidental ID portability. Write
authorization and credentials belong to the target operation, never the pack.

Begin with the small learner suite. Later generate 25,000-row and optional
100,000-row deliveries with bounded hierarchy depth, shared-parent fan-out,
and separately labelled error rates. Measure preparation, lookup requests,
connection counts, elapsed time, and memory. Do not describe an unmeasured
timing target as an accepted performance guarantee.

## Data provenance and maintenance

All new business values are synthetic. No customer row, diagnostic attachment,
company name, source identifier, hostname, or numeric target receipt is copied
into the pack. Reserved example domains identify fictional addresses.
Structural lessons from failures are retained without retaining their private
business values. This gives stronger publication assurance than renaming a
few columns in a real export.

Assign one owner to the dataset and another reviewer to the business oracle.
Changing a value, schedule, join, expectation, or fault variant requires a
reviewed manifest update and a version decision. Never regenerate expected
values from the Impodo output being tested. Store runtime evidence outside
the immutable suite and retain it according to its protected-data contract.

Rollback consists of restoring a consumer to its previous immutable bundle.
It must not erase a journal, reset an unexpected target, or remove records
that the scenario did not create. Cleanup uses retained receipts and verified
ownership, with children removed before their created parents.

## This delivery's verification boundary

This change supplies the proposal, inventory, scenario catalogue, and concrete
draft data. It does not change application behavior, activate a Recipe, run a
live migration, launch a browser, or interrupt the current Impodo session.
Draft source checks establish the internal consistency of the supplied files;
they do not establish Odoo 19 or Odoo 20 migration qualification.

The implementation work should start with customer enrichment and the mixed
payment-term CREATE/UPDATE case. Those reproduce current, consequential
failures and provide the first shared user and agent exercise. Add category
and manufacturing qualification next, then recovery and optional scale lanes.
