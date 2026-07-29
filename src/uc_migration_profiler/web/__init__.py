"""Local-only browser application for Impodo."""

from .app import create_app, create_local_app

__all__ = ["create_app", "create_local_app"]
