"""Adapter package. Built-ins are imported lazily so the registry stays cheap."""

from __future__ import annotations

import importlib
import logging

from .base import (
    ArchiveContext,
    BaseAdapter,
    available_adapters,
    detect_adapter,
    get_adapter,
    register,
)

log = logging.getLogger("socialmcp.adapters")

# Add new built-ins here. Contributors: see CONTRIBUTING.md — one module, one
# subclass, one line in this tuple.
BUILTIN_MODULES = ("socialmcp.adapters.linkedin",)

_loaded = False


def load_builtin_adapters() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module in BUILTIN_MODULES:
        try:
            importlib.import_module(module)
        except ImportError as exc:  # pragma: no cover - a broken contrib adapter
            log.error("could not load adapter module %s: %s", module, exc)


__all__ = [
    "ArchiveContext",
    "BaseAdapter",
    "available_adapters",
    "detect_adapter",
    "get_adapter",
    "load_builtin_adapters",
    "register",
]
