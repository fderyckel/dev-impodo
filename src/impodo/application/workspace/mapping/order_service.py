"""Build Stage 3 matching-order advice from saved local evidence only."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock, Thread
from typing import Callable, Iterable, Protocol
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingTargetMode,
    RelationshipMapping,
    RelationshipValueSource,
    ResolverOrigin,
    relationship_target_fields,
)
from impodo.domain.matching_order import (
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
    live_matching_order_recommendation,
    matching_order_recommendation_hash,
    recommend_dataset_matching_order,
)
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
    metadata_snapshot_payload,
    record_snapshot_payload,
)
from impodo.domain.execution.planner import (
    MAX_KEYS_PER_RECORD_REQUEST,
    PreflightRequirementPlan,
)
from impodo.domain.schema.governance import (
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.serialization import canonical_json
from impodo.domain.relationship_dependencies import (
    extract_dataset_dependency_edges,
)
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.shared.access import (
    Actor,
    Capability,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    MappingWorkingDraft,
    SourceDataset,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    RelatedDatasetLink,
)
from impodo.domain.workspace.errors import WorkspaceError


class MatchingOrderPreferenceRepository(Protocol):
    """Persist the current versioned table-order preference for a workspace."""

    def get_preference(
        self,
        workspace_id: str,
    ) -> MatchingOrderPreference | None: ...

    def save_preference(
        self,
        workspace_id: str,
        preference: MatchingOrderPreference,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None: ...

    def reset_preference(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None: ...

    def begin_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> tuple[MatchingOrderCheckAttempt, bool]: ...

    def update_check_attempt(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
    ) -> None: ...

    def publish_check(
        self,
        workspace_id: str,
        check: MatchingOrderCheck,
        *,
        protected_snapshot_json: str,
        actor: Actor,
    ) -> MatchingOrderCheckStatus: ...

    def fail_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> None: ...

    def get_check_attempt(
        self,
        workspace_id: str,
        check_id: str,
    ) -> MatchingOrderCheckAttempt | None: ...

    def get_active_check_attempt(
        self,
        workspace_id: str,
    ) -> MatchingOrderCheckAttempt | None: ...

    def get_current_check(
        self,
        workspace_id: str,
    ) -> MatchingOrderCheck | None: ...


class MatchingOrderSourceKeys(Protocol):
    """Read exact frozen source key tuples without publishing them."""

    def source_key_tuples(
        self,
        workspace_id: str,
        dataset_id: str,
        source_column_keys: tuple[str, ...],
    ) -> tuple[tuple[str | None, ...], ...]: ...


@dataclass(frozen=True, slots=True)
class MatchingOrderLiveProbe:
    """One exact in-memory hybrid relationship classification plan."""

    owner_dataset_id: str
    dependency_dataset_id: str
    target_field: str
    target_model: str
    target_fields: tuple[str, ...]
    source_keys: tuple[tuple[str, ...], ...]
    incoming_keys: tuple[tuple[str, ...], ...]


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
    probes: tuple[MatchingOrderLiveProbe, ...]
    unchecked_relationship_count: int
    requirements: PreflightRequirementPlan
    actor: Actor


MatchingOrderSnapshotReader = Callable[
    [PreflightRequirementPlan],
    tuple[MetadataSnapshot, RecordSnapshot],
]


class MatchingOrderService:
    """Recommend authoring order without reading Odoo or changing mapping."""

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
            updated_at=datetime.now(timezone.utc),
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
        probes: list[MatchingOrderLiveProbe] = []
        checkable_count = 0
        for owner in draft.definition.datasets:
            for relationship in owner.relationships:
                if (
                    relationship.resolver.dataset_id
                    and relationship.resolver.origin
                    in {ResolverOrigin.DATASET, ResolverOrigin.TARGET_THEN_DATASET}
                ):
                    checkable_count += 1
                probe = self._live_probe(
                    workspace_id,
                    owner,
                    relationship,
                    mapping_by_id,
                    schema_by_model,
                    confirmed_keys,
                )
                if probe is not None:
                    probes.append(probe)

        fields_by_model: dict[str, set[str]] = defaultdict(set)
        for probe in probes:
            fields_by_model[probe.target_model].update(probe.target_fields)
        metadata_requests = tuple(
            MetadataRequest(model=model, fields=tuple(sorted(fields)))
            for model, fields in sorted(fields_by_model.items())
        )
        projected_fields = {
            item.model: item.fields for item in metadata_requests
        }
        record_requests: list[RecordRequest] = []
        seen_requests: set[str] = set()
        for probe in probes:
            for domain in _matching_key_domain_chunks(
                probe.target_fields,
                probe.source_keys,
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
        requirements = PreflightRequirementPlan(
            metadata_requests=metadata_requests,
            record_requests=tuple(record_requests),
            reference_requirements=(),
            source_record_count=sum(len(item.source_keys) for item in probes),
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
            probes=tuple(probes),
            unchecked_relationship_count=max(0, checkable_count - len(probes)),
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
        now = datetime.now(timezone.utc)
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
        preferences, authorization = self._preference_dependencies()
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        return preferences.get_current_check(workspace_id)

    def active_check_attempt(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderCheckAttempt | None:
        preferences, authorization = self._preference_dependencies()
        authorization.require(actor, Capability.MAPPING_EDIT, workspace_id=workspace_id)
        return preferences.get_active_check_attempt(workspace_id)

    def check_attempt(
        self,
        workspace_id: str,
        check_id: str,
        *,
        actor: Actor,
    ) -> MatchingOrderCheckAttempt:
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
            message="Reading the exact Odoo keys in the saved matching draft",
            progress_percent=20,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
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
                message="Classifying target and incoming relationship keys",
                progress_percent=70,
                updated_at=datetime.now(timezone.utc),
            )
            preferences.update_check_attempt(prepared.workspace_id, attempt)
            schema_changed = _matching_schema_changed(prepared.schema, metadata)
            results = (
                ()
                if schema_changed
                else tuple(
                    _classify_live_probe(probe, records)
                    for probe in prepared.probes
                )
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
                    prepared.unchecked_relationship_count
                    + (len(prepared.probes) if schema_changed else 0)
                ),
                ordered_dataset_ids=recommendation.ordered_dataset_ids,
                recommendation_hash=matching_order_recommendation_hash(
                    recommendation
                ),
                schema_changed=schema_changed,
                captured_at=datetime.now(timezone.utc),
                actor_issuer=prepared.actor.identity.issuer,
                actor_subject=prepared.actor.identity.subject_id,
                actor_display_name=prepared.actor.identity.display_name,
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
                            "source_keys": [list(key) for key in item.source_keys],
                            "incoming_keys": [list(key) for key in item.incoming_keys],
                        }
                        for item in prepared.probes
                    ],
                }
            )
            attempt = replace(
                attempt,
                phase=MatchingOrderCheckPhase.PUBLISHING,
                message="Publishing the advisory recommendation",
                progress_percent=90,
                updated_at=datetime.now(timezone.utc),
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
        except Exception as error:
            finished = datetime.now(timezone.utc)
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
                return

    def _live_probe(
        self,
        workspace_id: str,
        owner: DatasetMapping,
        relationship: RelationshipMapping,
        mappings: dict[str, DatasetMapping],
        schema_by_model,
        confirmed_keys,
    ) -> MatchingOrderLiveProbe | None:
        resolver = relationship.resolver
        if (
            relationship.value_source is not RelationshipValueSource.SOURCE
            or resolver.origin is not ResolverOrigin.TARGET_THEN_DATASET
            or not resolver.dataset_id
            or not resolver.model
            or resolver.dataset_id == owner.dataset_id
        ):
            return None
        key_fields, scope_fields = relationship_target_fields(relationship)
        target_fields = (*key_fields, *scope_fields)
        if (
            not key_fields
            or (resolver.model, key_fields, scope_fields) not in confirmed_keys
        ):
            return None
        model = schema_by_model.get(resolver.model)
        schema_fields = (
            {item.name: item for item in model.fields} if model is not None else {}
        )
        if any(
            field not in schema_fields
            or schema_fields[field].type in {"many2one", "one2many", "many2many"}
            for field in target_fields
        ):
            return None
        source_by_target = {
            item.target_field: item.source_column_key
            for item in (*resolver.key_mappings, *resolver.scope_mappings)
        }
        source_columns = tuple(source_by_target.get(item, "") for item in target_fields)
        if any(not item for item in source_columns):
            return None
        dependency = mappings.get(resolver.dataset_id)
        if dependency is None or dependency.target_model != resolver.model:
            return None
        incoming_by_target = _direct_identity_source_columns(dependency)
        incoming_columns = tuple(
            incoming_by_target.get(item, "") for item in target_fields
        )
        if any(not item for item in incoming_columns):
            return None
        try:
            raw_source = self._source_keys.source_key_tuples(
                workspace_id,
                owner.dataset_id,
                source_columns,
            )
            raw_incoming = self._source_keys.source_key_tuples(
                workspace_id,
                dependency.dataset_id,
                incoming_columns,
            )
        except WorkspaceError:
            return None
        translations = {
            item.source_value: item.target_value for item in resolver.value_mappings
        }
        source_values: list[tuple[str, ...]] = []
        for row in raw_source:
            expanded = _relationship_row_keys(relationship, row)
            for key in expanded:
                if translations and len(key_fields) == 1:
                    key = (translations.get(key[0], key[0]), *key[1:])
                if all(value for value in key):
                    source_values.append(key)
        incoming_values = tuple(
            tuple(value for value in row if value is not None)
            for row in raw_incoming
            if all(value is not None and value != "" for value in row)
        )
        source_keys = tuple(sorted(set(source_values)))
        if not source_keys:
            return None
        return MatchingOrderLiveProbe(
            owner_dataset_id=owner.dataset_id,
            dependency_dataset_id=dependency.dataset_id,
            target_field=relationship.target_field,
            target_model=resolver.model,
            target_fields=target_fields,
            source_keys=source_keys,
            incoming_keys=tuple(sorted(incoming_values)),
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


def _relationship_row_keys(
    relationship: RelationshipMapping,
    row: tuple[str | None, ...],
) -> tuple[tuple[str, ...], ...]:
    """Expand one saved source row without guessing missing key parts."""

    if relationship.kind == "many2many":
        if len(row) != 1 or row[0] is None:
            return ()
        return tuple(
            (value,)
            for value in dict.fromkeys(
                item.strip() for item in row[0].split(relationship.separator)
            )
            if value
        )
    if any(value is None for value in row):
        return ()
    return (tuple(str(value) for value in row),)


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

    if live.fingerprint.target_hash != captured.connection_target_hash:
        return True
    captured_models = {item.name: item for item in captured.models}
    compared = (
        "type",
        "required",
        "readonly",
        "relation",
        "relation_field",
        "selection",
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
    return False


def _classify_live_probe(
    probe: MatchingOrderLiveProbe,
    snapshot: RecordSnapshot,
) -> MatchingOrderRelationshipResult:
    target_counts: Counter[tuple[str, ...]] = Counter()
    for record in snapshot.records.get(probe.target_model, ()):
        key = tuple(
            _matching_key_value(record.values.get(field))
            for field in probe.target_fields
        )
        if all(value is not None for value in key):
            target_counts[tuple(str(value) for value in key)] += 1
    incoming_counts = Counter(probe.incoming_keys)
    target = incoming = missing = ambiguous = 0
    for key in probe.source_keys:
        match_count = target_counts[key]
        if match_count == 1:
            target += 1
        elif match_count > 1:
            ambiguous += 1
        elif incoming_counts[key] == 1:
            incoming += 1
        elif incoming_counts[key] > 1:
            ambiguous += 1
        else:
            missing += 1
    if ambiguous:
        outcome = MatchingOrderRelationshipOutcome.AMBIGUOUS
    elif missing:
        outcome = MatchingOrderRelationshipOutcome.MISSING
    elif target and incoming:
        outcome = MatchingOrderRelationshipOutcome.MIXED
    elif target:
        outcome = MatchingOrderRelationshipOutcome.TARGET
    elif incoming:
        outcome = MatchingOrderRelationshipOutcome.INCOMING
    else:
        outcome = MatchingOrderRelationshipOutcome.UNCHECKED
    return MatchingOrderRelationshipResult(
        owner_dataset_id=probe.owner_dataset_id,
        dependency_dataset_id=probe.dependency_dataset_id,
        target_field=probe.target_field,
        outcome=outcome,
        target_count=target,
        incoming_count=incoming,
        missing_count=missing,
        ambiguous_count=ambiguous,
    )


def _matching_key_value(value: object) -> str | None:
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _safe_matching_order_failure(error: Exception) -> str:
    """Keep credentials, rows, and implementation details out of status."""

    if type(error).__module__.startswith("impodo."):
        message = str(error).strip()
        if message:
            return message[:1000]
    return (
        "Impodo stopped before publishing a new suggestion. The previous "
        "result is unchanged; review the Odoo read connection and try again."
    )
