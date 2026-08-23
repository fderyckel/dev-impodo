"""Validate portable Recipe meaning independently of lifecycle ownership."""

from __future__ import annotations

import json
import re

from ..models import assert_no_numeric_odoo_ids
from ..recipes import RecipeIntegrityError
from .serialization import content_hash


SEMANTIC_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "connection_target_hash",
        "credential_generation",
        "data_version_id",
        "database",
        "endpoint",
        "mapping_id",
        "password",
        "permission_hash",
        "principal_hash",
        "project_id",
        "recipe_id",
        "secret",
        "series_id",
        "source_artifact",
        "source_artifact_hash",
        "target_binding_id",
        "token",
    }
)
SEMANTIC_FIELDS = frozenset(
    {
        "contract_versions",
        "source_shape",
        "parameter_definitions",
        "source_preparation",
        "mapping",
        "odoo_target_contract",
        "target_governance",
        "quality",
        "reference_dependencies",
        "control_definitions",
    }
)
UUID_TEXT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def validate_recipe_envelope(envelope_bytes: bytes) -> dict[str, object]:
    """Return one exact portable envelope or reject forbidden runtime state."""

    try:
        envelope = json.loads(envelope_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecipeIntegrityError("Recipe payload is not valid JSON") from error
    if not isinstance(envelope, dict):
        raise RecipeIntegrityError("Recipe payload must be an object")
    if set(envelope) != {
        "recipe_contract_version",
        "semantic_hash",
        "payload_hash",
        "recipe",
        "compatibility_hints",
        "provenance",
    }:
        raise RecipeIntegrityError("Recipe envelope fields are invalid")
    if envelope.get("recipe_contract_version") != 2:
        raise RecipeIntegrityError("Recipe contract version is unsupported")
    recipe = envelope.get("recipe")
    if not isinstance(recipe, dict):
        raise RecipeIntegrityError("Recipe semantic payload is invalid")
    if set(recipe) != SEMANTIC_FIELDS:
        raise RecipeIntegrityError("Recipe semantic fields are invalid")
    if not isinstance(envelope.get("compatibility_hints"), dict):
        raise RecipeIntegrityError("Recipe compatibility hints are invalid")
    if not isinstance(envelope.get("provenance"), dict):
        raise RecipeIntegrityError("Recipe provenance is invalid")
    if content_hash(recipe) != envelope.get("semantic_hash"):
        raise RecipeIntegrityError("Recipe semantic hash is invalid")
    if content_hash(
        {key: value for key, value in envelope.items() if key != "payload_hash"}
    ) != envelope.get("payload_hash"):
        raise RecipeIntegrityError("Recipe payload hash is invalid")
    for key in _walk_keys(recipe):
        if key.casefold() in SEMANTIC_FORBIDDEN_KEYS:
            raise RecipeIntegrityError(
                f"Recipe semantic payload contains forbidden {key}"
            )
    for value in _walk_values(recipe):
        if isinstance(value, str) and UUID_TEXT.search(value):
            raise RecipeIntegrityError(
                "Recipe semantic payload contains a workspace identity"
            )
    try:
        assert_no_numeric_odoo_ids(recipe)
    except ValueError as error:
        raise RecipeIntegrityError(str(error)) from error
    contract_versions = recipe.get("contract_versions")
    if not isinstance(contract_versions, dict) or not contract_versions:
        raise RecipeIntegrityError("Recipe contract versions are missing")
    return envelope


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _walk_values(value: object):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_values(item)
    else:
        yield value
