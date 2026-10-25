"""Expose the supported Impodo web application entry points.

Deployment-specific construction lives under :mod:`impodo.web.composition`.
This module keeps the historical local import stable for launchers and tests.
"""

from impodo.web.composition.local_app import (
    DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS,
    create_local_app,
)

__all__ = ["DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS", "create_local_app"]
