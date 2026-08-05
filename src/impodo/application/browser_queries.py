"""Read-only application queries used by server-rendered browser workflows."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from ..derived_entities import DerivedEntityPlan
from ..domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactPage,
    TransformationImpactRow,
    TransformationImpactSnapshot,
)
from ..inspection import SourceFileCatalog
from ..mapping_semantics import (
    MappingRevision,
    MappingSubmission,
    MappingValidationResult,
    SchemaGovernance,
)
from ..projects import MigrationProject, ProjectSummary
from ..quality import QualityReviewPage, QualityRuleSet
from ..workspace_contracts import (
    MappingWorkingDraft,
    OdooModelCatalog,
    OdooSchemaCatalog,
    SourceConfiguration,
    SourceSelection,
)


class BrowserQueryRepository(Protocol):
    def list(self) -> tuple[ProjectSummary, ...]: ...
    def get(self, project_id: str) -> MigrationProject: ...
    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...
    def get_source_configurations(
        self, project_id: str
    ) -> tuple[SourceConfiguration, ...]: ...
    def get_source_selection(self, project_id: str) -> SourceSelection | None: ...
    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None: ...
    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None: ...
    def get_odoo_model_catalog(
        self, project_id: str
    ) -> OdooModelCatalog | None: ...
    def get_odoo_schema_catalog(
        self, project_id: str
    ) -> OdooSchemaCatalog | None: ...
    def get_schema_governance(
        self, project_id: str
    ) -> SchemaGovernance | None: ...
    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None: ...
    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None: ...
    def get_mapping_validation(
        self, project_id: str, version: int
    ) -> MappingValidationResult | None: ...
    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None: ...
    def get_current_quality_ruleset(
        self, project_id: str
    ) -> QualityRuleSet | None: ...
    def get_quality_review_page(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str,
        dataset: str,
        page: int,
        page_size: int,
    ) -> QualityReviewPage: ...
    def get_transformation_impact_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
    ) -> TransformationImpactSnapshot | None: ...
    def get_transformation_impact_page(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> TransformationImpactPage: ...
    def iter_transformation_impact_rows(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
    ) -> Iterator[TransformationImpactRow]: ...


class BrowserQueryService:
    """Expose browser reads without leaking the concrete DuckDB adapter."""

    def __init__(self, repository: BrowserQueryRepository) -> None:
        self._repository = repository

    def list(self) -> tuple[ProjectSummary, ...]:
        return self._repository.list()

    def get(self, project_id: str) -> MigrationProject:
        return self._repository.get(project_id)

    def get_source_catalogs(
        self, project_id: str
    ) -> tuple[SourceFileCatalog, ...]:
        return self._repository.get_source_catalogs(project_id)

    def get_source_configurations(
        self, project_id: str
    ) -> tuple[SourceConfiguration, ...]:
        return self._repository.get_source_configurations(project_id)

    def get_source_selection(self, project_id: str) -> SourceSelection | None:
        return self._repository.get_source_selection(project_id)

    def get_mapping_source_selection(
        self, project_id: str
    ) -> SourceSelection | None:
        return self._repository.get_mapping_source_selection(project_id)

    def get_derived_entity_plan(
        self, project_id: str
    ) -> DerivedEntityPlan | None:
        return self._repository.get_derived_entity_plan(project_id)

    def get_odoo_model_catalog(
        self, project_id: str
    ) -> OdooModelCatalog | None:
        return self._repository.get_odoo_model_catalog(project_id)

    def get_odoo_schema_catalog(
        self, project_id: str
    ) -> OdooSchemaCatalog | None:
        return self._repository.get_odoo_schema_catalog(project_id)

    def get_schema_governance(
        self, project_id: str
    ) -> SchemaGovernance | None:
        return self._repository.get_schema_governance(project_id)

    def get_mapping_working_draft(
        self, project_id: str
    ) -> MappingWorkingDraft | None:
        return self._repository.get_mapping_working_draft(project_id)

    def get_mapping_revision(
        self, project_id: str, version: int | None = None
    ) -> MappingRevision | None:
        return self._repository.get_mapping_revision(project_id, version)

    def get_mapping_validation(
        self, project_id: str, version: int
    ) -> MappingValidationResult | None:
        return self._repository.get_mapping_validation(project_id, version)

    def get_mapping_submission(
        self, project_id: str, version: int | None = None
    ) -> MappingSubmission | None:
        return self._repository.get_mapping_submission(project_id, version)

    def get_current_quality_ruleset(
        self, project_id: str
    ) -> QualityRuleSet | None:
        return self._repository.get_current_quality_ruleset(project_id)

    def get_quality_review_page(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str,
        dataset: str,
        page: int,
        page_size: int,
    ) -> QualityReviewPage:
        return self._repository.get_quality_review_page(
            project_id,
            run_id,
            status=status,
            dataset=dataset,
            page=page,
            page_size=page_size,
        )

    def get_transformation_impact_snapshot(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
    ) -> TransformationImpactSnapshot | None:
        return self._repository.get_transformation_impact_snapshot(
            project_id, identity
        )

    def get_transformation_impact_page(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
        *,
        page_size: int,
        after: int | None = None,
        before: int | None = None,
    ) -> TransformationImpactPage:
        return self._repository.get_transformation_impact_page(
            project_id,
            identity,
            filters,
            page_size=page_size,
            after=after,
            before=before,
        )

    def iter_transformation_impact_rows(
        self,
        project_id: str,
        identity: TransformationImpactIdentity,
        filters: TransformationImpactFilter,
    ) -> Iterator[TransformationImpactRow]:
        return self._repository.iter_transformation_impact_rows(
            project_id, identity, filters
        )
