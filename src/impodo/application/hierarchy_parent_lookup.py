"""Read bounded existing hierarchy paths through an authorized target reader.

See docs/developer/workflow/01-source-data.md. Readers run outside local
database scopes. Parent IDs remain transient; results contain business paths.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from impodo.domain.hierarchy import MAX_PARENT_DEPTH, HierarchyParentPolicy
from impodo.domain.odoo.compatibility import recognize_odoo_version
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordRequest, RecordSnapshot
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import (
    GovernedReferenceRequest,
    ReferenceReadPurpose,
    StandardReferenceFieldContract,
    authorize_hierarchy_parent_read,
    selectable_matching_field,
)

PARENT_CHOICE_LIMIT = 50
HierarchyRecordReader = Callable[[tuple[RecordRequest, ...], bool], RecordSnapshot]


@dataclass(frozen=True, slots=True)
class HierarchyParentChoice:
    """One accessible parent expressed with its full path and company names."""

    path: tuple[str, ...]
    companies: tuple[str | None, ...]

    @property
    def value(self) -> str:
        return json.dumps(
            {"path": self.path, "companies": self.companies},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @property
    def label(self) -> str:
        return " / ".join(self.path) + (
            f" · {self.companies[-1] or 'Shared'}" if self.companies else ""
        )


@dataclass(frozen=True, slots=True)
class HierarchyParentPage:
    """A bounded picker result that cannot claim to be a complete catalogue."""

    choices: tuple[HierarchyParentChoice, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class HierarchyLookupShape:
    """Captured fields that authorize one model-neutral parent lookup."""

    model: str
    name_field: str
    parent_field: str
    company_field: str | None
    active_field: str | None

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.name_field,
                self.parent_field,
                self.company_field,
                self.active_field,
            )
            if value is not None
        )


def hierarchy_lookup_shape(
    metadata: MetadataSnapshot, model: str, name_field: str, parent_field: str
) -> HierarchyLookupShape:
    """Authorize only a captured searchable name and writable self-parent."""
    if not metadata.complete or recognize_odoo_version(
        metadata.fingerprint.odoo_version
    ).major not in {19, 20}:
        raise WorkspaceError(
            "Check a supported Odoo 19 or Odoo 20 target before choosing existing parents"
        )
    captured = metadata.models.get(model)
    if captured is None:
        raise WorkspaceError(
            "This Odoo record type is unavailable to the checking user"
        )
    name = captured.fields.get(name_field)
    if name is None or not selectable_matching_field(name):
        raise WorkspaceError("Choose a searchable text field for the hierarchy names")
    parents = tuple(
        field
        for field in captured.fields.values()
        if field.type == "many2one"
        and field.relation == model
        and not field.readonly
        and field.searchable is not False
    )
    selected = (
        next((field for field in parents if field.name == parent_field), None)
        if parent_field
        else (parents[0] if len(parents) == 1 else None)
    )
    if selected is None:
        raise WorkspaceError(
            "Choose the parent relationship: "
            + ", ".join(field.name for field in parents)
        )
    company_fields = tuple(
        field.name
        for field in captured.fields.values()
        if field.type == "many2one"
        and field.name != selected.name
        and not field.readonly
        and field.relation == "res.company"
        and field.searchable is not False
    )
    if len(company_fields) > 1:
        raise WorkspaceError(
            "This record type has several company relationships; confirm its company matching rule before building the hierarchy"
        )
    shape = HierarchyLookupShape(
        model,
        name_field,
        selected.name,
        company_fields[0] if company_fields else None,
        "active"
        if "active" in captured.fields and captured.fields["active"].type == "boolean"
        else None,
    )
    decision = authorize_hierarchy_parent_read(
        GovernedReferenceRequest(
            parent_model=model,
            relationship_field=selected.name,
            relationship_type="many2one",
            relationship_model=model,
            related_model=model,
            key_fields=(name_field,),
            scope_fields=tuple(
                value for value in (selected.name, shape.company_field) if value
            ),
            requested_fields=shape.fields,
            purpose=ReferenceReadPurpose.HIERARCHY_PARENT_CHOICES,
            odoo_major_version=recognize_odoo_version(
                metadata.fingerprint.odoo_version
            ).major
            or -1,
        ),
        tuple(
            StandardReferenceFieldContract(
                field.name, field.type, field.required, field.readonly, field.relation
            )
            for field in captured.fields.values()
        ),
    )
    if not decision.accepted:
        raise WorkspaceError(
            "The selected parent relationship is unavailable for hierarchy matching"
        )
    return shape


def selected_parent_domain(
    value: str, shape: HierarchyLookupShape
) -> tuple[object, ...]:
    """Translate an inert choice into an exact path, including its root."""
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict) or set(raw) != {"path", "companies"}:
            raise ValueError
        path, companies = raw["path"], raw["companies"]
        if (
            not isinstance(path, list)
            or not 1 <= len(path) <= MAX_PARENT_DEPTH
            or any(
                not isinstance(item, str) or not item or len(item) > 500
                for item in path
            )
        ):
            raise ValueError
        if not isinstance(companies, list) or (
            len(companies) != len(path) if shape.company_field else bool(companies)
        ):
            raise ValueError
        if any(
            item is not None
            and (not isinstance(item, str) or not item or len(item) > 200)
            for item in companies
        ):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise WorkspaceError(
            "Find and choose the existing Odoo parent again"
        ) from error
    domain: list[object] = []
    prefix = ""
    for index in range(len(path) - 1, -1, -1):
        domain.append((prefix + shape.name_field, "=", path[index]))
        if shape.company_field:
            company = companies[index]
            domain.append(
                (
                    prefix
                    + shape.company_field
                    + (".name" if company is not None else ""),
                    "=",
                    company if company is not None else False,
                )
            )
        prefix += shape.parent_field + "."
    domain.append((prefix[:-1], "=", False))
    return tuple(domain)


def read_parent_choices(
    shape: HierarchyLookupShape,
    read: HierarchyRecordReader,
    *,
    search: str = "",
    selected: str = "",
) -> tuple[HierarchyParentChoice, ...]:
    """Search a bounded candidate set and fetch ancestors once per depth."""
    return _read_parent_page(shape, read, search=search, selected=selected).choices


def browse_parent_choices(
    shape: HierarchyLookupShape,
    read: HierarchyRecordReader,
    *,
    search: str = "",
) -> HierarchyParentPage:
    """Fetch up to fifty target parents; an optional filter narrows larger lists."""
    return _read_parent_page(shape, read, search=search, browse=True)


def _read_parent_page(
    shape: HierarchyLookupShape,
    read: HierarchyRecordReader,
    *,
    search: str = "",
    selected: str = "",
    browse: bool = False,
) -> HierarchyParentPage:
    if selected:
        domain = selected_parent_domain(selected, shape)
    elif len(search) <= 200 and (search.strip() or browse):
        domain = (
            ((shape.name_field, "ilike", search.strip()),) if search.strip() else ()
        )
    else:
        raise WorkspaceError(
            "Enter part of the existing parent name, then find its full Odoo path"
        )
    if shape.active_field is not None:
        domain = (*domain, (shape.active_field, "=", True))
    snapshot = read(
        (
            RecordRequest(
                shape.model, shape.fields, domain=domain, limit=PARENT_CHOICE_LIMIT + 1
            ),
        ),
        True,
    )
    if not snapshot.complete:
        raise WorkspaceError(
            "The Odoo parent search was incomplete; narrow the name and try again"
        )
    candidates = snapshot.records.get(shape.model, ())
    has_more = len(candidates) > PARENT_CHOICE_LIMIT
    if has_more and not browse:
        raise WorkspaceError(
            "More than 50 parents match. Enter a more specific name and search again"
        )
    if selected and len(candidates) != 1:
        raise WorkspaceError(
            "The selected parent is missing, inaccessible, or ambiguous in this company. Find its path again"
        )
    candidates = candidates[:PARENT_CHOICE_LIMIT]
    records = {record.odoo_id: record for record in candidates}
    frontier = set(records)
    for _depth in range(MAX_PARENT_DEPTH):
        missing = {
            parent
            for identifier in frontier
            if (parent := _relation_id(records[identifier], shape.parent_field))
            is not None
        } - records.keys()
        if not missing:
            break
        snapshot = read(
            (
                RecordRequest(
                    shape.model,
                    shape.fields,
                    domain=(("id", "in", tuple(sorted(missing))),),
                    limit=len(missing),
                ),
            ),
            False,
        )
        added = snapshot.records.get(shape.model, ())
        if not snapshot.complete or {record.odoo_id for record in added} != missing:
            raise WorkspaceError(
                "The checking user cannot read the complete parent path. Check Odoo access and try again"
            )
        records.update((record.odoo_id, record) for record in added)
        frontier = {record.odoo_id for record in added}
    else:
        raise WorkspaceError(
            "This Odoo hierarchy is too deep or contains a parent cycle"
        )
    company_names: dict[int, str] = {}
    if shape.company_field:
        company_ids = {
            company
            for record in records.values()
            if (company := _relation_id(record, shape.company_field)) is not None
        }
        if company_ids:
            companies = read(
                (
                    RecordRequest(
                        "res.company",
                        ("name",),
                        domain=(("id", "in", tuple(sorted(company_ids))),),
                        limit=len(company_ids),
                    ),
                ),
                False,
            )
            company_names = {
                record.odoo_id: str(record.values["name"])
                for record in companies.records.get("res.company", ())
            }
            if not companies.complete or company_names.keys() != company_ids:
                raise WorkspaceError(
                    "The checking user cannot read the parent companies"
                )
    choices = []
    for candidate in candidates:
        current, chain, seen = candidate, [], set()
        while True:
            if current.odoo_id in seen or len(chain) >= MAX_PARENT_DEPTH:
                raise WorkspaceError(
                    "This Odoo hierarchy is too deep or contains a parent cycle"
                )
            seen.add(current.odoo_id)
            chain.append(current)
            parent_id = _relation_id(current, shape.parent_field)
            if parent_id is None:
                break
            current = records[parent_id]
        chain.reverse()
        choices.append(
            HierarchyParentChoice(
                tuple(str(record.values[shape.name_field]) for record in chain),
                tuple(
                    company_names.get(_relation_id(record, shape.company_field) or 0)
                    for record in chain
                )
                if shape.company_field
                else (),
            )
        )
    if len({choice.value for choice in choices}) != len(choices):
        raise WorkspaceError(
            "Several Odoo records have the same full path and company. Resolve the duplicates in Odoo before selecting this parent"
        )
    return HierarchyParentPage(
        tuple(sorted(choices, key=lambda choice: choice.label)), has_more
    )


def parent_policy(
    shape: HierarchyLookupShape,
    choice: HierarchyParentChoice | None,
    existing_levels: int,
    company_name: str | None,
) -> HierarchyParentPolicy:
    """Bind selected paths and the imported company without persisting IDs."""
    return HierarchyParentPolicy(
        name_field=shape.name_field,
        parent_field=shape.parent_field,
        fixed_path=choice.path if choice else (),
        existing_source_levels=existing_levels,
        company_field=shape.company_field,
        company_name=company_name if shape.company_field else None,
        fixed_companies=choice.companies if choice else (),
    )


def _relation_id(record: TargetRecord, field: str) -> int | None:
    value = record.values.get(field)
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = value[0]
    if type(value) is not int or value <= 0:
        raise WorkspaceError("Odoo returned an invalid parent or company relationship")
    return value
