"""Export authorized load verification against unchanged frozen source cells."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path
from typing import Protocol
from uuid import UUID

from openpyxl.utils import get_column_letter, range_boundaries

from impodo.application.data_version.inspection import SourceFileCatalog
from impodo.application.fallout_workbook_port import (
    FalloutWorkbookCell,
    FalloutWorkbookRenderer,
)
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.workspace.execution.reconciliation import (
    ReconciliationService,
)
from impodo.application.workspace.review.original_values import workbook_field_providers
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.mapping.artifacts import MappingRevision
from impodo.domain.preflight.physical_lineage import PhysicalColumnIndex
from impodo.domain.preparation.staging_contracts import CanonicalStagingRun
from impodo.domain.reconciliation import ReconciliationRun, VerificationWorkbook
from impodo.domain.reconciliation_detail import ReconciliationDetailArtifact
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.shared.models import portable_value
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    SourceSelection,
    canonical_mapping_source_selection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    mapping_source_selection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import SourceFile, WorkspaceState


class FalloutPreflightReader(Protocol):
    def execution_snapshot(
        self,
        workspace_id: str,
        preflight_run_id: str,
    ) -> ExecutionSnapshot: ...


class WorkbookVerificationReader(Protocol):
    def get(self, workspace_id: str, reconciliation_id: str) -> ReconciliationRun | None: ...

    def previous_workbooks(
        self, workspace_id: str, report: ReconciliationRun, *, offset: int, limit: int
    ) -> tuple[VerificationWorkbook, ...]: ...


class FalloutStagingReader(Protocol):
    def get_canonical_staging_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        expected_content_hash: str | None = None,
    ) -> CanonicalStagingRun | None: ...


class FalloutSourceReader(Protocol):
    def get_source_selection(self, workspace_id: str) -> SourceSelection | None: ...

    def get_source_catalogs(
        self, workspace_id: str
    ) -> tuple[SourceFileCatalog, ...]: ...


class FalloutWorkspaceReader(Protocol):
    def get(self, workspace_id: str) -> WorkspaceState: ...


@dataclass(frozen=True, slots=True)
class GeneratedFalloutWorkbook:
    filename: str
    content: bytes
    affected_cell_count: int


class FalloutWorkbookService:
    """Create an authorized workbook from one internally consistent attempt."""

    def __init__(
        self,
        reconciliation: ReconciliationService,
        preflight: FalloutPreflightReader,
        staging: FalloutStagingReader,
        sources: FalloutSourceReader,
        workspaces: FalloutWorkspaceReader,
        artifacts: GovernedArtifactStores,
        authorization: WorkspaceAuthorizationPolicy,
        workbook_renderer: FalloutWorkbookRenderer,
        *,
        verifications: WorkbookVerificationReader,
        mapping_revision: Callable[[str, int], MappingRevision | None],
        derived_plan: Callable[[str, str], DerivedEntityPlan | None],
    ) -> None:
        self._reconciliation = reconciliation
        self._preflight = preflight
        self._staging = staging
        self._sources = sources
        self._workspaces = workspaces
        self._artifacts = artifacts
        self._authorization = authorization
        self._workbook_renderer = workbook_renderer
        self._verifications = verifications
        self._mapping_revision = mapping_revision
        self._derived_plan = derived_plan

    def previous_workbooks(
        self, workspace_id: str, report: ReconciliationRun, *, offset: int, actor: Actor,
    ) -> tuple[VerificationWorkbook, ...]:
        """Return one page and a lookahead without opening workbook artifacts."""

        self._authorization.require(actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=workspace_id)
        return self._verifications.previous_workbooks(workspace_id, report, offset=offset, limit=21)

    def generate(
        self,
        workspace_id: str,
        reconciliation_id: str,
        *,
        actor: Actor,
    ) -> GeneratedFalloutWorkbook:
        self._authorization.require(
            actor,
            Capability.PROTECTED_EVIDENCE_READ,
            workspace_id=workspace_id,
        )
        try:
            identifier = str(UUID(reconciliation_id))
        except ValueError as error:
            raise WorkspaceError("Choose a verification workbook on the load outcome page.") from error
        report = self._verifications.get(workspace_id, identifier)
        if report is None or report.reconciliation_id != identifier or report.workspace_id != workspace_id:
            raise WorkspaceError("This verification is unavailable in the selected workspace.")
        detail = self._reconciliation.detail_for(report, actor=actor)
        if detail is None:
            raise WorkspaceError("This verification has no saved source-cell evidence. Choose another verification.")
        execution_run = self._reconciliation.execution.get_run(
            workspace_id,
            report.execution_run_id,
        )
        if execution_run is None:
            raise WorkspaceError("The saved load journal is unavailable")
        snapshot = self._preflight.execution_snapshot(
            workspace_id,
            execution_run.preflight_run_id,
        )
        if (
            execution_run.run_id != report.execution_run_id
            or execution_run.target_hash != report.target_hash
            or execution_run.target_database != report.target_database
            or execution_run.snapshot_hash != report.snapshot_hash
            or snapshot.semantic_hash != report.snapshot_hash
            or snapshot.root_hash != execution_run.snapshot_root_hash
            or snapshot.target_hash != report.target_hash
            or snapshot.target_database != report.target_database
            or snapshot.workspace_id != workspace_id
        ):
            raise WorkspaceError("The saved verification no longer matches its load.")
        staging = self._staging.get_canonical_staging_run(
            workspace_id,
            snapshot.staging_run_id,
            expected_content_hash=snapshot.staging_content_hash,
        )
        selection = self._sources.get_source_selection(workspace_id)
        if staging is None or selection is None:
            raise WorkspaceError("The frozen source lineage is unavailable")
        if staging.physical_selection_hash != selection.content_hash:
            raise WorkspaceError(
                "The source tables for this load no longer match the saved "
                "preparation. Open the original data version to export its workbook."
            )

        workspace = self._workspaces.get(workspace_id)
        source_files = {item.file_id: item for item in workspace.source_files}
        workbook_cells, used_file_ids = self._source_cells(
            workspace_id, snapshot, staging, selection, detail, source_files
        )
        if not used_file_ids:
            used_file_ids.update(
                item.source.file_id
                for item in selection.datasets
                if isinstance(item.source, FileSourceBinding)
            )
        if len(used_file_ids) != 1:
            raise WorkspaceError(
                "This load uses several source files or has no source workbook. "
                "Download the review workbook from the saved outcome."
            )
        file_id = next(iter(used_file_ids))
        source_file = source_files.get(file_id)
        if source_file is None:
            raise WorkspaceError("The accepted source workbook is unavailable")
        binding = next(
            (
                item.source
                for item in selection.datasets
                if isinstance(item.source, FileSourceBinding)
                and item.source.file_id == file_id
            ),
            None,
        )
        if (
            binding is None
            or _canonical_hash(source_file.sha256) != binding.source_sha256
        ):
            raise WorkspaceError("The accepted source workbook failed verification")
        with self._artifacts.materialize_source(
            selection.data_version_id,
            source_file.stored_name,
        ) as source_path:
            with source_path.open("rb") as source:
                if _canonical_hash(file_digest(source, "sha256").hexdigest()) != binding.source_sha256:
                    raise WorkspaceError("The saved source file no longer matches the accepted upload.")
            content = self._workbook_renderer(
                Path(source_path),
                source_display_name=source_file.display_name,
                source_header_row=binding.header_row,
                source_encoding=binding.encoding,
                source_delimiter=binding.delimiter,
                report=report,
                cells=workbook_cells,
            )
        return GeneratedFalloutWorkbook(
            filename=f"impodo-load-results-{report.reconciliation_id}.xlsx",
            content=content,
            affected_cell_count=sum(bool(item.coordinate) for item in workbook_cells),
        )

    def _source_cells(
        self,
        workspace_id: str,
        snapshot: ExecutionSnapshot,
        staging: CanonicalStagingRun,
        selection: SourceSelection,
        detail: ReconciliationDetailArtifact,
        source_files: dict[str, SourceFile],
    ) -> tuple[list[FalloutWorkbookCell], set[str]]:
        if not detail.differences:
            return [], set()
        execution_rows = {item.source_trace_id: item for item in snapshot.rows}
        canonical_rows = {item.row_id: item for item in staging.rows}
        datasets = {item.dataset_id: item for item in selection.datasets}
        source_catalogs = {
            item.file_id: item
            for item in self._sources.get_source_catalogs(workspace_id)
        }
        workbook_cells: list[FalloutWorkbookCell] = []
        used_file_ids: set[str] = set()

        plan = self._derived_plan(workspace_id, staging.derived_plan_hash) if staging.derived_plan_hash else None
        if (plan.content_hash if plan else None) != staging.derived_plan_hash or (
            plan is not None and plan.workspace_id != workspace_id
        ):
            raise WorkspaceError("The preparation rules saved for this load are unavailable.")
        effective = mapping_source_selection(
            canonical_mapping_source_selection(selection), plan, source_catalogs.values()
        )
        if effective.content_hash != staging.source_selection_hash:
            raise WorkspaceError("The source columns no longer match this verification.")
        revision = self._mapping_revision(workspace_id, snapshot.mapping_version)
        if (
            revision is None or revision.mapping_id != snapshot.mapping_id
            or revision.definition.content_hash != snapshot.mapping_content_hash
            or revision.version != snapshot.mapping_version
        ):
            raise WorkspaceError("The field mapping saved for this load is unavailable.")
        index = PhysicalColumnIndex(effective, selection, plan)
        providers = workbook_field_providers(
            effective, revision.definition.datasets, {item.dataset: item.target_model for item in snapshot.datasets},
            include_identity=True,
        )
        columns = {
            (dataset.dataset_id, column.stable_key): column
            for dataset in selection.datasets for column in dataset.columns
        }
        for difference in detail.differences:
            execution_row = execution_rows.get(difference.source_trace_id)
            canonical_row = canonical_rows.get(difference.source_trace_id)
            if execution_row is None or canonical_row is None:
                raise WorkspaceError("A fallout row no longer matches frozen lineage")
            lineage = canonical_row.lineage
            if (
                lineage.dataset != difference.dataset or lineage.source_row != difference.source_row
                or canonical_row.target_model != difference.target_model
            ):
                raise WorkspaceError("A verification field no longer matches its saved source row.")
            matches = providers.get((lineage.dataset, difference.field), ())
            resolved = []
            if len(matches) == 1:
                provider = matches[0]
                keys = provider.inputs_for(
                    difference.field, lineage.field_sources.get(difference.field, ()), lineage
                )
                coordinates = index.resolve(provider.dataset.dataset_id, keys, lineage) if keys else ()
                for cell in coordinates or ():
                    binding = datasets[cell.dataset_id].source
                    if not isinstance(binding, FileSourceBinding):
                        raise WorkspaceError("The saved source cell does not belong to a file.")
                    column = columns[cell.dataset_id, cell.source_key]
                    number = _source_column_number(binding, column.ordinal, source_catalogs.get(binding.file_id))
                    worksheet = _worksheet_name(binding.table_key, source_files.get(binding.file_id))
                    resolved.append((binding.file_id, worksheet, f"{get_column_letter(number)}{cell.source_row}"))
            if not resolved:
                primary = datasets.get(canonical_row.lineage.physical_dataset_id)
                if primary is not None and isinstance(
                    primary.source, FileSourceBinding
                ):
                    used_file_ids.add(primary.source.file_id)
                    source_file = source_files.get(primary.source.file_id)
                    source_name = source_file.display_name if source_file else "Source"
                else:
                    source_name = "Generated value"
                workbook_cells.append(
                    _workbook_cell(
                        difference,
                        business_key=_business_key(execution_row.business_identity),
                        source_file=source_name,
                        worksheet="",
                        coordinate="",
                    )
                )
                continue
            for file_id, worksheet, coordinate in sorted(set(resolved)):
                source_file = source_files.get(file_id)
                if source_file is None:
                    raise WorkspaceError("An accepted source file is unavailable")
                used_file_ids.add(file_id)
                workbook_cells.append(
                    _workbook_cell(
                        difference,
                        business_key=_business_key(execution_row.business_identity),
                        source_file=source_file.display_name,
                        worksheet=worksheet,
                        coordinate=coordinate,
                    )
                )

        return workbook_cells, used_file_ids


def _workbook_cell(
    difference,
    *,
    business_key: str,
    source_file: str,
    worksheet: str,
    coordinate: str,
) -> FalloutWorkbookCell:
    return FalloutWorkbookCell(
        source_file=source_file,
        worksheet=worksheet,
        coordinate=coordinate,
        business_key=business_key,
        odoo_record=f"{difference.target_model} · {difference.odoo_id}",
        field=difference.field.replace("_", " ").title(),
        source_value=None,
        prepared_value=difference.expected_value,
        odoo_value=difference.observed_value,
        result="Different after Odoo read-back",
        reason_code=difference.reason_code,
        recommended_action=_recommended_action(difference.reason_code),
    )


def _recommended_action(reason_code: str) -> str:
    if reason_code == "TARGET_NUMERIC_PRECISION_LOSS":
        return (
            "Change Odoo precision or approve an explicit rounding or unit "
            "conversion rule. Do not reload the records."
        )
    if reason_code == "HTML_CONTENT_DIFFERENT":
        return (
            "Review the meaningful HTML difference or approve an explicit "
            "normalization rule. Do not reload the records."
        )
    return "Review the source, mapping, and Odoo value. Do not reload the records."


def _worksheet_name(table_key: str, source_file) -> str:
    if table_key.startswith("sheet:"):
        return table_key.removeprefix("sheet:")
    if table_key.startswith("table:"):
        return table_key.removeprefix("table:").rsplit(":", 1)[0]
    if table_key == "csv":
        return _safe_sheet_title(
            Path(source_file.display_name).stem if source_file else "Source"
        )
    return ""


def _source_column_number(
    binding: FileSourceBinding,
    ordinal: int,
    catalog: SourceFileCatalog | None,
) -> int:
    """Resolve a selected-table-relative ordinal to an Excel column number."""

    if not binding.table_key.startswith("table:"):
        return ordinal
    if catalog is None:
        raise WorkspaceError("The frozen source catalog is unavailable")
    table = next(
        (item for item in catalog.tables if item.table_key == binding.table_key),
        None,
    )
    if table is None or len(table.named_tables) != 1:
        raise WorkspaceError("The frozen Excel table bounds are unavailable")
    minimum_column, _minimum_row, maximum_column, _maximum_row = range_boundaries(
        table.named_tables[0].cell_range
    )
    if minimum_column is None or maximum_column is None:
        raise WorkspaceError("The frozen Excel table bounds are invalid")
    column_number = minimum_column + ordinal - 1
    if ordinal < 1 or column_number > maximum_column:
        raise WorkspaceError("The frozen Excel table column is invalid")
    return column_number


def _safe_sheet_title(value: str) -> str:
    cleaned = "".join("_" if item in "[]:*?/\\" else item for item in value)
    return (cleaned or "Source")[:31]


def _business_key(value: tuple[object, ...]) -> str:
    portable = portable_value(value)
    if isinstance(portable, list):
        return " · ".join(str(item) for item in portable)
    return str(portable)


def _canonical_hash(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


__all__ = ["FalloutWorkbookService", "GeneratedFalloutWorkbook"]
