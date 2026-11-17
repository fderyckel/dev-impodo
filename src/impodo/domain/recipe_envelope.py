"""Validate portable Recipe meaning independently of lifecycle ownership."""

from __future__ import annotations

import json
import re

from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    reference_preparation_from_dict,
)
from impodo.domain.mapping.reference_preparation import validate_reference_preparation
from impodo.domain.shared.models import assert_no_numeric_odoo_ids

from .recipe.models import RecipeIntegrityError
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
CURRENT_RECIPE_CONTRACT_VERSIONS = {
    "control_definitions": 1,
    "mapping_recipe": 3,
    "odoo_target_contract": 2,
    "quality_recipe": 1,
    "recipe_definition": 2,
    "recipe_parameter_definitions": 1,
    "reference_dependencies": 1,
    "source_preparation_recipe": 1,
    "source_shape_recipe": 1,
    "target_governance_recipe": 1,
}
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
    supported_versions = tuple(
        {**CURRENT_RECIPE_CONTRACT_VERSIONS,
         "mapping_recipe": mapping_version,
         "source_preparation_recipe": source_version}
        for mapping_version in (3, 4, 5, 6) for source_version in (1, 2)
    )
    if contract_versions not in supported_versions:
        raise RecipeIntegrityError(
            "Recipe contract versions do not match the current contract"
        )
    if "preparation" in set(_walk_keys(recipe.get("mapping"))):
        if contract_versions["mapping_recipe"] < 4:
            raise RecipeIntegrityError("Prepared references require mapping Recipe contract v4")
        _validate_reference_preparations(recipe["mapping"])
    if "blank_source_column_id" in set(_walk_keys(recipe.get("mapping"))) and contract_versions["mapping_recipe"] < 5:
        raise RecipeIntegrityError("Optional linked matching scopes require mapping Recipe contract v5")
    if "must_be_unique" in set(_walk_keys(recipe.get("mapping"))) and contract_versions["mapping_recipe"] < 6:
        raise RecipeIntegrityError("Must be unique requires mapping Recipe contract v6. Save the field matches and prepare a new Recipe.")
    preparation = recipe.get("source_preparation")
    if isinstance(preparation, dict) and any(isinstance(rule, dict) and rule.get("existing_parent") is not None for rule in preparation.get("rules", ())) and contract_versions["source_preparation_recipe"] != 2:
        raise RecipeIntegrityError("Existing hierarchy parents require source-preparation Recipe contract v2")
    return envelope


def _validate_reference_preparations(value: object) -> None:
    """Reject malformed portable reference rules before applying a Recipe."""

    if isinstance(value, list):
        for item in value:
            _validate_reference_preparations(item)
    elif isinstance(value, dict):
        if "preparation" in value:
            preparation = value["preparation"]
            try:
                if not isinstance(preparation, dict) or set(preparation) != {
                    "formula", "bindings", "parts",
                }:
                    raise ValueError("Prepared reference fields are invalid")
                if not isinstance(preparation["formula"], str) or not isinstance(
                    preparation["bindings"], list,
                ) or not isinstance(preparation["parts"], list):
                    raise TypeError("Prepared reference values are invalid")
                bindings = preparation["bindings"]
                parts = preparation["parts"]
                if any(not isinstance(item, dict) or set(item) != {
                    "name", "source_column_id",
                } or any(not isinstance(part, str) for part in item.values())
                       for item in bindings):
                    raise ValueError("Prepared reference bindings are invalid")
                if any(not isinstance(item, dict) or set(item) not in (
                    {"source_column_id"}, {"literal"},
                ) or any(not isinstance(part, str) for part in item.values())
                       for item in parts):
                    raise ValueError("Prepared reference parts are invalid")
                rule = reference_preparation_from_dict({
                    "formula": preparation["formula"],
                    "bindings": [(item["name"], item["source_column_id"])
                                 for item in bindings],
                    "parts": [{"source_column_key": item.get("source_column_id"),
                               "literal": item.get("literal")}
                              for item in parts],
                })
                validate_reference_preparation(ReferenceKeyMapping(
                    value["source_column_id"], value["target_field"], rule,
                ))
            except (ValueError, TypeError, KeyError) as error:
                raise RecipeIntegrityError("Prepared reference rule is invalid") from error
        for item in value.values():
            _validate_reference_preparations(item)


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
