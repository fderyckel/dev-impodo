"""Stable filesystem roots for tests at any package depth."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
