"""Define the bounded saved-evidence projection used by Mapping search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from impodo.derived_entities import DerivedEntityPlan
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.schema.governance import SchemaGovernance
from impodo.inspection import SourceFileCatalog
from impodo.workspace_contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceSelection,
)


@dataclass(frozen=True, slots=True)
class MappingFieldCatalogSnapshot:
    """Hold one consistent set of saved evidence needed by field search."""

    physical_selection: SourceSelection | None
    preparation_plan: DerivedEntityPlan | None
    source_catalogs: tuple[SourceFileCatalog, ...]
    schema: OdooSchemaCatalog | None
    governance: SchemaGovernance | None
    revision: MappingRevision | None
    working_draft: MappingWorkingDraft | None


class MappingFieldCatalogQueryRepository(Protocol):
    """Load Mapping field-search evidence without contacting Odoo."""

    def get_mapping_field_catalog_snapshot(
        self,
        workspace_id: str,
    ) -> MappingFieldCatalogSnapshot: ...
