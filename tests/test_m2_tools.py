"""M2 tool tests — booking, day and week.

The behaviours tested here are the ones where a plausible-looking implementation
is wrong in a way nobody notices until the operator is standing in the wrong
driveway:

- a booking must report WHICH day it resolved, because "next Thursday" is
  ambiguous in English
- an ambiguous client name must refuse rather than pick
- a clash must be caught, since a double-booking is the one error that is
  definitely wrong
- a day plan must not imply drive times it does not have yet
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
tools = plugin.tools
geo = plugin.geo
db = plugin.db


def _fresh_client(name, city="Fort Erie"):
    geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
    return json.loads(tools.sched_client_add({
        "name": name, "address": f"1 {name} St", "city": city,
        "duration": "3 hrs", "cadence": "weekly",
    }))["client"]


class Booking(unittest.TestCase):

    def setUp(self):
        fresh_db()
        geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        self.client = _fresh_client("Booker")

    def test_books_and_reports_the_resolved_day(self):
        """The resolved weekday must come back — the operator sees which Thursday."""
        out = json.loads(tools.sched_book({
            "client": "Booker", "when": "next Thursday", "duration": "2 hrs",
        }))
        self.assertEqual(out["status"], "booked")
        self.assertEqual(out["duration_min"], 120)
        self.assertIn("day", out)
        self.assertIn("readable", out)
        self.assertIn(out["day"], out["readable"])

    def test_unreadable_date_is_refused_not_guessed(self):
        out = json.loads(tools.sched_book({"client": "Booker", "when": "sometime soon"}))
        self.assertIn("error", out)
        self.assertNotIn("status", out)

    def test_unknown_client_is_refused(self):
        out = json.loads(tools.sched_book({"client": "Zebediah", "when": "tomorrow"}))
        self.assertIn("error", out)
        self.assertIn("no client matches", out["error"])

    def test_ambiguous_client_refuses_and_lists_candidates(self):
        """Two clients with the same name must never be silently resolved."""
        fresh_db()
        _fresh_client("Twinned", city="Niagara Falls")
        _fresh_client("Twinned", city="St. Catharines")
        out = json.loads(tools.sched_book({"client": "Twinned", "when": "tomorrow"}))
        self.assertIn("error", out)
        self.assertIn("candidates", out)
        self.assertGreaterEqual(len(out["candidates"]), 2)

    def test_clash_is_refused(self):
        """A double-booking is the one error that is definitely wrong."""
        fresh_db()
        _fresh_client("Booker")
        first = json.loads(tools.sched_book({
            "client": "Booker", "when": "2026-11-16", "duration": "3 hrs", "start": "09:00",
        }))
        self.assertEqual(first["status"], "booked")
        second = json.loads(tools.sched_book({
            "client": "Booker", "when": "2026-11-16", "duration": "3 hrs", "start": "10:00",
        }))
        self.assertIn("error", out := second)
        self.assertIn("clashes", out["error"])

    def test_non_working_day_is_refused(self):
        out = json.loads(tools.sched_book({"client": "Booker", "when": "next Sunday"}))
        self.assertIn("error", out)
        self.assertIn("not a working day", out["error"])


class DayPlan(unittest.TestCase):

    def setUp(self):
        fresh_db()
        geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")

    def test_empty_day_says_so_plainly(self):
        out = json.loads(tools.sched_day({"when": "2027-03-15"}))
        self.assertEqual(out["job_count"], 0)
        self.assertEqual(out["hands_on_minutes"], 0)

    def test_day_plan_reports_gaps_and_never_claims_drive_times(self):
        client = _fresh_client("Timeline")
        tools.sched_book({"client": "Timeline", "when": "2026-11-17",
                          "duration": "3 hrs", "start": "09:00"})
        tools.sched_book({"client": "Timeline", "when": "2026-11-17",
                          "duration": "2 hrs", "start": "14:00"})
        out = json.loads(tools.sched_day({"when": "2026-11-17"}))
        self.assertGreaterEqual(out["job_count"], 2)
        kinds = [t["kind"] for t in out["timeline"]]
        self.assertIn("job", kinds)
        self.assertIn("gap", kinds)
        # The honest-limits note must survive: a day told without travel must not
        # read as a validated day.
        self.assertIn("M3", out["note"])

    def test_break_is_placed_inside_the_working_window(self):
        _fresh_client("Timeline")
        tools.sched_book({"client": "Timeline", "when": "2026-11-18",
                          "duration": "3 hrs", "start": "09:00"})
        tools.sched_book({"client": "Timeline", "when": "2026-11-18",
                          "duration": "3 hrs", "start": "13:30"})
        out = json.loads(tools.sched_day({"when": "2026-11-18"}))
        breaks = [t for t in out["timeline"] if t["kind"] == "break"]
        for b in breaks:
            self.assertGreaterEqual(b["start"], plugin.config.BREAK_EARLIEST)


class WeekView(unittest.TestCase):

    def setUp(self):
        fresh_db()
        geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        _fresh_client("Booker")

    def test_week_totals_and_honest_gap(self):
        tools.sched_book({"client": "Booker", "when": "2026-11-16",
                          "duration": "3 hrs", "start": "09:00"})
        out = json.loads(tools.sched_week({"start": "2026-11-16"}))
        self.assertEqual(out["week_start"], "2026-11-16")   # a Monday
        self.assertEqual(out["week_end"], "2026-11-22")
        self.assertGreaterEqual(out["job_count"], 1)
        self.assertGreater(out["hands_on_hours"], 0)
        self.assertIn("M3", out["note"])


class EveryHandlerStillSafe(unittest.TestCase):
    """The contract: a string back, valid JSON, no exception — for every tool."""

    def setUp(self):
        fresh_db()

    def test_all_tools_survive_empty_input(self):
        for name in plugin.TOOL_NAMES:
            with self.subTest(tool=name):
                result = getattr(tools, name)({})
                self.assertIsInstance(result, str)
                json.loads(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
