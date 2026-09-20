from __future__ import annotations

import os
import sys
from pathlib import Path


def _looks_like_pytest_process() -> bool:
    argv0 = Path(str(sys.argv[0] or "")).name.lower()
    if "pytest" in argv0:
        return True
    return any("pytest" in str(arg).lower() for arg in sys.argv[1:3])


if _looks_like_pytest_process():
    # Prevent broken global pytest plugin autoload on this Windows machine.
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
