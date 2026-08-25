"""Derive stable accepted dataset names from portable Recipe identities."""

from __future__ import annotations

import re
import unicodedata
from hashlib import sha256


def logical_dataset_storage_name(logical_dataset_id: str) -> str:
    """Return one deterministic workspace dataset name for a logical input."""

    source = logical_dataset_id.removeprefix("dataset:").strip().casefold()
    candidate = re.sub(r"[^a-z0-9]+", "_", source).strip("_")
    if not candidate:
        candidate = "recipe_data"
    if not candidate[0].isalpha():
        candidate = f"data_{candidate}"
    if len(candidate) > 63:
        digest = sha256(logical_dataset_id.encode("utf-8")).hexdigest()[:10]
        candidate = f"{candidate[:52].rstrip('_')}_{digest}"
    return candidate


def normalize_recipe_source_name(value: str) -> str:
    """Normalize harmless source-label differences for unique matching."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", normalized).split())
