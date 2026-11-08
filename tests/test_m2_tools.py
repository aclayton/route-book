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


_GEO_SEQ = [43.0 + i * 0.05 for i in range(50)]


def _stub_matrix(monkey, minutes=12):
    """Replace the network matrix with a fixed, deterministic one.

    The suite must not call OSRM: it is a public demo instance with rate limits,
    and a test that depends on it fails for reasons unrelated to the code.
    """
    def fake_build(points, force=False):
        n = len(points)
        grid = [[0 if i == j else minutes for j in range(n)] for i in range(n)]
        return {"minutes": grid, "km": None, "approximate": False,
                "source": "stub", "points": list(points)}
    monkey.build_matrix = fake_build


def _fresh_client(name, city="Fort Erie", lat=None, lon=-79.0):
    """Create a client with a distinct location so matrices can be built.

    Distinct coordinates matter: identical ones make a degenerate matrix and
    hide indexing bugs.
    """
    if lat is None:
        lat = _GEO_SEQ.pop(0) if _GEO_SEQ else 43.0
    geo.geocode = lambda address, city="", country="Canada", _lat=lat, _lon=lon: (
        _lat, _lon, "stub")
    return json.loads(tools.sched_client_add({
        "name": name, "address": f"1 {name} St", "city": city,
        "duration": "3 hrs", "cadence": "weekly",
    }))["client"]


HOME = "12 Base Rd, Fort Erie, ON"


class Booking(unittest.TestCase):

    def setUp(self):
        fresh_db()
        plugin.config.HOME_BASE = HOME
        _stub_matrix(plugin.matrix)
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
        plugin.config.HOME_BASE = HOME
        _stub_matrix(plugin.matrix)
        geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")

    def test_empty_day_says_so_plainly(self):
        out = json.loads(tools.sched_day({"when": "2027-03-15"}))
        self.assertEqual(out["job_count"], 0)
        self.assertEqual(out["hands_on_minutes"], 0)

    def test_day_plan_reports_jobs_and_gaps(self):
        # Two located clients are needed for a travel matrix. With one, the tool
        # correctly declines to show gaps rather than implying a validated day.
        _fresh_client("Timeline")
        _fresh_client("Second")
        tools.sched_book({"client": "Timeline", "when": "2026-11-17",
                          "duration": "3 hrs", "start": "09:00"})
        tools.sched_book({"client": "Timeline", "when": "2026-11-17",
                          "duration": "2 hrs", "start": "14:00"})
        out = json.loads(tools.sched_day({"when": "2026-11-17"}))
        self.assertGreaterEqual(out["job_count"], 2)
        self.assertIn("jobs", out)
        self.assertIn("gaps", out)
        # The idle between 12:10 and 14:00 is real clock time and worth showing.
        self.assertTrue(out["gaps"], "expected a gap between the two jobs")

    def test_day_plan_says_when_travel_is_unavailable(self):
        """A day told without drive times must not read as a validated day.

        With no located clients there is no matrix, and the reply has to admit
        that rather than implying the day was checked.
        """
        _fresh_client("Timeline")
        tools.sched_book({"client": "Timeline", "when": "2026-11-17",
                          "duration": "3 hrs", "start": "09:00"})
        out = json.loads(tools.sched_day({"when": "2026-11-17"}))
        # Either it has a matrix and reports feasibility, or it admits it cannot.
        self.assertTrue(
            "feasible" in out or "warning" in out,
            f"day plan neither validated nor disclaimed: {sorted(out)}",
        )


class WeekView(unittest.TestCase):

    def setUp(self):
        fresh_db()
        plugin.config.HOME_BASE = HOME
        _stub_matrix(plugin.matrix)
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
        # M3 gave the week real drive time; where there is no matrix it says so
        # instead of reporting a confident zero.
        self.assertTrue(
            "drive_minutes" in out or "travel_note" in out,
            f"week neither measured driving nor disclaimed it: {sorted(out)}",
        )


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
