"""Run the expert, profile-driven CLI through ``python -m impodo``.

Migration stage: H — read-only target preflight (with source-only profiling
commands used before target comparison). The browser application has a
separate launcher in :mod:`impodo.web.launcher`.
"""

from impodo.web.composition.cli import main

raise SystemExit(main())
