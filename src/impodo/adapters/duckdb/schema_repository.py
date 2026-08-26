"""Persist Stage C model/schema catalogs and business-key governance.

Layer: adapter. Model and schema catalogs are current target-bound snapshots;
schema governance is immutable revision evidence with a current pointer.
Recapture and regovernance atomically invalidate downstream mapping and
staging pointers. A separately verified access rebind updates only credential
provenance when the semantic schema and read identity remain unchanged.

See ``docs/architecture/python-code-map.md`` and ``tests/test_workspace.py``.
"""

from __future__ import annotations

from dataclasses import asdict

from ...access import Actor
from ...domain.schema.governance import SchemaGovernance
from ...workspace_state import WorkspaceStateNotFoundError
from ...workspace_contracts import (
    OdooModelCatalog,
    OdooSchemaCatalog,
)
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository







class SchemaRepository(DuckDbRepository):
    """Own current schema catalogs and versioned governance evidence."""

    def get_odoo_schema_catalog(
        self,
        workspace_id: str,
    ) -> OdooSchemaCatalog | None:
        """Return the current detailed schema catalog, if captured."""

        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT catalog_json
              FROM odoo_schema_catalog
             WHERE singleton_id = 1
            """,
        )
        return OdooSchemaCatalog.from_json(value) if value else None
    def get_odoo_model_catalog(
        self,
        workspace_id: str,
    ) -> OdooModelCatalog | None:
        """Return current lightweight model discovery, if refreshed."""

        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT catalog_json
              FROM odoo_model_catalog
             WHERE singleton_id = 1
            """,
        )
        return OdooModelCatalog.from_json(value) if value else None
    def save_odoo_model_catalog(
        self,
        workspace_id: str,
        catalog: OdooModelCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Replace model discovery without changing the permitted scope itself."""

        self._save_singleton(
            workspace_id,
            table="odoo_model_catalog",
            value_column="catalog_json",
            value=catalog.to_json(),
            event_type="ODOO_MODEL_CATALOG_REFRESHED",
            detail=f"{len(catalog.models)} persistent model(s)",
            actor=actor,
        )

    def save_odoo_schema_catalog(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        actor: Actor,
    ) -> None:
        """Publish an exact schema catalog and retire governance/mapping/staging."""

        event_type = (
            "LOCAL_SCHEMA_DRAFT_CREATED"
            if catalog.origin.value == "LOCAL_MANUAL"
            else "ODOO_SCHEMA_CAPTURED"
        )
        source = (
            "unverified local draft"
            if catalog.origin.value == "LOCAL_MANUAL"
            else "authenticated Odoo capture"
        )
        self._save_singleton(
            workspace_id,
            table="odoo_schema_catalog",
            value_column="catalog_json",
            value=catalog.to_json(),
            event_type=event_type,
            detail=f"{len(catalog.models)} permitted model(s); {source}",
            actor=actor,
            invalidate=(
                "mapping_current",
                "odoo_capture_selection_current",
                "odoo_capture_manifest_current",
                "schema_governance_current",
            ),
        )

    def rebind_odoo_schema_access(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Replace access provenance without invalidating semantic dependents."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT catalog_json
                      FROM odoo_schema_catalog
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Capture the Odoo schema first")
                current = OdooSchemaCatalog.from_json(str(row[0]))
                unchanged_semantics = (
                    current.workspace_id == workspace_id
                    and current.content_hash == expected_content_hash
                    and current.read_credential_binding_hash
                    == expected_read_credential_binding_hash
                    and catalog.workspace_id == current.workspace_id
                    and catalog.content_hash == current.content_hash
                    and catalog.policy_hash == current.policy_hash
                    and catalog.connection_target_hash
                    == current.connection_target_hash
                    and catalog.connection_mode == current.connection_mode
                    and catalog.database == current.database
                    and catalog.odoo_version == current.odoo_version
                    and catalog.origin is current.origin
                    and catalog.models == current.models
                    and catalog.read_principal_hash
                    == current.read_principal_hash
                    and catalog.read_permission_hash
                    == current.read_permission_hash
                    and catalog.read_context_hash == current.read_context_hash
                )
                if not unchanged_semantics:
                    raise WorkspaceError(
                        "Odoo schema access was modified by another request"
                    )
                connection.execute(
                    """
                    UPDATE odoo_schema_catalog
                       SET catalog_json = ?
                     WHERE singleton_id = 1
                    """,
                    [catalog.to_json()],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ODOO_SCHEMA_ACCESS_REBOUND",
                    detail=(
                        f"{len(catalog.models)} permitted model(s); "
                        "semantic schema unchanged"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_odoo_schema_check(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Store freshness or a pending candidate without retiring dependents."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT catalog_json
                      FROM odoo_schema_catalog
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Capture the Odoo schema first")
                current = OdooSchemaCatalog.from_json(str(row[0]))
                unchanged_current = (
                    current.workspace_id == workspace_id
                    and current.content_hash == expected_content_hash
                    and current.read_credential_binding_hash
                    == expected_read_credential_binding_hash
                    and catalog.workspace_id == current.workspace_id
                    and catalog.content_hash == current.content_hash
                    and catalog.policy_hash == current.policy_hash
                    and catalog.connection_target_hash
                    == current.connection_target_hash
                    and catalog.connection_mode == current.connection_mode
                    and catalog.database == current.database
                    and catalog.odoo_version == current.odoo_version
                    and catalog.origin is current.origin
                    and catalog.models == current.models
                    and catalog.read_principal_hash
                    == current.read_principal_hash
                    and catalog.read_permission_hash
                    == current.read_permission_hash
                    and catalog.read_context_hash == current.read_context_hash
                    and catalog.captured_at == current.captured_at
                    and catalog.captured_by == current.captured_by
                )
                if not unchanged_current:
                    raise WorkspaceError(
                        "Odoo schema was modified by another request"
                    )
                pending = catalog.pending_refresh
                if (
                    pending is not None
                    and pending.expected_current_content_hash
                    != current.content_hash
                ):
                    raise WorkspaceError(
                        "Checked Odoo details do not match the current schema"
                    )
                connection.execute(
                    """
                    UPDATE odoo_schema_catalog
                       SET catalog_json = ?
                     WHERE singleton_id = 1
                    """,
                    [catalog.to_json()],
                )
                event_type = (
                    "ODOO_SCHEMA_CHANGE_DETECTED"
                    if pending is not None
                    else "ODOO_SCHEMA_REVERIFIED_UNCHANGED"
                )
                detail = (
                    f"{pending.change_count} semantic change(s) need review"
                    if pending is not None
                    else f"{len(catalog.models)} permitted model(s); semantic schema unchanged"
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type=event_type,
                    detail=detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_odoo_create_defaults(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_content_hash: str,
        expected_read_credential_binding_hash: str,
        actor: Actor,
    ) -> None:
        """Store default evidence only; preserve governance and mapping pointers."""

        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT catalog_json
                      FROM odoo_schema_catalog
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Capture the Odoo schema first")
                current = OdooSchemaCatalog.from_json(str(row[0]))
                unchanged_structure = (
                    current.workspace_id == workspace_id
                    and current.content_hash == expected_content_hash
                    and current.read_credential_binding_hash
                    == expected_read_credential_binding_hash
                    and catalog.workspace_id == current.workspace_id
                    and catalog.content_hash == current.content_hash
                    and catalog.policy_hash == current.policy_hash
                    and catalog.connection_target_hash
                    == current.connection_target_hash
                    and catalog.connection_mode == current.connection_mode
                    and catalog.database == current.database
                    and catalog.odoo_version == current.odoo_version
                    and catalog.origin is current.origin
                    and _models_without_create_defaults(catalog.models)
                    == _models_without_create_defaults(current.models)
                    and catalog.read_credential_binding_hash
                    == current.read_credential_binding_hash
                    and catalog.read_principal_hash
                    == current.read_principal_hash
                    and catalog.read_permission_hash
                    == current.read_permission_hash
                    and catalog.read_context_hash == current.read_context_hash
                    and catalog.captured_at == current.captured_at
                    and catalog.captured_by == current.captured_by
                    and current.pending_refresh is None
                    and catalog.pending_refresh is None
                )
                if not unchanged_structure:
                    raise WorkspaceError(
                        "Odoo schema was modified by another request"
                    )
                changed_count = _create_default_change_count(
                    current.models,
                    catalog.models,
                )
                if changed_count < 1:
                    raise WorkspaceError(
                        "Odoo did not return new create-default evidence"
                    )
                connection.execute(
                    """
                    UPDATE odoo_schema_catalog
                       SET catalog_json = ?
                     WHERE singleton_id = 1
                    """,
                    [catalog.to_json()],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ODOO_CREATE_DEFAULTS_REFRESHED",
                    detail=(
                        f"{changed_count} required field default"
                        f"{'s' if changed_count != 1 else ''}; "
                        "schema structure unchanged"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def confirm_odoo_schema_refresh(
        self,
        workspace_id: str,
        catalog: OdooSchemaCatalog,
        *,
        expected_current_content_hash: str,
        expected_candidate_id: str,
        expected_candidate_semantic_hash: str,
        actor: Actor,
    ) -> None:
        """Atomically promote the reviewed candidate and retire dependents."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                row = connection.execute(
                    """
                    SELECT catalog_json
                      FROM odoo_schema_catalog
                     WHERE singleton_id = 1
                    """
                ).fetchone()
                if row is None:
                    raise WorkspaceError("Capture the Odoo schema first")
                current = OdooSchemaCatalog.from_json(str(row[0]))
                pending = current.pending_refresh
                candidate_matches = (
                    current.content_hash == expected_current_content_hash
                    and pending is not None
                    and pending.candidate_id == expected_candidate_id
                    and pending.semantic_hash == expected_candidate_semantic_hash
                    and pending.expected_current_content_hash
                    == expected_current_content_hash
                    and catalog.workspace_id == workspace_id
                    and catalog.content_hash == pending.content_hash
                    and catalog.policy_hash == pending.policy_hash
                    and catalog.connection_target_hash
                    == pending.connection_target_hash
                    and catalog.connection_mode == pending.connection_mode
                    and catalog.database == pending.database
                    and catalog.odoo_version == pending.odoo_version
                    and catalog.models == pending.models
                    and catalog.origin is pending.origin
                    and catalog.read_credential_binding_hash
                    == pending.read_credential_binding_hash
                    and catalog.read_principal_hash == pending.read_principal_hash
                    and catalog.read_permission_hash
                    == pending.read_permission_hash
                    and catalog.read_context_hash == pending.read_context_hash
                    and catalog.pending_refresh is None
                )
                if not candidate_matches:
                    raise WorkspaceError(
                        "The checked Odoo details changed in another request"
                    )
                connection.execute(
                    """
                    UPDATE odoo_schema_catalog
                       SET catalog_json = ?
                     WHERE singleton_id = 1
                    """,
                    [catalog.to_json()],
                )
                for target in (
                    "mapping_current",
                    "odoo_capture_selection_current",
                    "odoo_capture_manifest_current",
                    "schema_governance_current",
                ):
                    connection.execute(f"DELETE FROM {target}")
                self._invalidate_canonical_staging(
                    connection,
                    reason="ODOO_SCHEMA_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ODOO_SCHEMA_CHANGE_ACCEPTED",
                    detail=(
                        f"{len(catalog.models)} permitted model(s); "
                        "reviewed Odoo changes accepted"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_schema_governance(
        self,
        workspace_id: str,
    ) -> SchemaGovernance | None:
        """Load the current governance revision selected by its pointer."""

        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT revision.governance_json
              FROM schema_governance_current AS current
              JOIN schema_governance_revision AS revision
                ON revision.governance_id = current.governance_id
               AND revision.version = current.version
             WHERE current.singleton_id = 1
            """,
        )
        return SchemaGovernance.from_json(value) if value else None
    def save_schema_governance(
        self,
        workspace_id: str,
        governance: SchemaGovernance,
        *,
        actor: Actor,
    ) -> None:
        """Append the next exact governance revision and invalidate dependents."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            schema_row = connection.execute(
                """
                SELECT catalog_json
                  FROM odoo_schema_catalog
                 WHERE singleton_id = 1
                """
            ).fetchone()
            if schema_row is None:
                raise WorkspaceError("Capture the Odoo schema first")
            schema = OdooSchemaCatalog.from_json(str(schema_row[0]))
            if (
                governance.workspace_id != workspace_id
                or governance.catalog_hash != schema.content_hash
                or tuple(sorted(governance.permitted_models))
                != tuple(sorted(model.name for model in schema.models))
            ):
                raise WorkspaceError(
                    "Schema governance does not match the captured schema"
                )
            current = connection.execute(
                """
                SELECT governance_id, version
                  FROM schema_governance_current
                 WHERE singleton_id = 1
                """
            ).fetchone()
            expected_version = int(current[1]) + 1 if current else 1
            expected_id = str(current[0]) if current else governance.governance_id
            if (
                governance.version != expected_version
                or governance.governance_id != expected_id
            ):
                raise WorkspaceError(
                    "Schema governance was modified by another request"
                )
            revision = self._workspace_revision(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO schema_governance_revision
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        governance.governance_id,
                        governance.version,
                        governance.catalog_hash,
                        governance.content_hash,
                        governance.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO schema_governance_current
                    VALUES (1, ?, ?)
                    """,
                    [governance.governance_id, governance.version],
                )
                connection.execute("DELETE FROM mapping_current")
                self._invalidate_canonical_staging(
                    connection,
                    reason="SCHEMA_GOVERNANCE_CHANGED",
                )
                self._insert_workspace_audit(
                    connection,
                    revision=revision,
                    event_type="SCHEMA_GOVERNANCE_CONFIRMED",
                    detail=(
                        f"version {governance.version}: "
                        f"{len(governance.business_keys)} business key(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def _models_without_create_defaults(models) -> list[dict[str, object]]:
    """Return exact schema structure with supplemental default values removed."""

    result: list[dict[str, object]] = []
    for model in models:
        value = asdict(model)
        for field in value["fields"]:
            field.pop("create_default_present", None)
            field.pop("create_default_value", None)
        result.append(value)
    return result


def _create_default_change_count(previous_models, observed_models) -> int:
    """Count fields whose supplemental create-default evidence changed."""

    previous = {
        (model.name, field.name): (
            field.create_default_present,
            field.create_default_value,
        )
        for model in previous_models
        for field in model.fields
    }
    return sum(
        previous.get((model.name, field.name))
        != (field.create_default_present, field.create_default_value)
        for model in observed_models
        for field in model.fields
    )
