#!/usr/bin/env python3
"""
scripts/check_dependency_direction.py

Cheap CI check for the one architectural rule this whole decoupling
depends on: `storage/`, `core/` (outside the composition root), and
`app/routers/*` must never import from `integrations.*`. Only
`app/main.py` and `integrations/__init__.py` itself are allowed to.

Run: python3 scripts/check_dependency_direction.py
Exits non-zero with offending files listed if the rule is violated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_IMPORT = re.compile(r"^\s*(from|import)\s+integrations(\.|(\s|$))")

ALLOWED_FILES = {
    ROOT / "app" / "main.py",
    ROOT / "core" / "migrations.py",  # composition root: enumerates integrations to init schema
    ROOT / "integrations" / "__init__.py",
}

SCAN_DIRS = [ROOT / "storage", ROOT / "core", ROOT / "app"]


def main() -> int:
    violations: list[str] = []

    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*.py"):
            if path in ALLOWED_FILES:
                continue
            # integrations/ itself is exempt from this scan (it's allowed
            # to import from itself and from storage/core, that's fine —
            # the rule is about the reverse direction).
            if "integrations" in path.parts:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if FORBIDDEN_IMPORT.match(line):
                    violations.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    if violations:
        print("Dependency-direction violations found (core/storage importing integrations):")
        for v in violations:
            print(f"  {v}")
        print(
            "\nCore code must never import from integrations.*. Use "
            "core.automation_engine.fire()/register() or storage.modules.is_enabled() "
            "instead of importing integration internals directly."
        )
        return 1

    print("OK: no core/storage -> integrations imports found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
