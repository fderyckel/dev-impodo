"""Assemble Odoo source capture, provenance, and publication services."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.odoo_capture_filters import OdooCaptureFilterStore
from impodo.application.odoo_capture_publication_service import (
    OdooCapturePublicationService,
    OdooCapturePublicationStore,
    OdooPublicationSelectionReader,
)
from impodo.application.odoo_provenance_service import (
    OdooCaptureSelectionReader as OdooProvenanceSelectionReader,
)
from impodo.application.odoo_provenance_service import (
    OdooProvenanceService,
    OdooProvenanceStore,
    OdooProvenanceWorkspaceReader,
)
from impodo.application.odoo_source_capture_service import (
    OdooCaptureRelationshipScopeReader,
    OdooCaptureSchemaReader,
    OdooCaptureSelectionReader,
    OdooCaptureWorkspaceReader,
    OdooSourceCaptureService,
)
from impodo.application.protected_evidence_codecs import (
    OdooComparisonCodec,
    OdooProvenanceCodec,
)
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.application.shared.secrets import SecretStore
from impodo.application.workspace.access import WorkspaceAccessService


@dataclass(frozen=True, slots=True)
class OdooSourcePorts:
    """Hold every deployment-selected port used by Odoo source services."""

    capture_workspaces: OdooCaptureWorkspaceReader
    capture_selections: OdooCaptureSelectionReader
    capture_schemas: OdooCaptureSchemaReader
    capture_filters: OdooCaptureFilterStore
    relationship_scopes: OdooCaptureRelationshipScopeReader
    provenance_workspaces: OdooProvenanceWorkspaceReader
    provenance_selections: OdooProvenanceSelectionReader
    provenance_store: OdooProvenanceStore
    publication_selections: OdooPublicationSelectionReader
    publication_store: OdooCapturePublicationStore
    source_artifacts: DataVersionSourceArtifactStore
    secrets: SecretStore
    provenance_codec: OdooProvenanceCodec
    comparison_codec: OdooComparisonCodec


@dataclass(frozen=True, slots=True)
class OdooSourceServiceGraph:
    """Hold the services that own one governed Odoo source capture lifecycle."""

    capture: OdooSourceCaptureService
    provenance: OdooProvenanceService
    publication: OdooCapturePublicationService


def build_odoo_source_service_graph(
    *,
    workspace_access: WorkspaceAccessService,
    ports: OdooSourcePorts,
) -> OdooSourceServiceGraph:
    """Wire Odoo source services without choosing storage or a deployment."""

    provenance = OdooProvenanceService(
        ports.provenance_workspaces,
        ports.provenance_selections,
        ports.provenance_store,
        ports.secrets,
        workspace_access,
        ports.provenance_codec,
        ports.comparison_codec,
    )
    capture = OdooSourceCaptureService(
        ports.capture_workspaces,
        ports.capture_selections,
        ports.capture_schemas,
        workspace_access,
        capture_filters=ports.capture_filters,
        relationship_scopes=ports.relationship_scopes,
    )
    publication = OdooCapturePublicationService(
        capture,
        ports.publication_selections,
        provenance,
        ports.publication_store,
        ports.source_artifacts,
        workspace_access,
    )
    return OdooSourceServiceGraph(
        capture=capture,
        provenance=provenance,
        publication=publication,
    )


__all__ = [
    "OdooSourcePorts",
    "OdooSourceServiceGraph",
    "build_odoo_source_service_graph",
]
