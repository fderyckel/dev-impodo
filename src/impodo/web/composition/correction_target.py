"""Build the deployment-owned Odoo read capability for correction review.

The shared correction workflow decides which evidence must be reviewed.  This
adapter resolves the deployment's secret, verifies the live Odoo identity, and
creates a scope-bound reader only after the local preparation stages finish.
It never stores a credential or Odoo client in the shared service graph.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from impodo.application.correction_orchestration import (
    CorrectionDatasetReviewInput,
    CorrectionTargetReviewEvidence,
)
from impodo.application.recipe_compilation_service import (
    RecipeAuthoringMappingRepository,
)
from impodo.application.shared.secrets import SecretStore, SecretStoreError
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    CorrectionOriginManifest,
)
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import OdooReadIdentity
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateRepository,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
)

CorrectionReadIdentityProbe = Callable[
    [WorkspaceState, str, tuple[str, ...]],
    OdooReadIdentity,
]
CorrectionReadbackReaderFactory = Callable[
    [WorkspaceState, str, OdooApiScope],
    OdooReadbackReader,
]


class CredentialBackedCorrectionTargetReviewStage:
    """Resolve one fresh, target-bound Odoo reader for a correction review."""

    def __init__(
        self,
        *,
        workspace_states: WorkspaceStateRepository,
        mappings: RecipeAuthoringMappingRepository,
        secrets: SecretStore,
        read_identity_probe: CorrectionReadIdentityProbe,
        readback_reader_factory: CorrectionReadbackReaderFactory,
    ) -> None:
        self.workspace_states = workspace_states
        self.mappings = mappings
        self.secrets = secrets
        self.read_identity_probe = read_identity_probe
        self.readback_reader_factory = readback_reader_factory

    def refresh_read_capability(
        self,
        manifest: CorrectionOriginManifest,
        mapping: MappingRevision,
        datasets: tuple[CorrectionDatasetReviewInput, ...],
        successor_workspace_id: str,
        *,
        actor: Actor,
    ) -> CorrectionTargetReviewEvidence:
        """Verify current credentials and return a reader closed over one scope."""

        # Authorization is enforced by the shared workflow stages. The target
        # adapter consumes neither prepared values nor actor capabilities.
        del actor, datasets
        successor_state = self.workspace_states.get(successor_workspace_id)
        completed_state = self.workspace_states.get(
            manifest.completed_workspace_id
        )
        credential = get_target_credential(
            self.secrets,
            successor_state,
            TargetCredentialRole.READ,
        ) or get_target_credential(
            self.secrets,
            completed_state,
            TargetCredentialRole.READ,
        )
        if credential is None:
            raise SecretStoreError(
                "Enter an Odoo read API key before reviewing the correction"
            )

        previous = self.mappings.get_mapping_revision(
            manifest.completed_workspace_id
        )
        if previous is None:
            raise CorrectionOriginError("Completed correction rules are missing")
        scope = correction_review_api_scope(manifest, previous, mapping)
        models = tuple(item.model for item in scope.models)
        identity = self.read_identity_probe(
            successor_state,
            credential.secret,
            models,
        )
        if (
            identity.target_hash != manifest.target_hash
            or not set(models).issubset(identity.readable_models)
        ):
            raise CorrectionOriginError(
                "Current Odoo read access does not match the completed load"
            )
        try:
            reviewed_at = datetime.fromisoformat(identity.observed_at)
        except ValueError as error:
            raise CorrectionOriginError(
                "Current Odoo read evidence has an invalid time"
            ) from error
        reader = self.readback_reader_factory(
            successor_state,
            credential.secret,
            scope,
        )
        return CorrectionTargetReviewEvidence(
            reader=reader,
            reader_scope_hash=scope.semantic_hash,
            read_credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            reviewed_at=reviewed_at,
        )


def correction_review_api_scope(
    manifest: CorrectionOriginManifest,
    previous: MappingRevision,
    corrected: MappingRevision,
) -> OdooApiScope:
    """Return the deterministic read scope for prior and corrected rules."""

    fields_by_model: dict[str, set[str]] = {}
    lookup_fields_by_model: dict[str, set[str]] = {}
    for definition in (previous.definition, corrected.definition):
        for dataset in definition.datasets:
            fields_by_model.setdefault(dataset.target_model, set()).update(
                field.target_field for field in dataset.fields
            )
            fields_by_model[dataset.target_model].update(
                relationship.target_field for relationship in dataset.relationships
            )
            for relationship in dataset.relationships:
                resolver = relationship.resolver
                if resolver.model and resolver.key_mappings:
                    lookup_fields = {
                        item.target_field
                        for item in (
                            *resolver.key_mappings,
                            *resolver.scope_mappings,
                        )
                    }
                    fields_by_model.setdefault(resolver.model, set()).update(
                        lookup_fields
                    )
                    lookup_fields_by_model.setdefault(
                        resolver.model,
                        set(),
                    ).update(lookup_fields)
    return OdooApiScope(
        preview_hash=content_hash(
            {
                "correction_origin": manifest.manifest_hash,
                "corrected_mapping": corrected.definition.content_hash,
            }
        ),
        models=tuple(
            OdooModelScope(
                model=model,
                read_fields=tuple(sorted(fields)),
                lookup_fields=tuple(
                    sorted(lookup_fields_by_model.get(model, set()))
                ),
            )
            for model, fields in sorted(fields_by_model.items())
            if fields
        ),
    )


__all__ = [
    "CorrectionReadIdentityProbe",
    "CorrectionReadbackReaderFactory",
    "CredentialBackedCorrectionTargetReviewStage",
    "correction_review_api_scope",
]
