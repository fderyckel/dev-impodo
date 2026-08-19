"""Coordinate Recipe lineage across registry, protected store, and workspaces."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import json
import re
from typing import Mapping
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..adapters.duckdb.recipe_repository import RecipeRepository
from ..adapters.protected_recipe_store import ProtectedRecipeStore
from ..domain.serialization import content_hash
from ..models import assert_no_numeric_odoo_ids
from ..projects import MigrationProject
from ..recipes import (
    DataVersion,
    DataVersionPurpose,
    Recipe,
    RecipeConflictError,
    RecipeIntegrityError,
    RecipeIntent,
    RecipeIntentKind,
    RecipeIntentState,
    RecipeRevision,
    RecipeSummary,
    SetupHydrationState,
    WorkspaceResolution,
    require_hash,
)


FaultInjector = Callable[[str], None]

_SEMANTIC_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "connection_target_hash",
        "credential_generation",
        "data_version_id",
        "database",
        "endpoint",
        "mapping_id",
        "password",
        "permission_hash",
        "principal_hash",
        "project_id",
        "recipe_id",
        "secret",
        "series_id",
        "source_artifact",
        "source_artifact_hash",
        "target_binding_id",
        "token",
        "workspace_project_id",
    }
)
_SEMANTIC_FIELDS = frozenset(
    {
        "contract_versions",
        "source_shape",
        "parameter_definitions",
        "source_preparation",
        "mapping",
        "odoo_target_contract",
        "target_governance",
        "quality",
        "reference_dependencies",
        "control_definitions",
    }
)
_UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


class RecipeService:
    """Enforce Recipe authorization, hashing, and restart-safe coordination."""

    def __init__(
        self,
        repository: RecipeRepository,
        store: ProtectedRecipeStore,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.store = store
        self.authorization = authorization

    def list(self, *, actor: Actor) -> tuple[RecipeSummary, ...]:
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        return self.repository.list()

    def get(self, recipe_id: str, *, actor: Actor) -> Recipe:
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        return self.repository.get(recipe_id)

    def data_versions(
        self,
        recipe_id: str,
        *,
        actor: Actor,
    ) -> tuple[DataVersion, ...]:
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        return self.repository.data_versions(recipe_id)

    def read_revision(
        self,
        recipe_id: str,
        version: int,
        *,
        actor: Actor,
    ) -> dict[str, object]:
        """Authorize, decrypt, and revalidate one immutable RecipeRevision."""

        self.authorization.require(actor, Capability.RECIPE_VIEW)
        self.authorization.require(actor, Capability.PROTECTED_EVIDENCE_READ)
        record = self.repository.revision_record(recipe_id, version)
        payload = self.store.read(
            recipe_id,
            storage_key=str(record["storage_key"]),
            logical_hash=str(record["payload_hash"]),
            expected_artifact_hash=str(record["artifact_hash"]),
        )
        envelope = self._validated_envelope(payload)
        if envelope["semantic_hash"] != record["semantic_hash"]:
            raise RecipeIntegrityError("Stored Recipe semantic identity changed")
        return envelope

    def revisions(
        self,
        recipe_id: str,
        *,
        actor: Actor,
    ) -> tuple[RecipeRevision, ...]:
        """Return bounded immutable lineage without decrypting Recipe content."""

        self.authorization.require(actor, Capability.RECIPE_VIEW)
        return self.repository.revisions(recipe_id)

    def resolve_workspace(
        self,
        workspace_project_id: str,
        *,
        actor: Actor,
    ) -> WorkspaceResolution:
        """Resolve current project URLs through exact Recipe/DataVersion IDs."""

        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=workspace_project_id,
        )
        return self.repository.resolve_workspace(workspace_project_id)

    def hydrate_legacy_project(
        self,
        project: MigrationProject,
        *,
        actor: Actor,
    ) -> Recipe:
        """Copy only the approved setup allowlist after one project is opened."""

        resolution = self.resolve_workspace(project.project_id, actor=actor)
        return self.repository.hydrate_legacy_setup(
            resolution.recipe_id,
            data_classification=project.data_classification.value,
            retention_days=project.retention_days,
            business_purpose=(project.description or project.name),
            actor=actor,
        )

    def publish_revision(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        envelope_bytes: bytes,
        actor: Actor,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Publish one verified immutable RecipeRevision through an intent."""

        self.authorization.require(actor, Capability.RECIPE_PUBLISH)
        envelope = self._validated_envelope(envelope_bytes)
        recipe = self.repository.get(recipe_id)
        if recipe.setup_hydration_state is not SetupHydrationState.READY:
            raise RecipeConflictError(
                "Open the current workspace to finish Recipe setup hydration"
            )
        version = (recipe.current_recipe_revision or 0) + 1
        provenance = envelope["provenance"]
        if not isinstance(provenance, dict):
            raise RecipeIntegrityError("Recipe provenance is invalid")
        if provenance.get("recipe_id") != recipe_id:
            raise RecipeIntegrityError("Recipe provenance identity is invalid")
        if provenance.get("recipe_revision") != version:
            raise RecipeIntegrityError("Recipe provenance revision is invalid")
        payload_hash = str(envelope["payload_hash"])
        semantic_hash = str(envelope["semantic_hash"])
        existing_version = self.repository.revision_version_by_semantic_hash(
            recipe_id,
            semantic_hash,
        )
        if existing_version is not None:
            raise RecipeConflictError(
                f"These reusable rules already exist as Recipe v{existing_version}"
            )
        storage_key = self.store.storage_key(
            recipe_id,
            kind="revisions",
            object_id=f"v{version}",
            logical_hash=payload_hash,
        )
        detail: dict[str, object] = {
            "actor": self._actor(actor),
            "contract_versions": envelope["recipe"]["contract_versions"],
            "payload_hash": payload_hash,
            "provenance": provenance,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "semantic_hash": semantic_hash,
            "storage_key": storage_key,
            "version": version,
        }
        intent = self.repository.reserve_intent(
            operation_id=operation_id or str(uuid4()),
            recipe_id=recipe_id,
            kind=RecipeIntentKind.RECIPE_PUBLICATION,
            expected_recipe_revision=expected_recipe_revision,
            detail=detail,
        )
        self._fault(fault, "INTENT_RESERVED")
        stored = self.store.put(
            recipe_id,
            kind="revisions",
            object_id=f"v{version}",
            logical_hash=payload_hash,
            payload=envelope_bytes,
        )
        self._fault(fault, "PAYLOAD_WRITTEN")
        detail.update(
            {
                "artifact_hash": stored.artifact_hash,
                "size_bytes": stored.size_bytes,
            }
        )
        intent = self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.RESERVED,
            new_state=RecipeIntentState.PAYLOAD_STORED,
            detail=detail,
        )
        self._fault(fault, "PAYLOAD_STORED")
        intent = self.repository.commit_publication(intent.operation_id)
        self._fault(fault, "REGISTRY_COMMITTED")
        return self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.REGISTRY_COMMITTED,
            new_state=RecipeIntentState.COMPLETE,
        )

    def create_data_version(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        workspace_project_id: str,
        purpose: DataVersionPurpose,
        label: str,
        actor: Actor,
        export_as_of_date: date | None = None,
        parameter_values_hash: str | None = None,
        operation_id: str | None = None,
        data_version_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Adopt a clean workspace as the next active DataVersion."""

        self.authorization.require(actor, Capability.DATA_VERSION_CREATE)
        recipe = self.repository.get(recipe_id)
        if recipe.setup_hydration_state is not SetupHydrationState.READY:
            raise RecipeConflictError(
                "Open the current workspace to finish Recipe setup hydration"
            )
        if parameter_values_hash is not None:
            require_hash(parameter_values_hash, "parameter_values_hash")
        clean_label = label.strip()
        if not clean_label or len(clean_label) > 200:
            raise RecipeConflictError("DataVersion label is invalid")
        detail = {
            "actor": self._actor(actor),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_version_id": data_version_id or str(uuid4()),
            "export_as_of_date": (
                export_as_of_date.isoformat() if export_as_of_date else None
            ),
            "intake_status": "PENDING",
            "label": clean_label,
            "parameter_values_hash": parameter_values_hash,
            "purpose": DataVersionPurpose(purpose).value,
            "workspace_project_id": workspace_project_id,
        }
        intent = self.repository.reserve_intent(
            operation_id=operation_id or str(uuid4()),
            recipe_id=recipe_id,
            kind=RecipeIntentKind.DATA_VERSION_CREATION,
            expected_recipe_revision=expected_recipe_revision,
            detail=detail,
        )
        self._fault(fault, "INTENT_RESERVED")
        intent = self.repository.commit_data_version(intent.operation_id)
        self._fault(fault, "REGISTRY_COMMITTED")
        self.repository.synchronize_workspace_markers(recipe_id)
        self._fault(fault, "WORKSPACE_LINKED")
        return self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.REGISTRY_COMMITTED,
            new_state=RecipeIntentState.COMPLETE,
        )

    def publish_qualification(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        evidence: Mapping[str, object],
        actor: Actor,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Protect and publish exact successful Test rehearsal evidence."""

        self.authorization.require(actor, Capability.RECIPE_QUALIFY)
        qualification_id = str(evidence.get("qualification_id") or uuid4())
        payload = dict(evidence)
        if str(payload.get("environment", "")) != "TEST":
            raise RecipeIntegrityError("Only Test evidence can qualify a Recipe")
        if str(payload.get("status", "")) != "TEST_QUALIFIED":
            raise RecipeIntegrityError("Qualification evidence is not successful")
        payload["qualification_id"] = qualification_id
        payload["recipe_id"] = recipe_id
        payload["actor"] = self._actor(actor)
        payload["qualified_at"] = str(
            payload.get("qualified_at") or datetime.now(timezone.utc).isoformat()
        )
        evidence_hash = content_hash(payload)
        payload_bytes = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        storage_key = self.store.storage_key(
            recipe_id,
            kind="qualifications",
            object_id=qualification_id,
            logical_hash=evidence_hash,
        )
        detail = dict(payload)
        detail.update({"evidence_hash": evidence_hash, "storage_key": storage_key})
        intent = self.repository.reserve_intent(
            operation_id=operation_id or str(uuid4()),
            recipe_id=recipe_id,
            kind=RecipeIntentKind.QUALIFICATION_PUBLICATION,
            expected_recipe_revision=expected_recipe_revision,
            detail=detail,
        )
        self._fault(fault, "INTENT_RESERVED")
        stored = self.store.put(
            recipe_id,
            kind="qualifications",
            object_id=qualification_id,
            logical_hash=evidence_hash,
            payload=payload_bytes,
        )
        self._fault(fault, "PAYLOAD_WRITTEN")
        detail.update(
            {"artifact_hash": stored.artifact_hash, "size_bytes": stored.size_bytes}
        )
        intent = self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.RESERVED,
            new_state=RecipeIntentState.PAYLOAD_STORED,
            detail=detail,
        )
        self._fault(fault, "PAYLOAD_STORED")
        intent = self.repository.commit_qualification(intent.operation_id)
        self._fault(fault, "REGISTRY_COMMITTED")
        return self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.REGISTRY_COMMITTED,
            new_state=RecipeIntentState.COMPLETE,
        )

    def record_application_projection(
        self,
        *,
        actor: Actor,
        application_id: str,
        recipe_id: str,
        recipe_revision: int,
        data_version_id: str,
        workspace_project_id: str,
        source_selection_hash: str,
        parameter_values_hash: str,
        target_binding_hash: str,
        credential_generation: str,
        binding_hash: str,
        issue_hash: str,
        mapping_id: str | None,
        mapping_content_hash: str | None,
        status: str,
        evidence_storage_key: str,
        evidence_hash: str,
        created_at: datetime,
    ) -> None:
        """Authorize one bounded immutable Recipe application projection."""

        self.authorization.require(actor, Capability.RECIPE_APPLY)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=workspace_project_id,
        )
        self.repository.record_application_projection(
            application_id=application_id,
            recipe_id=recipe_id,
            recipe_revision=recipe_revision,
            data_version_id=data_version_id,
            workspace_project_id=workspace_project_id,
            source_selection_hash=source_selection_hash,
            parameter_values_hash=parameter_values_hash,
            target_binding_hash=target_binding_hash,
            credential_generation=credential_generation,
            binding_hash=binding_hash,
            issue_hash=issue_hash,
            mapping_id=mapping_id,
            mapping_content_hash=mapping_content_hash,
            status=status,
            evidence_storage_key=evidence_storage_key,
            evidence_hash=evidence_hash,
            created_at=created_at,
        )

    def select_cutover_candidate(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        recipe_revision: int,
        qualification_id: str,
        qualification_evidence_hash: str,
        actor: Actor,
        operation_id: str | None = None,
        cutover_candidate_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Pin one exact qualified revision without Production authority."""

        self.authorization.require(actor, Capability.CUTOVER_SELECT)
        require_hash(qualification_evidence_hash, "qualification_evidence_hash")
        candidate_id = cutover_candidate_id or str(uuid4())
        selected_at = datetime.now(timezone.utc).isoformat()
        semantic = {
            "cutover_candidate_id": candidate_id,
            "qualification_evidence_hash": qualification_evidence_hash,
            "qualification_id": qualification_id,
            "recipe_id": recipe_id,
            "recipe_revision": recipe_revision,
            "selected_at": selected_at,
        }
        detail = {
            **semantic,
            "actor": self._actor(actor),
            "content_hash": content_hash(semantic),
        }
        intent = self.repository.reserve_intent(
            operation_id=operation_id or str(uuid4()),
            recipe_id=recipe_id,
            kind=RecipeIntentKind.CUTOVER_SELECTION,
            expected_recipe_revision=expected_recipe_revision,
            detail=detail,
        )
        self._fault(fault, "INTENT_RESERVED")
        intent = self.repository.commit_cutover(intent.operation_id)
        self._fault(fault, "REGISTRY_COMMITTED")
        return self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.REGISTRY_COMMITTED,
            new_state=RecipeIntentState.COMPLETE,
        )

    def begin_deletion(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        actor: Actor,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Tombstone a Recipe and persist its exact deletion target set."""

        self.authorization.require(actor, Capability.RECIPE_DELETE)
        intent = self.repository.reserve_intent(
            operation_id=operation_id or str(uuid4()),
            recipe_id=recipe_id,
            kind=RecipeIntentKind.RECIPE_DELETION,
            expected_recipe_revision=expected_recipe_revision,
            detail={"actor": self._actor(actor)},
        )
        self._fault(fault, "INTENT_RESERVED")
        intent = self.repository.enumerate_deletion(intent.operation_id)
        self._fault(fault, "TARGETS_ENUMERATED")
        return intent

    def recover_incomplete(self, *, actor: Actor) -> tuple[RecipeIntent, ...]:
        """Deterministically finish or abandon every recoverable operation."""

        self.authorization.require(actor, Capability.PROJECT_ADMIN)
        recovered: list[RecipeIntent] = []
        for intent in self.repository.incomplete_intents():
            if intent.kind is RecipeIntentKind.RECIPE_PUBLICATION:
                recovered.append(self._recover_publication(intent))
            elif intent.kind is RecipeIntentKind.QUALIFICATION_PUBLICATION:
                recovered.append(self._recover_qualification(intent))
            elif intent.kind is RecipeIntentKind.DATA_VERSION_CREATION:
                recovered.append(self._recover_data_version(intent))
            elif intent.kind is RecipeIntentKind.CUTOVER_SELECTION:
                recovered.append(self._recover_cutover(intent))
            elif intent.kind is RecipeIntentKind.RECIPE_DELETION:
                recovered.append(self._recover_deletion(intent))
        return tuple(recovered)

    def _recover_publication(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.RESERVED:
            intent = self._recover_stored_payload(intent, kind="revisions")
        if intent.state is RecipeIntentState.PAYLOAD_STORED:
            intent = self.repository.commit_publication(intent.operation_id)
        return self._complete_registry_intent(intent)

    def _recover_qualification(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.RESERVED:
            intent = self._recover_stored_payload(intent, kind="qualifications")
        if intent.state is RecipeIntentState.PAYLOAD_STORED:
            intent = self.repository.commit_qualification(intent.operation_id)
        return self._complete_registry_intent(intent)

    def _recover_data_version(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.RESERVED:
            intent = self.repository.commit_data_version(intent.operation_id)
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            self.repository.synchronize_workspace_markers(intent.recipe_id)
        return self._complete_registry_intent(intent)

    def _recover_cutover(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.RESERVED:
            intent = self.repository.commit_cutover(intent.operation_id)
        return self._complete_registry_intent(intent)

    def _recover_deletion(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.RESERVED:
            return self.repository.enumerate_deletion(intent.operation_id)
        return intent

    def _recover_stored_payload(
        self,
        intent: RecipeIntent,
        *,
        kind: str,
    ) -> RecipeIntent:
        detail = dict(intent.detail)
        storage_key = str(detail["storage_key"])
        logical_hash = str(
            detail["payload_hash"] if kind == "revisions" else detail["evidence_hash"]
        )
        if not self.store.exists(storage_key):
            return self.repository.transition_intent(
                intent.operation_id,
                expected_state=RecipeIntentState.RESERVED,
                new_state=RecipeIntentState.ABANDONED,
                last_error="Protected payload was not written",
            )
        stored = self.store.inspect(
            intent.recipe_id,
            storage_key=storage_key,
            logical_hash=logical_hash,
        )
        detail.update(
            {"artifact_hash": stored.artifact_hash, "size_bytes": stored.size_bytes}
        )
        return self.repository.transition_intent(
            intent.operation_id,
            expected_state=RecipeIntentState.RESERVED,
            new_state=RecipeIntentState.PAYLOAD_STORED,
            detail=detail,
        )

    def _complete_registry_intent(self, intent: RecipeIntent) -> RecipeIntent:
        if intent.state is RecipeIntentState.REGISTRY_COMMITTED:
            return self.repository.transition_intent(
                intent.operation_id,
                expected_state=RecipeIntentState.REGISTRY_COMMITTED,
                new_state=RecipeIntentState.COMPLETE,
            )
        return intent

    @staticmethod
    def _validated_envelope(envelope_bytes: bytes) -> dict[str, object]:
        try:
            envelope = json.loads(envelope_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipeIntegrityError("Recipe payload is not valid JSON") from error
        if not isinstance(envelope, dict):
            raise RecipeIntegrityError("Recipe payload must be an object")
        if set(envelope) != {
            "recipe_contract_version",
            "semantic_hash",
            "payload_hash",
            "recipe",
            "compatibility_hints",
            "provenance",
        }:
            raise RecipeIntegrityError("Recipe envelope fields are invalid")
        if envelope.get("recipe_contract_version") != 2:
            raise RecipeIntegrityError("Recipe contract version is unsupported")
        recipe = envelope.get("recipe")
        if not isinstance(recipe, dict):
            raise RecipeIntegrityError("Recipe semantic payload is invalid")
        if set(recipe) != _SEMANTIC_FIELDS:
            raise RecipeIntegrityError("Recipe semantic fields are invalid")
        if not isinstance(envelope.get("compatibility_hints"), dict):
            raise RecipeIntegrityError("Recipe compatibility hints are invalid")
        if not isinstance(envelope.get("provenance"), dict):
            raise RecipeIntegrityError("Recipe provenance is invalid")
        if content_hash(recipe) != envelope.get("semantic_hash"):
            raise RecipeIntegrityError("Recipe semantic hash is invalid")
        if content_hash(
            {key: value for key, value in envelope.items() if key != "payload_hash"}
        ) != envelope.get("payload_hash"):
            raise RecipeIntegrityError("Recipe payload hash is invalid")
        for key in RecipeService._walk_keys(recipe):
            if key.casefold() in _SEMANTIC_FORBIDDEN_KEYS:
                raise RecipeIntegrityError(
                    f"Recipe semantic payload contains forbidden {key}"
                )
        for value in RecipeService._walk_values(recipe):
            if isinstance(value, str) and _UUID_TEXT.search(value):
                raise RecipeIntegrityError(
                    "Recipe semantic payload contains a workspace identity"
                )
        try:
            assert_no_numeric_odoo_ids(recipe)
        except ValueError as error:
            raise RecipeIntegrityError(str(error)) from error
        contract_versions = recipe.get("contract_versions")
        if not isinstance(contract_versions, dict) or not contract_versions:
            raise RecipeIntegrityError("Recipe contract versions are missing")
        return envelope

    @staticmethod
    def _walk_keys(value: object):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from RecipeService._walk_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from RecipeService._walk_keys(item)

    @staticmethod
    def _walk_values(value: object):
        if isinstance(value, dict):
            for item in value.values():
                yield from RecipeService._walk_values(item)
        elif isinstance(value, list):
            for item in value:
                yield from RecipeService._walk_values(item)
        else:
            yield value

    @staticmethod
    def _actor(actor: Actor) -> dict[str, str]:
        return {
            "issuer": actor.identity.issuer,
            "subject_id": actor.identity.subject_id,
            "display_name": actor.identity.display_name,
        }

    @staticmethod
    def _fault(fault: FaultInjector | None, stage: str) -> None:
        if fault is not None:
            fault(stage)
