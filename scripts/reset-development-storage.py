"""Review or execute the recoverable Impodo development-storage reset."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from impodo.web.composition.development_reset import (
    execute_development_reset,
    plan_development_reset,
)
from impodo.adapters.protected_evidence.project_security import development_mode_enabled


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enumerate exact Impodo development storage, then move a confirmed "
            "unchanged plan into a recoverable quarantine directory."
        )
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--confirm", default="")
    arguments = parser.parse_args()

    plan = plan_development_reset(arguments.root)
    payload = {
        "storage_root": str(plan.storage_root),
        "targets": [str(item) for item in plan.targets],
        "unknown_entries": [str(item) for item in plan.unknown_entries],
        "fingerprint": plan.fingerprint,
        "confirmation_token": plan.confirmation_token,
        "can_execute": plan.can_execute,
    }
    if not arguments.confirm:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    quarantine = execute_development_reset(
        plan,
        confirmation_token=arguments.confirm,
        development_mode=development_mode_enabled(os.environ),
    )
    payload["quarantine"] = str(quarantine)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
