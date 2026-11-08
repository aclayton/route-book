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


def fresh_db():
    """Wipe the test database.

    Without this the suite is order-dependent: a test that adds a client leaks it
    into the next one, which then fails as *ambiguous* — an error that looks like
    a bug in the code and isn't. Tests that share state silently stop testing.
    """
    import shutil
    _install_storage_stub()
    storage = sys.modules["plugins.plugin_storage"]
    root = storage.plugin_data_dir("route-book")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)


def fail_on_resource_leaks():
    """Turn ResourceWarning into an error for the whole suite.

    An unclosed sqlite connection is invisible in a normal test run and becomes a
    file-descriptor leak in a gateway that runs for weeks. Promoting the warning
    means a leak fails the build the moment it is introduced, rather than being
    noticed in production.
    """
    import warnings
    warnings.simplefilter("error", ResourceWarning)


def forbid_network():
    """Make any outbound connection a test failure.

    The suite is supposed to run offline. It did not: two tests reached the live
    Nominatim and OSRM services, which fail for reasons unrelated to the code
    and are rude to public demo servers. A stub that is forgotten is silent;
    this is not.
    """
    import socket

    if getattr(socket, "_route_book_guarded", False):
        return

    real_socket = socket.socket

    class _Guarded(real_socket):
        def connect(self, address):
            import traceback
            # Name the caller. A guard that only says "something connected" makes
            # you hunt; one that says which test and which function does not.
            stack = traceback.extract_stack()
            ours = [f for f in stack
                    if "route-book" in f.filename and "_bootstrap" not in f.filename]
            where = " <- ".join(f"{f.name}" for f in ours[-3:]) or "unknown"
            raise AssertionError(
                f"network connection to {address!r} attempted from {where} — "
                "stub the outbound call instead"
            )

        def connect_ex(self, address):
            raise AssertionError(
                f"test attempted a network connection to {address!r}"
            )

    socket.socket = _Guarded
    socket._route_book_guarded = True


def stub_geocoder(plugin):
    """Replace both outbound geo helpers with deterministic stubs.

    Called by load_plugin so every test starts offline by default. Four separate
    setUps forgot to do this individually, and each omission reached the live
    Nominatim service — which is slow, rate-limited, and makes the suite fail for
    reasons unrelated to the code. Defaulting to the stub means forgetting is
    harmless instead of harmful.
    """
    plugin.geo.geocode = lambda address, city="", country="Canada": (43.09, -79.08, "stub")
    plugin.geo.area_bbox = lambda region: (-79.45, 42.85, -78.85, 43.15)


def stub_matrix(plugin, minutes=12):
    """Replace the network travel matrix with a deterministic one.

    The same failure as the geocoder, one layer down: `matrix.build_matrix`
    reaches the live OSRM demo server. It was stubbed in one test file only, so
    any test that configured a home base called out to a public service with rate
    limits. Stubbing by default means forgetting is harmless.
    """
    def fake_build(points, force=False):
        n = len(points)
        grid = [[0 if i == j else minutes for j in range(n)] for i in range(n)]
        return {"minutes": grid, "km": None, "approximate": False,
                "source": "stub", "points": list(points)}

    plugin.matrix.build_matrix = fake_build


def load_plugin():
    """Import the plugin as a package and return the module namespace."""
    _install_storage_stub()
    forbid_network()
    fail_on_resource_leaks()

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

    # Offline by default, now that the plugin exists to stub.
    stub_geocoder(module)
    stub_matrix(module)

    # `__init__.py` imports only what `register()` needs, so submodules that are
    # not on that path have to be pulled in explicitly for tests to reach them.
    for sub in ("db", "geo", "tools", "nl", "schemas", "engine", "matrix"):
        try:
            importlib.import_module(f"{PKG_NAME}.{sub}")
        except ImportError:
            pass

    return module
