# Browser workspace contract

## Status and scope

**Status:** Integrated in the local browser.

This contract covers the browser workflow after Project setup:

```text
Source discovery -> Target schema -> Governed mapping
```

Artifacts remain inside one registered migration project and are bound to
immutable source and target evidence.

Every dataset uses the same current discriminated source contract. `FILE`
binds an immutable registered file table, `ODOO` binds an authenticated capture
selection, and `DERIVED` binds a structural rule and its exact input datasets.
There are no placeholder files and no alternate historical JSON shape. An Odoo
project proceeds from authenticated model/field discovery to a bounded capture
plan without an export date.

## Source intake and catalog

Source files can be added only while the project is `DRAFT`. Intake accepts
bounded `.csv` and `.xlsx` files, validates them in a spawned worker, assigns
generated storage names, and records exact byte size and SHA-256 evidence.

After registration, Source discovery creates a profile-free catalog without
changing the source. Each catalog is bound to the file identifier, size,
source hash, contract version, and inspection timestamp.

CSV inspection records encoding, delimiter, candidate header, row shape, and
column profiles. XLSX inspection inventories worksheets, named tables, ranges,
headers, formulas, errors, merged cells, and bounded previews. Type inference
and samples are advisory; digit strings with leading zeroes remain strings.
Formulas are inventoried but never executed or trusted.

Preview, cardinality, row, column, and cell limits prevent inspection from
becoming an unbounded in-memory workload.

## Odoo capture selection

An Odoo-source project may save one current bounded capture plan after a live,
identity-bound schema capture. Saving the plan performs no Odoo request and
does not freeze rows. Each immutable revision binds:

- one current captured model and a stable project/model dataset identity;
- unique technical field names from the closed scalar set: boolean, character,
  text, integer, date, datetime, and selection;
- stable column identities derived from model and technical field name;
- active-only or active-plus-archived policy, with no raw caller domain;
- a maximum of 50 fields, at most 10,000 rows, and a fixed 500-row future page
  contract;
- the exact current Odoo-source policy hash, including byte/disk/history limits,
  protected-data handling, and production-write disposition;
- connection-target, schema-scope, read-principal, observed-permission, and
  context hashes; and
- actor, timestamp, version, and deterministic content hash.

The plan contains no credential, numeric Odoo record/user/company/group ID, or
raw protected filter. Revisions are append-only; a current pointer chooses the
active plan. Recapturing schema, changing target/model scope, or saving a new
plan invalidates dependent current evidence while preserving immutable
revisions. Only current authenticated schema evidence can authorize a plan; an
unverified local manual draft cannot.

The current policy fixes Odoo 19 JSON-2, same-target protected-ID update-only
semantics, and connection-only instance assurance. It explicitly marks native
production writes unsupported: JSON-2 cannot prove restore/clone identity or
perform the required atomic compare-and-write operation. Captured IDs and
business values are restricted evidence and require application-level
encryption before persistence; live row capture is therefore still absent.

## Confirmation and dataset freeze

A source confirmation binds:

- source-file and catalog hashes;
- effective parsing and header settings;
- selected CSV table, worksheet, or named-table keys;
- warning acknowledgement, actor, and timestamp.

Blank or duplicate headers block confirmation. Other warnings require
explicit acknowledgement; acknowledgement proves review, not data quality.

Freezing assigns unique snake-case dataset names and stores versioned dataset
keys, row counts, ordered columns, effective parsing, source/catalog hashes,
actor, timestamp, and a canonical selection hash. Before the frozen pointer
advances, each selected physical table is parsed once through the governed
reader and published as an immutable Parquet `SourceSnapshot`. The manifest
binds the selection, source/catalog hashes, stable column schema, original
source-row numbers, row count, reader version, logical hash, exact Parquet
hash, and content-addressed storage key. Selection and per-dataset snapshot
pointers advance together in one DuckDB transaction.

After freezing, normal mapping preview and preparation resolve and hash-check
the Parquet snapshot rather than reopen CSV/XLSX. The original registered file
remains immutable evidence for audit/reinspection, not the routine preparation
data path. Snapshot publication is mapping-independent and has no user-facing
format or backend choice.

Invalidation is fail-closed:

| Change | Invalidated active evidence |
| --- | --- |
| Reinspect or reconfirm a source | Frozen selection, source-snapshot pointers, and mapping pointer |
| Freeze a new selection | Mapping pointer |
| Recapture target schema | Odoo capture-plan pointer, schema governance, and mapping pointer |
| Change target identity or model scope | Odoo capture-plan pointer and target-derived mapping/submission evidence |
| Save a new Odoo capture plan | Prior current source/snapshot, derived-plan, mapping, and staging pointers |
| Change governed keys | Target-derived mapping and submission evidence |

Historical immutable revisions remain available for audit but are not current.
A recoverable working mapping draft is retained across evidence changes, but
it is restored only when its frozen-source and governed-schema hashes still
match. Stale working state is disclosed rather than applied to a different
field catalogue.

## Target schema

Target evidence has one explicit origin:

- `LIVE_API`: verified capture from the selected Odoo target;
- `LOCAL_MANUAL`: unverified draft available only for a local project.

Live discovery captures permitted models and their effective Odoo 19 fields,
including type, required/readonly state, relation metadata, inverse field, and
selection values. It also attempts one batched read of explicit Odoo database
uniqueness constraints for all permitted models. Constraint access is optional:
if the remote read user cannot see this metadata, schema capture continues
without constraint-backed recommendations. Abstract and transient models are
excluded. The permitted model set is explicit; related models are not silently
added.

Model discovery is paginated and read-only. Field capture performs one
`fields_get` request per selected model, never one call per field or source
row.

A local manual draft requires frozen datasets, an explicit model allowlist,
and acknowledgement that the schema is unverified. It may support mapping
drafts, but mapping submission remains blocked until authenticated live
capture replaces it.

## Schema governance

A user with `schema.govern` confirms versioned natural business keys for each
target model. A definition contains an ordered key, optional company/tenant
scope, description, actor, content hash, and confirmed status. Keys are never
inferred from field names.

The browser may show one non-binding recommendation before confirmation. A
recommendation comes from an exact versioned model rule or one unambiguous,
supported Odoo uniqueness constraint. Multiple possible constraints do not
produce a guess. The recommendation remains outside governed state until the
user explicitly selects and confirms it.

Relationships and target matching use governed business keys, not remembered
numeric Odoo IDs. A narrow code-controlled allowlist may provide an exact key
for a standard Odoo reference model that is not itself a migration target.
Reviewed rules currently cover country, language, and currency codes. The same
data-driven rule contract can support further stable Odoo reference models;
each rule is explicitly selected in the mapping UI and is never inferred from
a field name.

## Governed mapping

Each frozen dataset declares:

- one permitted target model and `upsert`, `create`, or `reference` mode;
- source trace identity and governed target identity/scope;
- one explicit provider per target scalar field: source, constant,
  source-with-fallback, or leave-unset/Odoo-default;
- allowlisted trim, whitespace, empty-to-null, ordered text cleanup,
  literal/bounded-pattern replacement, casing, decimal-locale and rounding,
  date-format, boolean, UTC-datetime, and bounded formula transformations;
- guided exact-length and first/last/whole-value character checks, with an
  optional bounded expert custom pattern;
- required, comparison, validate-only, and null policies;
- many2one/many2many relationships resolved through another dataset or an
  existing-target business key;
- exact source-choice-to-Odoo-choice mappings for captured scalar selections
  and single-column many2one relationships using one unscoped, confirmed or
  reviewed standard text-based target business key.

One2many is represented through the child dataset's owning many2one. One
source column may feed several explicitly governed target mappings.
Odoo-default intent stays visibly unverified until controlled target rehearsal.

Mapping contract version 8 stores text cleanup exclusively as ordered
`text_steps`. The former scalar-level `search_value`, `replacement_value`,
`search_mode`, and `replace_all` fields and their browser form names are not
accepted or converted. A payload that still contains those fields fails with
an explicit migration error; this prevents a stale rule from being silently
dropped or reinterpreted.

The browser value-matching dialog counts every distinct non-empty value in one
frozen physical source column. It suggests exact key matches only. Scalar
targets come from the captured Odoo selection metadata; many2one targets come
from one batched, read-only target-model request. The dialog omits ambiguous
business-key values and persists portable source and target keys, never Odoo
numeric IDs. Quick matching is bounded to 500 source choices and 2,000 target
records; composite or scoped keys continue through the normal governed mapping
workflow.

Captured selection codes are metadata evidence, not a second transformation
language. Constants and fallbacks are closed to those codes, while
source-based matches remain portable `source_value -> target_value` pairs.
Full-row preflight reuses the compiled migration plan, indexes freshly fetched
choice codes once per mapped field, and validates final prepared values
without an Odoo call inside the row loop.

End-user explanations of every scalar provider, type, transformation, policy,
and preview belong in the
[local-browser scalar mapping reference](../operations/01-local-browser-user-guide.md#scalar-fields-choose-what-impodo-should-do).

Derived-entity rules may create deterministic related-dataset plans from
denormalized source columns. Their bounded previews do not execute full-row
staging; see [derived-entity authoring](../derived-entity-authoring.md).

The browser may persist one recoverable working draft before validation. It
uses stable dataset IDs and Odoo technical field names, accepts incomplete
mapping choices, records actor/time/content hash, and uses optimistic
concurrency. Saving it performs no semantic validation and creates no mapping
revision, validation result, submission, or Odoo request.

## Validation and submission

The pure semantic validator checks hashes, permitted models/fields, governed
identity and scope, type compatibility, readonly/required fields, relation
shape and key arity, provider/transform/value-rule policies, formula and custom
pattern bounds, dependencies, and required-at-create cycles. Deferrable
relationship cycles remain valid for reviewed two-step execution.
It emits deterministic issues, coverage, deferred checks, and a validation
hash.

Row-level uniqueness, required values, full transformations, and actual
relationship resolution are deferred from mapping validation to the Summary
readiness check. Bounded preview is not clean-package certification.

DuckDB retains append-only mapping revisions, validation results, and
submissions. `SUBMITTED` binds the exact mapping and validation hashes and
requires all blocking issues resolved and warnings acknowledged. It is ready
for the row-level readiness check; it is not approval and grants no Odoo write
capability.

## Row-level readiness

Summary offers one **Check data readiness** action for the current submitted
mapping. It reloads every frozen source row, applies generated parent/child
dataset rules, and checks business keys, values, relationships, and target
matches through the existing preflight engine.

The UI keeps three row-level outcomes: **Ready**, **Needs review**, and
**Blocked**. Status cards and dataset counts filter the source-row table;
plain-language reason and next-action text is shown first, while technical
codes stay collapsed. Target reads are grouped by model, never by source row.
When all rows are ready, the user can generate the Excel review package from
the canonical JSON evidence. All target access remains read-only.
