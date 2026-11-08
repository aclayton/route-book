"""M6 tests — the spoken renderer and voice confirmation.

Two things are being tested here, and they are different in kind.

**The renderer** is checked for the ways speech differs from text: a spoken reply
cannot be re-read, so it must be short; it cannot be scanned, so numbers become
words; and it must never state a wrong time confidently, because nobody can
glance back and check.

**The confirmation** is a safety property, not a feature. A voice booking is
staged and read back before it is written, because dictation mishears and a
misheard date creates a real appointment in a real customer's week. The person
who would catch it is listening, not reading — so the read-back is the only
chance.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
spoken = plugin.spoken
tools = plugin.tools
config = plugin.config


def speech(payload):
    """Render a payload and return the sentence."""
    return spoken.render(payload)


class NumbersBecomeWords(unittest.TestCase):
    """A phone reads "2026-11-12" as digits run together."""

    def test_a_date_is_spoken_in_the_order_it_is_said(self):
        self.assertEqual(speech := spoken.say_date("2026-11-12"),
                         "Thursday the twelfth of November")

    def test_every_ordinal_is_covered(self):
        """A missing ordinal silently falls back to a bare number."""
        for day in range(1, 32):
            with self.subTest(day=day):
                result = spoken.say_date(f"2026-01-{day:02d}")
                self.assertNotIn(str(day), result.split("the")[-1].split("of")[0].strip())

    def test_times_carry_a_part_of_day(self):
        """"2" alone is ambiguous; "2 in the morning" is not."""
        self.assertIn("in the morning", spoken._hour(8 * 60))
        self.assertIn("in the afternoon", spoken._hour(14 * 60))
        self.assertIn("in the evening", spoken._hour(19 * 60))

    def test_half_past_is_spoken_not_digitised(self):
        self.assertIn("half past", spoken._hour(8 * 60 + 30))

    def test_noon_is_not_said_as_zero(self):
        spoken_noon = spoken._hour(12 * 60)
        self.assertNotIn("0", spoken_noon)
        self.assertIn("12", spoken_noon)

    def test_minutes_are_minutes_not_min(self):
        """'24 min' is written shorthand. Spoken, it is 'min'."""
        self.assertIn("minutes", spoken.speak_embedded_times("adds 24 min of driving"))
        self.assertNotIn(" min ", spoken.speak_embedded_times("adds 24 min of driving") + " ")

    def test_a_duration_reads_naturally(self):
        self.assertEqual(spoken._duration_words(60), "an hour")
        self.assertEqual(spoken._duration_words(180), "3 hours")
        self.assertEqual(spoken._duration_words(30), "30 minutes")


class NeverStatesAWrongTime(unittest.TestCase):
    """The worst failure this module can have: a wrong time, spoken as fact.

    An earlier version read `payload["start_min"]`, which the booking tool does
    not emit. It defaulted to zero and announced "12 in the morning" for an 08:00
    job. Nobody reading a screen would have been fooled; somebody listening would
    have been.
    """

    def test_a_booked_time_comes_from_the_clock_strings(self):
        sentence = speech({
            "status": "booked",
            "client": {"name": "Bala Construction"},
            "date": "2026-11-12",
            "start": "08:00", "end": "11:00", "duration_min": 180,
        })
        self.assertIn("8 in the morning", sentence)
        self.assertIn("11 in the morning", sentence)
        self.assertNotIn("12 in the morning", sentence)

    def test_a_missing_time_is_omitted_not_defaulted_to_midnight(self):
        """Omitting the time is honest. Saying midnight is a lie."""
        sentence = speech({
            "status": "booked",
            "client": {"name": "Bala Construction"},
            "date": "2026-11-12",
            "duration_min": 180,
        })
        self.assertNotIn("12 in the morning", sentence)
        self.assertIn("3 hours", sentence)

    def test_clock_parsing_returns_none_for_junk(self):
        self.assertIsNone(spoken.clock_to_minutes("nonsense"))
        self.assertIsNone(spoken.clock_to_minutes(None))
        self.assertEqual(spoken.clock_to_minutes("08:00"), 480)

    def test_times_inside_tool_messages_are_translated(self):
        """Tool messages are written for a screen and carry '08:12'."""
        out = spoken.speak_embedded_times("cannot reach Bala by 08:00 — arriving 08:12")
        self.assertNotIn("08:00", out)
        self.assertNotIn("08:12", out)
        self.assertIn("8 in the morning", out)


class ReadBackConfirmation(unittest.TestCase):
    """A voice booking writes nothing until it is confirmed."""

    def setUp(self):
        fresh_db()
        tools.bind_context(None)
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        plugin.matrix.build_matrix = lambda points, force=False: {
            "minutes": [[0 if i == j else 12 for j in range(len(points))]
                        for i in range(len(points))],
            "km": None, "approximate": False, "source": "stub", "points": list(points)}
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})

    def test_a_spoken_booking_writes_nothing(self):
        """THE safety property. If this fails, voice books things nobody said."""
        out = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                          "format": "spoken"}))
        self.assertEqual(out["status"], "needs_confirmation")
        self.assertIn("token", out)

        # Nothing on the calendar.
        for offset in range(0, 14):
            import datetime
            day = (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()
            day_view = json.loads(tools.sched_day({"when": day}))
            self.assertEqual(day_view["job_count"], 0,
                             f"a booking was written to {day} before confirmation")

    def test_the_read_back_says_who_and_when_and_asks(self):
        out = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                          "format": "spoken"}))
        read_back = out["read_back"]
        self.assertIn("Bala Construction", read_back)
        self.assertIn("?", read_back, "a read-back must ask")
        self.assertIn("3 hours", read_back)

    def test_the_read_back_uses_the_operators_own_words(self):
        """Hearing "next Thursday" back is how a mishearing gets caught.

        A resolved date read aloud sounds authoritative and confirms nothing.
        """
        out = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                          "format": "spoken"}))
        self.assertIn("next thursday", out["read_back"].lower())

    def test_the_read_back_does_not_say_the_weekday_twice(self):
        out = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                          "format": "spoken"}))
        self.assertEqual(out["read_back"].lower().count("thursday"), 1,
                         f"weekday repeated: {out['read_back']}")

    def test_confirming_writes_it(self):
        staged = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                             "format": "spoken"}))
        done = json.loads(tools.sched_ask({"confirm": staged["token"], "format": "spoken"}))
        self.assertIn("reply", done)
        self.assertIn("Booked", done["reply"])
        self.assertIn("8 in the morning", done["reply"])

        day_view = json.loads(tools.sched_day({"when": staged["date"]}))
        self.assertEqual(day_view["job_count"], 1, "confirmation did not write")

    def test_a_token_cannot_be_reused(self):
        """One yes, one booking."""
        staged = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                             "format": "spoken"}))
        tools.sched_ask({"confirm": staged["token"], "format": "spoken"})
        again = json.loads(tools.sched_ask({"confirm": staged["token"], "format": "spoken"}))
        self.assertIn("reply", again)
        self.assertNotIn("Booked", again["reply"])

    def test_an_unknown_token_fails_safely(self):
        out = json.loads(tools.sched_ask({"confirm": "vb-not-a-real-token",
                                          "format": "spoken"}))
        self.assertIn("reply", out)
        self.assertIn("reply", out)

    def test_a_stale_token_expires(self):
        """A confirmation answered ten minutes later is one nobody remembers giving."""
        staged = spoken.stage_booking({"when_text": "next Thursday"},
                                     {"id": 1, "name": "Bala Construction"},
                                     "2026-11-12", 180)
        token = staged[0]
        spoken._PENDING[token]["staged_at"] = time.time() - 3600
        self.assertIsNone(spoken.take_booking(token))

    def test_saying_no_writes_nothing(self):
        staged = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs",
                                             "format": "spoken"}))
        # The operator declines by simply not confirming. The stage expires.
        day_view = json.loads(tools.sched_day({"when": staged["date"]}))
        self.assertEqual(day_view["job_count"], 0)

    def test_confirmation_can_be_turned_off(self):
        """The setting exists for someone who trusts their dictation."""
        original = config.VOICE_CONFIRM_BOOKINGS
        try:
            config.VOICE_CONFIRM_BOOKINGS = False
            out = json.loads(tools.sched_ask(
                {"text": "bala construction, next Thursday, 3 hrs", "format": "spoken"}))
            self.assertIn("reply", out)
            self.assertIn("Booked", out["reply"])
        finally:
            config.VOICE_CONFIRM_BOOKINGS = original


class SpeechIsShort(unittest.TestCase):
    """A spoken reply cannot be re-read, so it says less than the written one."""

    def setUp(self):
        fresh_db()
        tools.bind_context(None)
        config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        plugin.matrix.build_matrix = lambda points, force=False: {
            "minutes": [[0 if i == j else 12 for j in range(len(points))]
                        for i in range(len(points))],
            "km": None, "approximate": False, "source": "stub", "points": list(points)}
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})
        tools.sched_client_add({"name": "Niagara Septic", "address": "9 King St",
                                "city": "Fort Erie", "duration": "2 hrs"})

    def test_a_recommendation_names_one_day_not_five(self):
        """Eleven ranked days is unusable when you cannot scan.

        The count of others is kept, because it tells the listener a choice
        exists; the alternatives themselves are not, because nobody hears five
        dates and remembers the second.
        """
        tools.sched_book({"client": "Niagara Septic", "when": "2026-11-10",
                          "duration": "2 hrs", "start": "09:00"})
        out = json.loads(tools.sched_ask(
            {"text": "recommend the best day for bala in two weeks", "format": "spoken"}))
        reply = out["reply"]
        self.assertLess(len(reply), 240, f"too long to hear: {reply!r}")
        # Only ONE date is named.
        self.assertEqual(reply.count(" of November"), 1, f"multiple dates: {reply!r}")

    def test_replies_are_bounded_in_length(self):
        for sentence in ["what does my week look like", "list my clients",
                         "what is on today", "recommend the best day for bala in two weeks"]:
            with self.subTest(sentence=sentence):
                out = json.loads(tools.sched_ask({"text": sentence, "format": "spoken"}))
                self.assertLess(len(out.get("reply") or ""), 300,
                                f"{sentence!r} produced an unhearable reply")

    def test_no_raw_json_or_ids_are_spoken(self):
        for sentence in ["what does my week look like", "list my clients",
                         "what is on today", "bala construction, next Thursday, 3 hrs"]:
            with self.subTest(sentence=sentence):
                out = json.loads(tools.sched_ask({"text": sentence, "format": "spoken"}))
                reply = out.get("reply") or out.get("read_back") or ""
                for forbidden in ("{", "}", "[", "]", "uid", "client_id", "2026-"):
                    self.assertNotIn(forbidden, reply,
                                     f"{forbidden!r} leaked into speech: {reply!r}")


class RendererRobustness(unittest.TestCase):

    def test_an_unrecognised_payload_still_says_something(self):
        self.assertEqual(speech({"unexpected": True}), "Done.")

    def test_junk_does_not_raise(self):
        for payload in [None, 42, "string", [], {"error": None}]:
            with self.subTest(payload=payload):
                self.assertIsInstance(speech(payload), str)

    def test_an_error_is_spoken_as_a_sentence(self):
        out = speech({"error": "nothing booked for Bala Construction on that day"})
        self.assertIn("did not work", out)
        self.assertTrue(out.endswith((".", "?", "!")))

    def test_a_clash_is_spoken_with_the_advice(self):
        out = speech({"error": "clashes with Bala Construction at 08:00"})
        self.assertIn("Pick another time", out)
        self.assertNotIn("08:00", out)

    def test_a_clarifying_question_is_passed_through(self):
        out = speech({"status": "needs_clarification", "question": "Which client is this for?"})
        self.assertEqual(out, "Which client is this for?")

    def test_an_infeasible_day_explains_itself(self):
        """The one thing worth interrupting for, in words rather than a 24h clock."""
        out = speech({
            "job_count": 1, "date": "2026-11-12",
            "problems": ["cannot reach Bala Construction by 08:00 — arriving 08:12"],
        })
        self.assertNotIn("08:00", out)
        self.assertIn("8 in the morning", out)

    def test_a_spliced_clause_does_not_start_a_new_sentence(self):
        """A lowercase clause after a full stop reads as broken speech.

        The tool message is written for a screen, so it starts lowercase. Spoken
        after "That did not work:" it must not look like a new sentence begun
        badly.
        """
        out = speech({"error": "nothing booked for Bala Construction on that day"})
        self.assertTrue(out.startswith("That did not work"))
        body = out[len("That did not work"):].lstrip(": ")
        self.assertTrue(body[0].islower(), f"unexpected capital mid-reply: {out!r}")

    def test_the_pronoun_I_survives_lowercasing(self):
        out = speech({"error": "I could not tell what to do with that"})
        self.assertIn("I could", out)


class TheShimForwardsSpokenMode(unittest.TestCase):
    """The /ask endpoint tells the agent the reply will be read aloud."""

    def test_the_shim_reads_the_spoken_flag(self):
        source = Path("/home/boss/.hermes/scripts/vault-ingest-server.py").read_text()
        self.assertIn('data.get("spoken")', source)
        self.assertIn("reading aloud", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
