"""Expose the explicit local Impodo application composition entry point.

The service-graph extraction from :mod:`impodo.web.app` is intentionally
incremental. New local launchers import this module while the legacy entry
point remains available to existing integrations during Phase 1.
"""

from impodo.web.app import create_local_app

__all__ = ["create_local_app"]
