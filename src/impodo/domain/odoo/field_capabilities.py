"""Classify editable scalar values using native Odoo field descriptions.

Odoo's public ``fields_get`` description advertises storage, editability,
searchability, and exportability. It does not normally advertise the Python
compute or inverse methods. A missing inverse description is therefore not
the same evidence as an explicitly absent inverse.

These capabilities describe the native editable surface, not proof that a
custom inverse will preserve an assigned value. Callers must keep selected
fields, company context, exact review, and destination read-back as separate
requirements. See docs/developer/workflow/02-odoo-data.md.
"""

from __future__ import annotations

from typing import Protocol

_SCALAR_FIELD_TYPES = frozenset(
    {
        "boolean",
        "char",
        "date",
        "datetime",
        "float",
        "html",
        "integer",
        "monetary",
        "selection",
        "text",
    }
)


class OdooScalarFieldShape(Protocol):
    """Native field capabilities shared by source and destination metadata."""

    @property
    def type(self) -> str: ...

    @property
    def relation(self) -> str | None: ...

    @property
    def readonly(self) -> bool: ...

    @property
    def stored(self) -> bool | None: ...

    @property
    def computed(self) -> bool | None: ...

    @property
    def has_inverse(self) -> bool | None: ...

    @property
    def related(self) -> bool | None: ...

    @property
    def company_dependent(self) -> bool | None: ...

    @property
    def searchable(self) -> bool | None: ...

    @property
    def exportable(self) -> bool | None: ...


def is_editable_scalar_projection(field: OdooScalarFieldShape | None) -> bool:
    """Recognize one declared editable, searchable non-stored scalar value.

    Positive native inverse evidence qualifies the projection. When a public
    description omits both compute and inverse details, its declared native
    editable surface also qualifies. Known absence of an inverse, or a known
    computation without inverse evidence, does not qualify. Context-dependent
    storage itself remains outside this contract; callers read and write the
    exposed scalar under their separately verified operation context.
    """

    return bool(
        _is_editable_exportable_scalar(field)
        and field is not None
        and field.stored is False
        and field.searchable is True
        and (
            field.has_inverse is True
            or (field.computed is None and field.has_inverse is None)
        )
    )


def is_editable_scalar_field(field: OdooScalarFieldShape | None) -> bool:
    """Recognize stored editable scalars and declared editable projections.

    A stored value can persist an explicit assignment even when it is also
    computed and has no inverse. This capability does not admit readonly,
    related, relational, non-exportable, or company-dependent storage fields.
    """

    return bool(
        _is_editable_exportable_scalar(field)
        and field is not None
        and (field.stored is True or is_editable_scalar_projection(field))
    )


def _is_editable_exportable_scalar(field: OdooScalarFieldShape | None) -> bool:
    """Require explicit editable scalar and portable-storage descriptions."""

    return bool(
        field is not None
        and field.type in _SCALAR_FIELD_TYPES
        and field.relation is None
        and field.readonly is False
        and field.related is not True
        and field.company_dependent is False
        and field.exportable is True
    )
