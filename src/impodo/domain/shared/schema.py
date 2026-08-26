"""Portable identity of the current workspace storage contract."""

# The adapter implements this contract; workers use the same identity without
# importing the DuckDB implementation inward.
WORKSPACE_SCHEMA_GENERATION = "impodo-workspace-engine-2026-08-workspace-owned"
WORKSPACE_SCHEMA_BASELINE_VERSION = 1
WORKSPACE_SCHEMA_VERSION = 2
