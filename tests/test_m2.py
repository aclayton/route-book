"""M2 golden tests — sentences in, intents out.

These are the specification for the deterministic parser. Each case is a
sentence a real operator would say, paired with exactly what it must resolve to.

`today` is pinned so the tests mean the same thing on every run. A test for
"next Thursday" that passes only on some days is worse than no test, because it
teaches you to distrust the suite.

Monday 9 November 2026 is the reference date: chosen because the week is
unambiguous and "next Thursday" lands on the 12th, not the 5th.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import load_plugin  # noqa: E402

plugin = load_plugin()
nl = plugin.nl

MON = date(2026, 11, 9)   # a Monday
THU = date(2026, 11, 12)
FRI = date(2026, 11, 13)


class Dates(unittest.TestCase):
    """The bounded grammar. None means 'ask', never 'probably today'."""

    def test_absolute_anchor_days(self):
        self.assertEqual(nl.parse_when("today", MON), MON)
        self.assertEqual(nl.parse_when("tonight", MON), MON)
        self.assertEqual(nl.parse_when("tomorrow", MON), date(2026, 11, 10))
        self.assertEqual(nl.parse_when("the day after tomorrow", MON), date(2026, 11, 11))

    def test_next_weekday_is_strictly_after_today(self):
        """'next Thursday' on a Monday is the 12th, not the 5th."""
        self.assertEqual(nl.parse_when("next Thursday", MON), THU)
        self.assertEqual(nl.parse_when("next thursday", MON), THU)
        self.assertEqual(nl.parse_when("next thu", MON), THU)
        self.assertEqual(nl.parse_when("Thursday", MON), THU)

    def test_next_weekday_skips_today(self):
        """On a Thursday, 'next Thursday' is a week away.

        This is the whole US/UK ambiguity, resolved deliberately: strictly after.
        """
        self.assertEqual(nl.parse_when("next Thursday", THU), date(2026, 11, 19))

    def test_this_weekday_may_be_today(self):
        """'this Thursday' on a Thursday is today — 'this' is not strict."""
        self.assertEqual(nl.parse_when("this Thursday", THU), THU)
        self.assertEqual(nl.parse_when("this Thursday", MON), THU)

    def test_in_n_units(self):
        self.assertEqual(nl.parse_when("in 2 days", MON), date(2026, 11, 11))
        self.assertEqual(nl.parse_when("in two weeks", MON), date(2026, 11, 23))
        self.assertEqual(nl.parse_when("in 1 week", MON), date(2026, 11, 16))

    def test_months_are_refused_rather_than_approximated(self):
        """'in 2 months' is not 60 days.

        A month has no fixed length, so any number here is wrong some of the
        time — and wrong silently. Refusing means the caller asks.
        """
        self.assertIsNone(nl.parse_when("in 2 months", MON))
        self.assertIsNone(nl.parse_when("next month", MON))

    def test_ordinal_day(self):
        self.assertEqual(nl.parse_when("the 20th", MON), date(2026, 11, 20))
        self.assertEqual(nl.parse_when("on the 20th", MON), date(2026, 11, 20))

    def test_ordinal_rolls_to_next_month_when_passed(self):
        self.assertEqual(nl.parse_when("the 5th", MON), date(2026, 12, 5))

    def test_month_day_both_orders(self):
        self.assertEqual(nl.parse_when("Nov 20", MON), date(2026, 11, 20))
        self.assertEqual(nl.parse_when("November 20", MON), date(2026, 11, 20))
        self.assertEqual(nl.parse_when("20 Nov", MON), date(2026, 11, 20))

    def test_iso(self):
        self.assertEqual(nl.parse_when("2026-11-20", MON), date(2026, 11, 20))
        self.assertIsNone(nl.parse_when("2026-02-30", MON))  # impossible date

    def test_british_week_form(self):
        self.assertEqual(nl.parse_when("a week Tuesday", MON), date(2026, 11, 17))
        self.assertEqual(nl.parse_when("Tuesday week", MON), date(2026, 11, 17))

    def test_unreadable_returns_none(self):
        """The important one. Never a guess — ask instead."""
        for text in ("soon", "sometime", "whenever", "the usual", "", None, "next month-ish"):
            with self.subTest(text=text):
                self.assertIsNone(nl.parse_when(text, MON))


class Durations(unittest.TestCase):

    def test_reads_the_forms_people_type(self):
        cases = {"3 hrs": 180, "3 hours": 180, "3h": 180, "90 min": 90,
                 "90m": 90, "2.5 hrs": 150, "45": 45, "three hours": 180,
                 "half a day": 240, "all day": 480}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(nl.parse_duration(text), expected)

    def test_unreadable_is_none(self):
        for text in ("", None, "ages", "a while", "soon"):
            with self.subTest(text=text):
                self.assertIsNone(nl.parse_duration(text))


class Verbs(unittest.TestCase):
    """Classifying the verb first — getting this wrong books a day they asked about."""

    def test_interrogative_is_recommend_not_book(self):
        cases = [
            "recommend the best day for bala in two weeks",
            "when should I do bala?",
            "which day works for bala",
            "what day is good for bala",
            "when can I fit bala in",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(nl.parse_verb(text), "recommend")

    def test_imperative_is_book(self):
        for text in ("book bala thursday 3 hours", "schedule bala for tuesday",
                     "add bala next week"):
            with self.subTest(text=text):
                self.assertEqual(nl.parse_verb(text), "book")

    def test_move_and_cancel(self):
        self.assertEqual(nl.parse_verb("move bala to friday"), "move")
        self.assertEqual(nl.parse_verb("reschedule bala"), "move")
        self.assertEqual(nl.parse_verb("cancel bala this week"), "cancel")

    def test_list(self):
        for text in ("what does next Tuesday look like", "my week",
                     "show me thursday"):
            with self.subTest(text=text):
                self.assertEqual(nl.parse_verb(text), "list")

    def test_unknown(self):
        for text in ("", "bala", "hmm"):
            with self.subTest(text=text):
                self.assertEqual(nl.parse_verb(text), "unknown")


class ClientResolution(unittest.TestCase):
    """Never a single pick. Both Harrises, or neither."""

    def setUp(self):
        self.clients = [
            {"id": 1, "name": "Bala", "aliases": "", "spoken_names": "baya,baja", "city": "Fort Erie"},
            {"id": 2, "name": "Mrs. Harris", "aliases": "harris", "spoken_names": "", "city": "Fort Erie"},
            {"id": 3, "name": "Harris", "aliases": "", "spoken_names": "", "city": "Niagara Falls"},
        ]

    def test_exact(self):
        self.assertEqual(nl.resolve_client("bala", self.clients)[0]["id"], 1)

    def test_case_and_punctuation_insensitive(self):
        self.assertEqual(nl.resolve_client("BALA", self.clients)[0]["id"], 1)

    def test_exact_name_beats_alias(self):
        """'harris' is client 3's actual name and client 2's alias.

        Exact must rank first — but both still come back, because which one the
        operator meant is not a decision this function gets to make.
        """
        ids = [c["id"] for c in nl.resolve_client("harris", self.clients)]
        self.assertEqual(ids[0], 3, "exact name should rank above alias")
        self.assertIn(2, ids, "the alias match should still be offered")

    def test_speech_to_text_spellings(self):
        """'baya' and 'baja' are what an STT engine returns for 'Bala'."""
        self.assertEqual(nl.resolve_client("baya", self.clients)[0]["id"], 1)
        self.assertEqual(nl.resolve_client("baja", self.clients)[0]["id"], 1)

    def test_token_match(self):
        """'mrs harris' should find 'Mrs. Harris' despite the period."""
        ids = [c["id"] for c in nl.resolve_client("mrs harris", self.clients)]
        self.assertIn(2, ids)

    def test_ambiguity_returns_every_match(self):
        """Two Harrises must both come back so the caller can disambiguate."""
        ids = set(c["id"] for c in nl.resolve_client("haris", self.clients))
        self.assertTrue({2, 3} & ids, "expected at least the fuzzy Harris matches")

    def test_unknown_name_returns_nothing_rather_than_a_guess(self):
        self.assertEqual(nl.resolve_client("zebediah", self.clients), [])

    def test_empty_input(self):
        self.assertEqual(nl.resolve_client("", self.clients), [])
        self.assertEqual(nl.resolve_client(None, self.clients), [])


class WholeRequests(unittest.TestCase):
    """The conversational forms from the design, end to end."""

    def setUp(self):
        self.clients = [
            {"id": 1, "name": "Bala", "aliases": "", "spoken_names": "baya", "city": "Fort Erie"},
        ]

    def test_the_flagship_booking(self):
        """'bala, next Thursday, 3 hrs' — the sentence the whole design is built around."""
        out = nl.parse_request("bala, next Thursday, 3 hrs", MON, self.clients)
        self.assertEqual(out["verb"], "book")
        self.assertEqual(out["clients"][0]["id"], 1)
        self.assertEqual(out["when"], "2026-11-12")
        self.assertEqual(out["duration_min"], 180)

    def test_the_flagship_recommendation(self):
        out = nl.parse_request("recommend the best day for bala in two weeks", MON, self.clients)
        self.assertEqual(out["verb"], "recommend")
        self.assertEqual(out["clients"][0]["id"], 1)

    def test_booking_without_a_duration(self):
        """No duration means the client's default — not zero, not a guess."""
        out = nl.parse_request("book bala tomorrow", MON, self.clients)
        self.assertEqual(out["verb"], "book")
        self.assertEqual(out["when"], "2026-11-10")
        self.assertIsNone(out["duration_min"])

    def test_unresolved_date_is_reported_as_none(self):
        out = nl.parse_request("book bala soon", MON, self.clients)
        self.assertEqual(out["verb"], "book")
        self.assertIsNone(out["when"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
