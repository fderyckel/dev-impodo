"""Read the Mapping field catalogue from one consistent DuckDB snapshot."""

from __future__ import annotations

import duckdb

from ...application.mapping_field_catalog_query import (
    MappingFieldCatalogSnapshot,
)
from ...derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
)
from ...domain.mapping.artifacts import MappingRevision
from ...domain.schema.governance import SchemaGovernance
from ...inspection import SourceFileCatalog
from ...workspace_state import WorkspaceStateNotFoundError
from ...workspace_contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceSelection,
)
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository


class MappingFieldCatalogRepository(DuckDbRepository):
    """Load only the saved evidence required by Mapping field search."""

    def get_mapping_field_catalog_snapshot(
        self,
        project_id: str,
    ) -> MappingFieldCatalogSnapshot:
        """Return one coherent snapshot while opening DuckDB only once."""

        database_path = self.workspace_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Project not found")

        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                selection_json = self._optional_json(
                    connection,
                    """
                    SELECT selection_json
                      FROM source_selection
                     WHERE singleton_id = 1
                    """,
                )
                physical_selection = self._source_selection(selection_json)
                plan_json = self._optional_json(
                    connection,
                    """
                    SELECT revision.plan_json
                      FROM derived_entity_plan_current AS current
                      JOIN derived_entity_plan_revision AS revision
                        ON revision.plan_id = current.plan_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """,
                )
                preparation_plan = (
                    DerivedEntityPlan.from_json(plan_json) if plan_json else None
                )
                source_catalog_jsons = (
                    self._source_catalog_jsons(connection)
                    if physical_selection is not None
                    and self._plan_needs_source_catalogs(preparation_plan)
                    else ()
                )
                schema_json = self._optional_json(
                    connection,
                    """
                    SELECT catalog_json
                      FROM odoo_schema_catalog
                     WHERE singleton_id = 1
                    """,
                )
                governance_json = self._optional_json(
                    connection,
                    """
                    SELECT revision.governance_json
                      FROM schema_governance_current AS current
                      JOIN schema_governance_revision AS revision
                        ON revision.governance_id = current.governance_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """,
                )
                revision_json = self._optional_json(
                    connection,
                    """
                    SELECT revision.revision_json
                      FROM mapping_current AS current
                      JOIN mapping_revision AS revision
                        ON revision.mapping_id = current.mapping_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """,
                )
                working_draft_json = self._optional_json(
                    connection,
                    """
                    SELECT draft_json
                      FROM mapping_working_draft
                     WHERE singleton_id = 1
                    """,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return MappingFieldCatalogSnapshot(
            physical_selection=physical_selection,
            preparation_plan=preparation_plan,
            source_catalogs=tuple(
                SourceFileCatalog.from_json(value)
                for value in source_catalog_jsons
            ),
            schema=OdooSchemaCatalog.from_json(schema_json) if schema_json else None,
            governance=(
                SchemaGovernance.from_json(governance_json)
                if governance_json
                else None
            ),
            revision=(
                MappingRevision.from_json(revision_json) if revision_json else None
            ),
            working_draft=(
                MappingWorkingDraft.from_json(working_draft_json)
                if working_draft_json
                else None
            ),
        )

    @staticmethod
    def _optional_json(
        connection: duckdb.DuckDBPyConnection,
        query: str,
    ) -> str | None:
        row = connection.execute(query).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else None

    @staticmethod
    def _source_selection(value: str | None) -> SourceSelection | None:
        if value is None:
            return None
        try:
            return SourceSelection.from_json(value)
        except (TypeError, ValueError) as error:
            raise WorkspaceError("Stored source selection is invalid") from error

    @staticmethod
    def _source_catalog_jsons(
        connection: duckdb.DuckDBPyConnection,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT catalog.catalog_json
              FROM source_file AS source
              JOIN source_catalog AS catalog
                ON catalog.file_id = source.file_id
             ORDER BY source.received_at, source.file_id
            """
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _plan_needs_source_catalogs(
        plan: DerivedEntityPlan | None,
    ) -> bool:
        return bool(
            plan
            and any(
                isinstance(rule, (DerivedEntityRule, RelatedDatasetRule))
                for rule in plan.rules
            )
        )

