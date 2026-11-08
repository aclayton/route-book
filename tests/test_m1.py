"""M1 tests — clients, areas and duration parsing.

These test behaviour that is easy to get subtly wrong and expensive when it is:
what happens when geocoding fails, whether an ambiguous name matches the wrong
client, and whether a duration typo silently becomes a wrong number.

The network is never touched. `geo.geocode` is monkeypatched, so the suite runs
offline and deterministically — which is also the only way to test the failure
paths, since you cannot ask a live service to fail on demand.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_plugin  # noqa: E402

plugin = load_plugin()
tools = plugin.tools
geo = plugin.geo


class DurationParsing(unittest.TestCase):
    """A wrong duration silently corrupts every time calculation downstream."""

    def test_reads_the_forms_people_actually_type(self):
        cases = {
            "3 hrs": 180, "3 hours": 180, "3h": 180, "3 H": 180,
            "90 min": 90, "90m": 90, "45 minutes": 45,
            "2.5 hrs": 150, "1 hour": 60,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tools._parse_duration(text), expected)

    def test_bare_number_is_minutes(self):
        self.assertEqual(tools._parse_duration("45"), 45)

    def test_unreadable_returns_none_not_a_default(self):
        # None is visible and fixable; a default silently becomes a wrong slot.
        for text in ("", None, "ages", "half a day", "soon"):
            with self.subTest(text=text):
                self.assertIsNone(tools._parse_duration(text))


class ClientAdd(unittest.TestCase):

    def setUp(self):
        geo.geocode = lambda address, city="", country="Canada": (43.09, -79.08, "stub")
        geo.area_bbox = lambda region: (-79.45, 42.85, -78.85, 43.15)

    def test_requires_a_name_and_an_address(self):
        out = json.loads(tools.sched_client_add({"name": "Bala"}))
        self.assertIn("error", out)
        out = json.loads(tools.sched_client_add({"address": "1 Main St"}))
        self.assertIn("error", out)

    def test_stores_and_geocodes(self):
        out = json.loads(tools.sched_client_add({
            "name": "Bala", "address": "1 Main St", "city": "Fort Erie",
            "cadence": "biweekly", "duration": "3 hrs",
        }))
        self.assertEqual(out["status"], "added")
        self.assertTrue(out["client"]["has_coordinates"])
        self.assertEqual(out["client"]["default_duration_min"], 180)
        self.assertEqual(out["client"]["cadence"], "biweekly")

    def test_geocode_failure_still_stores_the_client(self):
        """The important one: a network failure must not lose the operator's typing."""
        def boom(address, city="", country="Canada"):
            raise geo.GeoError("could not reach the geocoding service")

        geo.geocode = boom
        out = json.loads(tools.sched_client_add({
            "name": "No Network", "address": "9 Nowhere Rd",
        }))
        self.assertEqual(out["status"], "added")
        self.assertFalse(out["client"]["has_coordinates"])
        self.assertIn("warning", out)
        self.assertIn("without coordinates", out["warning"])


class ClientList(unittest.TestCase):

    def test_reports_which_clients_lack_coordinates(self):
        tools.sched_client_add({"name": "Zzz Ungeocoded", "address": "1 X St"})

        def boom(address, city="", country="Canada"):
            raise geo.GeoError("offline")

        geo.geocode = boom
        tools.sched_client_add({"name": "Yyy Ungeocoded", "address": "2 Y St"})
        geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")

        out = json.loads(tools.sched_client_list({"search": "Ungeocoded"}))
        self.assertGreaterEqual(out["count"], 2)
        self.assertIn("missing_coordinates", out)

    def test_ambiguous_name_returns_every_match_rather_than_one(self):
        """Two clients called Harris are indistinguishable by name.

        The tool must show both so the caller can disambiguate, never pick one.
        """
        for city in ("Fort Erie", "Niagara Falls"):
            tools.sched_client_add({"name": "Harris", "address": "1 Oak St", "city": city})
        out = json.loads(tools.sched_client_list({"search": "Harris"}))
        cities = sorted(c["city"] for c in out["clients"])
        self.assertIn("Fort Erie", cities)
        self.assertIn("Niagara Falls", cities)


class AreaSet(unittest.TestCase):

    def setUp(self):
        # BOTH outbound helpers, every time. An earlier version stubbed only
        # geocode, so this class reached the live Nominatim service for real —
        # and the suite silently depended on it.
        geo.geocode = lambda address, city="", country="Canada": (43.09, -79.08, "stub")
        geo.area_bbox = lambda region: (-79.45, 42.85, -78.85, 43.15)

    def test_needs_a_name(self):
        out = json.loads(tools.sched_area_set({"region": "Niagara region"}))
        self.assertIn("error", out)

    def test_needs_either_a_region_or_cities(self):
        out = json.loads(tools.sched_area_set({"name": "home patch"}))
        self.assertIn("error", out)

    def test_resolves_a_named_region_to_a_bbox(self):
        out = json.loads(tools.sched_area_set({"name": "home patch", "region": "Niagara region"}))
        self.assertEqual(out["status"], "set")
        self.assertEqual(out["kind"], "named_area")
        self.assertIn("bbox", out)
        self.assertLess(out["bbox"]["west"], out["bbox"]["east"])
        self.assertLess(out["bbox"]["south"], out["bbox"]["north"])

    def test_unknown_region_is_stored_with_a_warning_not_an_error(self):
        """Informal names ('the peninsula') often aren't in OpenStreetMap.

        Losing the operator's words would be worse than losing the boundary.
        """
        geo.area_bbox = lambda region: None
        out = json.loads(tools.sched_area_set({"name": "peninsula", "region": "the peninsula"}))
        self.assertEqual(out["status"], "set")
        self.assertNotIn("bbox", out)
        self.assertIn("warning", out)
        geo.area_bbox = lambda region: (-79.45, 42.85, -78.85, 43.15)

    def test_accepts_a_bare_city_list(self):
        out = json.loads(tools.sched_area_set({
            "name": "south", "cities": ["Niagara Falls", "Fort Erie"],
        }))
        self.assertEqual(out["kind"], "city_list")
        self.assertEqual(out["cities"], ["Niagara Falls", "Fort Erie"])


class StubsAreHonest(unittest.TestCase):
    """An unimplemented tool must say so rather than return something plausible."""

    def test_any_unimplemented_tool_names_a_valid_milestone(self):
        """Written to survive every future milestone.

        Earlier versions of this test listed which tools were still stubs, so
        each milestone broke it — three times. The list was the bug: it pinned a
        moving target. This asserts the *shape* of an unimplemented reply
        instead, which stays true as tools come online.
        """
        valid = {f"M{n}" for n in range(0, 10)} | {f"M{n}b" for n in range(0, 10)}
        seen_unimplemented = []

        for name in plugin.TOOL_NAMES:
            with self.subTest(tool=name):
                result = json.loads(getattr(tools, name)({}))
                if result.get("error") == "not_implemented":
                    seen_unimplemented.append(name)
                    self.assertIn(result.get("milestone"), valid,
                                  f"{name} names an implausible milestone")
                    self.assertIn("message", result)

        # Not an assertion about WHICH are stubs — just that the roster is sane.
        self.assertLess(len(seen_unimplemented), len(plugin.TOOL_NAMES),
                        "every tool is a stub; at least one milestone should be real")

    def test_completed_milestone_tools_are_not_stubs(self):
        """The converse, pinned once per milestone so a regression is loud.

        If a finished tool ever starts returning not_implemented again, that is a
        real regression and this catches it by name.
        """
        for fn in (tools.sched_client_add, tools.sched_area_set,
                   tools.sched_book, tools.sched_day, tools.sched_week):
            with self.subTest(tool=fn.__name__):
                out = json.loads(fn({}))
                self.assertNotEqual(out.get("error"), "not_implemented",
                                    f"{fn.__name__} regressed to a stub")

    def test_every_handler_returns_a_string_and_never_raises(self):
        for name in plugin.TOOL_NAMES:
            with self.subTest(tool=name):
                result = getattr(tools, name)({})
                self.assertIsInstance(result, str)
                json.loads(result)  # must be valid JSON


if __name__ == "__main__":
    unittest.main(verbosity=2)
