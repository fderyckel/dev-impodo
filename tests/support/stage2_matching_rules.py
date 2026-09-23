"""Representative Stage 2 matching-rule evidence for tests and screenshots."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from impodo.domain.shared.models import UniqueConstraintMetadata
from impodo.domain.workspace.business_keys import BUSINESS_KEY_POLICY_VERSION
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


def representative_stage2_models() -> tuple[SchemaModel, ...]:
    """Return standard, extended-standard, and custom Odoo 19 shapes."""

    return (
        SchemaModel(
            name="res.partner",
            label="Contact",
            fields=(
                _field("ref", "Reference", required=False),
                _field("name", "Name", required=True),
            ),
        ),
        SchemaModel(
            name="product.template",
            label="Product",
            fields=(
                _field("default_code", "Internal Reference", required=False),
                _field("x_legacy_code", "Legacy Reference", required=False),
                _field("name", "Name", required=True),
            ),
        ),
        SchemaModel(
            name="x.asset",
            label="Asset",
            fields=(
                _field("code", "Asset Code", required=True),
                _field("name", "Name", required=True),
            ),
            unique_constraints=(
                UniqueConstraintMetadata(
                    name="x_asset_code_uniq",
                    definition="UNIQUE(code)",
                ),
            ),
        ),
    )


def unresolved_stage2_model() -> SchemaModel:
    """Return a custom model whose field names provide no identity evidence."""

    return SchemaModel(
        name="x.unresolved",
        label="Unresolved Custom Record",
        fields=(
            _field("name", "Name", required=True),
            _field("x_reference", "Reference", required=False),
        ),
    )


def install_stage2_schema(
    fixture,
    workspace_id: str,
    models: tuple[SchemaModel, ...],
    *,
    hash_character: str,
):
    """Replace the fixture schema and retire its earlier governance locally."""

    context = fixture.app.state.context
    current = context.queries.get_odoo_schema_catalog(workspace_id)
    if current is None:
        raise RuntimeError("The representative Stage 2 workspace has no schema.")
    schema = replace(
        current,
        captured_at=datetime.now(timezone.utc),
        models=models,
        content_hash="sha256:" + hash_character * 64,
    )
    context.schema_workspace.schemas.save_odoo_schema_catalog(
        workspace_id,
        schema,
        actor=context.actor,
    )
    return schema


def matching_rule_form(
    csrf_token: str,
    schema,
    selections: dict[
        str,
        tuple[tuple[str, ...], tuple[str, ...], str],
    ],
) -> dict[str, str]:
    """Build the exact server form for one complete Stage 2 review."""

    form = {
        "csrf_token": csrf_token,
        "expected_schema_hash": schema.content_hash,
        "expected_business_key_policy_version": str(
            BUSINESS_KEY_POLICY_VERSION
        ),
    }
    for index, model in enumerate(schema.models):
        key_fields, scope_fields, description = selections.get(
            model.name,
            ((), (), ""),
        )
        form.update(
            {
                f"primary_key_field_{index}": (
                    key_fields[0] if len(key_fields) == 1 else ""
                ),
                f"primary_scope_field_{index}": (
                    scope_fields[0] if len(scope_fields) == 1 else ""
                ),
                f"key_fields_{index}": ", ".join(key_fields),
                f"scope_fields_{index}": ", ".join(scope_fields),
                f"key_description_{index}": description,
            }
        )
    return form


def representative_stage2_selections() -> dict[
    str,
    tuple[tuple[str, ...], tuple[str, ...], str],
]:
    """Return the expected normal review for the representative schema."""

    return {
        "res.partner": (("ref",), (), "Contact reference"),
        "product.template": (
            ("default_code",),
            (),
            "Product internal reference",
        ),
        "x.asset": (("code",), (), "Unique asset code"),
    }


def _field(name: str, label: str, *, required: bool) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type="char",
        required=required,
        readonly=False,
        relation=None,
        relation_field=None,
        selection=(),
    )
