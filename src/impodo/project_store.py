"""Compatibility facade for the split DuckDB repositories."""

from .adapters.duckdb.database import (
    NORMALIZATION_ROW_BATCH_SIZE,
    QUALITY_ROW_BATCH_SIZE,
    SCHEMA_VERSION,
    STAGING_ROW_BATCH_SIZE,
    TRANSFORMATION_IMPACT_ROW_BATCH_SIZE,
)
from .adapters.duckdb.database import DuckDbDatabaseMixin
from .adapters.duckdb.project_repository import ProjectRepositoryMixin
from .adapters.duckdb.source_repository import SourceRepositoryMixin
from .adapters.duckdb.derived_entity_repository import DerivedEntityRepositoryMixin
from .adapters.duckdb.schema_repository import SchemaRepositoryMixin
from .adapters.duckdb.mapping_repository import MappingRepositoryMixin
from .adapters.duckdb.staging_repository import StagingRepositoryMixin
from .adapters.duckdb.quality_repository import QualityRepositoryMixin
from .adapters.duckdb.normalization_repository import NormalizationRepositoryMixin
from .adapters.duckdb.preflight_repository import PreflightRepositoryMixin
from .adapters.duckdb.transformation_impact_repository import TransformationImpactRepositoryMixin


class DuckDbProjectRepository(
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
    """Backward-compatible aggregate over narrow DuckDB repositories."""


__all__ = [
    "DuckDbProjectRepository",
    "NORMALIZATION_ROW_BATCH_SIZE",
    "QUALITY_ROW_BATCH_SIZE",
    "SCHEMA_VERSION",
    "STAGING_ROW_BATCH_SIZE",
    "TRANSFORMATION_IMPACT_ROW_BATCH_SIZE",
]
