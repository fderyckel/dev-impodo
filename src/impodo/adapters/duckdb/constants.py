"""Shared DuckDB schema and write-batch settings."""

# This build supports one exact current schema. A contract change starts a new
# generation; projects from another generation are rejected, never upgraded.
SCHEMA_GENERATION = "impodo-project-2026-08-current-s9"
SCHEMA_VERSION = 1
PREFLIGHT_ROW_BATCH_SIZE = 1_000
STAGING_ROW_BATCH_SIZE = 1_000
QUALITY_ROW_BATCH_SIZE = 1_000
TRANSFORMATION_IMPACT_ROW_BATCH_SIZE = 1_000
NORMALIZATION_ROW_BATCH_SIZE = 1_000
DUCKDB_JSON_BATCH_MAX_BYTES = 16 * 1024 * 1024
DUCKDB_CANONICAL_JSON_BATCH_MAX_BYTES = 2 * 1024 * 1024
RESOLUTION_ROW_BATCH_SIZE = 1_000
PREPARATION_SESSION_ROW_BATCH_SIZE = 5_000
# Phase-7 production-worker evidence showed that 96 MB and 128 MB cannot hold
# the bounded 96,000-effect Product/BOM normalization transaction. 192 MB keeps
# the one-thread session below the 900 MiB worker gate without repeated OOMs.
PREPARATION_SESSION_MEMORY_LIMIT = "192MB"
