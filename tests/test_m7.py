"""M7 tests — setup reporting and data export.

The setup wizard has one behaviour worth testing: it names the SINGLE next
thing, not a checklist. A wizard that presents everything at once is a form, and
a form gets abandoned.

The export has one property worth testing: nothing that was never stored appears
in it. Door codes and alarm codes are not in this database by design, and the
test pins that as a property rather than a promise.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
setup = plugin.setup
export = plugin.export
tools = plugin.tools
db = plugin.db
config = plugin.config


class SetupNamesOneThing(unittest.TestCase):

    def setUp(self):
        fresh_db()
        tools.bind_context(None)
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        self._original_home = config.HOME_BASE
        config.HOME_BASE = ""

    def tearDown(self):
        config.HOME_BASE = self._original_home

    def test_a_fresh_install_wants_a_home_base_first(self):
        out = json.loads(tools.sched_setup({}))
        self.assertFalse(out["ready"])
        self.assertEqual(out["next_step"], "home_base")
        self.assertEqual(out["next"]["key"], "home_base")

    def test_the_next_step_explains_why_it_matters(self):
        """'No home base set' is a chore. The reason is why someone bothers."""
        out = json.loads(tools.sched_setup({}))
        why = out["next"]["why"]
        self.assertTrue(why and len(why) > 40, f"no useful reason: {why!r}")
        self.assertIn("travel", why.lower())

    def test_it_returns_one_next_step_not_a_list(self):
        out = json.loads(tools.sched_setup({}))
        self.assertIsInstance(out["next"], dict)
        self.assertNotIsInstance(out["next"], list)

    def test_it_moves_on_as_steps_are_done(self):
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        out = json.loads(tools.sched_setup({}))
        self.assertNotEqual(out["next_step"], "home_base")

    def test_one_client_is_partial_not_done(self):
        """One located client cannot make a matrix.

        Reporting 'done' here would leave someone with a routing engine that
        quietly reports nothing, and no idea why.
        """
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        tools.sched_client_add({"name": "Only One", "address": "1 A St",
                                "city": "Fort Erie"})
        out = json.loads(tools.sched_setup({}))
        client_step = next(s for s in out["steps"] if s["key"] == "first_client")
        self.assertEqual(client_step["state"], "partial")
        self.assertIn("one more", client_step["detail"])

    def test_two_located_clients_completes_that_step(self):
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        for name in ("First", "Second"):
            tools.sched_client_add({"name": name, "address": f"1 {name} St",
                                    "city": "Fort Erie"})
        out = json.loads(tools.sched_setup({}))
        client_step = next(s for s in out["steps"] if s["key"] == "first_client")
        self.assertEqual(client_step["state"], "done")

    def test_a_configured_install_reports_ready(self):
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        for name in ("First", "Second"):
            tools.sched_client_add({"name": name, "address": f"1 {name} St",
                                    "city": "Fort Erie"})
        with db.open_db() as conn:
            db.set_area(conn, name="Niagara region", kind="region",
                        payload="niagara", bbox=(-79.45, 42.85, -78.85, 43.15))
        out = json.loads(tools.sched_setup({}))
        self.assertTrue(out["ready"], f"not ready: {out['blocking']}")
        self.assertIn("Everything is set up", out["summary"])

    def test_the_summary_is_a_sentence(self):
        out = json.loads(tools.sched_setup({}))
        self.assertTrue(out["summary"][0].isupper())
        self.assertTrue(out["summary"].endswith("."))

    def test_calendar_is_optional_not_blocking(self):
        """It stays local by default and that is a complete configuration."""
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        for name in ("First", "Second"):
            tools.sched_client_add({"name": name, "address": f"1 {name} St",
                                    "city": "Fort Erie"})
        with db.open_db() as conn:
            db.set_area(conn, name="Niagara", kind="region", payload="n",
                        bbox=(-79.45, 42.85, -78.85, 43.15))
        out = json.loads(tools.sched_setup({}))
        cal = next(s for s in out["steps"] if s["key"] == "calendar")
        self.assertEqual(cal["state"], "optional")
        self.assertNotIn("calendar", out["blocking"])


class ExportIsComplete(unittest.TestCase):

    def setUp(self):
        fresh_db()
        tools.bind_context(None)
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})
        tools.sched_client_add({"name": "Niagara Septic", "address": "9 King St",
                                "city": "Port Colborne"})
        tools.sched_book({"client": "Bala Construction", "when": "2026-11-12",
                          "duration": "3 hrs", "start": "09:00"})

    def test_both_tables_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            self.assertEqual(out["status"], "written")
            self.assertEqual(out["counts"]["clients"], 2)
            self.assertEqual(out["counts"]["appointments"], 1)
            for key in ("clients_csv", "appointments_csv", "json"):
                self.assertTrue(Path(out["files"][key]).exists(), key)

    def test_the_csv_is_readable_by_a_spreadsheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            text = Path(out["files"]["clients_csv"]).read_text(encoding="utf-8")
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(len(rows), 2)
            self.assertIn("Bala Construction", [r["name"] for r in rows])

    def test_the_appointment_csv_carries_the_client_name(self):
        """An export full of foreign keys is a database dump, not a record."""
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            text = Path(out["files"]["appointments_csv"]).read_text(encoding="utf-8")
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(rows[0]["client_name"], "Bala Construction")
            self.assertEqual(rows[0]["client_city"], "Fort Erie")
            self.assertNotEqual(rows[0]["client_name"], "1")   # not a bare id

    def test_an_empty_export_still_has_headers(self):
        """A file with no header looks like a failure; with one it looks empty."""
        fresh_db()
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            self.assertEqual(out["counts"]["clients"], 0)
            text = Path(out["files"]["clients_csv"]).read_text(encoding="utf-8")
            self.assertIn("name", text.splitlines()[0])

    def test_the_json_bundle_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            data = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(len(data["clients"]), 2)
            self.assertEqual(len(data["appointments"]), 1)

    def test_no_entry_instructions_or_codes_are_exported(self):
        """Not a promise — a property. The plugin does not store them, so the
        export cannot leak them. This test fails if that ever stops being true."""
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            data = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
            # Only the DATA. The bundle's own note says "no door codes, alarm
            # codes or entry instructions" — scanning the whole file for the
            # word "alarm" matches the disclaimer, which proves nothing.
            blob = json.dumps({"clients": data["clients"],
                               "appointments": data["appointments"]}).lower()
            for forbidden in ("door", "alarm", "lockbox", "gate", "entry",
                              "keypad", "access_code", "code"):
                self.assertNotIn(forbidden, blob,
                                 f"{forbidden!r} appeared in exported data")

    def test_the_export_says_what_it_does_not_contain(self):
        """So the absence is legible to someone auditing the file later."""
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            data = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
            self.assertIn("door codes", data["note"])

    def test_cancelled_appointments_are_included(self):
        """A record of what was cancelled is part of the history."""
        tools.sched_cancel({"client": "Bala Construction", "when": "2026-11-12"})
        with tempfile.TemporaryDirectory() as tmp:
            out = json.loads(tools.sched_export({"directory": tmp}))
            self.assertEqual(out["counts"]["appointments"], 1)
            data = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(data["appointments"][0]["status"], "cancelled")


class BothToolsAreRegistered(unittest.TestCase):

    def test_the_tool_surface_is_complete(self):
        self.assertIn("sched_setup", plugin.TOOL_NAMES)
        self.assertIn("sched_export", plugin.TOOL_NAMES)

    def test_every_registered_tool_has_a_schema_and_a_handler(self):
        """The validator catches a missing schema at register time; this catches
        it in the test suite, where the message is better."""
        for name in plugin.TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertTrue(hasattr(plugin.schemas, name.upper()),
                                f"{name} has no schema constant")
                self.assertTrue(callable(getattr(plugin.tools, name, None)),
                                f"{name} has no handler")

    def test_no_tool_still_returns_not_implemented(self):
        """M7 is the last milestone, so nothing may still be a stub.

        Checked by CALLING every tool rather than grepping the source: the
        definition of the stub helper legitimately remains, and a source scan
        cannot tell it apart from a use.
        """
        calls = {
            "sched_client_list": {}, "sched_day": {"when": "today"},
            "sched_week": {}, "sched_matrix_refresh": {}, "sched_setup": {},
            "sched_export": {}, "sched_ask": {"text": "what does my week look like"},
        }
        for name, args in calls.items():
            with self.subTest(tool=name):
                out = json.loads(getattr(tools, name)(args))
                self.assertNotEqual(out.get("error"), "not_implemented",
                                    f"{name} is still a stub")


if __name__ == "__main__":
    unittest.main(verbosity=2)
