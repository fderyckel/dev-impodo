"""Set bounded defaults before the process imports the Polars runtime."""

from __future__ import annotations

import os


POLARS_DEFAULT_MAX_THREADS = 1


def configure_columnar_runtime() -> None:
    """Bound native concurrency while preserving an explicit operator override."""

    os.environ.setdefault(
        "POLARS_MAX_THREADS",
        str(POLARS_DEFAULT_MAX_THREADS),
    )
