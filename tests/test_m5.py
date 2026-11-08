"""M5 tests — the conversational layer.

The most important test in this file is `PrivacyContract`. Everything else
checks that the layer works; that class checks that using it does not ship a
customer database to a model provider.

The recording fake is how it does that. A stub that returns a canned answer
proves nothing about privacy — you have to capture the actual payload.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
ask = plugin.ask
nl = plugin.nl
tools = plugin.tools

TODAY = date(2026, 11, 8)   # a Sunday


class RecordingModel:
    """A stand-in ctx.llm that records every call.

    The record is the point: it lets a test assert on what was *sent*, not just
    on what came back. A privacy bug lives in the outbound payload.
    """

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply or {"intent": "week"}

    def complete_structured(self, prompt=None, system=None, schema=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system, "schema": schema})
        return self.reply

    @property
    def called(self):
        return bool(self.calls)

    def everything_sent(self):
        """All text that went outbound, as one string."""
        return " ".join(
            f"{c.get('prompt') or ''} {c.get('system') or ''}" for c in self.calls
        )


class FakeCtx:
    def __init__(self, llm):
        self.llm = llm


class Nil:
    """A plugin context with no model at all — the default install."""


def clients():
    return [
        {"id": 1, "name": "Bala Construction", "city": "Fort Erie",
         "address": "41 Niagara Blvd", "postal": "L2A 1A1"},
        {"id": 2, "name": "Bala Septic", "city": "Port Colborne",
         "address": "9 King St", "postal": "L3K 2B2"},
        {"id": 3, "name": "Harris, Mrs.", "city": "Niagara Falls",
         "address": "1445 Victoria Ave", "postal": "L2E 4G1"},
    ]


class PrivacyContract(unittest.TestCase):
    """What must never leave this machine, at any point, for any reason."""

    def setUp(self):
        fresh_db()
        tools.bind_context(Nil())

    def test_the_system_instruction_names_no_customer_and_no_date(self):
        """The instruction is a constant. It must not carry context.

        The tempting mistake is 'helpful' context — a customer list to
        disambiguate against, or today's date so the model can resolve 'next
        Thursday'. Both leak. The model is not told either.
        """
        system = ask.model_sentence("anything")
        for forbidden in ("Bala", "Harris", "Niagara", "Fort Erie", "Victoria Ave"):
            self.assertNotIn(forbidden, system, f"{forbidden!r} reached the model")
        # No date either: the model is never told what day it is.
        self.assertNotIn("2026", system)
        self.assertNotIn("November", system)

    def test_a_sentence_the_rules_handle_never_reaches_the_model(self):
        """Rules first is a privacy control, not just a cost saving.

        Most requests never leave the machine because most requests parse.
        """
        # The client has to EXIST. Without it the rules correctly cannot resolve
        # "bala", fall through to the model, and the test would have proved
        # nothing about privacy while appearing to.
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})

        model = RecordingModel()
        tools.bind_context(FakeCtx(model))
        out = json.loads(tools.sched_ask({"text": "bala, next Thursday, 3 hrs"}))
        self.assertEqual(out.get("routed_to"), "sched_book")
        self.assertFalse(model.called, "the model was called for a sentence the rules parse")

    def test_when_the_model_runs_it_receives_no_customer_data(self):
        """The sentence itself, and nothing else. This is the whole contract."""
        model = RecordingModel({"intent": "week"})
        tools.bind_context(FakeCtx(model))
        sentence = "sort me out for sometime next week please"
        tools.sched_ask({"text": sentence})

        self.assertTrue(model.called, "expected the model to be consulted")
        sent = model.everything_sent()
        for forbidden in ("Bala", "Harris", "Construction", "Septic",
                          "Victoria Ave", "Niagara Blvd", "Fort Erie",
                          "L2A", "L3K", "L2E"):
            self.assertNotIn(forbidden, sent, f"{forbidden!r} was sent to the model")

    def test_ambiguity_is_resolved_locally_not_by_the_model(self):
        """The tempting shortcut: ask the model 'Bala Construction or Bala Septic?'

        That resolves it neatly and hands over the customer list. Instead the
        model gets the string the operator said and Python resolves it against
        the local database.
        """
        model = RecordingModel({"intent": "book", "client": "bala",
                                "when": "next Thursday", "duration": "3 hrs"})
        tools.bind_context(FakeCtx(model))
        tools.sched_ask({"text": "sort bala out for next Thursday for 3 hrs please"})

        sent = model.everything_sent()
        self.assertNotIn("Construction", sent)
        self.assertNotIn("Septic", sent)
        # The ambiguous string itself is fine to send — it is the operator's word.
        self.assertIn("bala", sent.lower())


class RulesFirst(unittest.TestCase):

    def setUp(self):
        fresh_db()
        tools.bind_context(Nil())
        # Stub BEFORE creating the client. Doing it after made the geocoder
        # reach the network, which the suite guard reported — correctly.
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})

    def test_the_flagship_shorthand_books(self):
        """'bala, next Thursday, 3 hrs' — no verb word at all."""
        out = json.loads(tools.sched_ask({"text": "bala, next Thursday, 3 hrs"}))
        self.assertEqual(out["routed_to"], "sched_book")
        self.assertEqual(out["source"], "rules")
        self.assertIn("Bala Construction", json.dumps(out))

    def test_it_reports_which_parser_was_used(self):
        out = json.loads(tools.sched_ask({"text": "bala, next Thursday, 3 hrs"}))
        self.assertEqual(out["source"], "rules")


class ModelPath(unittest.TestCase):
    """When the rules fail, the model fills the gap — and is then discarded."""

    def setUp(self):
        fresh_db()
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})

    def test_the_model_supplies_structure_and_python_resolves_it(self):
        model = RecordingModel({"intent": "book", "client": "bala construction",
                                "when": "next Thursday", "duration": "3 hrs"})
        tools.bind_context(FakeCtx(model))
        out = json.loads(tools.sched_ask({"text": "please sort out bala's next visit soon"}))

        # Whatever the model said, the client was resolved against the local DB.
        self.assertIn("Bala Construction", json.dumps(out))
        self.assertEqual(out.get("source"), "model")

    def test_the_model_is_never_asked_to_compute_a_date(self):
        """It gets an expression and returns an expression."""
        model = RecordingModel({"intent": "week"})
        tools.bind_context(FakeCtx(model))
        tools.sched_ask({"text": "how does it look in a fortnight then"})
        self.assertNotIn("2026-11", model.everything_sent())


class ModelDateTampering(unittest.TestCase):
    """A model that resolves a date has done the exact thing it was told not to."""

    def setUp(self):
        fresh_db()
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})

    def test_a_date_the_operator_never_said_is_refused_not_trusted(self):
        """'Next Thursday' misheard is a question. A wrong Thursday is a locked door."""
        model = RecordingModel({"intent": "book", "client": "bala construction",
                                "when": "2026-11-19", "duration": "3 hrs"})
        tools.bind_context(FakeCtx(model))
        out = json.loads(tools.sched_ask({"text": "get bala booked in soon please"}))

        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("when", out["missing"])
        self.assertIn("not going to guess", out["question"])
        self.assertIsNone(out["understood"]["when"])

    def test_nothing_was_booked(self):
        """The refusal must actually prevent the write."""
        model = RecordingModel({"intent": "book", "client": "bala construction",
                                "when": "2026-11-19", "duration": "3 hrs"})
        tools.bind_context(FakeCtx(model))
        tools.sched_ask({"text": "get bala booked in soon please"})
        listed = json.loads(tools.sched_day({"when": "2026-11-19"}))
        self.assertEqual(listed["job_count"], 0)

    def test_an_expression_the_operator_did_say_is_resolved_normally(self):
        """The guard must not break the honest case."""
        model = RecordingModel({"intent": "book", "client": "bala construction",
                                "when": "next Thursday", "duration": "3 hrs"})
        tools.bind_context(FakeCtx(model))
        out = json.loads(tools.sched_ask({"text": "get bala booked in soon please"}))
        self.assertNotEqual(out.get("status"), "needs_clarification")
        self.assertIsNotNone(out["called_with"]["when"])


class ClarifyingQuestions(unittest.TestCase):
    """Ask rather than guess. A wrong booking costs more than a question."""

    def setUp(self):
        fresh_db()
        tools.bind_context(Nil())

    def test_a_missing_client_is_asked_for(self):
        out = json.loads(tools.sched_ask({"text": "book someone for next Thursday"}))
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("client", out["missing"])
        self.assertIn("Which client", out["question"])

    def test_a_cancel_with_no_date_targets_the_soonest(self):
        """A cancel needs no date: the soonest appointment is the obvious target.

        Asking "which one?" when there is only one is the kind of question that
        makes an assistant tiring.
        """
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        out = json.loads(tools.sched_ask({"text": "cancel bala construction"}))
        # No appointments exist, so it reports that rather than asking a question.
        self.assertNotEqual(out.get("status"), "needs_clarification")

    def test_an_unreadable_date_asks_rather_than_defaulting_to_today(self):
        """Admitting the doubt beats defaulting to today.

        The rules handle this one — a booking with a known client and no readable
        date — so this asserts the RULE path asks. Where the model supplies the
        unreadable expression instead, `ModelDateTampering` covers it.
        """
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})
        model = RecordingModel()
        tools.bind_context(FakeCtx(model))
        out = json.loads(tools.sched_ask({"text": "book bala construction whenever suits"}))
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("when", out["missing"])
        self.assertFalse(model.called, "the rules handled this; the model should not run")

    def test_clarification_carries_what_was_already_understood(self):
        """So the operator supplies the gap, not the whole sentence again."""
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})
        out = json.loads(tools.sched_ask({"text": "book bala construction sometime"}))
        self.assertEqual(out["status"], "needs_clarification")
        self.assertIn("intent", out["understood"])
        self.assertEqual(out["understood"]["intent"], "book")


class NoModelInstalled(unittest.TestCase):
    """The plugin works fully offline. This is the default install."""

    def setUp(self):
        fresh_db()
        tools.bind_context(Nil())
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")

    def test_a_rules_sentence_still_works(self):
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie"})
        out = json.loads(tools.sched_ask({"text": "bala, next Thursday, 3 hrs"}))
        self.assertEqual(out["routed_to"], "sched_book")

    def test_nonsense_never_raises_and_always_returns_json(self):
        """The contract, stated so it stays true: JSON out, no exception.

        An earlier version used "flibbertigibbet the wossname" as a stand-in for
        unparseable — but the list patterns match it, so it exercises the
        dispatch path, not the failure path. Asserting the contract is better
        than asserting a sentence is nonsense, because the parser keeps learning
        words.
        """
        for sentence in ["flibbertigibbet the wossname", "???", "9", "book"]:
            with self.subTest(sentence=sentence):
                raw = tools.sched_ask({"text": sentence})
                payload = json.loads(raw)          # must be JSON
                self.assertIsInstance(payload, dict)

    def test_an_exception_from_the_model_does_not_break_the_tool(self):
        """A conversational layer that dies when the model is unavailable is
        worse than one that falls back."""
        class Exploding:
            def complete_structured(self, **kwargs):
                raise RuntimeError("provider is down")

        tools.bind_context(FakeCtx(Exploding()))
        out = json.loads(tools.sched_ask({"text": "book bala construction soon"}))
        self.assertIsInstance(out, dict)
        self.assertNotEqual(out.get("source"), "model")

    def test_a_model_returning_rubbish_does_not_break_the_tool(self):
        for rubbish in ["not json at all", 42, None, {"intent": "explode"},
                        {"nope": True}]:
            with self.subTest(reply=rubbish):
                tools.bind_context(FakeCtx(RecordingModel(rubbish)))
                # Must not raise, and must return JSON — whatever the model said.
                out = json.loads(tools.sched_ask({"text": "book bala construction soon"}))
                self.assertIsInstance(out, dict)
                self.assertNotEqual(out.get("source"), "model")


class Routing(unittest.TestCase):
    """Every intent reaches the right tool."""

    def setUp(self):
        fresh_db()
        tools.bind_context(Nil())
        # Stub BEFORE creating the client. Doing it after made the geocoder
        # reach the network, which the suite guard reported — correctly.
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        tools.sched_client_add({"name": "Bala Construction", "address": "41 Niagara Blvd",
                                "city": "Fort Erie", "duration": "3 hrs"})

    def test_intents_map_to_tools(self):
        cases = [
            ("what does my week look like", "sched_week"),
            ("what is on today", "sched_day"),
            ("list my clients", "sched_client_list"),
            ("show me the calendar feed", "sched_ical_export"),
        ]
        for sentence, expected in cases:
            with self.subTest(sentence=sentence):
                out = json.loads(tools.sched_ask({"text": sentence}))
                self.assertEqual(out.get("routed_to"), expected,
                                 f"{sentence!r} -> {out.get('routed_to')}")

    def test_recommend_routes_to_recommend(self):
        out = json.loads(tools.sched_ask(
            {"text": "recommend the best day for bala construction in two weeks"}))
        self.assertEqual(out.get("routed_to"), "sched_recommend")

    def test_an_empty_sentence_is_rejected_clearly(self):
        out = json.loads(tools.sched_ask({"text": "   "}))
        self.assertIn("error", out)

    def test_a_conversational_booking_obeys_the_same_clash_rule(self):
        """One code path, not two that drift.

        The ask path relays to sched_book, so a booking made by talking cannot
        slip past a rule a booking made by typing has to respect. Repeating the
        same sentence must therefore clash — which also proves the first one
        really was written.
        """
        first = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs"}))
        self.assertEqual(first.get("routed_to"), "sched_book")
        self.assertEqual(first["status"], "booked")

        again = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs"}))
        self.assertEqual(again.get("routed_to"), "sched_book")
        self.assertIn("error", again)
        self.assertIn("clashes", again["error"])

    def test_the_confirmation_echoes_the_operators_own_words(self):
        """So a misheard date is visible in the reply, not buried in a resolved one."""
        out = json.loads(tools.sched_ask({"text": "bala construction, next Thursday, 3 hrs"}))
        self.assertIn("Thursday", out["readable"])
        self.assertEqual(out["called_with"]["client"], "Bala Construction")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RecommendOnAConfiguredInstall(unittest.TestCase):
    """The recommend path with a home base and a matrix — a real install.

    `sched_recommend` read a sqlite3.Row with `.get()`, which Row does not
    support, so it raised AttributeError on every properly configured setup. It
    was invisible until an unrelated test happened to set HOME_BASE: without one
    the tool stops earlier with "fewer than two located points" and never reaches
    the bug.

    The lesson is the shape of the test: exercise the path that a WORKING install
    takes, not the early return that hides it.
    """

    def setUp(self):
        fresh_db()
        plugin.config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        tools.bind_context(Nil())
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")
        for name, city in (("Bala Construction", "Fort Erie"), ("Second Client", "Fort Erie")):
            tools.sched_client_add({"name": name, "address": f"1 {name} St",
                                    "city": city, "duration": "3 hrs"})

    def test_recommend_works_when_everything_is_configured(self):
        out = json.loads(tools.sched_ask(
            {"text": "recommend the best day for bala construction in two weeks"}))
        self.assertNotIn("error", out, f"recommend failed on a configured install: {out}")
        self.assertEqual(out["routed_to"], "sched_recommend")
        self.assertIn("recommended", out)
        self.assertTrue(out["recommended"], "no day was recommended")

    def test_recommend_directly_also_works(self):
        out = json.loads(tools.sched_recommend(
            {"client": "Bala Construction", "within": "in two weeks"}))
        self.assertNotIn("error", out, f"{out}")
