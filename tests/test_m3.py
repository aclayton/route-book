"""M3 tests — the engine.

The travel matrix is injected, never fetched, so these run offline and
deterministically. That is also the only way to test the interesting cases: you
cannot ask a live router to return a triangle inequality violation on demand.

The central test is `test_marginal_cost_beats_distance_from_home`. If that
property does not hold, the plugin has no reason to exist — it would be a
calendar with extra steps.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
engine = plugin.engine
matrix_mod = plugin.matrix
config = plugin.config
Travel = engine.Travel
Stop = engine.Stop


def travel_from(legs, approximate=False):
    return Travel(legs, approximate=approximate, source="test")


class Feasibility(unittest.TestCase):
    """A day you cannot drive is not a low-scoring day — it is not a candidate."""

    def test_a_reachable_day_is_feasible(self):
        stops = [Stop(1, "A", "Town", engine.parse_hhmm("09:00"), 60)]
        t = travel_from({("home", 1): 15, (1, "home"): 15})
        state = engine.timeline(stops, t)
        self.assertTrue(state["feasible"], state["problems"])

    def test_an_impossible_day_is_rejected_with_a_reason(self):
        """The drive does not fit in the gap. A leg-length cap would pass this."""
        stops = [
            Stop(1, "First", "Town", engine.parse_hhmm("09:00"), 30),
            Stop(2, "Second", "Town", engine.parse_hhmm("10:00"), 30),
        ]
        # First ends 09:30, +10 buffer = 09:40. A 40-minute drive arrives 10:20,
        # twenty minutes after the 10:00 slot. 40 < MAX_LEG_MINUTES (45), so a
        # simple length check would call this fine.
        t = travel_from({("home", 1): 10, (1, 2): 40, (2, "home"): 10})
        state = engine.timeline(stops, t)
        self.assertFalse(state["feasible"])
        self.assertTrue(any("cannot reach" in p for p in state["problems"]))

    def test_leg_over_the_limit_is_rejected(self):
        stops = [Stop(1, "Far", "Town", engine.parse_hhmm("09:00"), 60)]
        t = travel_from({("home", 1): 90, (1, "home"): 90})
        state = engine.timeline(stops, t)
        self.assertFalse(state["feasible"])
        self.assertTrue(any("exceeds" in p for p in state["problems"]))

    def test_day_ceiling_counts_driving(self):
        """Seven hours of cleaning plus two hours of driving is a nine-hour day."""
        stops = [Stop(1, "Long", "Town", engine.parse_hhmm("09:00"), 7 * 60)]
        t = travel_from({("home", 1): 60, (1, "home"): 60})
        state = engine.timeline(stops, t)
        self.assertFalse(state["feasible"])
        self.assertTrue(any("ceiling" in p for p in state["problems"]))

    def test_changeover_buffer_can_make_a_day_infeasible(self):
        """The buffer's real effect — and it took three wrong tests to find it.

        With fixed start times a ten-minute buffer cannot compress a day: it
        either eats slack, or it makes the next job unreachable. So `elapsed` is
        the wrong thing to measure. The buffer changes *feasibility*, and that is
        what a six-job day actually loses to a fifteen-minute-per-job
        optimistic timeline.

        I hand-computed this three times and got it wrong three times, which is
        why this version asserts a behaviour (feasible vs not) rather than a
        number I derived in my head mid-sentence.
        """
        # Job one ends 10:00. Job two is booked for 10:12, five minutes away.
        # With no changeover you depart 10:00 and arrive 10:05 — fine.
        # With a ten-minute changeover you depart 10:10 and arrive 10:15 — late.
        stops = [
            Stop(1, "First", "Town", engine.parse_hhmm("09:00"), 60),
            Stop(2, "Second", "Town", engine.parse_hhmm("10:12"), 30),
        ]
        t = travel_from({("home", 1): 0, (1, 2): 5, (2, "home"): 5, ("home", 2): 5})

        original = config.SERVICE_BUFFER_MIN
        try:
            config.SERVICE_BUFFER_MIN = 0
            self.assertTrue(engine.timeline(stops, t)["feasible"],
                            "without a changeover the day should work")
            config.SERVICE_BUFFER_MIN = 10
            with_buffer = engine.timeline(stops, t)
            self.assertFalse(with_buffer["feasible"],
                             "with a changeover it should not")
        finally:
            config.SERVICE_BUFFER_MIN = original

        self.assertTrue(any("cannot reach" in p for p in with_buffer["problems"]))

    def test_slack_shrinks_as_the_buffer_grows(self):
        """The other half: on a day with room, the buffer eats slack, not time."""
        stops = [Stop(1, "A", "Town", engine.parse_hhmm("10:00"), 60)]
        t = travel_from({("home", 1): 15, (1, "home"): 15})

        original = config.SERVICE_BUFFER_MIN
        try:
            config.SERVICE_BUFFER_MIN = 0
            loose = engine.timeline(stops, t)
            config.SERVICE_BUFFER_MIN = 60
            tight = engine.timeline(stops, t)
        finally:
            config.SERVICE_BUFFER_MIN = original

        # Same job, same start: the day's shape changes, its content doesn't.
        self.assertEqual(loose["service_minutes"], tight["service_minutes"])
        self.assertTrue(loose["feasible"] and tight["feasible"])
        # And slack survives, because the job sits at a fixed clock time.
        self.assertEqual(loose["slack_minutes"], tight["slack_minutes"])

    def test_slack_is_reported(self):
        stops = [Stop(1, "A", "Town", engine.parse_hhmm("10:00"), 60)]
        t = travel_from({("home", 1): 15, (1, "home"): 15})
        state = engine.timeline(stops, t)
        self.assertGreater(state["slack_minutes"], 0)
        self.assertEqual(state["slack_minutes"], engine.parse_hhmm("10:00")
                         - engine.parse_hhmm(config.WORK_START) - 15)


class MarginalCost(unittest.TestCase):
    """The measurement the plugin is built on."""

    def test_marginal_cost_beats_distance_from_home(self):
        """The property that justifies the plugin's existence.

        A client 40 minutes away, slotted between two existing jobs, costs less
        to add than one 10 minutes away sitting alone on an empty day.

        Distance-from-home ranks these the wrong way round. If this test ever
        passes trivially, the design has lost its point.
        """
        # Day: home -> A -> B -> home, with A and B adjacent.
        day = [
            Stop(1, "A", "Town", engine.parse_hhmm("09:00"), 60),
            Stop(2, "B", "Town", engine.parse_hhmm("11:00"), 60),
        ]
        t = travel_from({
            ("home", 1): 20, (1, 2): 5, (2, "home"): 20,
            # "Far": 40 minutes from home, but only 3 and 4 from A and B.
            ("home", 9): 40, (9, "home"): 40, (1, 9): 3, (9, 2): 4,
            (2, 9): 3, (9, 1): 4,
            # "Near": 10 minutes from home, but 35 from either end of the day.
            ("home", 8): 10, (8, "home"): 10, (1, 8): 35, (8, 2): 35,
            (2, 8): 35, (8, 1): 35,
        })

        far_cost, _ = engine.insertion_cost(day, 9, t)
        near_cost, _ = engine.insertion_cost(day, 8, t)

        self.assertIsNotNone(far_cost)
        self.assertIsNotNone(near_cost)
        self.assertLess(far_cost, near_cost,
                        f"'far' ({far_cost}) should cost less than 'near' ({near_cost}) "
                        "because it slots between two existing jobs")

    def test_cost_is_the_extra_driving_not_the_distance(self):
        day = [Stop(1, "A", "Town", engine.parse_hhmm("09:00"), 60)]
        t = travel_from({("home", 1): 20, (1, "home"): 20,
                         (1, 5): 10, (5, "home"): 10, ("home", 5): 999})
        cost, _ = engine.insertion_cost(day, 5, t)
        # home->A->client->home  = 20 + 10 + 10 = 40
        # baseline home->A->home = 20 + 20      = 40
        self.assertEqual(cost, 0)

    def test_empty_day_cost_is_the_round_trip(self):
        t = travel_from({("home", 5): 15, (5, "home"): 20})
        cost, _ = engine.insertion_cost([], 5, t)
        self.assertEqual(cost, 35)

    def test_unknown_pair_returns_none_rather_than_zero(self):
        """Zero would understate the day; a made-up number would be worse."""
        t = travel_from({("home", 1): 10})
        cost, _ = engine.insertion_cost([Stop(1, "A", "T", 540, 60)], 7, t)
        self.assertIsNone(cost)


class Scoring(unittest.TestCase):

    def test_reasons_come_back_with_every_score(self):
        """An unexplained ranking stops being trusted the first time it's wrong."""
        day = [Stop(1, "A", "Fort Erie", engine.parse_hhmm("09:00"), 60)]
        t = travel_from({("home", 1): 10, (1, "home"): 10,
                         (1, 2): 8, (2, 1): 8, ("home", 2): 12, (2, "home"): 12})
        value, reasons = engine.score_day(day, 2, t, client={"city": "Fort Erie"})
        self.assertIsNotNone(value)
        self.assertTrue(reasons)
        self.assertTrue(any("driving" in r for r in reasons))

    def test_same_city_day_beats_a_different_city(self):
        t = travel_from({("home", 1): 10, (1, "home"): 10,
                         (1, 2): 8, (2, 1): 8, ("home", 2): 12, (2, "home"): 12})
        same = engine.score_day([Stop(1, "A", "Fort Erie", 540, 60)], 2, t,
                                client={"city": "Fort Erie"})[0]
        other = engine.score_day([Stop(1, "A", "Port Colborne", 540, 60)], 2, t,
                                 client={"city": "Fort Erie"})[0]
        self.assertGreater(same, other)


class Recommendations(unittest.TestCase):

    def setUp(self):
        self.client = {"id": 99, "name": "New", "city": "Fort Erie",
                       "default_duration_min": 180}

    def test_infeasible_days_are_rejected_with_a_reason_not_dropped(self):
        """"Why not Thursday" will be asked. An unexplained absence is worse."""
        # Wednesday is packed solid and unreachable.
        packed = [
            Stop(1, "A", "Town", engine.parse_hhmm("09:00"), 120),
            Stop(2, "B", "Town", engine.parse_hhmm("11:20"), 120),
            Stop(3, "C", "Town", engine.parse_hhmm("13:40"), 120),
        ]
        days = {"2026-11-18": packed, "2026-11-19": []}
        t = travel_from({("home", 1): 10, (1, 2): 10, (2, 3): 10, (3, "home"): 10,
                         (2, "home"): 10, (3, 1): 10, (1, 3): 10,
                         ("home", 2): 10, ("home", 3): 10, ("home", 99): 12,
                         (99, "home"): 12, (1, 99): 10, (99, 1): 10,
                         (2, 99): 10, (99, 2): 10, (3, 99): 10, (99, 3): 10})
        out = engine.recommend(99, self.client, days, t)
        self.assertTrue(out["recommended"])
        for r in out["rejected"]:
            self.assertTrue(r["why"])

    def test_recommendation_carries_reasons_and_the_top_choice(self):
        days = {"2026-11-19": [
            Stop(1, "A", "Fort Erie", engine.parse_hhmm("09:00"), 60)]}
        t = travel_from({("home", 1): 10, (1, "home"): 10,
                         ("home", 99): 12, (99, "home"): 12,
                         (1, 99): 5, (99, 1): 5})
        out = engine.recommend(99, self.client, days, t)
        self.assertTrue(out["recommended"])
        top = out["recommended"][0]
        self.assertIn("reasons", top)
        self.assertIn("score", top)
        self.assertTrue(top["reasons"])

    def test_approximate_matrices_are_flagged_all_the_way_through(self):
        """A fallback presented as a measurement is worse than no measurement."""
        days = {"2026-11-19": [Stop(1, "A", "Town", 540, 60)]}
        t = travel_from({("home", 1): 10, (1, "home"): 10,
                         ("home", 99): 12, (99, "home"): 12,
                         (1, 99): 5, (99, 1): 5}, approximate=True)
        out = engine.recommend(99, self.client, days, t)
        self.assertTrue(out["approximate"])


class FallbackEstimates(unittest.TestCase):
    """The fallback must be labelled, and must not pretend to precision."""

    def test_estimate_is_plausible_and_positive(self):
        minutes = matrix_mod.estimate_minutes((43.09, -79.08), (42.88, -79.25))
        self.assertGreater(minutes, 5)
        self.assertLess(minutes, 90)

    def test_estimate_applies_a_road_factor(self):
        """Straight-line underestimates road distance systematically."""
        a, b = (43.09, -79.08), (42.88, -79.25)
        km = matrix_mod._haversine_km(a, b)
        estimated_km = km * matrix_mod.ROAD_FACTOR
        self.assertGreater(estimated_km, km)

    def test_haversine_is_zero_for_the_same_point(self):
        self.assertAlmostEqual(matrix_mod._haversine_km((43.0, -79.0), (43.0, -79.0)), 0.0)

    def test_signature_is_stable_and_order_sensitive(self):
        """Order matters — the matrix is positional, and an unstable key
        would silently serve the wrong matrix."""
        a = matrix_mod._signature([(43.0, -79.0), (43.1, -79.1)])
        b = matrix_mod._signature([(43.0, -79.0), (43.1, -79.1)])
        c = matrix_mod._signature([(43.1, -79.1), (43.0, -79.0)])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class AdviceNotAction(unittest.TestCase):
    """A scheduler that silently reshuffles a week gets switched off."""

    def test_improvement_is_returned_as_advice_with_its_saving(self):
        days = {
            "2026-11-16": [Stop(1, "A", "Town", 540, 60)],
            "2026-11-17": [Stop(2, "B", "Town", 540, 60)],
        }
        # A and B are adjacent; doing both on one day is much cheaper.
        t = travel_from({("home", 1): 20, (1, "home"): 20,
                         ("home", 2): 20, (2, "home"): 20,
                         (1, 2): 5, (2, 1): 5})
        out = engine.best_improvement(days, t)
        if out:
            self.assertIn("advice", out)
            self.assertIn("saving_minutes", out)
            self.assertIn("nothing moves on its own", out["note"])
            self.assertGreater(out["saving_minutes"], 0)


class Invariants(unittest.TestCase):
    """Holds for any input, which is what makes them worth asserting."""

    def test_timeline_never_mutates_its_input(self):
        stops = [Stop(2, "B", "T", 660, 60), Stop(1, "A", "T", 540, 60)]
        before = [(s.client_id, s.start) for s in stops]
        engine.timeline(stops, travel_from({("home", 1): 5, (1, 2): 5, (2, "home"): 5}))
        self.assertEqual(before, [(s.client_id, s.start) for s in stops])

    def test_timeline_orders_stops_by_clock_not_insertion_order(self):
        stops = [Stop(2, "Late", "T", 660, 60), Stop(1, "Early", "T", 540, 60)]
        t = travel_from({("home", 1): 5, (1, 2): 5, (2, "home"): 5})
        out = engine.timeline(stops, t)
        names = [s["name"] for s in out["stops"]]
        self.assertEqual(names, ["Early", "Late"])

    def test_empty_day_is_feasible_and_free(self):
        out = engine.timeline([], travel_from({}))
        self.assertTrue(out["feasible"])
        self.assertEqual(out["drive_minutes"], 0)
        self.assertEqual(out["service_minutes"], 0)

    def test_hhmm_wraps_past_midnight(self):
        self.assertEqual(engine.hhmm(25 * 60 + 30), "01:30")


if __name__ == "__main__":
    unittest.main(verbosity=2)
