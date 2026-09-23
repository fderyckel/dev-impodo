---
audience: developer
stage: odoo
status: current
---

# Odoo data

## Responsibility

Odoo data captures selected qualified Odoo 19 or final Odoo 20 model and field
metadata. The shared
**Odoo access** page configures the destination and checking credential outside
the numbered file-source stages. In file mode, Odoo data also governs the
business keys used by mapping and comparison. It owns
the confirmed target-schema boundary. It keeps source-read checks separate from
destination-read checks and records where the captured metadata came from.

It does not read an unrestricted business-record export and does not expose a
generic RPC escape hatch.

## Entry conditions

File mode requires frozen source data before Stage 2 metadata capture. Shared
Odoo access can be configured during Stage 1. If the destination is not
configured when the schema route opens, it redirects to Odoo access and returns
after a successful read-only check. Odoo source mode already has its source connection
and reaches schema capture before source records are frozen because eligible
fields define what may be captured.

The file-source sidebar links directly to **Odoo access** outside the six
numbered stages. Stage 2 lists only **Choose Odoo records**. A Stage 1 lookup
or hierarchy request carries one of two allowlisted return tokens through
connection testing and local-stack setup. Saving a checked connection then
refreshes the model list and returns to the requesting form. A missing key on
an already configured Remote target uses the inline read-key dialog and
resumes the same Stage 1 model refresh.

## Implementation flow

`target.py` and `OdooConnectionTestService` identify the exact database and
verify either source-read or destination-read purpose without discovering
models or fields. `schema.py` then refreshes the permitted model catalogue,
saves the selected scope, captures field details, and, for file mode, submits
key governance. `SchemaWorkspaceService` coordinates these operations through
separate ports for the model catalogue, schema catalogue, source selection,
and mapping invalidation.

After a live destination capture, `supporting_models.py` derives direct
relationships from the selected schema. It classifies each relationship as
reuse existing, checked default, Odoo managed, or review incoming. The Stage 2
presenter joins that domain result with the saved model catalogue and projects
business-facing groups plus the shared read-only workflow-issue shape. Opening
the page remains local: this projection is not persisted and makes no Odoo
request.

For a non-Production target, the connection form can explicitly keep the same
submitted or already stored secret for later loading. `target.py` writes two
target-bound vault envelopes with distinct `READ` and `WRITE` roles and audits
both credential generations. The option grants no write operation and performs
no speculative write call. Stage 6 probes the saved `WRITE` role against the
exact reviewed execution scope before constructing a writer. Production setup
does not expose this option and continues to require a separate limited write
key.

The first capture publishes the selected target schema. A later **Check for
Odoo changes** builds and validates a candidate before publication. The
service compares its semantic fingerprint with the current catalogue:

- An unchanged result keeps the current catalogue content hash and dependent
  current pointers. It records the check time, actor, and current access
  binding, and clears any older pending candidate.
- A changed result stores the candidate and its bounded model/field change
  summary beside the current catalogue. It marks the stage **Needs attention**
  and blocks new Odoo source freezes without invalidating current evidence.
- **Use updated Odoo details** compares the candidate and current hashes again,
  then atomically publishes the candidate and invalidates schema governance,
  source-capture selection and snapshot pointers, mapping, and later evidence.

The semantic fingerprint binds technical target identity, selected model
scope, field types and flags, relationship metadata, selection codes,
constraints, usable required create defaults, origin, and read-access
meaning. It excludes capture/check times, actors, credential generations, and
translated display labels. Those facts are freshness, access provenance, or
presentation rather than schema structure.

Local capture uses the isolated local reader. Remote capture uses the narrow
JSON-2 read connector. Both normalize metadata into the same domain catalogue
before governance is saved.

## Contract invariants

Connection and capture checks use the shared operation policy in
[`domain/odoo/compatibility.py`](../../../src/impodo/domain/odoo/compatibility.py).
The adapters cross-check structured version information when Odoo provides it.
Unknown, malformed, or contradictory evidence cannot authorize capture. Odoo
19 retains its existing acceptance rules. Final Odoo 20 enables connection,
schema capture, bounded source capture, comparison reads, and Recipe authoring;
write, recovery, and Production operations remain disabled. See the
[Phase 3 report](../../testing/odoo-compatibility-phase3.md) for the live
Community and Enterprise evidence and exact limitations.

Target evidence is either verified `LIVE_API` capture or an unverified
`LOCAL_MANUAL` draft. A manual draft may support mapping work but cannot
authorize mapping submission. Abstract and transient models are excluded, and
related models are never silently added to the permitted scope.

Supporting-model derivation is generic and limited to direct captured
required relationship fields outside the selected write scope, plus
one-to-many child data with a captured inverse. It excludes non-exportable and
company-dependent fields, does not recursively traverse the Odoo model graph,
and never changes `intended_models`. Optional many-to-one and many-to-many
fields wait for accepted-source or Recipe intent in a later slice. A
one-to-many with a captured inverse is presented as related incoming business
data; computed, related, read-only, and inverse-managed relationships remain
Odoo managed. A usable required create default remains a checked-default
decision.

A required relationship classified as reuse existing must point to a model in
the current target-bound model catalogue. Otherwise the presenter emits a
current **Must fix** issue and `govern_complete` rejects Stage 2 completion. An
unavailable inverse-owned child model remains a **Review** finding. This proves
model availability only; Stage 3 and later checks still own source-value
coverage, identity, ambiguity, and record-level relationship resolution.

Field capture records the effective inherited field set from the connected
Odoo major. For each field, it records requirements, read-only state,
relationships, inverse fields,
and selection codes. It performs one `fields_get` request per selected model,
then at most one `default_get` request for that model's supported required
writable fields; neither request runs per field or source row. A positive
Many2one record identity can be retained only as evidence for this exact target
context. Other relational defaults and unusable scalar values are not retained.
Impodo fetches optional uniqueness metadata in one bounded model batch. If it
cannot read that metadata, it does not present a recommendation as confirmed
governance.

Final Odoo 20 uses `uom_id` on `mrp.bom` and `mrp.bom.line`, removes
`product.template.uom_po_id`, and replaces the Odoo 19 Unit of Measure category,
rounding, and unit-type shape with `relative_uom_id`, `relative_factor`, and a
stored `factor`. The schema adapter preserves those native contracts. It does
not introduce field aliases. The Odoo 19 curated Unit name within Category
recommendation therefore does not match the Odoo 20 schema; an Odoo 20 unit
identity needs an explicit reviewed rule until a separate version-specific
recommendation is approved.

Business keys are explicit, versioned, and actor-confirmed. A recommendation
may come from one exact supported rule or one unambiguous Odoo uniqueness
constraint, but it remains non-authoritative until confirmation. Relationships
and matching use these portable keys rather than remembered numeric Odoo IDs.

`business_keys.py` owns recommendation policy version 1. It classifies a
preferred rule as `ODOO_ENFORCED` or `CURATED_CONVENTION`, and returns
`MULTIPLE_CANDIDATES` or `NO_SAFE_CANDIDATE` without a preferred rule when it
cannot choose safely. A curated rule is an exact model, field type, and relation
contract; a translated label or a field merely named `code`, `ref`, or `name`
does not qualify another model.

The current Odoo 19 matching-rule policy covers Country code, Language code,
Currency code, Contact reference, Company name, Product and Product variant
Internal Reference, Product category name within its parent, Unit name within
its category, BoM reference, BoM line sequence within its parent BoM, and Work
Center Usage operation name within its BoM. Convention rules carry a warning
when Odoo permits blanks or duplicates. In particular, BoM Component within
Parent BoM is not the default because the same component may legitimately
occur more than once; it remains available through the advanced editor.

`_schema_key_views` server-renders the preferred fields, scope, and description
when there is no submitted draft and no confirmed rule. A submitted draft wins
over the recommendation after validation failure, and an existing confirmed
rule wins over a later policy version. JavaScript updates the advanced editor,
but it is not required to place suggested values in the submitted form.

The authoring route binds confirmation to both the captured schema content hash
and the recommendation-policy version. `SchemaWorkspaceService.govern_complete`
requires exactly one confirmed rule per captured model before publishing new
Stage 2 governance. Recipe application continues to use `govern`, because its
saved Recipe already defines the required model subset. A used suggestion
stores its basis and policy version in backward-compatible governance evidence;
an advanced override stores only the reviewed matching shape. Page rendering
and confirmation remain local and make no additional Odoo call.

The representative browser qualification installs captured Odoo 19 evidence
for a standard Contact, an extended-standard Product with a custom legacy
field, and a custom Asset with an enforced unique constraint. It proves one
confirmation, override persistence after a validation error and reload, and a
visible unresolved custom-model blocker. Connector call vectors are recorded
after capture and must remain unchanged across every review and confirmation
request.

`reference_keys.py` owns separate Odoo 19 and Odoo 20 governed-reference
policies. A
captured parent relation may authorize a reviewed supporting model outside the
primary schema only for its exact key, scope, display fields, and read purpose.
The policy rejects write use, unrestricted metadata, extra fields, wrong
relations, and incompatible captured metadata. The supporting model therefore
remains outside the migration write scope.

## Code references

| Role | Code |
| --- | --- |
| Schema orchestration | [`SchemaWorkspaceService`](../../../src/impodo/application/schema_workspace_service.py) |
| Purpose-specific connection check | [`OdooConnectionTestService`](../../../src/impodo/application/odoo_connection_service.py) |
| Schema governance | [`governance.py`](../../../src/impodo/domain/schema/governance.py) |
| Matching-rule recommendations | [`business_keys.py`](../../../src/impodo/domain/workspace/business_keys.py) |
| Supporting-model derivation | [`supporting_models.py`](../../../src/impodo/domain/workspace/supporting_models.py) |
| Governed supporting references | [`reference_keys.py`](../../../src/impodo/domain/workspace/reference_keys.py) |
| Workflow issue projection | [`issues.py`](../../../src/impodo/application/workspace/issues.py) |
| Browser routes | [`schema.py`](../../../src/impodo/web/routers/schema.py) |
| Browser presenter | [`schema.py`](../../../src/impodo/web/presenters/schema.py) |
| Supporting-data presenter | [`supporting_models.py`](../../../src/impodo/web/presenters/supporting_models.py) |
| Browser review | [`workspace_schema.html`](../../../src/impodo/web/templates/workspace_schema.html) |
| Qualified screenshot capture | [`capture_stage2_matching_rule_screenshots.py`](../../../scripts/capture_stage2_matching_rule_screenshots.py) |
| Local reader | [`local_odoo_reader.py`](../../../src/impodo/adapters/odoo/local_reader.py) |

## Evidence and state

The model catalogue records the available scope. The schema catalogue binds
models, fields, types, requirements, selections, relations, and target
provenance. It may also carry one unconfirmed refresh candidate while its
current content hash continues to identify the published schema. In file mode,
`SchemaGovernance` binds the confirmed business-key rules to that exact schema
revision.

Stable technical model and field names are evidence; translated UI labels are
presentation. Numeric database IDs must not become portable identities.
Supporting groups and workflow issues are derived projections of the current
schema and model catalogue, not a second mutable evidence store.

## Completion and navigation

File mode completes only when both the schema catalogue and schema governance
exist and no required reuse dependency is absent from current model discovery;
it then unlocks Match data. Odoo source mode completes its first responsibility
when the eligible schema exists, then unlocks the bounded capture and freeze
responsibility.

The captured file-mode target is fresh evidence owned by the current
DataVersion. Optional Recipe publication can compile required portable Odoo
semantics, but the server identity, schema capture, and credentials never
become Recipe meaning. Project-owned application planning belongs to the
integrated Test workflow.

## Invalidation and recovery

An unchanged schema check does not invalidate dependent evidence. Detecting a
change preserves the current revision while the data manager reviews the
candidate. Confirming that candidate invalidates dependent mapping revisions
and later artifacts. Governance changes retain their existing invalidation
boundary. Local draft capture is a deliberate development path and may not be
presented as live Odoo evidence. Connector failures must retain the upstream
cause instead of being reduced to a generic browser status.

When field capture detects that the saved model catalogue belongs to older
read access, `SchemaWorkspaceService` raises
`OdooModelCatalogRefreshRequired`. The schema reader refreshes model discovery
once and repeats the field read with fresh identity verification. Both the
browser request and Recipe background job use this recovery. Model discovery
keeps the selected scope and current schema evidence. An existing live schema
still uses candidate comparison and explicit confirmation of changes.

Only this stale-catalogue failure triggers recovery. Authentication, transport,
target, and field-validation failures retain their normal handling. If access
changes again during the retry, the operation stops and retains the current
schema and saved choices.

## Odoo 19 and performance

Opening `/workspaces/{workspace_id}/schema` renders saved evidence through
`run_page_read`. The worker retains database handles for that page read and
closes them before returning. This avoids repeated database opens and keeps
the event loop responsive to other requests. Opening the page does not call
Odoo or refresh evidence; capture and change checks remain explicit actions.

Supporting-model derivation is one bounded pass over the captured fields plus
local catalogue joins. It performs no recursive graph walk and adds no network,
per-model, per-field, or per-row request.

Read capability is explicit and narrow: model catalogue, metadata, target
fingerprint, and planned record requests. Batch metadata and record reads by
model. Never call `fields_get`, selection providers, or relationship catalogues
inside a source-row loop.

A schema check performs the same bounded metadata read as an initial capture:
one `fields_get` request and at most one `default_get` request per selected
model, plus the bounded constraint batch. Candidate comparison and confirmation
run locally. Confirmation does not call Odoo again and introduces no per-field
or per-row requests.

A stale-catalogue recovery adds at most one model-discovery read and one repeat
of the bounded field check. A normal field check adds no model-discovery call.
The model-discovery read and its persistence run in the synchronous worker.

Odoo 19 inherited fields and dynamic selections must come from the connected
database. Do not hard-code a standard-only catalogue when custom modules are in
scope.

## Verification

- [`tests/integration/duckdb/test_workspace.py`](../../../tests/integration/duckdb/test_workspace.py)
- [`tests/integration/odoo/test_local_reader.py`](../../../tests/integration/odoo/test_local_reader.py)
- [`tests/application/workspace/test_odoo_connection.py`](../../../tests/application/workspace/test_odoo_connection.py)
- [`tests/application/workspace/test_schema_governance.py`](../../../tests/application/workspace/test_schema_governance.py)
- [`tests/domain/workspace/test_business_keys.py`](../../../tests/domain/workspace/test_business_keys.py)
- [`tests/domain/workspace/test_supporting_models.py`](../../../tests/domain/workspace/test_supporting_models.py)
- [`tests/application/workspace/test_workflow_issues.py`](../../../tests/application/workspace/test_workflow_issues.py)
- [`tests/integration/web/test_schema_presenter.py`](../../../tests/integration/web/test_schema_presenter.py)
- [`tests/integration/web/test_stage2_supporting_models.py`](../../../tests/integration/web/test_stage2_supporting_models.py)
- [`tests/integration/web/test_target_workflow.py`](../../../tests/integration/web/test_target_workflow.py)
- [`tests/integration/web/test_schema_capture_recovery.py`](../../../tests/integration/web/test_schema_capture_recovery.py)
- [`tests/e2e/test_stage2_matching_rules.py`](../../../tests/e2e/test_stage2_matching_rules.py)

Verify inherited fields, selection normalization, business-key revisioning,
read-only capability, batched requests, invalidation, and both source modes.

## Related documentation

- [User guide: Odoo data](../../user/workflow/02-odoo-data.md)
- [Workflow evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Stage 2 matching-rule acceptance evidence](../../testing/acceptance.md#stage-2-matching-rule-qualification-2026-09-23)
- [Architecture decisions](../../decisions/README.md)
