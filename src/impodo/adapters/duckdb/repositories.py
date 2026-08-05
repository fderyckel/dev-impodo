"""DuckDB repository collection implementing the application's narrow ports."""

from .database import DuckDbDatabaseMixin
from .derived_entity_repository import DerivedEntityRepositoryMixin
from .mapping_repository import MappingRepositoryMixin
from .normalization_repository import NormalizationRepositoryMixin
from .preflight_repository import PreflightRepositoryMixin
from .project_repository import ProjectRepositoryMixin
from .quality_repository import QualityRepositoryMixin
from .schema_repository import SchemaRepositoryMixin
from .source_repository import SourceRepositoryMixin
from .staging_repository import StagingRepositoryMixin
from .transformation_impact_repository import TransformationImpactRepositoryMixin


class DuckDbRepositories(
    DuckDbDatabaseMixin,
    ProjectRepositoryMixin,
    SourceRepositoryMixin,
    DerivedEntityRepositoryMixin,
    SchemaRepositoryMixin,
    MappingRepositoryMixin,
    StagingRepositoryMixin,
    QualityRepositoryMixin,
    NormalizationRepositoryMixin,
    PreflightRepositoryMixin,
    TransformationImpactRepositoryMixin,
):
    """Concrete collection of repository ports over one DuckDB boundary."""
