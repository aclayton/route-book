"""Test bootstrap for the route-book plugin.

Two problems this solves, and both would otherwise make the test suite
un-runnable outside this one machine:

1. **The directory is `route-book`, which is not a legal Python package name** —
   so it cannot simply be imported. It is loaded through importlib under a
   synthetic package name instead.
2. **`db.py` imports `plugins.plugin_storage`, which only exists inside a Hermes
   install.** A stub is injected into `sys.modules` so the suite runs on a bare
   checkout with no Hermes present. The stub provides the same contract — a data
   directory and a sqlite connection — pointed at a temp dir.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = "route_book_under_test"


def _install_storage_stub():
    """Provide `plugins.plugin_storage` pointing at a throwaway directory."""
    if "plugins.plugin_storage" in sys.modules:
        return

    tmp = Path(tempfile.mkdtemp(prefix="route-book-test-"))

    def plugin_data_dir(name):
        d = tmp / "plugin-data" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def plugin_db(name, filename="data.db"):
        conn = sqlite3.connect(plugin_data_dir(name) / filename, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    pkg = types.ModuleType("plugins")
    pkg.__path__ = []  # a namespace package, not a real directory
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = plugin_data_dir
    storage.plugin_db = plugin_db
    pkg.plugin_storage = storage
    sys.modules["plugins"] = pkg
    sys.modules["plugins.plugin_storage"] = storage


def load_plugin():
    """Import the plugin as a package and return the module namespace."""
    _install_storage_stub()

    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]

    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"could not build an import spec for {PLUGIN_DIR}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module
