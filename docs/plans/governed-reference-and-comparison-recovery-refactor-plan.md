# Governed reference reads and comparison recovery refactor plan

## Status and authority

**Status:** Implemented on 2026-08-22. Verification results and any environment
limitations belong in the implementation handoff.

Sections 1 through 14 retain the delivery wording and acceptance gates used
during implementation. The current workflow and contract pages describe the
resulting operator and developer behavior.

**Plan date:** 2026-08-22.

This plan records the delivered behavior across Match data, Final review, and
Recipe reuse. The current user and developer workflow pages are authoritative
for operation. The refactor did not edit accepted source files, frozen
mappings, prepared data, comparison evidence, or published Recipe revisions in
place.

## 1. Decision summary

Impodo will replace two related implementation shortcuts with explicit shared
contracts:

1. One versioned governed-reference policy will authorize every read of a
   related Odoo model that is not part of the captured project schema. Match
   data, Final review, Recipe publication, and Recipe application will use the
   same decision.
2. One typed Odoo-read failure contract will identify what failed, which
   workflow stage owns recovery, and which action the operator can take. Final
   review will show the credential form only when a missing or unusable read
   credential is the problem.

The current `res.partner.country_id -> res.country.code` case will therefore
complete a read-only comparison without asking the operator to add Country to
the project schema. If the Odoo read credential is actually missing or
rejected, Final review will provide an inline way to enter a replacement and
retry.

This is a structural refactor, not a display-label exception. The policy will
be keyed by Odoo technical model and field identities, will be versioned and
hash-bound, and will reject every model or field combination that has not been
reviewed explicitly.

## 2. Product outcome

A data manager who has mapped **Country** by its reviewed Odoo business key can
approve the prepared data and compare it with Odoo. Impodo reads the bounded
supporting Country records required by that mapping, but it does not add
Country to the migration scope and does not write Country records.

When comparison cannot start, the page explains the actual recovery:

- A missing or rejected read key opens the read-key form.
- Invalid connection details link back to Odoo data.
- A temporary transport failure offers **Try comparison again**.
- Stale captured Odoo evidence offers **Refresh Odoo data**.
- Stale Match or Prepare evidence returns the operator to the owning stage.
- An unexpected internal failure shows safe support details and does not imply
  that the credential is wrong.

Every failure message continues to state that comparison is read-only and that
nothing was sent to Odoo.

## 3. Why the current behavior is incomplete

The current implementation already contains the beginning of the correct
policy in `src/impodo/reference_keys.py`. Match validation recognizes reviewed
standard references such as `res.country.code`, `res.lang.code`, and
`res.currency.name`.

Final review currently reconstructs a similar allow decision from flattened
metadata and record requests in `src/impodo/web/target_readers.py`. This avoids
one immediate false block, but it leaves separate implementations free to
drift. Recipe publication and Recipe application still expect every related
model to exist in the captured schema. A mapping may consequently pass Match
and Final review but fail when the same governed meaning is reused as a Recipe.

The comparison route also catches several unrelated exception families in one
branch. For a remote target, that branch opens `_remote_read_recovery.html`
regardless of whether the failure concerns credentials, transport, captured
schema, mapping evidence, prepared evidence, or local storage. The page can
therefore suggest a credential remedy for a schema-policy defect.

Changing only the displayed message would preserve both inconsistencies. This
plan instead gives the domain decision and the recovery decision one owner
each.

## 4. Scope

### 4.1 Included

The refactor includes:

- the reviewed standard-reference registry and its Odoo 19 field contracts;
- Match relationship validation and supporting-value lookups;
- Final review requirement planning and local or remote target reads;
- Recipe target-contract publication and Recipe application assessment;
- policy versioning, semantic hashes, and evidence invalidation;
- typed read failures and presentation-neutral recovery instructions;
- Final review routes, presenters, templates, support details, and tests;
- user and developer documentation affected by the delivered behavior.

### 4.2 Excluded

The refactor does not:

- expand the migration model scope automatically;
- authorize an arbitrary relation merely because Odoo exposes it;
- write supporting models or invoke a generic Odoo method;
- make numeric Odoo record IDs portable mapping or Recipe identities;
- change the mapping selected by the data manager;
- repair a frozen artifact in place;
- redesign the whole Odoo connection stage;
- introduce a general retry framework for every browser operation.

## 5. Invariants

The implementation must preserve these boundaries:

- Impodo targets Odoo 19 and uses the repository's existing read-only JSON-2
  connector boundaries.
- A supporting-reference read may use only `fields_get` and bounded record
  reads already allowed by the connector contract.
- A reviewed supporting model remains outside the migration write scope.
- The captured parent relationship must name the expected related model. A
  display label or coincidental field name cannot grant access.
- The requested key, scope, and display fields must match the reviewed
  technical contract exactly. Extra fields, `all_fields`, constraint discovery,
  and write use are denied.
- When the related model is present in captured evidence, captured metadata
  must agree with the reviewed contract. The registry cannot conceal drift.
- Remote records remain target-bound and identity-bound. Impodo stores neither
  read credentials nor Odoo record IDs in portable mapping or Recipe evidence.
- Existing submitted mappings, prepared evidence, comparison results, and
  Recipe revisions remain immutable. A changed policy creates new evidence.
- The reader groups work by model and bounded domain. It never performs an
  Odoo request inside a source-row loop.
- A recovery action is selected from an exception type or stable failure code,
  never by parsing an exception message.
- Credentials travel only in a protected POST body. They are never placed in a
  URL, rendered back into HTML, logged, or copied into project evidence.

## 6. Target architecture

### 6.1 One governed-reference policy

`src/impodo/reference_keys.py` will become the single domain owner for
supporting-reference authorization. The implementation may retain that module
name to avoid needless churn, but its public contract must describe more than
a key tuple.

The policy will expose immutable values equivalent to:

| Contract | Responsibility |
| --- | --- |
| `GovernedReferenceContract` | Declares one reviewed Odoo model, ordered key and scope fields, display field, exact readable field types, and allowed read purposes. |
| `ReferenceReadPurpose` | Distinguishes Match choices, Final review, Recipe publication, and Recipe application without changing the underlying authorization rules. |
| `ReferenceEvidenceKind` | Distinguishes a model governed by captured schema from a reviewed standard model outside that schema. |
| `GovernedReferenceDecision` | Returns the accepted contract and evidence kind, or a stable denial reason with the affected model and field. |
| `REFERENCE_POLICY_VERSION` | Identifies the policy contract version. |
| `REFERENCE_POLICY_HASH` | Hashes the canonical ordered registry and field contracts. |

The policy function will receive the captured parent relationship, related
model, ordered business key, scope fields, requested fields, intended purpose,
and optional captured related-model metadata. It will return a decision; its
callers will not repeat subsets of the rule.

Adding another reviewed standard reference will require a policy change,
Odoo 19 field-contract evidence, focused domain tests, and a new policy hash.
It will not require a page-specific allowlist.

### 6.2 Explicit Final review read requirements

`PreflightRequirementPlan` currently carries flattened metadata and record
requests. The target reader then tries to infer why an outside model appears.
The planner will instead emit an ordered `ReferenceReadRequirement` for every
supporting relationship read.

Each requirement will contain:

- the captured parent model and relationship field;
- the related model;
- the ordered key and scope fields;
- the bounded fields and domain required by comparison;
- the evidence kind and policy hash;
- the source mapping path used for safe support details.

The preflight service will pass the complete read plan, or an equivalent
immutable target-read request, to the reader. The reader will re-authorize each
requirement against the current captured schema and the shared policy before it
contacts Odoo. It will not infer authority from a union of field names.

For a remote comparison, the reader may perform at most one exact captured
schema-identity probe and one combined supplemental identity probe. It will
group record requests by model and bounded domain. For a local comparison, it
will request only the captured and authorized supplemental models named by the
plan; it will not include every relation that happens to appear in the schema.

The existing injected reader seam may use a temporary adapter while tests are
migrated. The adapter must be removed before the final acceptance gate so the
old flattened authorization path cannot become a second policy.

### 6.3 Match and supporting lookup evidence

Relationship validation and the Match value-choice reader will call the same
policy function. `SupportingLookupSnapshot` will gain a new contract version
that includes `reference_policy_hash`.

Old lookup snapshots will remain readable for audit. They will not be reusable
under the new policy because they do not prove which policy authorized the
read. The browser will obtain fresh bounded choices when they are next needed.
The lookup payload is already stored as versioned JSON, so this change should
not require a new DuckDB column.

`MappingValidationResult` will gain a new contract version containing the
reference-policy hash. Existing validation JSON remains readable. A validation
without the current hash is not current enough for a new submission or new
preparation, so the data manager must validate and submit the unchanged mapping
again. Because the validation hash changes, downstream preparation regenerates
through the existing evidence lifecycle instead of being edited in place.

### 6.4 Recipe publication and application

Recipe authoring will use the governed-reference decision when it compiles a
relationship target. A reviewed standard model may be absent from captured
schema only when the captured parent relation, business key, scope, and field
contracts agree with the policy.

Newly published Recipes will use target-contract version 2. The target contract
will record the reference evidence kind and policy hash, and will state that a
reviewed standard reference is read-only supporting evidence rather than a
migration write target.

Recipe application will assess target-contract version 2 with the same policy.
Absence of the reviewed supporting model from captured project schema will be
valid. If the model is captured, incompatible metadata will block application
rather than allowing the registry to override it.

Version 1 Recipe revisions will remain readable and applicable under their
current rules. Impodo will not rewrite published revisions. Newly published
revisions use version 2, and qualification creates fresh project evidence as it
does today.

### 6.5 One typed Odoo-read failure contract

A presentation-neutral application module will classify failures shared by
Odoo connection checks and Final review. It will reuse the existing typed
connector exceptions in `src/impodo/connectors.py` and the safe codes currently
owned by `src/impodo/web/remote_connection.py`.

The contract will expose values equivalent to:

| Contract | Responsibility |
| --- | --- |
| `OdooReadFailureCode` | Gives the failure a stable machine identity. |
| `RecoveryOwner` | Names Odoo data, Match data, Prepare data, Final review, or Support as the stage that can resolve it. |
| `OdooReadRecoveryKind` | Selects enter key, replace key, review connection, retry, refresh schema, review mapping, prepare again, reopen local profile, or contact support. |
| `OdooReadFailure` | Carries safe affected-object context and an optional support code; it never carries a secret. |
| `classify_odoo_read_failure()` | Maps typed exceptions and stable domain codes to one failure contract. |

Preflight and target-read boundaries will replace operator-visible generic
`ReadinessError` or `WorkspaceError` cases with typed subclasses or stable
failure codes. `_plain_ui_error()` may continue to sanitize unexpected text for
support display, but it must not choose a recovery action.

The remote connection status service and Final review will call the same
classifier. Their presenters may use different business wording, but they will
not maintain separate authentication, authorization, transport, or incomplete
response taxonomies.

### 6.6 Recovery presentation

The summary presenter will accept one `comparison_recovery` view instead of
independent `open_remote_read_recovery` and `remote_read_error` switches. The
view will contain safe business copy, one primary action, an optional secondary
navigation link, and support details.

`_remote_read_recovery.html` will be replaced or divided into:

- a general comparison-recovery panel; and
- a read-key form rendered only for enter-key or replace-key recovery.

The existing protected comparison POST may continue to accept a replacement
read key, bind it to the selected target, and immediately retry the read. The
route must retain its credential audit and secret-store behavior. Storage
unavailability is a separate failure from a missing key; the page must not
claim the key is missing when the secret store failed.

The panel will use an accessible alert or status role, preserve keyboard focus,
and show one obvious next action. Raw technical detail remains collapsed under
**Support details**.

## 7. Failure and recovery matrix

The classifier and presenter tests will implement this minimum matrix:

| Failure code | Owner | Primary recovery | Credential form |
| --- | --- | --- | --- |
| `ODOO_READ_KEY_MISSING` | Final review | Enter Odoo read key | Yes |
| `ODOO_READ_KEY_REJECTED` | Final review | Replace Odoo read key | Yes |
| `ODOO_READ_ACCESS_MISSING` | Odoo data | Use a key with read access | Yes |
| `ODOO_CONNECTION_DETAILS_INVALID` | Odoo data | Review Odoo connection | No |
| `ODOO_TARGET_UNREACHABLE` | Final review | Try comparison again | No |
| `ODOO_RESPONSE_INCOMPLETE` | Final review | Try comparison again | No |
| `ODOO_SCHEMA_EVIDENCE_MISSING` | Odoo data | Capture Odoo data | No |
| `ODOO_SCHEMA_EVIDENCE_STALE` | Odoo data | Refresh Odoo data | No |
| `REFERENCE_POLICY_MISMATCH` | Match data | Review field match | No |
| `MAPPING_EVIDENCE_STALE` | Match data | Review and submit matching | No |
| `PREPARED_EVIDENCE_STALE` | Prepare data | Prepare data again | No |
| `LOCAL_ODOO_PROFILE_REQUIRED` | Final review | Reconnect local Odoo | No |
| `COMPARISON_STORAGE_FAILED` | Support | Retry safely or view support details | No |
| `UNEXPECTED_COMPARISON_FAILURE` | Support | View support details | No |

A submitted mapping that uses a reviewed standard reference should not reach
`REFERENCE_POLICY_MISMATCH`. If it does, Impodo treats the event as an evidence
invariant failure and exposes support detail; it does not ask the operator to
add the supporting model merely to bypass the policy.

## 8. Delivery slices

### Slice 0: Characterize the existing boundaries

Before changing a contract, add focused regression fixtures for:

- `res.partner.country_id -> res.country.code` with only `res.partner` in the
  captured schema;
- the same relationship with compatible captured `res.country` metadata;
- an unreviewed outside model;
- missing, rejected, unauthorized, unreachable, incomplete, schema, mapping,
  preparation, storage, and unexpected comparison failures;
- local and remote readers with request-count assertions.

This slice proves the defect and freezes security, evidence, and batching
behavior. It must not assert the current incorrect credential prompt as desired
behavior.

### Slice 1: Introduce the canonical reference policy

1. Extend `reference_keys.py` with exact Odoo 19 field contracts, purpose,
   decision, version, and canonical hash.
2. Move relationship authorization from validators and browser readers behind
   that public contract.
3. Reject wrong relations, wrong keys, wrong scopes, extra fields, incompatible
   captured metadata, and write intent.
4. Keep compatibility wrappers only while callers migrate; mark and remove
   them in Slice 7.

Gate: domain tests demonstrate identical decisions for every caller and no web
dependency enters the domain module.

### Slice 2: Bind Match evidence and supporting lookups

1. Add the reference-policy hash to mapping-validation contract version 3.
2. Make submission and preparation currentness checks require the current
   policy hash.
3. Add the policy hash to supporting-lookup contract version 2 and its reuse
   identity.
4. Read prior contract versions for audit but refresh them before reuse.

Gate: a policy change cannot silently reuse validation, submission,
preparation, or lookup evidence authorized by the old policy.

### Slice 3: Make Final review requirements explicit

1. Add `ReferenceReadRequirement` to the preflight plan and semantic hash.
2. Pass the explicit plan to the local and remote target readers.
3. Re-authorize at the I/O boundary before contacting Odoo.
4. Limit local reads to planned models.
5. Preserve one primary schema probe, one combined supplemental probe, and
   grouped bounded record reads.

Gate: the Country regression compares successfully without changing schema
scope, and request-count tests prove that row count does not increase Odoo call
count.

### Slice 4: Give Recipe reuse the same meaning

1. Publish target-contract version 2 with reference evidence kind and policy
   hash.
2. Compile absent reviewed supporting models through the canonical policy.
3. Apply version 2 contracts through the same policy.
4. Preserve version 1 read and application compatibility.
5. Block incompatible explicit captured metadata and every write use.

Gate: direct Match, Final review, Recipe publication, and Recipe application
all accept or reject the same reference shapes.

### Slice 5: Introduce typed read failures

1. Extract connector failure classification from the remote status presenter
   into the application-level failure contract.
2. Add typed preflight, policy, evidence-currentness, storage, and local-profile
   failures at their source boundaries.
3. Map every known exception to the failure matrix.
4. Retain one explicit unknown fallback with a stable support code.

Gate: table-driven tests cover every exception subtype and no recovery routing
uses raw-message matching.

### Slice 6: Render the correct recovery

1. Replace summary recovery booleans with one typed presenter view.
2. Render the general recovery panel and conditional read-key form.
3. Preserve protected key replacement, target binding, audit, and immediate
   retry.
4. Add accessible focus and status behavior.
5. Keep technical detail under **Support details**.

Gate: browser tests prove that only credential failures render credential
inputs and that every other failure shows its owning action.

### Slice 7: Remove duplication and complete the evidence trail

1. Remove compatibility adapters, duplicate subset checks, broad recovery
   switches, and dead template branches.
2. Update workflow, contract, and runbook documentation.
3. Capture current screenshots when the user-visible recovery panel changes.
4. Run focused, scale, documentation, and full regression checks.

Gate: repository search finds one reference-policy implementation and one
failure classifier, and every definition-of-done item passes.

## 9. Expected file impact

The implementation should remain close to these owners:

| Area | Expected files |
| --- | --- |
| Reference policy | `src/impodo/reference_keys.py`; relationship validation under `src/impodo/domain/mapping/validation/`; focused policy tests |
| Match lookup evidence | `src/impodo/supporting_lookups.py`; `src/impodo/application/supporting_lookup_service.py`; mapping presenters and readers; lookup and mapping-validation tests |
| Final review planning | `src/impodo/planner.py`; `src/impodo/application/preflight_service.py`; preflight domain contracts and tests |
| Odoo read boundary | `src/impodo/web/target_readers.py`; local reader and connector tests |
| Recipe parity | `src/impodo/application/recipe_authoring_service.py`; `src/impodo/application/recipe_application_service.py`; Recipe contract and persistence tests |
| Failure classification | `src/impodo/connectors.py`; a new application failure module; `src/impodo/web/remote_connection.py`; classifier tests |
| Final review recovery | `src/impodo/web/routers/preflight.py`; `src/impodo/web/presenters/summary.py`; comparison recovery templates; `tests/test_web_app.py` |
| Documentation | Final review, Odoo data, Match data, Recipe, preflight contract, and evidence-lifecycle pages; current user screenshots |

The implementer must inspect the live diff before every slice. Existing
unrelated changes in these files belong to their current author and must not be
overwritten.

## 10. Verification strategy

### 10.1 Reference-policy tests

Tests must prove that:

- Country, Language, and Currency pass only with their reviewed technical
  relation and exact key, scope, display, and field-type contracts.
- An unreviewed model, wrong parent relation, wrong key order, wrong scope,
  extra field, `all_fields`, constraint discovery, or write use is rejected.
- An absent reviewed model and compatible explicit captured metadata produce
  equivalent authorization meaning.
- Incompatible captured metadata blocks the read.
- Local and remote readers enforce the same decision.
- Changing the policy changes validation, lookup, preflight, and Recipe hashes.
- Old evidence remains readable but cannot be silently reused as current.

### 10.2 Performance and Odoo-call tests

Tests must count requests, not merely inspect elapsed time:

- Final review performs no request per source row.
- Adding reviewed supporting models does not create one schema probe per model.
- Remote comparison uses at most one primary exact identity probe and one
  combined supplemental probe.
- Record reads remain grouped by model and bounded domain.
- Match choices retain their existing bounded limit and do not query once per
  distinct source row.
- Local reads include only models named by the explicit plan.

These checks guard against N+1 regressions that small fixtures would otherwise
hide.

### 10.3 Recovery tests

Table-driven tests must cover the full failure matrix. Browser tests must prove
that:

- missing and rejected keys show a blank credential input;
- permission failure never reveals the rejected value;
- transport and incomplete-response failures show retry without a key input;
- connection and schema failures link to Odoo data;
- mapping and preparation failures link to their owning stage;
- local-profile recovery retains its local reconnection flow;
- storage and unknown failures show safe support detail without blaming Odoo
  credentials;
- raw exceptions and secrets do not appear in normal page copy, URLs, logs, or
  stored evidence;
- comparison does not create or update an Odoo record on any failure path.

### 10.4 Focused test groups

The implementation should run at least these focused groups as their slices
land:

- `tests.test_business_keys`
- `tests.test_mapping_validation`
- supporting-lookup and target-reader tests
- `tests.test_preflight_service`
- preflight scale tests
- `tests.test_recipe_authoring`
- `tests.test_recipe_application`
- connector and local-reader tests
- `tests.test_web_app`
- presenter tests for safe error copy and recovery routing

The final slice must also run the repository's full test suite with the
workspace-safe temporary-directory configuration. Any unrelated failure or
timeout must be reported separately and must not be described as a pass.

## 11. Documentation and screenshot work

After the behavior exists, update these meanings:

- [Odoo data](../developer/workflow/02-odoo-data.md) explains captured migration
  scope versus reviewed read-only supporting references.
- [Match data](../developer/workflow/03-match-data.md) explains that one shared
  policy governs relationship keys and supporting choices.
- [Final review](../developer/workflow/05-final-review.md) explains typed
  recovery ownership and bounded supplemental reads.
- [Preflight contract](../developer/contracts/preflight.md) defines explicit
  reference read requirements and request-count bounds.
- [Recipe lifecycle](../developer/contracts/recipe-lifecycle.md) defines target
  contract version 2 and version 1 compatibility.
- [Evidence lifecycle](../developer/contracts/evidence-lifecycle.md) defines
  policy-hash invalidation without mutation.
- The corresponding user pages show what the data manager sees and which
  action resolves each failure.

Replace affected user screenshots only after the browser tests pass. Captions
must describe the operator's action and result, not the template or route name.

## 12. Blind spots and risk controls

### 12.1 Recipe application drift

Fixing Recipe publication without Recipe application would create a Recipe
that can be published but not reused. Both sides must ship in the same slice.

### 12.2 Explicit captured metadata disagreement

A permissive registry could hide an Odoo customization or capture defect. When
captured supporting metadata exists, disagreement is a block, not a reason to
fall back to the registry.

### 12.3 Policy changes and frozen evidence

Changing a reviewed contract changes meaning. The implementation must
invalidate currentness through hashes and regenerate evidence. It must never
patch stored validation, preparation, comparison, or Recipe JSON.

### 12.4 Local reader overreach

The local reader currently has enough context to include related models beyond
the explicit supporting requirement. The refactor must narrow local reads to
the plan rather than treating local access as broader authority.

### 12.5 Secret-store failure

Failure to store or retrieve a key is not proof that the operator omitted it.
The classifier must keep missing credential, rejected credential, and secret
storage failure separate.

### 12.6 Retry safety

Retry is appropriate for a read-only transport or incomplete-response failure.
It must not conceal a deterministic authorization or evidence mismatch, and it
must not create concurrent comparison publication races.

### 12.7 Error taxonomy ownership

Copying the remote connection service's exception mapping into Final review
would recreate the problem under new names. Classification belongs below both
presenters; business wording belongs in each presenter.

### 12.8 Odoo call growth

A clean domain design can still introduce N+1 metadata or record reads at the
adapter boundary. Request-count tests must cover multiple supporting models and
large source-row counts.

## 13. Rejected approaches

The implementation will not use these shortcuts:

- **Always show the credential form.** This gives an available action, but it
  misdiagnoses schema, mapping, preparation, storage, and transport failures.
- **Match the current error string.** Wording is not a stable recovery
  contract and can expose technical detail.
- **Add Country automatically to captured schema.** That changes migration
  scope, invalidates mapping evidence, and treats a read-only reference as a
  write target.
- **Permit every related Odoo model.** A relationship does not establish a
  reviewed portable business key or bounded read policy.
- **Keep a Final review allowlist.** Match and Recipe would continue to make
  different decisions.
- **Make existing frozen evidence current by migration.** A storage migration
  cannot prove that the new policy authorized an old read.
- **Probe each missing model independently.** This is an avoidable Odoo-call
  multiplier and risks N+1 behavior.

## 14. Definition of done

The refactor is complete only when all of these statements are true:

1. The Country regression compares successfully without adding `res.country`
   to the captured migration schema.
2. Match, Final review, Recipe publication, and Recipe application call one
   governed-reference policy and agree on every tested shape.
3. The policy has an explicit version and canonical hash bound into all new
   dependent evidence.
4. Old evidence remains readable but cannot silently pass as current under a
   new policy.
5. Final review receives explicit reference requirements and does not infer
   authorization from flattened request fields.
6. Remote and local readers enforce the same bounded model and field scope.
7. Odoo request-count tests rule out source-row and supporting-model N+1
   behavior.
8. Every known comparison failure maps to a typed owner and recovery action.
9. The credential form appears only for credential or read-access recovery.
10. Recovery routing contains no exception-message parsing.
11. No test or implementation path writes supporting records or stores a
    secret in project evidence.
12. Recipe target-contract version 2 works, and version 1 revisions retain
    compatibility.
13. User and developer documentation and screenshots describe the delivered
    behavior accurately.
14. Focused tests, scale tests, the full suite, documentation quality checks,
    `git diff --check`, and a final dirty-worktree review pass, or any
    limitations are reported precisely.
