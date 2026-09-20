from __future__ import annotations

import os
from typing import Dict

from backend import paths


_GENE_EDITOR_ENV = "LIURUYAN_ENABLE_GENE_EDITOR"
_GENE_EDITOR_UNLOCK_DISABLED_ENV = "LIURUYAN_DISABLE_GENE_EDITOR_UNLOCK"


def is_dev_runtime() -> bool:
    return not paths._is_frozen() and os.environ.get("LIURUYAN_PROD") != "1"


def is_gene_editor_unlock_available() -> bool:
    return os.environ.get(_GENE_EDITOR_UNLOCK_DISABLED_ENV) != "1"


def is_gene_editor_enabled() -> bool:
    return is_gene_editor_unlock_available() and os.environ.get(_GENE_EDITOR_ENV) == "1"


def gene_editor_status() -> Dict[str, bool]:
    unlock_available = is_gene_editor_unlock_available()
    env_enabled = os.environ.get(_GENE_EDITOR_ENV) == "1"
    return {
        "dev_runtime": unlock_available,
        "enabled": unlock_available and env_enabled,
    }


def unlock_gene_editor_for_dev_session() -> Dict[str, bool]:
    if is_gene_editor_unlock_available():
        os.environ[_GENE_EDITOR_ENV] = "1"
    return gene_editor_status()
