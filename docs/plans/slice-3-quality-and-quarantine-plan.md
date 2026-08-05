# Slice 3 plan: data quality and quarantine

## Status and outcome

**Status:** Ready for implementation after bounded Slice 2 closure.

Slice 3 turns durable prepared data into a governed quality result before any
row is allowed into the Odoo comparison. The data manager should be able to
press one obvious action, understand what can continue, see what was set aside,
and know who must correct it. Odoo remains read-only.

The visible outcome is:

```text
Check all rows
-> prepare and save exact canonical rows
-> run deterministic data-quality checks
-> set unsafe business-data rows aside
-> compare only eligible rows with Odoo
-> show one reconciled Review page
```

Technical rule definitions, hashes, field identifiers, and trace evidence stay
inside progressive disclosure. The primary UI uses business labels and the
terms **Ready**, **Review**, **Set aside**, and **Fix setup**.

## Decisions locked for this slice

### Two levels of row accountability

A physical source row can produce several canonical records, such as a BOM
header and a BOM line. It is therefore incorrect to force one import outcome
onto the physical row and silently apply it to every constructed record.

Slice 3 will keep two explicit ledgers:

1. One `SourceAccountingEntry` for every physical `(dataset, row)` with one
   accounting state: `REPRESENTED`, `QUARANTINED_BEFORE_TRANSFORM`, or
   `EXCLUDED_BY_RULE`. A represented entry points to every canonical row it
   contributed to.
2. One `QualityRowResult` for every canonical row with one effective
   disposition: `CANDIDATE`, `REFERENCE`, `BLOCKED`, `QUARANTINED`, or
   `EXCLUDED`.

Mixed outcomes are valid when one physical row contributes to multiple
canonical records. They are visible through the fan-out pointers, never hidden
inside an ambiguous physical-row status. `UNREPRESENTED` is a failing
reconciliation state and cannot pass the Slice 3 gate.

### Immutable quality overlay

Canonical staging runs remain immutable. Quality evaluation creates a separate
`QualityRun` bound to the exact staging content hash, ruleset hash, mapping
hash, schema hash, evaluator version, and project retention context. It does
not rewrite canonical JSON or registered source files.

An effective row disposition is the base staging disposition plus the current
quality result. A changed source, mapping, schema, derived plan, control total,
ruleset, ownership policy, or correction creates a new run and invalidates the
old current pointer while retaining history.

### When to block and when to set aside

- A missing rule definition, unsupported context, stale evidence, or system
  inconsistency blocks the project as **Fix setup**.
- A deterministic problem in one business record uses **Set aside** when the
  configured policy allows the remaining clean records to continue.
- A warning remains **Review** and cannot silently become a pass.
- An exclusion requires an explicit governed rule; absence, parse failure, or
  lookup failure is never treated as exclusion.
- Fuzzy matches and guessed lookup values are never automatic.

## First quality families

The first integrated rules reuse existing mapping and preparation semantics.
They do not introduce a second transformation or expression language.

| Family | First-slice behavior | Default outcome |
| --- | --- | --- |
| Post-transformation identity collisions | Group by governed identity and scope after normalization; retain every row pointer | Set aside the complete collision group |
| Required values | Reuse mapping and Odoo-schema required checks on canonical values | Set aside affected record |
| Bounded format and value checks | Reuse exact length, segments, character classes, allowlisted patterns, selections, and typed parsing | Set aside affected record |
| Governed lookups | Require a unique mapped value or governed business key; never guess | Set aside unknown or ambiguous value |
| Cross-field rules | Add a small allowlisted library: required-if, exactly-one-of, ordered comparison, and equality/inequality | Policy chooses Review or Set aside |
| Relationship readiness | Require a resolvable source business key and a ready incoming dataset record | Set aside dependent record; fix setup for invalid rule |

Currency conversions, unit conversions, locale assumptions, and company rules
run only when their context is explicitly proven. Otherwise they block as a
setup problem.

## Versioned rule contract

Add a project-scoped `QualityRuleSet` with deterministic ordering and portable
rules. Each rule records:

- stable rule ID and contract version;
- dataset and quality family;
- plain-language name and explanation;
- governed input fields and bounded parameters;
- outcome policy: warning, block, quarantine, or explicit exclusion;
- default owner role and optional review-by policy;
- evidence-display policy for sensitive values;
- source: mapping-derived, schema-derived, or manager-authored.

Mapping- and schema-derived checks are shown as recommended automatic checks.
Their technical parameters are read-only in the normal UI. Manager-authored
business checks use guided choices; arbitrary Python, SQL, Odoo domains, and
unbounded regular expressions remain prohibited.

## Quarantine contract and lifecycle

Each quarantined canonical row creates immutable evidence containing:

- quality run, staging run, row ID, dataset, and physical source pointers;
- rule ID, reason code, plain explanation, and affected fields;
- owner role, recorded owner label, recorded time, and review-by date when set;
- correction route: replace source evidence, change mapping, change a governed
  rule, or provide normalization evidence in Slice 4;
- superseding quality-run ID after a successful rerun.

The default owner is the project's data manager. A functional owner may be
selected for a business-policy decision. Review-by dates cannot exceed project
retention. Rows are never edited in place and quarantine history is never
deleted merely because a later run passes.

Slice 3 does not add a free-form spreadsheet editor. The first correction paths
are deliberately simple: correct and re-register the source, correct the field
match, or correct the quality rule, then rerun. Governed proposed-value editing
and approval belong to Slice 4.

## Persistence and bounded execution

Add DuckDB tables for:

- immutable quality-run headers and current pointer;
- normalized quality issues and per-row results;
- physical source-accounting entries and fan-out links;
- immutable quarantine entries;
- ruleset revisions and current pointer.

Publication is atomic and idempotent. A failed batch leaves the previous
current quality run untouched. Repository APIs scan canonical rows in bounded
batches; quality evaluation must not reconstruct the full canonical run merely
to process row-local checks. Cross-row identity checks use one grouped DuckDB
operation or a bounded index, never a query per row. Slice 3 makes no Odoo call.

The existing 100,000-physical-row browser limit remains in force. The scale
probe is rerun after the quality overlay is integrated and the lower proven
limit wins. Memory, runtime, issue count, and database size are recorded.

## Data-manager UI

Extend the existing Review page rather than adding another technical wizard.

### One primary action

The existing **Check all rows** action runs preparation and target-independent
quality checks first. Rows set aside by quality are removed from the temporary
compatibility bundle sent to read-only Odoo preflight. Eligible rows continue;
set-aside rows remain visible and reconciled. Slice 5 later replaces that
temporary compatibility adapter with direct reads from durable staging.

### Review summary

Show four plain cards:

- **Ready for Odoo check**;
- **Review**;
- **Set aside**;
- **Fix setup**.

The top card always offers one next action. A row shows table, source row,
record label, what Impodo found, owner, and what to do. Rule IDs, hashes,
canonical row IDs, physical fan-out, and evaluator versions remain in a
collapsed **Technical details** section.

### Quality-check setup

Add a collapsed **Data checks** section to Field matching:

- recommended checks are preselected and described in business language;
- optional business checks use guided dropdowns and labelled values;
- outcome wording is **Ask me to review**, **Set affected records aside**, or
  **Stop this project**;
- owner choices use project roles, not user IDs;
- advanced parameters and masking controls open only on demand.

No page asks the data manager to enter JSON, model names, field IDs, hashes, or
rule codes.

## Implementation sequence

### 3A - Contracts and storage

Implement `QualityRuleSet`, `QualityRun`, `QualityIssue`, `QualityRowResult`,
`SourceAccountingEntry`, and quarantine evidence. Add schema migration,
atomic/idempotent publication, retrieval, invalidation, and history tests.

**Gate:** tampered, stale, partially written, or unreconciled evidence is
rejected; older current evidence is retained after failure.

### 3B - Mandatory automatic checks

Lower existing mapping/schema/preparation findings into the rule vocabulary.
Add post-transformation collision grouping and physical/canonical accounting.
Do not build optional authoring UI yet.

**Gate:** every physical row is accounted for once, every canonical row has one
effective disposition, and no existing error disappears during translation.

### 3C - Quarantine and rerun

Apply outcome policies, persist immutable quarantine evidence, assign project
roles, and invalidate/supersede on changed evidence. Adapt the current in-memory
preflight compatibility path to exclude quarantined and governed-excluded rows.

**Gate:** no set-aside row reaches Odoo record comparison; every omitted row is
present in reconciliation and visible evidence.

### 3D - Data-manager UI

Add the Review cards, filters, owner/action wording, guided Data checks section,
and collapsed technical evidence. Keep **Check all rows** as the primary action.

**Gate:** the normal flow can be completed without technical identifiers and
always presents one obvious next action.

### 3E - Acceptance and scale closure

Run deterministic reruns, restart retrieval, rollback injection, pagination,
masking, collision, relationship, empty/malformed, and mixed fan-out fixtures.
Repeat the 100,000-row measurement with the quality overlay and run the full
suite.

**Gate:** recorded bounds hold, database and Odoo access are batched, and no
N+1 query or connector pattern is introduced.

## Required acceptance cases

- unchanged inputs and rules produce identical quality JSON and hashes;
- one changed rule, owner policy, source, mapping, schema, or staging hash
  creates a new run and invalidates the old current pointer;
- every physical row has one accounting entry and all fan-out links resolve;
- every canonical row has exactly one effective disposition;
- collision groups quarantine all members and retain all source pointers;
- required, format, lookup, cross-field, and relationship issues use the same
  meaning in bounded preview and full-row execution;
- no unknown lookup, ambiguous relationship, or failed rule is silently
  excluded or changed into a warning;
- failed atomic publication preserves the prior current run;
- set-aside rows never produce Odoo record requests;
- Odoo requests remain grouped and paged per model, never per source row;
- sensitive evidence obeys masking and project retention;
- the UI has one primary action and hides technical evidence by default;
- existing projects migrate safely and stale quality evidence is not shown as
  current.

## Out of scope

- Odoo create, update, unlink, or arbitrary RPC;
- clean-package certification or execution approval;
- inline canonical-value editing and normalization approval;
- automatic fuzzy matching, survivor selection, anomaly thresholds, joins, or
  aggregations beyond the already declared control sums;
- arbitrary code, SQL, server actions, or unbounded expressions;
- streaming above the current browser limit.

## Definition of done

Slice 3 is complete when the data manager can check a supported project from
the browser, understand and rerun its quality result, set unsafe records aside
without losing them, and send only fully accounted eligible records into the
existing read-only Odoo comparison. The result must survive restart, reconcile
physical and constructed rows, remain deterministic, and grant no Odoo write
capability.
