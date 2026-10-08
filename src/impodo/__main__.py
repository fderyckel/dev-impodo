"""Run the expert, profile-driven CLI through ``python -m impodo``.

The CLI supports source-only profiling and read-only target comparison. The
browser application has a separate launcher in :mod:`impodo.web.launcher`.
"""

from impodo.web.composition.cli import main

raise SystemExit(main())
