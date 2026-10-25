"""Build deployment-neutral application service groups from injected ports.

The local and server composition roots may choose different persistence,
identity, and authorization adapters. They share these builders so those
deployment choices do not duplicate application-service wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from impodo.application.data_version.ports import DataVersionRepository
from impodo.application.data_version.service import DataVersionService
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackageService,
    DataVersionSourceRepository,
    WorkspaceSourceProjectionRepository,
    WorkspaceSourceProjectionService,
)
from impodo.application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.odoo_capture_filters import OdooCaptureFilterStore
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.odoo_source_restart_service import OdooSourceRestartService
from impodo.application.preflight_service import PreflightService
from impodo.application.project.ports import MigrationProjectRepository
from impodo.application.project.service import MigrationProjectService
from impodo.application.recipe.ports import RecipeRepository
from impodo.application.recipe.service import RecipeService
from impodo.application.recipe_application_service import RecipeApplicationService
from impodo.application.recipe_compilation_service import (
    RecipeAuthoringMappingRepository,
    RecipeAuthoringParameterRepository,
    RecipeAuthoringPreparationRepository,
    RecipeAuthoringQualityRepository,
    RecipeAuthoringReferenceRepository,
    RecipeAuthoringSchemaRepository,
    RecipeAuthoringSourceRepository,
    RecipeCompiler,
)
from impodo.application.recipe_publication_service import RecipePublicationService
from impodo.application.run.ports import MigrationRunRepository
from impodo.application.run.service import MigrationRunService
from impodo.application.schema_workspace_service import (
    SchemaWorkspaceReader,
    SchemaWorkspaceRepository,
    SchemaWorkspaceService,
    SourceSelectionReader,
)
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.source_workspace_service import (
    OdooCaptureSchemaReader,
    SourceWorkspaceRepository,
    SourceWorkspaceService,
    WorkspaceStateReader,
)
from impodo.application.workspace.access import (
    WorkspaceAccessRepository,
    WorkspaceAccessService,
)
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
    CategoricalSourceRepository,
)
from impodo.application.workspace.mapping.numeric_precision_coverage import (
    NumericPrecisionCoverageService,
    NumericPrecisionSourceRepository,
)
from impodo.application.workspace.mapping.order_service import (
    MatchingOrderPreferenceRepository,
    MatchingOrderService,
)
from impodo.application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewRepository,
    RowInclusionReviewService,
)
from impodo.application.workspace.mapping.service import (
    MappingDownstreamInvalidator,
    MappingRowInclusionConfirmationRepository,
    MappingSchemaRepository,
    MappingSourceRepository,
    MappingWorkspaceRepository,
    MappingWorkspaceService,
    RecipeMappingPolicy,
)
from impodo.application.workspace.mapping.supporting_values import (
    MappingSupportingLookupReader,
)
from impodo.application.workspace.mapping.transformation_impact import (
    TransformationImpactDerivedRepository,
    TransformationImpactMappingRepository,
    TransformationImpactRepository,
    TransformationImpactService,
    TransformationImpactSourceRepository,
    TransformationImpactWorkspaceRepository,
)
from impodo.application.workspace.ports import MigrationWorkspaceRepository
from impodo.application.workspace.preparation.columnar_transformation_port import (
    ColumnarTransformationPort,
)
from impodo.application.workspace.preparation.normalization_service import (
    NormalizationService,
)
from impodo.application.workspace.preparation.preparation_service import (
    PreparationService,
)
from impodo.application.workspace.preparation.quality_service import (
    QualityService,
    RecipeQualitySeedRepository,
)
from impodo.application.workspace.preparation.readiness_ports import (
    NormalizationRepository,
    PreflightEffectiveRepository,
    PreflightMappingRepository,
    PreflightNormalizationRepository,
    PreflightQualityRepository,
    PreflightRepository,
    PreflightSchemaRepository,
    PreflightSourceRepository,
    PreflightStagingRepository,
    PreflightWorkspaceRepository,
    PreparationDerivedRepository,
    PreparationMappingRepository,
    PreparationSessionRepository,
    PreparationSourceRepository,
    PreparationStagingRepository,
    PreparationWorkspaceRepository,
    QualityMappingRepository,
    QualityRepository,
    QualitySourceRepository,
)
from impodo.application.workspace.preparation.resolution_service import (
    ResolutionRepository,
    ResolutionService,
    ResolutionStagingRepository,
)
from impodo.application.workspace.service import MigrationWorkspaceService
from impodo.application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.preparation.quality import QualityRule
from impodo.domain.run.setup import (
    MigrationRunTargetSetupRepository,
    MigrationRunTargetSetupService,
)
from impodo.domain.shared.access import (
    Actor,
    AuthorizationPolicy,
    WorkspaceAuthorizationPolicy,
)
from impodo.domain.workspace.workbench import (
    WorkspaceStateRepository,
    WorkspaceStateService,
)


class FoundationServiceRepository(
    MigrationProjectRepository,
    DataVersionRepository,
    MigrationRunRepository,
    MigrationRunTargetSetupRepository,
    MigrationWorkspaceRepository,
    DataVersionSourceRepository,
    WorkspaceAccessRepository,
    Protocol,
):
    """Combine the narrow ports required by the Project foundation services."""


class PreparationWorkspacePorts(
    PreparationWorkspaceRepository,
    PreflightWorkspaceRepository,
    Protocol,
):
    """Combine Workspace reads shared by preparation and final review."""


class PreparationSourcePorts(
    PreparationSourceRepository,
    QualitySourceRepository,
    PreflightSourceRepository,
    Protocol,
):
    """Combine source-evidence reads shared by the preparation services."""


class PreparationMappingPorts(
    PreparationMappingRepository,
    QualityMappingRepository,
    PreflightMappingRepository,
    Protocol,
):
    """Combine mapping reads shared by preparation and final review."""


class PreparationStagingPorts(
    PreparationStagingRepository,
    ResolutionStagingRepository,
    PreflightStagingRepository,
    Protocol,
):
    """Combine canonical-staging publication and review reads."""


class PreparationQualityPorts(
    QualityRepository,
    PreflightQualityRepository,
    Protocol,
):
    """Combine quality publication and final-review reads."""


class PreparationNormalizationPorts(
    NormalizationRepository,
    PreflightNormalizationRepository,
    Protocol,
):
    """Combine normalization commands and final-review reads."""


class PreparationResolutionPorts(
    ResolutionRepository,
    PreflightEffectiveRepository,
    Protocol,
):
    """Combine duplicate resolution and effective-data reads."""


class AuthoringFoundationRepository(
    WorkspaceSourceProjectionRepository,
    Protocol,
):
    """Persist the immutable DataVersion projection used by a Workspace."""


class AuthoringMappingSourcePorts(
    RecipeAuthoringSourceRepository,
    MappingSourceRepository,
    Protocol,
):
    """Expose the effective physical-plus-derived mapping selection."""


class AuthoringWorkspacePorts(
    WorkspaceStateReader,
    SchemaWorkspaceReader,
    TransformationImpactWorkspaceRepository,
    Protocol,
):
    """Combine Workspace state reads used across source and Match data."""


class AuthoringSourcePorts(
    SourceWorkspaceRepository,
    SourceSelectionReader,
    CategoricalSourceRepository,
    NumericPrecisionSourceRepository,
    TransformationImpactSourceRepository,
    Protocol,
):
    """Combine immutable source-evidence reads and source commands."""


class AuthoringSchemaPorts(
    OdooCaptureSchemaReader,
    SchemaWorkspaceRepository,
    MappingSchemaRepository,
    RecipeAuthoringSchemaRepository,
    Protocol,
):
    """Combine schema capture, governance, mapping, and Recipe reads."""


class AuthoringMappingPorts(
    RecipeAuthoringMappingRepository,
    MappingWorkspaceRepository,
    TransformationImpactMappingRepository,
    Protocol,
):
    """Combine Match-data command and immutable evidence ports."""


class AuthoringDerivedPorts(
    RecipeAuthoringPreparationRepository,
    TransformationImpactDerivedRepository,
    Protocol,
):
    """Combine derived-data reads used by authoring and impact review."""


class AuthoringRowInclusionPorts(
    RowInclusionReviewRepository,
    MappingRowInclusionConfirmationRepository,
    Protocol,
):
    """Combine row-inclusion review persistence and submission proof."""


class AuthoringMatchingOrderPorts(
    MatchingOrderPreferenceRepository,
    Protocol,
):
    """Persist matching-order preferences and live-check attempts."""


class AuthoringRecipeApplicationState(RecipeMappingPolicy, Protocol):
    """Persist one Recipe application's mapping policy and quality seed."""

    def save_quality_seed(
        self,
        workspace_id: str,
        *,
        application_id: str,
        mapping_content_hash: str,
        mapping_definition: MappingDefinition,
        rules: tuple[QualityRule, ...],
        actor: Actor,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FoundationServiceGraph:
    """Hold one consistently wired Project foundation service group."""

    workspace_access: WorkspaceAccessService
    workspace_states: WorkspaceStateService
    migration_projects: MigrationProjectService
    data_versions: DataVersionService
    migration_runs: MigrationRunService
    migration_run_target_setup: MigrationRunTargetSetupService
    migration_workspaces: MigrationWorkspaceService
    source_packages: DataVersionSourcePackageService
    project_authoring: MigrationProjectAuthoringService
    odoo_source_restarts: OdooSourceRestartService


@dataclass(frozen=True, slots=True)
class AuthoringServiceGraph:
    """Hold source, Recipe, and Match-data services for either deployment."""

    recipe_compiler: RecipeCompiler
    recipes: RecipeService
    recipe_publication: RecipePublicationService
    source_workspace: SourceWorkspaceService
    source_projections: WorkspaceSourceProjectionService
    data_version_source_projection: WorkspaceDataVersionSourceService
    categorical_coverage: CategoricalCoverageService
    numeric_precision_coverage: NumericPrecisionCoverageService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    transformation_impacts: TransformationImpactService
    row_inclusion_reviews: RowInclusionReviewService
    matching_order: MatchingOrderService
    recipe_application: RecipeApplicationService


@dataclass(frozen=True, slots=True)
class PreparationServiceGraph:
    """Hold the target-independent preparation and final-review services."""

    quality: QualityService
    normalization: NormalizationService
    resolution: ResolutionService
    preparation: PreparationService
    preflight: PreflightService


def build_foundation_service_graph(
    repository: FoundationServiceRepository,
    workspace_state_repository: WorkspaceStateRepository,
    authorization: AuthorizationPolicy,
) -> FoundationServiceGraph:
    """Wire Project foundation use cases without choosing a deployment adapter."""

    workspace_access = WorkspaceAccessService(repository, authorization)
    workspace_states = WorkspaceStateService(
        workspace_state_repository,
        workspace_access,
    )
    migration_projects = MigrationProjectService(repository, authorization)
    data_versions = DataVersionService(repository, authorization)
    migration_runs = MigrationRunService(repository, authorization)
    migration_run_target_setup = MigrationRunTargetSetupService(
        repository,
        authorization,
    )
    migration_workspaces = MigrationWorkspaceService(repository, authorization)
    source_packages = DataVersionSourcePackageService(repository, authorization)
    project_authoring = MigrationProjectAuthoringService(
        migration_projects,
        data_versions,
        migration_runs,
        migration_workspaces,
        source_packages,
        workspace_states,
    )
    odoo_source_restarts = OdooSourceRestartService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
    )
    return FoundationServiceGraph(
        workspace_access=workspace_access,
        workspace_states=workspace_states,
        migration_projects=migration_projects,
        data_versions=data_versions,
        migration_runs=migration_runs,
        migration_run_target_setup=migration_run_target_setup,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        project_authoring=project_authoring,
        odoo_source_restarts=odoo_source_restarts,
    )


def build_authoring_service_graph(
    *,
    foundation: FoundationServiceGraph,
    foundation_repository: AuthoringFoundationRepository,
    authorization: AuthorizationPolicy,
    workspaces: AuthoringWorkspacePorts,
    sources: AuthoringSourcePorts,
    mapping_sources: AuthoringMappingSourcePorts,
    schemas: AuthoringSchemaPorts,
    mappings: AuthoringMappingPorts,
    quality: RecipeAuthoringQualityRepository,
    derived_entities: AuthoringDerivedPorts,
    references: RecipeAuthoringReferenceRepository,
    recipe_parameters: RecipeAuthoringParameterRepository,
    supporting_lookups: MappingSupportingLookupReader,
    downstream_invalidator: MappingDownstreamInvalidator,
    recipe_applications: AuthoringRecipeApplicationState,
    row_inclusion: AuthoringRowInclusionPorts,
    matching_order: AuthoringMatchingOrderPorts,
    transformation_impacts: TransformationImpactRepository,
    recipes: RecipeRepository,
    artifacts: GovernedArtifactStores,
    capture_filters: OdooCaptureFilterStore,
) -> AuthoringServiceGraph:
    """Wire source, Recipe, and Match data without choosing storage adapters."""

    # These established constructors call the workspace-scoped ``require``
    # contract while their annotations still name the Project authorization
    # protocol. Keep that compatibility cast at this composition boundary.
    workspace_authorization = cast(
        AuthorizationPolicy,
        foundation.workspace_access,
    )
    recipe_compiler = RecipeCompiler(
        mapping_sources,
        mappings,
        schemas,
        quality,
        derived_entities,
        references,
        recipe_parameters,
        supporting_references=supporting_lookups,
    )
    recipe_service = RecipeService(recipes, authorization)
    recipe_publication = RecipePublicationService(
        recipes,
        recipe_compiler,
        authorization,
    )
    source_workspace = SourceWorkspaceService(
        workspaces,
        sources,
        foundation.workspace_access,
        artifacts,
        schemas=schemas,
        capture_filters=capture_filters,
    )
    source_projections = WorkspaceSourceProjectionService(
        foundation_repository,
        authorization,
    )
    data_version_source_projection = WorkspaceDataVersionSourceService(
        foundation.workspace_states,
        sources,
        foundation.data_versions,
        foundation.migration_workspaces,
        foundation.source_packages,
        source_projections,
        source_workspace,
    )
    categorical_coverage = CategoricalCoverageService(sources, artifacts)
    numeric_precision_coverage = NumericPrecisionCoverageService(
        sources,
        artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        workspaces,
        sources,
        schemas,
        workspace_authorization,
    )
    mapping_workspace = MappingWorkspaceService(
        mapping_sources,
        schemas,
        mappings,
        workspace_authorization,
        categorical_coverage=categorical_coverage,
        numeric_precision_coverage=numeric_precision_coverage,
        supporting_lookups=supporting_lookups,
        downstream_invalidator=downstream_invalidator,
        recipe_applications=recipe_applications,
        row_inclusion_confirmations=row_inclusion,
    )
    transformation_impact_service = TransformationImpactService(
        workspaces,
        mappings,
        sources,
        derived_entities,
        transformation_impacts,
        artifacts,
        workspace_authorization,
    )
    row_inclusion_reviews = RowInclusionReviewService(
        transformation_impact_service,
        row_inclusion,
        foundation.workspace_access,
    )
    matching_order_service = MatchingOrderService(
        matching_order,
        foundation.workspace_access,
        categorical_coverage,
    )
    recipe_application = RecipeApplicationService(
        sources=mapping_sources,
        schemas=schemas,
        schema_workspace=schema_workspace,
        references=references,
        preparation=derived_entities,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        application_state=recipe_applications,
        row_inclusion_reviews=row_inclusion_reviews,
    )
    return AuthoringServiceGraph(
        recipe_compiler=recipe_compiler,
        recipes=recipe_service,
        recipe_publication=recipe_publication,
        source_workspace=source_workspace,
        source_projections=source_projections,
        data_version_source_projection=data_version_source_projection,
        categorical_coverage=categorical_coverage,
        numeric_precision_coverage=numeric_precision_coverage,
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        transformation_impacts=transformation_impact_service,
        row_inclusion_reviews=row_inclusion_reviews,
        matching_order=matching_order_service,
        recipe_application=recipe_application,
    )


def build_preparation_service_graph(
    *,
    workspaces: PreparationWorkspacePorts,
    sources: PreparationSourcePorts,
    derived_entities: PreparationDerivedRepository,
    mappings: PreparationMappingPorts,
    staging: PreparationStagingPorts,
    sessions: PreparationSessionRepository,
    artifacts: GovernedArtifactStores,
    authorization: WorkspaceAuthorizationPolicy,
    quality_repository: PreparationQualityPorts,
    normalization_repository: PreparationNormalizationPorts,
    resolution_repository: PreparationResolutionPorts,
    preflight_repository: PreflightRepository,
    schemas: PreflightSchemaRepository,
    columnar_transformations: ColumnarTransformationPort,
    odoo_provenance: OdooProvenanceService,
    recipe_quality: RecipeQualitySeedRepository,
) -> PreparationServiceGraph:
    """Wire preparation and final review without choosing concrete adapters."""

    # The established preparation constructors call the workspace-scoped
    # ``require`` contract even though their annotations still name the older
    # Project authorization protocol. Keep that compatibility cast inside the
    # shared builder until those historical diagnostics can be retired.
    legacy_authorization = cast(AuthorizationPolicy, authorization)
    quality = QualityService(
        mappings,
        sources,
        quality_repository,
        recipe_quality=recipe_quality,
    )
    normalization = NormalizationService(
        normalization_repository,
        legacy_authorization,
    )
    resolution = ResolutionService(resolution_repository, staging)
    preparation = PreparationService(
        workspaces,
        sources,
        derived_entities,
        mappings,
        staging,
        sessions,
        artifacts,
        legacy_authorization,
        quality,
        normalization,
        columnar_transformations,
        resolution,
        odoo_provenance=odoo_provenance,
        schemas=schemas,
    )
    preflight = PreflightService(
        staging,
        quality_repository,
        normalization_repository,
        mappings,
        workspaces,
        sources,
        preflight_repository,
        artifacts,
        legacy_authorization,
        resolution_repository,
        schemas,
        odoo_provenance,
    )
    return PreparationServiceGraph(
        quality=quality,
        normalization=normalization,
        resolution=resolution,
        preparation=preparation,
        preflight=preflight,
    )
