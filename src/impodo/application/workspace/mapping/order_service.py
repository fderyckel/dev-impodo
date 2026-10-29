"""Build Stage 3 identity, relationship, and matching-order evidence."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import RLock, Thread
from typing import Protocol
from uuid import uuid4

from impodo.domain.execution.planner import (
    MAX_KEYS_PER_RECORD_REQUEST,
    PreflightRequirementPlan,
)
from impodo.domain.mapping.contracts import (
    MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES,
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    MappingDefinition,
    MappingTargetMode,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    RowInclusionPolicy,
    relationship_target_fields,
)
from impodo.domain.matching_order import (
    MatchingIdentityCheckStatus,
    MatchingIdentityChoiceFilter,
    MatchingIdentityChoicePage,
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
    MatchingOrderCheck,
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
    MatchingOrderConfidence,
    MatchingOrderFact,
    MatchingOrderPreference,
    MatchingOrderRecommendation,
    MatchingOrderRelationshipOutcome,
    MatchingOrderRelationshipResult,
    MatchingOrderSource,
    classify_matching_identity,
    live_matching_order_recommendation,
    matching_order_recommendation_hash,
    recommend_dataset_matching_order,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
    metadata_snapshot_payload,
    record_snapshot_payload,
)
from impodo.domain.relationship_dependencies import (
    extract_dataset_dependency_edges,
)
from impodo.domain.relationship_health import (
    RelationshipHealthNotCheckableReason,
    RelationshipHealthResult,
    RelationshipHealthStatus,
    classify_relationship_health,
)
from impodo.domain.schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.shared.access import (
    Actor,
    Capability,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    RelatedDatasetLink,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import (
    REFERENCE_POLICY_HASH,
    reference_policy_hash,
)


class MatchingOrderPreferenceRepository(Protocol):
    """Persist the current versioned table-order preference for a workspace."""

    def get_preference(
        self,
        workspace_id: str,
    ) -> MatchingOrderPreference | None:
        """Return the preference used by matching-order query and edit commands."""
        ...

    def save_preference(
        self,
        workspace_id: str,
        preference: MatchingOrderPreference,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Persist an authorized preference replacement from ``save_preference``."""
        ...

    def reset_preference(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Remove the expected preference version for ``reset_preference``."""
        ...

    def begin_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> tuple[MatchingOrderCheckAttempt, bool]:
        """Create or reuse the idempotent live-check attempt before Odoo opens."""
        ...

    def update_check_attempt(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
    ) -> None:
        """Persist progress from the background live-check worker."""
        ...

    def publish_check(
        self,
        workspace_id: str,
        check: MatchingOrderCheck,
        *,
        protected_snapshot_json: str,
        actor: Actor,
    ) -> MatchingOrderCheckStatus:
        """Publish a completed check and its protected snapshot atomically."""
        ...

    def fail_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> None:
        """Persist an expected terminal failure from the live-check worker."""
        ...

    def get_check_attempt(
        self,
        workspace_id: str,
        check_id: str,
    ) -> MatchingOrderCheckAttempt | None:
        """Return the named attempt for the check-status route."""
        ...

    def get_active_check_attempt(
        self,
        workspace_id: str,
    ) -> MatchingOrderCheckAttempt | None:
        """Return the in-progress attempt used to avoid duplicate workers."""
        ...

    def get_current_check(
        self,
        workspace_id: str,
    ) -> MatchingOrderCheck | None:
        """Return the last published check for matching-order presentation."""
        ...

    def get_identity_choice_review_page(
        self,
        workspace_id: str,
        check_id: str,
        filters: MatchingIdentityChoiceFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> MatchingIdentityChoicePage:
        """Return one protected page for the exact current live check."""
        ...


class MatchingOrderSourceKeys(Protocol):
    """Read exact frozen source key tuples without publishing them."""

    def source_key_tuples(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_keys: tuple[str, ...],
    ) -> tuple[tuple[str | None, ...], ...]:
        """Read exact source keys for direct live identity checks."""
        ...

    def source_identity_key_tuples(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_keys: tuple[str, ...],
        row_inclusion: RowInclusionPolicy,
    ) -> tuple[tuple[str | None, ...], ...]:
        """Read identity keys subject to the mapping's row-inclusion policy."""
        ...

    def source_included_key_tuples(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_keys: tuple[str, ...],
        row_inclusion: RowInclusionPolicy,
    ) -> tuple[tuple[str | None, ...], ...]:
        """Read relationship keys subject to the mapping's row-inclusion policy."""
        ...


@dataclass(frozen=True, slots=True)
class RelationshipHealthLiveProbe:
    """One exact in-memory relationship classification plan."""

    owner_dataset_id: str
    dependency_dataset_id: str | None
    target_field: str
    relationship_kind: str
    resolver_origin: ResolverOrigin
    required: bool
    target_model: str
    target_fields: tuple[str, ...]
    source_row_count: int
    source_key_counts: tuple[tuple[tuple[str, ...], int], ...]
    blank_row_count: int
    incomplete_row_count: int
    incoming_key_counts: tuple[tuple[tuple[str, ...], int], ...]
    relational_key_probe: IdentityRelationalLiveProbe | None = None

    @property
    def source_keys(self) -> tuple[tuple[str, ...], ...]:
        """Return distinct protected keys for bounded Odoo read planning."""

        return tuple(key for key, _count in self.source_key_counts)


@dataclass(frozen=True, slots=True)
class MatchingIdentityLiveProbe:
    """One exact saved identity and its ephemeral source keys."""

    dataset_id: str
    target_model: str
    target_fields: tuple[str, ...]
    source_keys: tuple[tuple[str | None, ...], ...]
    domain_fields: tuple[str, ...]
    domain_keys: tuple[tuple[str | None, ...], ...]
    relational_components: tuple[IdentityRelationalLiveProbe, ...]
    mode: MappingTargetMode
    on_existing: str | None


@dataclass(frozen=True, slots=True)
class IdentityRelationalLiveProbe:
    """One bounded governed Many2one key, including nested key components."""

    target_field: str
    target_index: int
    resolver_origin: ResolverOrigin
    related_model: str
    related_fields: tuple[str, ...]
    related_field_types: tuple[str, ...]
    source_widths: tuple[int, ...]
    source_keys: tuple[tuple[str, ...], ...]
    incoming_key_counts: tuple[tuple[tuple[str, ...], int], ...]
    nested_components: tuple[IdentityRelationalLiveProbe, ...] = ()
    incoming_dataset_id: str | None = None
    incoming_columns: tuple[str, ...] = ()
    incoming_row_inclusion: RowInclusionPolicy | None = None
    value_mappings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _SourceTupleCacheEntry:
    dataset_id: str
    row_inclusion: RowInclusionPolicy
    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]


class _MatchingSourceTupleCache:
    """Reuse one broader source projection for narrower Stage 3 checks."""

    def __init__(self, reader: MatchingOrderSourceKeys) -> None:
        self._reader = reader
        self._entries: list[_SourceTupleCacheEntry] = []

    def read(
        self,
        workspace_id: str,
        dataset_id: str,
        columns: tuple[str, ...],
        row_inclusion: RowInclusionPolicy,
        *,
        identity: bool,
    ) -> tuple[tuple[str | None, ...], ...]:
        """Return a row-aligned projection, slicing a cached superset when safe."""

        for entry in self._entries:
            if (
                entry.dataset_id != dataset_id
                or entry.row_inclusion != row_inclusion
                or any(column not in entry.columns for column in columns)
            ):
                continue
            indexes = tuple(entry.columns.index(column) for column in columns)
            return tuple(
                tuple(row[index] for index in indexes) for row in entry.rows
            )
        rows = (
            self._reader.source_identity_key_tuples(
                workspace_id,
                dataset_id,
                columns,
                row_inclusion,
            )
            if identity
            else self._reader.source_included_key_tuples(
                workspace_id,
                dataset_id,
                columns,
                row_inclusion,
            )
        )
        self._entries.append(
            _SourceTupleCacheEntry(
                dataset_id=dataset_id,
                row_inclusion=row_inclusion,
                columns=columns,
                rows=rows,
            )
        )
        return rows


@dataclass(frozen=True, slots=True)
class MatchingOrderPreparedCheck:
    """Immutable, bounded input assembled before opening Odoo."""

    check_id: str
    workspace_id: str
    selection: SourceSelection
    schema: OdooSchemaCatalog
    governance_hash: str
    draft_version: int
    draft_hash: str
    local_recommendation: MatchingOrderRecommendation
    tie_break_order: tuple[str, ...]
    relationship_probes: tuple[RelationshipHealthLiveProbe, ...]
    relationship_health_results: tuple[RelationshipHealthResult, ...]
    identity_probes: tuple[MatchingIdentityLiveProbe, ...]
    identity_results: tuple[MatchingIdentityResult, ...]
    requirements: PreflightRequirementPlan
    actor: Actor


MatchingOrderSnapshotReader = Callable[
    [PreflightRequirementPlan],
    tuple[MetadataSnapshot, RecordSnapshot],
]


_IDENTITY_CHECK_FIELD_TYPES = frozenset(
    {"char", "date", "datetime", "integer", "selection", "text"}
)
_RELATIONSHIP_CHECK_FIELD_TYPES = _IDENTITY_CHECK_FIELD_TYPES
_MAX_RELATIONAL_IDENTITY_DEPTH = 5


class _IdentityPlanError(Exception):
    """Stop one live identity probe without weakening its matching rule."""

    def __init__(
        self,
        reason: MatchingIdentityNotCheckableReason,
        *,
        evidence_limit: bool = False,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.evidence_limit = evidence_limit


class MatchingOrderService:
    """Recommend authoring order and run an explicit read-only refinement."""

    def __init__(
        self,
        preferences: MatchingOrderPreferenceRepository | None = None,
        authorization: WorkspaceAuthorizationPolicy | None = None,
        source_keys: MatchingOrderSourceKeys | None = None,
    ) -> None:
        self._preferences = preferences
        self._authorization = authorization
        self._source_keys = source_keys
        self._check_lock = RLock()
        self._running_check_ids: set[str] = set()

    def recommend(
        self,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        definition: MappingDefinition | None,
        *,
        related_links: Iterable[RelatedDatasetLink] = (),
        derived_links: Iterable[DerivedDatasetLink] = (),
        tie_break_order: Iterable[str] | None = None,
    ) -> MatchingOrderRecommendation:
        """Derive a deterministic local table order from saved mapping evidence.

        Called by matching-order pages and ``prepare_live_check``. It never
        opens Odoo: the optional live refinement starts only through
        ``start_live_check`` after its protected read plan is assembled.
        """
        dataset_ids = tuple(item.dataset_id for item in selection.datasets)
        known = set(dataset_ids)
        related_link_tuple = tuple(related_links)
        derived_link_tuple = tuple(derived_links)
        mapping_by_dataset = {
            item.dataset_id: item
            for item in (definition.datasets if definition is not None else ())
            if item.dataset_id in known
        }
        facts: set[MatchingOrderFact] = set()

        if definition is not None:
            for edge in extract_dataset_dependency_edges(definition.datasets):
                if (
                    edge.owner_dataset not in known
                    or edge.dependency_dataset not in known
                    or edge.is_self_reference
                ):
                    continue
                facts.add(
                    MatchingOrderFact(
                        owner_dataset=edge.owner_dataset,
                        dependency_dataset=edge.dependency_dataset,
                        source=MatchingOrderSource.SAVED_MAPPING,
                        confidence=MatchingOrderConfidence.CONFIRMED,
                        target_field=edge.target_field,
                    )
                )

        for link in related_link_tuple:
            if (
                link.child_dataset_id in known
                and link.parent_dataset_id in known
            ):
                facts.add(
                    MatchingOrderFact(
                        owner_dataset=link.child_dataset_id,
                        dependency_dataset=link.parent_dataset_id,
                        source=MatchingOrderSource.RELATED_TABLE,
                        confidence=MatchingOrderConfidence.CONFIRMED,
                    )
                )
        for link in derived_link_tuple:
            if (
                link.consumer_dataset_id in known
                and link.derived_dataset_id in known
            ):
                facts.add(
                    MatchingOrderFact(
                        owner_dataset=link.consumer_dataset_id,
                        dependency_dataset=link.derived_dataset_id,
                        source=MatchingOrderSource.DERIVED_TABLE,
                        confidence=MatchingOrderConfidence.CONFIRMED,
                    )
                )

        confirmed_pairs = {
            (fact.owner_dataset, fact.dependency_dataset)
            for fact in facts
            if fact.confidence is MatchingOrderConfidence.CONFIRMED
        }
        model_by_dataset = self._saved_models(
            selection.datasets,
            mapping_by_dataset,
            derived_link_tuple,
        )
        datasets_by_model: dict[str, list[str]] = {}
        for dataset_id, model_name in model_by_dataset.items():
            datasets_by_model.setdefault(model_name, []).append(dataset_id)
        schema_by_model = {item.name: item for item in schema.models}
        for owner_dataset, model_name in model_by_dataset.items():
            model = schema_by_model.get(model_name)
            if model is None:
                continue
            for field in model.fields:
                if (
                    not field.relation
                    or field.readonly
                    or field.type not in {"many2one", "many2many"}
                ):
                    continue
                candidates = tuple(
                    item
                    for item in datasets_by_model.get(field.relation, ())
                    if item != owner_dataset
                )
                if len(candidates) != 1:
                    continue
                pair = (owner_dataset, candidates[0])
                if pair in confirmed_pairs:
                    continue
                facts.add(
                    MatchingOrderFact(
                        owner_dataset=owner_dataset,
                        dependency_dataset=candidates[0],
                        source=MatchingOrderSource.ODOO_SCHEMA,
                        confidence=MatchingOrderConfidence.PRELIMINARY,
                        target_field=field.name,
                    )
                )

        return recommend_dataset_matching_order(
            dataset_ids,
            facts,
            tie_break_order=tie_break_order,
        )

    def current_preference(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderPreference | None:
        """Return the current display preference after edit authorization."""

        preferences, authorization = self._preference_dependencies()
        authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        return preferences.get_preference(workspace_id)

    def save_preference(
        self,
        workspace_id: str,
        selection: SourceSelection,
        ordered_dataset_ids: Iterable[str],
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> MatchingOrderPreference:
        """Save one exact current-dataset permutation with optimistic locking."""

        preferences, authorization = self._preference_dependencies()
        authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        ordered = tuple(ordered_dataset_ids)
        current_ids = tuple(item.dataset_id for item in selection.datasets)
        if len(ordered) != len(current_ids) or set(ordered) != set(current_ids):
            raise WorkspaceError(
                "Table order must contain every current source table exactly once"
            )
        preference = MatchingOrderPreference(
            workspace_id=workspace_id,
            version=(expected_version or 0) + 1,
            source_selection_hash=selection.content_hash,
            ordered_dataset_ids=ordered,
            updated_at=datetime.now(UTC),
            actor_issuer=actor.identity.issuer,
            actor_subject=actor.identity.subject_id,
            actor_display_name=actor.identity.display_name,
        )
        preferences.save_preference(
            workspace_id,
            preference,
            expected_version=expected_version,
            actor=actor,
        )
        return preference

    def reset_preference(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Remove a custom order and return Stage 3 to Impodo's order."""

        preferences, authorization = self._preference_dependencies()
        authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        preferences.reset_preference(
            workspace_id,
            expected_version=expected_version,
            actor=actor,
        )

    def prepare_live_check(
        self,
        workspace_id: str,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
        draft: MappingWorkingDraft | None,
        local_recommendation: MatchingOrderRecommendation,
        *,
        tie_break_order: Iterable[str] | None = None,
        actor: Actor,
    ) -> MatchingOrderPreparedCheck:
        """Build the complete source and target read plan before Odoo access."""

        preferences, authorization = self._preference_dependencies()
        del preferences
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        if self._source_keys is None:
            raise RuntimeError("Matching-order source-key reader is not configured")
        if draft is None:
            raise WorkspaceError(
                "Save your current matching choices before checking Odoo"
            )
        expected_governance_hash = (
            governance.content_hash if governance is not None else schema.content_hash
        )
        if (
            draft.workspace_id != workspace_id
            or draft.definition.source_selection_hash != selection.content_hash
            or draft.definition.schema_hash != expected_governance_hash
        ):
            raise WorkspaceError(
                "Save the current matching choices before checking Odoo"
            )

        mapping_by_id = {
            item.dataset_id: item for item in draft.definition.datasets
        }
        confirmed_keys = {
            (item.model, item.key_fields, item.scope_fields)
            for item in (governance.business_keys if governance is not None else ())
            if item.status is BusinessKeyStatus.CONFIRMED
        }
        schema_by_model = {item.name: item for item in schema.models}
        source_cache = _MatchingSourceTupleCache(self._source_keys)
        identity_probes: list[MatchingIdentityLiveProbe] = []
        identity_results: list[MatchingIdentityResult] = []
        for dataset in draft.definition.datasets:
            probe, result = self._identity_probe(
                workspace_id,
                dataset,
                mapping_by_id,
                schema_by_model,
                confirmed_keys,
                source_cache,
            )
            if probe is not None:
                identity_probes.append(probe)
            elif result is not None:
                identity_results.append(result)
        relationship_probes: list[RelationshipHealthLiveProbe] = []
        relationship_health_results: list[RelationshipHealthResult] = []
        for owner in draft.definition.datasets:
            for relationship in owner.relationships:
                probe, result = self._relationship_probe(
                    workspace_id,
                    owner,
                    relationship,
                    mapping_by_id,
                    schema_by_model,
                    confirmed_keys,
                    source_cache,
                )
                if probe is not None:
                    relationship_probes.append(probe)
                elif result is not None:
                    relationship_health_results.append(result)

        fields_by_model: dict[str, set[str]] = defaultdict(set)
        for probe in relationship_probes:
            if probe.relational_key_probe is None:
                fields_by_model[probe.target_model].update(probe.target_fields)
            else:
                for nested in _walk_relational_probes(
                    probe.relational_key_probe
                ):
                    fields_by_model[nested.related_model].update(
                        nested.related_fields
                    )
        for probe in identity_probes:
            fields_by_model[probe.target_model].update(probe.target_fields)
            for component in probe.relational_components:
                for nested in _walk_relational_probes(component):
                    fields_by_model[nested.related_model].update(
                        nested.related_fields
                    )
        metadata_requests = tuple(
            MetadataRequest(model=model, fields=tuple(sorted(fields)))
            for model, fields in sorted(fields_by_model.items())
        )
        projected_fields = {
            item.model: item.fields for item in metadata_requests
        }
        record_requests: list[RecordRequest] = []
        seen_requests: set[str] = set()
        relationship_keys_by_rule: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]],
            set[tuple[str, ...]],
        ] = defaultdict(set)
        for probe in relationship_probes:
            if probe.relational_key_probe is not None:
                for nested in _walk_relational_probes(
                    probe.relational_key_probe
                ):
                    if nested.resolver_origin is ResolverOrigin.DATASET:
                        continue
                    rule = (
                        nested.related_model,
                        _relational_leaf_fields(nested),
                        _relational_leaf_types(nested),
                    )
                    relationship_keys_by_rule[rule].update(nested.source_keys)
                continue
            if probe.resolver_origin is not ResolverOrigin.DATASET:
                related_fields = {
                    item.name: item
                    for item in schema_by_model[probe.target_model].fields
                }
                rule = (
                    probe.target_model,
                    probe.target_fields,
                    tuple(
                        related_fields[field].type
                        for field in probe.target_fields
                    ),
                )
                relationship_keys_by_rule[rule].update(probe.source_keys)
        for probe in identity_probes:
            for component in probe.relational_components:
                for nested in _walk_relational_probes(component):
                    rule = (
                        nested.related_model,
                        _relational_leaf_fields(nested),
                        _relational_leaf_types(nested),
                    )
                    relationship_keys_by_rule[rule].update(nested.source_keys)
        for (
            target_model,
            target_fields,
            field_types,
        ), source_keys in sorted(relationship_keys_by_rule.items()):
            for domain in _relationship_key_domain_chunks(
                target_fields,
                source_keys,
                field_types,
            ):
                request = RecordRequest(
                    model=target_model,
                    fields=projected_fields[target_model],
                    domain=tuple(domain),
                )
                identity = canonical_json(
                    {
                        "model": request.model,
                        "fields": list(request.fields),
                        "domain": list(request.domain),
                    }
                )
                if identity not in seen_requests:
                    seen_requests.add(identity)
                    record_requests.append(request)
        for probe in identity_probes:
            for domain in _matching_key_domain_chunks(
                probe.domain_fields,
                (
                    tuple(str(value) for value in key)
                    for key in probe.domain_keys
                    if all(value is not None for value in key)
                ),
            ):
                request = RecordRequest(
                    model=probe.target_model,
                    fields=projected_fields[probe.target_model],
                    domain=tuple(domain),
                )
                identity = canonical_json(
                    {
                        "model": request.model,
                        "fields": list(request.fields),
                        "domain": list(request.domain),
                    }
                )
                if identity not in seen_requests:
                    seen_requests.add(identity)
                    record_requests.append(request)
        version_decision = assess_odoo_operation(
            schema.odoo_version,
            OdooOperation.COMPARE,
        )
        if version_decision.allowed:
            odoo_major_version = version_decision.version.major
            if odoo_major_version is None:
                raise RuntimeError(
                    "An allowed Odoo operation must have a recognized major version"
                )
            current_reference_policy_hash = reference_policy_hash(
                odoo_major_version
            )
        else:
            current_reference_policy_hash = None
        requirements = PreflightRequirementPlan(
            metadata_requests=metadata_requests,
            record_requests=tuple(record_requests),
            reference_requirements=(),
            source_record_count=(
                sum(item.source_row_count for item in relationship_probes)
                + sum(len(item.source_keys) for item in identity_probes)
            ),
            reference_policy_hash=(
                current_reference_policy_hash or REFERENCE_POLICY_HASH
            ),
        )
        return MatchingOrderPreparedCheck(
            check_id=str(uuid4()),
            workspace_id=workspace_id,
            selection=selection,
            schema=schema,
            governance_hash=expected_governance_hash,
            draft_version=draft.version,
            draft_hash=draft.content_hash,
            local_recommendation=local_recommendation,
            tie_break_order=tuple(
                tie_break_order or local_recommendation.ordered_dataset_ids
            ),
            relationship_probes=tuple(relationship_probes),
            relationship_health_results=tuple(relationship_health_results),
            identity_probes=tuple(identity_probes),
            identity_results=tuple(identity_results),
            requirements=requirements,
            actor=actor,
        )

    def start_live_check(
        self,
        prepared: MatchingOrderPreparedCheck,
        reader: MatchingOrderSnapshotReader,
    ) -> MatchingOrderCheckAttempt:
        """Reserve one durable attempt and start its bounded read worker."""

        preferences, _authorization = self._preference_dependencies()
        now = datetime.now(UTC)
        attempt = MatchingOrderCheckAttempt(
            check_id=prepared.check_id,
            workspace_id=prepared.workspace_id,
            status=MatchingOrderCheckStatus.QUEUED,
            phase=MatchingOrderCheckPhase.QUEUED,
            message="Waiting to check Odoo",
            progress_percent=0,
            created_at=now,
            updated_at=now,
        )
        with self._check_lock:
            stored, created = preferences.begin_check(
                prepared.workspace_id,
                attempt,
                actor=prepared.actor,
            )
            if not created:
                return stored
            self._running_check_ids.add(prepared.check_id)
            Thread(
                target=self._run_live_check,
                args=(prepared, reader),
                name="impodo-matching-order-check",
                daemon=True,
            ).start()
        return attempt

    def current_check(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderCheck | None:
        """Return the latest completed live check after workspace authorization."""
        preferences, authorization = self._preference_dependencies()
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        return preferences.get_current_check(workspace_id)

    def identity_choice_review_page(
        self,
        workspace_id: str,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
        draft: MappingWorkingDraft | None,
        filters: MatchingIdentityChoiceFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
        actor: Actor,
    ) -> MatchingIdentityChoicePage:
        """Read current protected relational choices after exact authorization."""

        preferences, authorization = self._preference_dependencies()
        authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            workspace_id=workspace_id,
        )
        check = preferences.get_current_check(workspace_id)
        if check is None or not self.check_is_current(
            check,
            selection,
            schema,
            governance,
            draft,
        ):
            raise WorkspaceError(
                "Check current data again before reviewing identity choices"
            )
        return preferences.get_identity_choice_review_page(
            workspace_id,
            check.check_id,
            filters,
            page_size=page_size,
            after=after,
            before=before,
        )

    def active_check_attempt(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderCheckAttempt | None:
        """Return the active live-check attempt for polling and duplicate control."""
        preferences, authorization = self._preference_dependencies()
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        attempt = preferences.get_active_check_attempt(workspace_id)
        if attempt is None:
            return None
        with self._check_lock:
            running_here = attempt.check_id in self._running_check_ids
        if running_here:
            return attempt

        # A persisted active row with no worker in this process is an
        # interrupted prior attempt. Retire it instead of polling forever.
        finished = datetime.now(UTC)
        failed = replace(
            attempt,
            status=MatchingOrderCheckStatus.FAILED,
            phase=MatchingOrderCheckPhase.COMPLETE,
            message="Odoo check was interrupted",
            progress_percent=100,
            updated_at=finished,
            finished_at=finished,
            failure_message=(
                "Impodo restarted before publishing a new suggestion. "
                "The previous result is unchanged; start the check again."
            ),
        )
        preferences.fail_check(workspace_id, failed, actor=actor)
        return None

    def check_attempt(
        self,
        workspace_id: str,
        check_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderCheckAttempt:
        """Return one live-check attempt after bounded workspace authorization."""
        preferences, authorization = self._preference_dependencies()
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        attempt = preferences.get_check_attempt(workspace_id, check_id)
        if attempt is None:
            raise WorkspaceError("Matching-order check not found")
        return attempt

    @staticmethod
    def check_is_current(
        check: MatchingOrderCheck,
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance | None,
        draft: MappingWorkingDraft | None,
    ) -> bool:
        """Evaluate freshness without opening Odoo."""

        governance_hash = (
            governance.content_hash if governance is not None else schema.content_hash
        )
        return bool(
            not check.schema_changed
            and draft is not None
            and check.source_selection_hash == selection.content_hash
            and check.schema_hash == schema.content_hash
            and check.governance_hash == governance_hash
            and check.working_draft_version == draft.version
            and check.working_draft_hash == draft.content_hash
            and check.target_hash == schema.connection_target_hash
            and check.read_credential_binding_hash
            == schema.read_credential_binding_hash
            and check.read_principal_hash == schema.read_principal_hash
            and check.read_permission_hash == schema.read_permission_hash
            and check.read_context_hash == schema.read_context_hash
        )

    def _run_live_check(
        self,
        prepared: MatchingOrderPreparedCheck,
        reader: MatchingOrderSnapshotReader,
    ) -> None:
        preferences, _authorization = self._preference_dependencies()
        attempt = MatchingOrderCheckAttempt(
            check_id=prepared.check_id,
            workspace_id=prepared.workspace_id,
            status=MatchingOrderCheckStatus.RUNNING,
            phase=MatchingOrderCheckPhase.READING,
            message=(
                "Reading the exact Odoo identity and relationship keys in "
                "the saved matching draft"
            ),
            progress_percent=20,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        try:
            preferences.update_check_attempt(prepared.workspace_id, attempt)
            metadata, records = bind_snapshot_hashes(*reader(prepared.requirements))
            _validate_matching_snapshot_projection(
                prepared.requirements,
                metadata,
                records,
            )
            attempt = replace(
                attempt,
                phase=MatchingOrderCheckPhase.CLASSIFYING,
                message="Classifying record identities and relationship keys",
                progress_percent=70,
                updated_at=datetime.now(UTC),
            )
            preferences.update_check_attempt(prepared.workspace_id, attempt)
            schema_changed = _matching_schema_changed(prepared.schema, metadata)
            checked_relationship_health = (
                tuple(
                    _not_checkable_relationship_result(
                        owner_dataset_id=probe.owner_dataset_id,
                        target_field=probe.target_field,
                        relationship_kind=probe.relationship_kind,
                        resolver_origin=probe.resolver_origin,
                        related_model=probe.target_model,
                        dependency_dataset_id=probe.dependency_dataset_id,
                        required=probe.required,
                        reason=(
                            RelationshipHealthNotCheckableReason.ODOO_SCHEMA_CHANGED
                        ),
                    )
                    for probe in prepared.relationship_probes
                )
                if schema_changed
                else tuple(
                    _classify_relationship_probe(probe, records)
                    for probe in prepared.relationship_probes
                )
            )
            relationship_health_results = (
                *prepared.relationship_health_results,
                *checked_relationship_health,
            )
            results = (
                ()
                if schema_changed
                else tuple(
                    item
                    for health in checked_relationship_health
                    if (item := _matching_order_relationship_result(health))
                    is not None
                )
            )
            checked_identity_results = (
                tuple(
                    _not_checkable_identity_result(
                        dataset_id=probe.dataset_id,
                        target_model=probe.target_model,
                        target_fields=probe.target_fields,
                        reason=(
                            MatchingIdentityNotCheckableReason.ODOO_SCHEMA_CHANGED
                        ),
                    )
                    for probe in prepared.identity_probes
                )
                if schema_changed
                else tuple(
                    _classify_identity_probe(probe, records)
                    for probe in prepared.identity_probes
                )
            )
            identity_by_dataset = {
                item.dataset_id: item
                for item in (
                    *prepared.identity_results,
                    *checked_identity_results,
                )
            }
            identity_results = tuple(
                identity_by_dataset[dataset.dataset_id]
                for dataset in prepared.selection.datasets
                if dataset.dataset_id in identity_by_dataset
            )
            recommendation = (
                prepared.local_recommendation
                if schema_changed
                else live_matching_order_recommendation(
                    prepared.local_recommendation,
                    results,
                    tie_break_order=prepared.tie_break_order,
                )
            )
            check = MatchingOrderCheck(
                check_id=prepared.check_id,
                workspace_id=prepared.workspace_id,
                source_selection_hash=prepared.selection.content_hash,
                schema_hash=prepared.schema.content_hash,
                governance_hash=prepared.governance_hash,
                working_draft_version=prepared.draft_version,
                working_draft_hash=prepared.draft_hash,
                target_hash=metadata.fingerprint.target_hash,
                read_credential_binding_hash=(
                    prepared.schema.read_credential_binding_hash
                ),
                read_principal_hash=prepared.schema.read_principal_hash,
                read_permission_hash=prepared.schema.read_permission_hash,
                read_context_hash=prepared.schema.read_context_hash,
                relationship_results=results,
                unchecked_relationship_count=(
                    sum(not item.checked for item in relationship_health_results)
                ),
                ordered_dataset_ids=recommendation.ordered_dataset_ids,
                recommendation_hash=matching_order_recommendation_hash(
                    recommendation
                ),
                schema_changed=schema_changed,
                captured_at=datetime.now(UTC),
                actor_issuer=prepared.actor.identity.issuer,
                actor_subject=prepared.actor.identity.subject_id,
                actor_display_name=prepared.actor.identity.display_name,
                identity_results=identity_results,
                relationship_health_results=relationship_health_results,
            )
            protected_snapshot = canonical_json(
                {
                    "metadata": metadata_snapshot_payload(metadata),
                    "records": record_snapshot_payload(records),
                    "source_keys": [
                        {
                            "owner_dataset_id": item.owner_dataset_id,
                            "dependency_dataset_id": item.dependency_dataset_id,
                            "target_field": item.target_field,
                            "target_model": item.target_model,
                            "target_fields": list(item.target_fields),
                            "source_key_counts": [
                                {"key": list(key), "count": count}
                                for key, count in item.source_key_counts
                            ],
                            "incoming_key_counts": [
                                {"key": list(key), "count": count}
                                for key, count in item.incoming_key_counts
                            ],
                        }
                        for item in prepared.relationship_probes
                    ],
                    "identity_source_keys": [
                        {
                            "dataset_id": item.dataset_id,
                            "target_model": item.target_model,
                            "target_fields": list(item.target_fields),
                            "source_keys": [
                                list(key) for key in item.source_keys
                            ],
                            "relational_components": [
                                _protected_relational_probe_shape(component)
                                for component in item.relational_components
                            ],
                        }
                        for item in prepared.identity_probes
                    ],
                    "unresolved_identity_choices": (
                        []
                        if schema_changed
                        else [
                            choice
                            for probe in prepared.identity_probes
                            for choice in _unresolved_identity_choices(
                                probe,
                                records,
                            )
                        ]
                    ),
                }
            )
            attempt = replace(
                attempt,
                phase=MatchingOrderCheckPhase.PUBLISHING,
                message="Publishing the advisory recommendation",
                progress_percent=90,
                updated_at=datetime.now(UTC),
            )
            preferences.update_check_attempt(prepared.workspace_id, attempt)
            published_status = preferences.publish_check(
                prepared.workspace_id,
                check,
                protected_snapshot_json=protected_snapshot,
                actor=prepared.actor,
            )
            if published_status is MatchingOrderCheckStatus.STALE:
                return
        except Exception as error:  # noqa: BLE001 - background-check boundary
            finished = datetime.now(UTC)
            failed = replace(
                attempt,
                status=MatchingOrderCheckStatus.FAILED,
                phase=MatchingOrderCheckPhase.COMPLETE,
                message="Odoo check could not be completed",
                progress_percent=100,
                updated_at=finished,
                finished_at=finished,
                failure_message=_safe_matching_order_failure(error),
            )
            try:
                preferences.fail_check(
                    prepared.workspace_id,
                    failed,
                    actor=prepared.actor,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not persist a failed matching-order check"
                )
                return
        finally:
            with self._check_lock:
                self._running_check_ids.discard(prepared.check_id)

    def _relationship_probe(
        self,
        workspace_id: str,
        owner: DatasetMapping,
        relationship: RelationshipMapping,
        mappings: dict[str, DatasetMapping],
        schema_by_model,
        confirmed_keys,
        source_cache: _MatchingSourceTupleCache,
    ) -> tuple[RelationshipHealthLiveProbe | None, RelationshipHealthResult | None]:
        """Prepare one relationship simulation or one explicit safe reason."""

        resolver = relationship.resolver
        dependency = (
            mappings.get(resolver.dataset_id) if resolver.dataset_id else None
        )
        owner_model = schema_by_model.get(owner.target_model)
        owner_field = next(
            (
                item
                for item in (owner_model.fields if owner_model is not None else ())
                if item.name == relationship.target_field
            ),
            None,
        )
        related_model = resolver.model or (
            dependency.target_model if dependency is not None else ""
        ) or (owner_field.relation if owner_field is not None else "")
        required = relationship.required or relationship.required_on_create

        def unchecked(
            reason: RelationshipHealthNotCheckableReason,
        ) -> tuple[None, RelationshipHealthResult]:
            return None, _not_checkable_relationship_result(
                owner_dataset_id=owner.dataset_id,
                target_field=relationship.target_field,
                relationship_kind=relationship.kind,
                resolver_origin=resolver.origin,
                related_model=related_model or "unknown",
                dependency_dataset_id=resolver.dataset_id,
                required=required,
                reason=reason,
            )

        if relationship.kind == "one2many":
            return unchecked(
                RelationshipHealthNotCheckableReason.ONE2MANY_INVERSE_REQUIRED
            )
        if relationship.kind not in {"many2one", "many2many"}:
            return unchecked(
                RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP
            )
        if not related_model:
            return unchecked(RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE)
        if (
            owner_field is None
            or owner_field.type != relationship.kind
            or owner_field.relation != related_model
        ):
            return unchecked(RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE)
        key_fields, scope_fields = (
            _dataset_identity_fields(dependency)
            if resolver.origin is ResolverOrigin.DATASET
            and dependency is not None
            else relationship_target_fields(relationship)
        )
        target_fields = (*key_fields, *scope_fields)
        if not key_fields:
            return unchecked(RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE)
        if resolver.origin in {
            ResolverOrigin.TARGET_CATALOG,
            ResolverOrigin.TARGET_THEN_DATASET,
        } and (related_model, key_fields, scope_fields) not in confirmed_keys:
            return unchecked(
                RelationshipHealthNotCheckableReason.UNCONFIRMED_MATCHING_RULE
            )
        model = schema_by_model.get(related_model)
        schema_fields = (
            {item.name: item for item in model.fields} if model is not None else {}
        )
        if any(
            field not in schema_fields
            or schema_fields[field].type
            not in {*_RELATIONSHIP_CHECK_FIELD_TYPES, "many2one"}
            or schema_fields[field].exportable is False
            for field in target_fields
        ):
            return unchecked(
                RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP
            )
        nested_key = any(
            schema_fields[field].type == "many2one" for field in target_fields
        )
        if nested_key:
            if (
                relationship.kind != "many2one"
                or relationship.value_source is not RelationshipValueSource.SOURCE
            ):
                return unchecked(
                    RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP
                )
            return self._nested_relationship_probe(
                workspace_id,
                owner,
                relationship,
                mappings,
                schema_by_model,
                confirmed_keys,
                source_cache,
                target_fields=target_fields,
                required=required,
            )

        if relationship.value_source is RelationshipValueSource.SOURCE:
            source_by_target = {
                item.target_field: item.source_column_key
                for item in (*resolver.key_mappings, *resolver.scope_mappings)
            }
            source_columns = tuple(
                source_by_target.get(item, "") for item in target_fields
            )
            if any(not item for item in source_columns):
                return unchecked(
                    RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE
                )
        else:
            source_columns = ()

        incoming_columns: tuple[str, ...] = ()
        if resolver.origin in {
            ResolverOrigin.DATASET,
            ResolverOrigin.TARGET_THEN_DATASET,
        }:
            if (
                dependency is None
                or dependency.dataset_id == owner.dataset_id
                or dependency.target_model != related_model
            ):
                return unchecked(
                    RelationshipHealthNotCheckableReason.RELATED_DATASET_UNAVAILABLE
                )
            incoming_by_target = _direct_identity_source_columns(dependency)
            incoming_columns = tuple(
                incoming_by_target.get(item, "") for item in target_fields
            )
            if any(not item for item in incoming_columns):
                return unchecked(
                    RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE
                )
        try:
            raw_source = source_cache.read(
                workspace_id,
                owner.dataset_id,
                source_columns,
                owner.row_inclusion,
                identity=False,
            )
            raw_incoming = (
                source_cache.read(
                    workspace_id,
                    dependency.dataset_id,
                    incoming_columns,
                    dependency.row_inclusion,
                    identity=False,
                )
                if dependency is not None and incoming_columns
                else ()
            )
        except WorkspaceError:
            return unchecked(
                RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE
            )
        (
            source_key_counts,
            blank_row_count,
            incomplete_row_count,
        ) = _relationship_source_key_counts(
            relationship,
            raw_source,
            target_fields=target_fields,
        )
        incoming_key_counts = Counter(
            tuple(str(value).strip() for value in row)
            for row in raw_incoming
            if row and all(value is not None and str(value).strip() for value in row)
        )
        if (
            len(source_key_counts) > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES
            or len(incoming_key_counts)
            > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES
        ):
            return unchecked(
                RelationshipHealthNotCheckableReason.EVIDENCE_LIMIT_EXCEEDED
            )
        return RelationshipHealthLiveProbe(
            owner_dataset_id=owner.dataset_id,
            dependency_dataset_id=resolver.dataset_id,
            target_field=relationship.target_field,
            relationship_kind=relationship.kind,
            resolver_origin=resolver.origin,
            required=required,
            target_model=related_model,
            target_fields=target_fields,
            source_row_count=len(raw_source),
            source_key_counts=tuple(sorted(source_key_counts.items())),
            blank_row_count=blank_row_count,
            incomplete_row_count=incomplete_row_count,
            incoming_key_counts=tuple(sorted(incoming_key_counts.items())),
        ), None

    def _nested_relationship_probe(
        self,
        workspace_id: str,
        owner: DatasetMapping,
        relationship: RelationshipMapping,
        mappings: dict[str, DatasetMapping],
        schema_by_model,
        confirmed_keys,
        source_cache: _MatchingSourceTupleCache,
        *,
        target_fields: tuple[str, ...],
        required: bool,
    ) -> tuple[RelationshipHealthLiveProbe | None, RelationshipHealthResult | None]:
        """Prepare nested Many2one health from the shared recursive key tree."""

        resolver = relationship.resolver
        dependency = (
            mappings.get(resolver.dataset_id) if resolver.dataset_id else None
        )
        related_model = resolver.model or (
            dependency.target_model if dependency is not None else ""
        )

        def unchecked(
            reason: RelationshipHealthNotCheckableReason,
        ) -> tuple[None, RelationshipHealthResult]:
            return None, _not_checkable_relationship_result(
                owner_dataset_id=owner.dataset_id,
                target_field=relationship.target_field,
                relationship_kind=relationship.kind,
                resolver_origin=resolver.origin,
                related_model=related_model or "unknown",
                dependency_dataset_id=resolver.dataset_id,
                required=required,
                reason=reason,
            )

        try:
            skeleton = self._identity_relational_skeleton(
                IdentityComponentMapping(
                    source_column_keys=relationship.source_column_keys,
                    target_fields=(relationship.target_field,),
                    resolver=resolver,
                ),
                parent_model=owner.target_model,
                target_index=0,
                mappings=mappings,
                schema_by_model=schema_by_model,
                confirmed_keys=confirmed_keys,
            )
        except _IdentityPlanError as error:
            return unchecked(_relationship_plan_error_reason(error))
        if skeleton.related_fields != target_fields:
            return unchecked(
                RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE
            )

        try:
            raw_source = source_cache.read(
                workspace_id,
                owner.dataset_id,
                relationship.source_column_keys,
                owner.row_inclusion,
                identity=False,
            )
        except WorkspaceError:
            return unchecked(
                RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE
            )
        (
            raw_source_key_counts,
            blank_row_count,
            incomplete_row_count,
        ) = _relationship_source_key_counts(
            relationship,
            raw_source,
            target_fields=target_fields,
            expected_width=sum(skeleton.source_widths),
        )
        source_key_counts: Counter[tuple[str, ...]] = Counter()
        for source_key, count in raw_source_key_counts.items():
            normalized = _normalized_relational_source_key(
                skeleton,
                source_key,
                apply_current_translation=False,
            )
            if normalized is None:
                incomplete_row_count += count
            else:
                source_key_counts[normalized] += count
        if len(source_key_counts) > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES:
            return unchecked(
                RelationshipHealthNotCheckableReason.EVIDENCE_LIMIT_EXCEEDED
            )
        try:
            relational_key_probe = self._materialize_identity_relational_probe(
                workspace_id,
                skeleton,
                tuple(source_key_counts),
                source_cache,
            )
        except WorkspaceError:
            return unchecked(
                RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE
            )
        except _IdentityPlanError as error:
            return unchecked(_relationship_plan_error_reason(error))
        return RelationshipHealthLiveProbe(
            owner_dataset_id=owner.dataset_id,
            dependency_dataset_id=resolver.dataset_id,
            target_field=relationship.target_field,
            relationship_kind=relationship.kind,
            resolver_origin=resolver.origin,
            required=required,
            target_model=related_model,
            target_fields=target_fields,
            source_row_count=len(raw_source),
            source_key_counts=tuple(sorted(source_key_counts.items())),
            blank_row_count=blank_row_count,
            incomplete_row_count=incomplete_row_count,
            incoming_key_counts=relational_key_probe.incoming_key_counts,
            relational_key_probe=relational_key_probe,
        ), None

    def _identity_relational_skeleton(
        self,
        component: IdentityComponentMapping,
        *,
        parent_model: str,
        target_index: int,
        mappings: dict[str, DatasetMapping],
        schema_by_model,
        confirmed_keys,
        depth: int = 1,
        ancestors: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> IdentityRelationalLiveProbe:
        """Build one depth-limited related-key shape before reading source rows."""

        resolver = component.resolver
        if resolver is None or len(component.target_fields) != 1:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )
        if depth > _MAX_RELATIONAL_IDENTITY_DEPTH:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )
        dependency = (
            mappings.get(resolver.dataset_id) if resolver.dataset_id else None
        )
        parent_schema = schema_by_model.get(parent_model)
        parent_fields = {
            item.name: item
            for item in (parent_schema.fields if parent_schema is not None else ())
        }
        target_field = component.target_fields[0]
        parent_field = parent_fields.get(target_field)
        related_model = resolver.model or (
            dependency.target_model if dependency is not None else ""
        ) or (parent_field.relation if parent_field is not None else "")
        if (
            parent_field is None
            or parent_field.type != "many2one"
            or parent_field.relation != related_model
            or parent_field.exportable is False
        ):
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD
            )

        if resolver.origin is ResolverOrigin.DATASET:
            if dependency is None or dependency.target_model != related_model:
                raise _IdentityPlanError(
                    MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                )
            related_components = (
                *dependency.target_identity,
                *dependency.target_scope,
            )
            related_key_fields, related_scope_fields = _dataset_identity_fields(
                dependency
            )
        else:
            related_key_fields = tuple(
                item.target_field for item in resolver.key_mappings
            )
            related_scope_fields = tuple(
                item.target_field for item in resolver.scope_mappings
            )
            related_fields = (*related_key_fields, *related_scope_fields)
            shape_candidates = tuple(
                item
                for item in mappings.values()
                if item.target_model == related_model
                and _dataset_identity_fields(item)
                == (related_key_fields, related_scope_fields)
            )
            if dependency is not None:
                if dependency not in shape_candidates:
                    raise _IdentityPlanError(
                        MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                    )
                related_components = (
                    *dependency.target_identity,
                    *dependency.target_scope,
                )
            elif len(shape_candidates) == 1:
                shape = shape_candidates[0]
                related_components = (*shape.target_identity, *shape.target_scope)
            else:
                related_components = tuple(
                    IdentityComponentMapping(
                        source_column_keys=(item.source_column_key,),
                        target_fields=(item.target_field,),
                    )
                    for item in (*resolver.key_mappings, *resolver.scope_mappings)
                )

        related_fields = (*related_key_fields, *related_scope_fields)
        if not related_key_fields or (
            related_model,
            related_key_fields,
            related_scope_fields,
        ) not in confirmed_keys:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE
            )
        if tuple(
            field
            for item in related_components
            for field in item.target_fields
        ) != related_fields:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )
        signature = (related_model, related_fields)
        if signature in ancestors:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )

        related_schema = schema_by_model.get(related_model)
        related_schema_fields = {
            item.name: item
            for item in (
                related_schema.fields if related_schema is not None else ()
            )
        }
        related_field_types: list[str] = []
        source_widths: list[int] = []
        nested_components: list[IdentityRelationalLiveProbe] = []
        related_index = 0
        for related_component in related_components:
            if related_component.resolver is None:
                if len(related_component.source_column_keys) != len(
                    related_component.target_fields
                ):
                    raise _IdentityPlanError(
                        MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                    )
                for field_name in related_component.target_fields:
                    field = related_schema_fields.get(field_name)
                    if (
                        field is None
                        or field.type not in _IDENTITY_CHECK_FIELD_TYPES
                        or field.exportable is False
                    ):
                        raise _IdentityPlanError(
                            MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD
                        )
                    related_field_types.append(field.type)
                    source_widths.append(1)
                    related_index += 1
                continue
            if (
                len(related_component.target_fields) != 1
                or related_component.null_policy
                is IdentityNullPolicy.EXPLICIT_SCOPE_NULL
            ):
                raise _IdentityPlanError(
                    MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                )
            nested = self._identity_relational_skeleton(
                related_component,
                parent_model=related_model,
                target_index=related_index,
                mappings=mappings,
                schema_by_model=schema_by_model,
                confirmed_keys=confirmed_keys,
                depth=depth + 1,
                ancestors=(*ancestors, signature),
            )
            nested_width = sum(nested.source_widths)
            if len(related_component.source_column_keys) != nested_width:
                raise _IdentityPlanError(
                    MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
                )
            related_field_types.append("many2one")
            source_widths.append(nested_width)
            nested_components.append(nested)
            related_index += 1

        if len(component.source_column_keys) != sum(source_widths):
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )
        incoming_columns = (
            tuple(
                column
                for item in (
                    *dependency.target_identity,
                    *dependency.target_scope,
                )
                for column in item.source_column_keys
            )
            if dependency is not None
            else ()
        )
        if resolver.origin in {
            ResolverOrigin.DATASET,
            ResolverOrigin.TARGET_THEN_DATASET,
        } and (
            dependency is None
            or not incoming_columns
            or len(incoming_columns) != sum(source_widths)
        ):
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY
            )
        return IdentityRelationalLiveProbe(
            target_field=target_field,
            target_index=target_index,
            resolver_origin=resolver.origin,
            related_model=related_model,
            related_fields=related_fields,
            related_field_types=tuple(related_field_types),
            source_widths=tuple(source_widths),
            source_keys=(),
            incoming_key_counts=(),
            nested_components=tuple(nested_components),
            incoming_dataset_id=(
                dependency.dataset_id if dependency is not None else None
            ),
            incoming_columns=incoming_columns,
            incoming_row_inclusion=(
                dependency.row_inclusion if dependency is not None else None
            ),
            value_mappings=tuple(
                (item.source_value, item.target_value)
                for item in resolver.value_mappings
            ),
        )

    def _materialize_identity_relational_probe(
        self,
        workspace_id: str,
        skeleton: IdentityRelationalLiveProbe,
        source_keys: tuple[tuple[str, ...], ...],
        source_cache: _MatchingSourceTupleCache,
    ) -> IdentityRelationalLiveProbe:
        """Attach bounded incoming evidence to one recursive key shape."""

        distinct_source_keys = tuple(sorted(set(source_keys)))
        if len(distinct_source_keys) > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY,
                evidence_limit=True,
            )
        incoming_counts: Counter[tuple[str, ...]] = Counter()
        if skeleton.incoming_dataset_id and skeleton.incoming_columns:
            incoming_rows = source_cache.read(
                workspace_id,
                skeleton.incoming_dataset_id,
                skeleton.incoming_columns,
                skeleton.incoming_row_inclusion or RowInclusionPolicy(),
                identity=False,
            )
            incoming_counts.update(
                normalized
                for row in incoming_rows
                if (
                    normalized := _normalized_relational_source_key(
                        skeleton,
                        row,
                        apply_current_translation=False,
                    )
                )
                is not None
            )
        if len(incoming_counts) > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES:
            raise _IdentityPlanError(
                MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY,
                evidence_limit=True,
            )

        nested_by_index = {
            item.target_index: item for item in skeleton.nested_components
        }
        nested_components: list[IdentityRelationalLiveProbe] = []
        offset_by_index = _relational_source_offsets(skeleton.source_widths)
        for index, nested in sorted(nested_by_index.items()):
            start = offset_by_index[index]
            stop = start + skeleton.source_widths[index]
            nested_keys = tuple(
                sorted({key[start:stop] for key in source_keys})
            )
            nested_components.append(
                self._materialize_identity_relational_probe(
                    workspace_id,
                    nested,
                    nested_keys,
                    source_cache,
                )
            )
        return replace(
            skeleton,
            source_keys=distinct_source_keys,
            incoming_key_counts=tuple(sorted(incoming_counts.items())),
            nested_components=tuple(nested_components),
        )

    def _identity_probe(
        self,
        workspace_id: str,
        dataset: DatasetMapping,
        mappings: dict[str, DatasetMapping],
        schema_by_model,
        confirmed_keys,
        source_cache: _MatchingSourceTupleCache,
    ) -> tuple[MatchingIdentityLiveProbe | None, MatchingIdentityResult | None]:
        """Prepare one governed scalar/relational identity live check."""

        if not dataset.target_model:
            return None, None
        key_fields = tuple(
            field
            for component in dataset.target_identity
            for field in component.target_fields
        )
        scope_fields = tuple(
            field
            for component in dataset.target_scope
            for field in component.target_fields
        )
        target_fields = (*key_fields, *scope_fields)
        if dataset.mode is MappingTargetMode.ODOO_PINNED_UPDATE:
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=MatchingIdentityNotCheckableReason.ODOO_PINNED_SELECTION,
            )
        if (
            not dataset.source_identity_column_keys
            or not key_fields
            or len(target_fields) != len(set(target_fields))
        ):
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=MatchingIdentityNotCheckableReason.MAPPING_INCOMPLETE,
            )
        if (
            dataset.target_model,
            key_fields,
            scope_fields,
        ) not in confirmed_keys:
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=(
                    MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE
                ),
            )
        model = schema_by_model.get(dataset.target_model)
        fields = {item.name: item for item in model.fields} if model else {}

        source_columns: list[str] = []
        domain_fields: list[str] = []
        relational_plans: list[IdentityRelationalLiveProbe] = []
        target_index = 0
        for component in (*dataset.target_identity, *dataset.target_scope):
            component_source_columns = component.source_column_keys
            source_columns.extend(component_source_columns)
            if component.resolver is None:
                if len(component_source_columns) != len(component.target_fields):
                    return None, _not_checkable_identity_result(
                        dataset_id=dataset.dataset_id,
                        target_model=dataset.target_model,
                        target_fields=target_fields,
                        reason=MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY,
                    )
                if any(
                    field_name not in fields
                    or fields[field_name].type not in _IDENTITY_CHECK_FIELD_TYPES
                    or fields[field_name].exportable is False
                    for field_name in component.target_fields
                ):
                    return None, _not_checkable_identity_result(
                        dataset_id=dataset.dataset_id,
                        target_model=dataset.target_model,
                        target_fields=target_fields,
                        reason=(
                            MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD
                        ),
                    )
                domain_fields.extend(component.target_fields)
                target_index += len(component.target_fields)
                continue

            try:
                plan = self._identity_relational_skeleton(
                    component,
                    parent_model=dataset.target_model,
                    target_index=target_index,
                    mappings=mappings,
                    schema_by_model=schema_by_model,
                    confirmed_keys=confirmed_keys,
                )
            except _IdentityPlanError as error:
                return None, _not_checkable_identity_result(
                    dataset_id=dataset.dataset_id,
                    target_model=dataset.target_model,
                    target_fields=target_fields,
                    reason=error.reason,
                )
            domain_fields.extend(
                f"{plan.target_field}.{field_name}"
                for field_name in _relational_leaf_fields(plan)
            )
            relational_plans.append(plan)
            target_index += 1

        if not source_columns or len(source_columns) != len(set(source_columns)):
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY,
            )
        try:
            raw_source_keys = source_cache.read(
                workspace_id,
                dataset.dataset_id,
                tuple(source_columns),
                dataset.row_inclusion,
                identity=True,
            )
        except WorkspaceError:
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=(
                    MatchingIdentityNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE
                ),
            )

        source_keys: list[tuple[str | None, ...]] = []
        domain_keys: list[tuple[str | None, ...]] = []
        relational_source_keys: list[list[tuple[str, ...]]] = [
            [] for _item in relational_plans
        ]
        relational_plan_by_target_index = {
            plan.target_index: (index, plan)
            for index, plan in enumerate(relational_plans)
        }
        for row in raw_source_keys:
            owner_key: list[str | None] = []
            domain_key: list[str | None] = []
            source_offset = 0
            current_target_index = 0
            for component in (*dataset.target_identity, *dataset.target_scope):
                width = len(component.source_column_keys)
                values = tuple(row[source_offset : source_offset + width])
                source_offset += width
                if component.resolver is None:
                    owner_key.extend(values)
                    domain_key.extend(values)
                    current_target_index += len(component.target_fields)
                    continue
                plan_index, plan = relational_plan_by_target_index[
                    current_target_index
                ]
                normalized = _normalized_relational_source_key(
                    plan,
                    values,
                    apply_current_translation=True,
                )
                token = (
                    _relational_probe_source_token(plan, normalized)
                    if normalized is not None
                    else None
                )
                owner_key.append(token)
                domain_key.extend(normalized or values)
                if token is not None and normalized is not None:
                    relational_source_keys[plan_index].append(normalized)
                current_target_index += 1
            source_keys.append(tuple(owner_key))
            domain_keys.append(tuple(domain_key))

        relational_components: list[IdentityRelationalLiveProbe] = []
        try:
            for plan_index, plan in enumerate(relational_plans):
                relational_components.append(
                    self._materialize_identity_relational_probe(
                        workspace_id,
                        plan,
                        tuple(relational_source_keys[plan_index]),
                        source_cache,
                    )
                )
        except (WorkspaceError, _IdentityPlanError) as error:
            return None, _not_checkable_identity_result(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                reason=(
                    error.reason
                    if isinstance(error, _IdentityPlanError)
                    else MatchingIdentityNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE
                ),
            )
        return (
            MatchingIdentityLiveProbe(
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_fields=target_fields,
                source_keys=tuple(source_keys),
                domain_fields=tuple(domain_fields),
                domain_keys=tuple(domain_keys),
                relational_components=tuple(relational_components),
                mode=dataset.mode,
                on_existing=dataset.on_existing,
            ),
            None,
        )

    @staticmethod
    def effective_preference_order(
        preference: MatchingOrderPreference | None,
        recommended_dataset_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Reconcile a saved preference with the current selected datasets."""

        recommended = tuple(recommended_dataset_ids)
        if preference is None:
            return recommended
        known = set(recommended)
        retained = tuple(
            dataset_id
            for dataset_id in preference.ordered_dataset_ids
            if dataset_id in known
        )
        retained_set = set(retained)
        return (
            *retained,
            *(
                dataset_id
                for dataset_id in recommended
                if dataset_id not in retained_set
            ),
        )

    @staticmethod
    def move_dataset(
        ordered_dataset_ids: Iterable[str],
        dataset_id: str,
        *,
        direction: str,
    ) -> tuple[str, ...]:
        """Apply one accessible one-position move to a complete order."""

        ordered = list(ordered_dataset_ids)
        if direction not in {"up", "down"} or dataset_id not in ordered:
            raise WorkspaceError("Choose a current table and move direction")
        current = ordered.index(dataset_id)
        target = current + (-1 if direction == "up" else 1)
        if target < 0 or target >= len(ordered):
            raise WorkspaceError("This table cannot move further in that direction")
        ordered[current], ordered[target] = ordered[target], ordered[current]
        return tuple(ordered)

    def next_incomplete_dataset_id(
        self,
        selection: SourceSelection,
        recommendation: MatchingOrderRecommendation,
        definition: MappingDefinition | None,
        *,
        current_dataset_id: str,
        work_order: Iterable[str] | None = None,
    ) -> str | None:
        """Return the next recommended table lacking minimum saved choices."""

        source_by_id = {item.dataset_id: item for item in selection.datasets}
        mapping_by_id = {
            item.dataset_id: item
            for item in (definition.datasets if definition is not None else ())
        }
        incomplete = {
            dataset_id
            for dataset_id in recommendation.ordered_dataset_ids
            if not self.ready_for_check(
                source_by_id[dataset_id],
                mapping_by_id.get(dataset_id),
            )
        }
        if not incomplete:
            return None
        ordered = tuple(work_order or recommendation.ordered_dataset_ids)
        if set(ordered) != set(recommendation.ordered_dataset_ids):
            raise WorkspaceError("Matching work order does not match its datasets")
        try:
            current_index = ordered.index(current_dataset_id)
        except ValueError:
            current_index = -1
        candidates = (*ordered[current_index + 1 :], *ordered[: current_index + 1])
        return next(
            (
                dataset_id
                for dataset_id in candidates
                if dataset_id in incomplete and dataset_id != current_dataset_id
            ),
            None,
        )

    def _preference_dependencies(
        self,
    ) -> tuple[
        MatchingOrderPreferenceRepository,
        WorkspaceAuthorizationPolicy,
    ]:
        if self._preferences is None or self._authorization is None:
            raise RuntimeError("Matching-order preference storage is not configured")
        return self._preferences, self._authorization

    @staticmethod
    def ready_for_check(
        source: SourceDataset,
        mapping: DatasetMapping | None,
    ) -> bool:
        """Return whether one table has the minimum saved identity choices."""

        if mapping is None:
            return False
        if isinstance(source.source, OdooSourceBinding):
            return mapping.mode is MappingTargetMode.ODOO_PINNED_UPDATE
        return bool(
            mapping.target_model
            and mapping.source_identity_column_keys
            and mapping.target_identity
            and all(
                component.source_column_keys
                for component in mapping.target_identity
            )
        )

    @staticmethod
    def _saved_models(
        sources: Iterable[SourceDataset],
        mapping_by_dataset: dict[str, DatasetMapping],
        derived_links: Iterable[DerivedDatasetLink],
    ) -> dict[str, str]:
        derived_models = {
            item.derived_dataset_id: item.target_model for item in derived_links
        }
        result: dict[str, str] = {}
        for source in sources:
            mapping = mapping_by_dataset.get(source.dataset_id)
            if mapping is not None and mapping.target_model:
                result[source.dataset_id] = mapping.target_model
            elif isinstance(source.source, OdooSourceBinding):
                result[source.dataset_id] = source.source.model
            elif source.dataset_id in derived_models:
                result[source.dataset_id] = derived_models[source.dataset_id]
        return result


def _direct_identity_source_columns(
    dataset: DatasetMapping,
) -> dict[str, str]:
    """Return exact direct source-to-target identity fields, or an empty map."""

    result: dict[str, str] = {}
    for component in (*dataset.target_identity, *dataset.target_scope):
        if (
            component.resolver is not None
            or len(component.source_column_keys) != len(component.target_fields)
        ):
            return {}
        for source, target in zip(
            component.source_column_keys,
            component.target_fields,
            strict=True,
        ):
            if target in result:
                return {}
            result[target] = source
    return result


def _dataset_identity_fields(
    dataset: DatasetMapping,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the ordered governed key and scope fields of one dataset."""

    return (
        tuple(
            field
            for component in dataset.target_identity
            for field in component.target_fields
        ),
        tuple(
            field
            for component in dataset.target_scope
            for field in component.target_fields
        ),
    )


def _relational_source_offsets(widths: tuple[int, ...]) -> tuple[int, ...]:
    """Return deterministic starts for one flattened recursive source key."""

    result: list[int] = []
    offset = 0
    for width in widths:
        result.append(offset)
        offset += width
    return tuple(result)


def _relational_leaf_fields(
    probe: IdentityRelationalLiveProbe,
) -> tuple[str, ...]:
    """Flatten a related key to dotted scalar leaves for one bounded domain."""

    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    result: list[str] = []
    for index, field in enumerate(probe.related_fields):
        nested = nested_by_index.get(index)
        if nested is None:
            result.append(field)
            continue
        result.extend(
            f"{field}.{leaf}" for leaf in _relational_leaf_fields(nested)
        )
    return tuple(result)


def _walk_relational_probes(
    probe: IdentityRelationalLiveProbe,
) -> tuple[IdentityRelationalLiveProbe, ...]:
    """Return one deterministic pre-order traversal of a related-key tree."""

    return (
        probe,
        *(
            nested
            for child in sorted(
                probe.nested_components,
                key=lambda item: item.target_index,
            )
            for nested in _walk_relational_probes(child)
        ),
    )


def _relational_leaf_types(
    probe: IdentityRelationalLiveProbe,
) -> tuple[str, ...]:
    """Flatten the scalar leaf types in the same order as dotted fields."""

    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    result: list[str] = []
    for index, field_type in enumerate(probe.related_field_types):
        nested = nested_by_index.get(index)
        if nested is None:
            result.append(field_type)
        else:
            result.extend(_relational_leaf_types(nested))
    return tuple(result)


def _normalized_relational_source_key(
    probe: IdentityRelationalLiveProbe,
    values: tuple[str | None, ...],
    *,
    apply_current_translation: bool,
) -> tuple[str, ...] | None:
    """Normalize one flattened key while retaining recursive component shape."""

    if len(values) != sum(probe.source_widths):
        return None
    normalized_values = tuple(
        None if value is None else str(value).strip() for value in values
    )
    if any(not value for value in normalized_values):
        return None
    result = tuple(str(value) for value in normalized_values)
    if apply_current_translation and probe.value_mappings:
        translations = dict(probe.value_mappings)
        result = (translations.get(result[0], result[0]), *result[1:])

    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    offsets = _relational_source_offsets(probe.source_widths)
    rebuilt: list[str] = []
    for index, width in enumerate(probe.source_widths):
        start = offsets[index]
        part = result[start : start + width]
        nested = nested_by_index.get(index)
        if nested is None:
            rebuilt.extend(part)
            continue
        nested_key = _normalized_relational_source_key(
            nested,
            part,
            apply_current_translation=True,
        )
        if nested_key is None:
            return None
        rebuilt.extend(nested_key)
    return tuple(rebuilt)


def _relational_probe_source_token(
    probe: IdentityRelationalLiveProbe,
    source_key: tuple[str, ...],
) -> str | None:
    """Rebuild one portable nested token from flattened protected leaves."""

    if len(source_key) != sum(probe.source_widths):
        return None
    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    offsets = _relational_source_offsets(probe.source_widths)
    components: list[object | None] = []
    for index, width in enumerate(probe.source_widths):
        start = offsets[index]
        part = source_key[start : start + width]
        nested = nested_by_index.get(index)
        if nested is None:
            if width != 1:
                return None
            components.append(part[0])
        else:
            components.append(_relational_probe_source_token(nested, part))
    return _relational_identity_token(tuple(components))


def _translated_identity_values(
    values: tuple[str | None, ...],
    resolver: RelationshipResolver,
) -> tuple[str | None, ...]:
    """Apply the reviewed single-key translation used by a resolver."""

    if not values or not resolver.value_mappings or values[0] is None:
        return values
    translations = {
        item.source_value: item.target_value for item in resolver.value_mappings
    }
    return (translations.get(values[0], values[0]), *values[1:])


def _relational_identity_token(
    values: tuple[object | None, ...],
) -> str | None:
    """Encode one protected related business key without using its Odoo ID."""

    if not values or any(
        value is None or value is False or not str(value).strip()
        for value in values
    ):
        return None
    normalized = tuple(str(value).strip() for value in values)
    if len(normalized) == 1:
        return normalized[0]
    return canonical_json(list(normalized))


def _relationship_source_key_counts(
    relationship: RelationshipMapping,
    rows: Iterable[tuple[str | None, ...]],
    *,
    target_fields: tuple[str, ...],
    expected_width: int | None = None,
) -> tuple[Counter[tuple[str, ...]], int, int]:
    """Reduce included source rows to bounded choice counts and row causes."""

    source_counts: Counter[tuple[str, ...]] = Counter()
    blank_rows = 0
    incomplete_rows = 0
    translations = {
        item.source_value: item.target_value
        for item in relationship.resolver.value_mappings
    }
    constant_key = None
    if relationship.value_source is RelationshipValueSource.CONSTANT_EXISTING:
        reference = relationship.constant_reference
        if reference is not None:
            constant_key = tuple(
                item.value.strip()
                for item in (*reference.key_values, *reference.scope_values)
            )
    for row in rows:
        if constant_key is not None:
            if len(constant_key) != len(target_fields) or any(
                not item for item in constant_key
            ):
                incomplete_rows += 1
            else:
                source_counts[constant_key] += 1
            continue
        if not row or all(value is None for value in row):
            blank_rows += 1
            continue
        if relationship.kind == "many2many":
            if len(row) != 1 or len(target_fields) != 1 or row[0] is None:
                incomplete_rows += 1
                continue
            choices = tuple(
                value
                for value in dict.fromkeys(
                    item.strip() for item in row[0].split(relationship.separator)
                )
                if value
            )
            if not choices:
                blank_rows += 1
                continue
            for value in choices:
                source_counts[(translations.get(value, value),)] += 1
            continue
        if len(row) != (expected_width or len(target_fields)) or any(
            value is None for value in row
        ):
            incomplete_rows += 1
            continue
        key = tuple(str(value) for value in row)
        if translations and key:
            key = (translations.get(key[0], key[0]), *key[1:])
        source_counts[key] += 1
    return source_counts, blank_rows, incomplete_rows


def _matching_key_domain_chunks(
    fields: tuple[str, ...],
    keys: Iterable[tuple[str, ...]],
) -> tuple[list[object], ...]:
    """Build deterministic bounded exact domains outside every source-row loop."""

    unique = tuple(sorted(set(keys)))
    chunks: list[list[object]] = []
    for start in range(0, len(unique), MAX_KEYS_PER_RECORD_REQUEST):
        batch = unique[start : start + MAX_KEYS_PER_RECORD_REQUEST]
        if len(fields) == 1:
            chunks.append([[fields[0], "in", [item[0] for item in batch]]])
            continue
        expressions: list[list[object]] = []
        for key in batch:
            terms: list[object] = [
                [field, "=", value]
                for field, value in zip(fields, key, strict=True)
            ]
            expressions.append(["&"] * (len(terms) - 1) + terms)
        chunks.append(
            ["|"] * (len(expressions) - 1)
            + [item for expression in expressions for item in expression]
        )
    return tuple(chunks)


def _relationship_key_domain_chunks(
    fields: tuple[str, ...],
    keys: Iterable[tuple[str, ...]],
    field_types: tuple[str, ...],
) -> tuple[list[object], ...]:
    """Build bounded exact-or-case-only relationship read domains.

    Text fields use ``=ilike`` so the in-memory classifier can distinguish an
    exact match from a case-only near match. Non-text key parts retain exact
    comparison semantics. Requests remain grouped by model, rule, and bounded
    distinct-key batches rather than by source row.
    """

    if len(fields) != len(field_types):
        raise WorkspaceError("Relationship read-plan shape is invalid")
    unique = tuple(sorted(set(keys)))
    chunks: list[list[object]] = []
    text_types = {"char", "text", "selection"}
    for start in range(0, len(unique), MAX_KEYS_PER_RECORD_REQUEST):
        batch = unique[start : start + MAX_KEYS_PER_RECORD_REQUEST]
        if len(fields) == 1 and field_types[0] not in text_types:
            chunks.append([[fields[0], "in", [item[0] for item in batch]]])
            continue
        expressions: list[list[object]] = []
        for key in batch:
            terms: list[object] = [
                [
                    field,
                    "=ilike" if field_type in text_types else "=",
                    value,
                ]
                for field, field_type, value in zip(
                    fields,
                    field_types,
                    key,
                    strict=True,
                )
            ]
            expressions.append(["&"] * (len(terms) - 1) + terms)
        chunks.append(
            ["|"] * (len(expressions) - 1)
            + [item for expression in expressions for item in expression]
        )
    return tuple(chunks)


def _validate_matching_snapshot_projection(
    requirements: PreflightRequirementPlan,
    metadata: MetadataSnapshot,
    records: RecordSnapshot,
) -> None:
    """Reject missing or broadened live data before classification."""

    expected_metadata = {
        item.model: set(item.fields) for item in requirements.metadata_requests
    }
    if set(metadata.models) != set(expected_metadata):
        raise WorkspaceError("Odoo returned an incomplete metadata snapshot")
    for model, fields in expected_metadata.items():
        if set(metadata.models[model].fields) != fields:
            raise WorkspaceError("Odoo returned fields outside the bounded check")
    expected_records = {
        item.model: item.fields for item in requirements.record_requests
    }
    if set(records.records) != set(expected_records) or set(
        records.requested_fields
    ) != set(expected_records):
        raise WorkspaceError("Odoo returned an incomplete record snapshot")
    for model, fields in expected_records.items():
        if tuple(records.requested_fields[model]) != fields:
            raise WorkspaceError("Odoo omitted fields from the bounded check")


def _matching_schema_changed(
    captured: OdooSchemaCatalog,
    live: MetadataSnapshot,
) -> bool:
    """Compare only planned field semantics and the captured target identity."""

    fingerprint = live.fingerprint
    if (
        fingerprint.target_hash != captured.connection_target_hash
        or fingerprint.connection_mode != captured.connection_mode
        or fingerprint.database != captured.database
        or fingerprint.odoo_version != captured.odoo_version
    ):
        return True
    captured_models = {item.name: item for item in captured.models}
    compared = (
        "type",
        "required",
        "readonly",
        "relation",
        "relation_field",
        "stored",
        "computed",
        "has_inverse",
        "related",
        "translated",
        "company_dependent",
        "searchable",
        "sortable",
        "exportable",
        "digits",
        "currency_field",
    )
    for model_name, live_model in live.models.items():
        captured_model = captured_models.get(model_name)
        if captured_model is None:
            return True
        captured_fields = {item.name: item for item in captured_model.fields}
        for field_name, live_field in live_model.fields.items():
            captured_field = captured_fields.get(field_name)
            if captured_field is None or any(
                getattr(captured_field, name) != getattr(live_field, name)
                for name in compared
            ):
                return True
            if tuple(item[0] for item in captured_field.selection) != tuple(
                item[0] for item in live_field.selection
            ):
                return True
    return False


def _classify_relationship_probe(
    probe: RelationshipHealthLiveProbe,
    snapshot: RecordSnapshot,
) -> RelationshipHealthResult:
    source_key_counts = dict(probe.source_key_counts)
    incoming_key_counts = dict(probe.incoming_key_counts)
    if probe.relational_key_probe is None:
        target_keys = tuple(
            tuple(record.values.get(field) for field in probe.target_fields)
            for record in snapshot.records.get(probe.target_model, ())
        )
    else:
        relational = probe.relational_key_probe
        target_indexes: dict[
            int,
            tuple[Counter[str], dict[int, str]],
        ] = {}
        _target_counts, token_by_id = _relational_probe_target_index(
            relational,
            snapshot,
            target_indexes,
        )
        target_keys = tuple((token,) for token in token_by_id.values())
        tokenized_source: Counter[tuple[str, ...]] = Counter()
        for key, count in source_key_counts.items():
            token = _relational_probe_source_token(relational, key)
            if token is not None:
                tokenized_source[(token,)] += count
        tokenized_incoming: Counter[tuple[str, ...]] = Counter()
        for key, count in incoming_key_counts.items():
            token = _relational_probe_source_token(relational, key)
            if token is not None and _relational_nested_parts_are_resolved(
                relational,
                key,
                target_indexes,
            ):
                tokenized_incoming[(token,)] += count
        source_key_counts = dict(tokenized_source)
        incoming_key_counts = dict(tokenized_incoming)
    return classify_relationship_health(
        owner_dataset_id=probe.owner_dataset_id,
        target_field=probe.target_field,
        relationship_kind=probe.relationship_kind,
        resolver_origin=probe.resolver_origin,
        related_model=probe.target_model,
        dependency_dataset_id=probe.dependency_dataset_id,
        required=probe.required,
        source_row_count=probe.source_row_count,
        source_key_counts=source_key_counts,
        blank_row_count=probe.blank_row_count,
        incomplete_row_count=probe.incomplete_row_count,
        target_keys=target_keys,
        incoming_key_counts=incoming_key_counts,
    )


def _matching_order_relationship_result(
    health: RelationshipHealthResult,
) -> MatchingOrderRelationshipResult | None:
    """Project relationship coverage back to the conservative order hint."""

    if not health.checked or not health.dependency_dataset_id:
        return None
    missing = health.missing_count + health.incomplete_row_count
    if health.required:
        missing += health.blank_row_count
    ambiguous = health.ambiguous_count + health.case_mismatch_count
    if ambiguous:
        outcome = MatchingOrderRelationshipOutcome.AMBIGUOUS
    elif missing:
        outcome = MatchingOrderRelationshipOutcome.MISSING
    elif health.target_count and health.incoming_count:
        outcome = MatchingOrderRelationshipOutcome.MIXED
    elif health.target_count:
        outcome = MatchingOrderRelationshipOutcome.TARGET
    elif health.incoming_count:
        outcome = MatchingOrderRelationshipOutcome.INCOMING
    else:
        outcome = MatchingOrderRelationshipOutcome.UNCHECKED
    return MatchingOrderRelationshipResult(
        owner_dataset_id=health.owner_dataset_id,
        dependency_dataset_id=health.dependency_dataset_id,
        target_field=health.target_field,
        outcome=outcome,
        target_count=health.target_count,
        incoming_count=health.incoming_count,
        missing_count=missing,
        ambiguous_count=ambiguous,
    )


def _not_checkable_relationship_result(
    *,
    owner_dataset_id: str,
    target_field: str,
    relationship_kind: str,
    resolver_origin: ResolverOrigin,
    related_model: str,
    dependency_dataset_id: str | None,
    required: bool,
    reason: RelationshipHealthNotCheckableReason,
) -> RelationshipHealthResult:
    return RelationshipHealthResult(
        owner_dataset_id=owner_dataset_id,
        target_field=target_field,
        relationship_kind=relationship_kind,
        resolver_origin=resolver_origin,
        related_model=related_model,
        dependency_dataset_id=dependency_dataset_id,
        required=required,
        status=RelationshipHealthStatus.NOT_CHECKABLE,
        not_checkable_reason=reason,
    )


def _relationship_plan_error_reason(
    error: _IdentityPlanError,
) -> RelationshipHealthNotCheckableReason:
    """Project recursive identity planning failures into relationship health."""

    if error.evidence_limit:
        return RelationshipHealthNotCheckableReason.EVIDENCE_LIMIT_EXCEEDED
    if (
        error.reason
        is MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE
    ):
        return RelationshipHealthNotCheckableReason.UNCONFIRMED_MATCHING_RULE
    if (
        error.reason
        is MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD
    ):
        return RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP
    return RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE


def _relational_probe_target_index(
    probe: IdentityRelationalLiveProbe,
    snapshot: RecordSnapshot,
    cache: dict[int, tuple[Counter[str], dict[int, str]]],
) -> tuple[Counter[str], dict[int, str]]:
    """Build one recursive target token index once per related-key node."""

    cache_key = id(probe)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    nested_tokens_by_index: dict[int, dict[int, str]] = {}
    for index, nested in sorted(nested_by_index.items()):
        _counts, token_by_id = _relational_probe_target_index(
            nested,
            snapshot,
            cache,
        )
        nested_tokens_by_index[index] = token_by_id

    token_counts: Counter[str] = Counter()
    token_by_id: dict[int, str] = {}
    for record in snapshot.records.get(probe.related_model, ()):
        components: list[object | None] = []
        unresolved_relation = False
        for index, field in enumerate(probe.related_fields):
            nested = nested_by_index.get(index)
            if nested is None:
                components.append(record.values.get(field))
                continue
            relation_id = _many2one_record_id(record.values.get(field))
            if relation_id is None:
                components.append(None)
                continue
            token = nested_tokens_by_index[index].get(relation_id)
            if token is None:
                unresolved_relation = True
                break
            components.append(token)
        if unresolved_relation:
            continue
        token = _relational_identity_token(tuple(components))
        if token is None:
            continue
        token_counts[token] += 1
        token_by_id[record.odoo_id] = token

    result = (token_counts, token_by_id)
    cache[cache_key] = result
    return result


def _relational_source_key_is_resolved(
    probe: IdentityRelationalLiveProbe,
    source_key: tuple[str, ...],
    target_indexes: dict[int, tuple[Counter[str], dict[int, str]]],
) -> bool:
    """Check one flattened key against every resolver node without new reads."""

    if len(source_key) != sum(probe.source_widths):
        return False
    nested_by_index = {
        item.target_index: item for item in probe.nested_components
    }
    offsets = _relational_source_offsets(probe.source_widths)
    for index, nested in nested_by_index.items():
        start = offsets[index]
        stop = start + probe.source_widths[index]
        if not _relational_source_key_is_resolved(
            nested,
            source_key[start:stop],
            target_indexes,
        ):
            return False

    token_counts, _token_by_id = target_indexes[id(probe)]
    incoming_counts = dict(probe.incoming_key_counts)
    source_token = _relational_probe_source_token(probe, source_key)
    if source_token is None:
        return False
    target_count = token_counts[source_token]
    incoming_count = incoming_counts.get(source_key, 0)
    if target_count > 1:
        return False
    if probe.resolver_origin is ResolverOrigin.TARGET_CATALOG:
        return target_count == 1
    if probe.resolver_origin is ResolverOrigin.DATASET:
        return incoming_count == 1
    return target_count == 1 or (target_count == 0 and incoming_count == 1)


def _relational_nested_parts_are_resolved(
    probe: IdentityRelationalLiveProbe,
    source_key: tuple[str, ...],
    target_indexes: dict[int, tuple[Counter[str], dict[int, str]]],
) -> bool:
    """Require nested components before an incoming parent can be usable."""

    if len(source_key) != sum(probe.source_widths):
        return False
    offsets = _relational_source_offsets(probe.source_widths)
    for nested in probe.nested_components:
        start = offsets[nested.target_index]
        stop = start + probe.source_widths[nested.target_index]
        if not _relational_source_key_is_resolved(
            nested,
            source_key[start:stop],
            target_indexes,
        ):
            return False
    return True


def _relational_probe_resolution(
    probe: IdentityRelationalLiveProbe,
    snapshot: RecordSnapshot,
) -> tuple[bool, Counter[str], dict[int, str]]:
    """Resolve one related-key tree bottom-up from a bounded target snapshot."""

    target_indexes: dict[int, tuple[Counter[str], dict[int, str]]] = {}
    token_counts, token_by_id = _relational_probe_target_index(
        probe,
        snapshot,
        target_indexes,
    )
    resolved = all(
        _relational_source_key_is_resolved(
            probe,
            source_key,
            target_indexes,
        )
        for source_key in probe.source_keys
    )
    return resolved, token_counts, token_by_id


def _classify_identity_probe(
    probe: MatchingIdentityLiveProbe,
    snapshot: RecordSnapshot,
) -> MatchingIdentityResult:
    relational_by_index = {
        item.target_index: item for item in probe.relational_components
    }
    related_tokens_by_component: dict[int, dict[int, str]] = {}
    for component in probe.relational_components:
        resolved, _counts, token_by_id = _relational_probe_resolution(
            component,
            snapshot,
        )
        if not resolved:
            return _not_checkable_identity_result(
                dataset_id=probe.dataset_id,
                target_model=probe.target_model,
                target_fields=probe.target_fields,
                reason=(
                    MatchingIdentityNotCheckableReason.RELATIONAL_IDENTITY_UNRESOLVED
                ),
            )
        related_tokens_by_component[component.target_index] = token_by_id

    target_keys: list[tuple[object | None, ...]] = []
    for record in snapshot.records.get(probe.target_model, ()):
        values: list[object | None] = []
        unresolved_target_relation = False
        for index, field in enumerate(probe.target_fields):
            component = relational_by_index.get(index)
            if component is None:
                values.append(record.values.get(field))
                continue
            relation_id = _many2one_record_id(record.values.get(field))
            if relation_id is None:
                values.append(None)
                continue
            token_by_id = related_tokens_by_component[index]
            token = token_by_id.get(relation_id)
            if token is None:
                unresolved_target_relation = True
                break
            values.append(token)
        if unresolved_target_relation:
            return _not_checkable_identity_result(
                dataset_id=probe.dataset_id,
                target_model=probe.target_model,
                target_fields=probe.target_fields,
                reason=(
                    MatchingIdentityNotCheckableReason.RELATIONAL_IDENTITY_UNRESOLVED
                ),
            )
        target_keys.append(tuple(values))
    return classify_matching_identity(
        dataset_id=probe.dataset_id,
        target_model=probe.target_model,
        target_fields=probe.target_fields,
        source_keys=probe.source_keys,
        target_keys=tuple(target_keys),
        mode=probe.mode,
        on_existing=probe.on_existing,
    )


def _unresolved_identity_choices(
    probe: MatchingIdentityLiveProbe,
    snapshot: RecordSnapshot,
) -> tuple[dict[str, object], ...]:
    """Build protected, value-bearing causes for relational identity review."""

    result: list[dict[str, object]] = []
    for component in probe.relational_components:
        result.extend(
            _unresolved_relational_choices(
                dataset_id=probe.dataset_id,
                probe=component,
                snapshot=snapshot,
                field_path=component.target_field,
            )
        )
    deduplicated = {
        canonical_json(item): item for item in result
    }
    return tuple(deduplicated[key] for key in sorted(deduplicated))


def _protected_relational_probe_shape(
    probe: IdentityRelationalLiveProbe,
) -> dict[str, object]:
    """Describe a recursive protected probe without duplicating key values."""

    return {
        "target_field": probe.target_field,
        "resolver_origin": probe.resolver_origin.value,
        "related_model": probe.related_model,
        "related_fields": list(probe.related_fields),
        "leaf_fields": list(_relational_leaf_fields(probe)),
        "nested_components": [
            _protected_relational_probe_shape(item)
            for item in sorted(
                probe.nested_components,
                key=lambda item: item.target_index,
            )
        ],
    }


def _unresolved_relational_choices(
    *,
    dataset_id: str,
    probe: IdentityRelationalLiveProbe,
    snapshot: RecordSnapshot,
    field_path: str,
) -> tuple[dict[str, object], ...]:
    """Return deepest actionable failures before reporting their parent key."""

    nested_choices = tuple(
        choice
        for nested in sorted(
            probe.nested_components,
            key=lambda item: item.target_index,
        )
        for choice in _unresolved_relational_choices(
            dataset_id=dataset_id,
            probe=nested,
            snapshot=snapshot,
            field_path=f"{field_path}.{nested.target_field}",
        )
    )
    if nested_choices:
        return nested_choices

    _resolved, target_counts, _token_by_id = _relational_probe_resolution(
        probe,
        snapshot,
    )
    incoming_counts = dict(probe.incoming_key_counts)
    result: list[dict[str, object]] = []
    for source_key in probe.source_keys:
        source_token = _relational_probe_source_token(probe, source_key)
        if source_token is None:
            continue
        target_count = target_counts[source_token]
        incoming_count = incoming_counts.get(source_key, 0)
        if target_count > 1:
            cause = "ambiguous_target"
        elif probe.resolver_origin is ResolverOrigin.TARGET_CATALOG:
            cause = "missing_target" if target_count == 0 else ""
        elif probe.resolver_origin is ResolverOrigin.DATASET:
            cause = (
                "ambiguous_incoming"
                if incoming_count > 1
                else "missing_incoming"
                if incoming_count == 0
                else ""
            )
        else:
            cause = (
                "ambiguous_incoming"
                if target_count == 0 and incoming_count > 1
                else "missing_target_and_incoming"
                if target_count == 0 and incoming_count == 0
                else ""
            )
        if not cause:
            continue
        result.append(
            {
                "dataset_id": dataset_id,
                "target_field": field_path,
                "related_model": probe.related_model,
                "related_fields": list(_relational_leaf_fields(probe)),
                "key": list(source_key),
                "cause": cause,
                "target_match_count": target_count,
                "incoming_match_count": incoming_count,
            }
        )
    return tuple(result)


def _many2one_record_id(value: object) -> int | None:
    """Return one ephemeral Odoo relation ID from a search-read value."""

    raw = value[0] if isinstance(value, (list, tuple)) and value else value
    if raw is None or raw is False or isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, str)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _not_checkable_identity_result(
    *,
    dataset_id: str,
    target_model: str,
    target_fields: tuple[str, ...],
    reason: MatchingIdentityNotCheckableReason,
) -> MatchingIdentityResult:
    return MatchingIdentityResult(
        dataset_id=dataset_id,
        target_model=target_model,
        target_fields=target_fields,
        status=MatchingIdentityCheckStatus.NOT_CHECKABLE,
        not_checkable_reason=reason,
    )


def _matching_key_value(value: object) -> str | None:
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _safe_matching_order_failure(_error: Exception) -> str:
    """Keep credentials, rows, and implementation details out of status."""

    return (
        "Impodo stopped before publishing a new suggestion. The previous "
        "result is unchanged; review the Odoo read connection and try again."
    )
