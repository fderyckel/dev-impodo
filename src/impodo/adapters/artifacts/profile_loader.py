"""Load portable Recipe profiles from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from impodo.domain.recipe.profile import ProfileDocument


class ProfileLoadError(ValueError):
    """Raised when a profile cannot be parsed or validated."""


def load_profile(path: str | Path) -> ProfileDocument:
    """Load YAML and return one fully validated immutable profile.

    File, YAML, and Pydantic errors are normalized into `ProfileLoadError`
    messages with actionable dotted field locations for CLI/browser display.
    """

    profile_path = Path(path)
    try:
        loaded = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileLoadError(f"cannot read profile {profile_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProfileLoadError(f"profile {profile_path} must contain a YAML object")

    try:
        return ProfileDocument.model_validate(loaded)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(item) for item in error["loc"])
            errors.append(f"{location or '<root>'}: {error['msg']}")
        raise ProfileLoadError(
            f"invalid profile {profile_path}:\n- " + "\n- ".join(errors)
        ) from exc

