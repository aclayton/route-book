"""M4 tests — the iCalendar feed.

These test the specification details that break real clients, because that is
where calendar code actually fails. A feed can look perfect in a text editor and
be rejected by Outlook, or worse, be silently mis-parsed so every event is an
hour out.

Four failure modes, each with a test:

- **folding** — over 75 octets, and octet-aware so a multi-byte character is
  never split
- **CRLF** — Unix endings work in Apple Calendar and fail in Outlook
- **escaping** — "Harris, Mrs." ends the field early in a naive generator
- **UID stability** — a UID that changes per export duplicates every event

There is no stdlib iCalendar parser, so structure is checked directly rather
than by round-tripping.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bootstrap import fresh_db, load_plugin  # noqa: E402

plugin = load_plugin()
ical = plugin.ical
config = plugin.config


def rows(*specs):
    """Build fake appointment rows in the shape the feed expects."""
    out = []
    for i, (name, when, start, minutes) in enumerate(specs, start=1):
        out.append({"id": i, "date": when, "start_local": start,
                    "duration_min": minutes, "client_name": name})
    return out


class LineEndings(unittest.TestCase):

    def test_every_line_ends_with_crlf(self):
        """Unix LF works in Apple Calendar and fails in Outlook.

        That is the worst combination: it looks correct to whoever wrote it.
        """
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        self.assertIn("\r\n", doc)
        # No bare LF anywhere — every LF must be preceded by CR.
        for idx, char in enumerate(doc):
            if char == "\n":
                self.assertEqual(doc[idx - 1], "\r",
                                 f"bare LF at offset {idx}")


class Folding(unittest.TestCase):
    """RFC 5545 caps a content line at 75 octets."""

    def test_short_lines_are_untouched(self):
        self.assertEqual(ical._fold("SUMMARY:short"), "SUMMARY:short")

    def test_long_lines_are_folded_under_75_octets(self):
        line = "SUMMARY:" + ("x" * 300)
        folded = ical._fold(line)
        self.assertIn("\r\n ", folded)
        for part in folded.split("\r\n"):
            self.assertLessEqual(len(part.encode("utf-8")), 75,
                                 f"segment over 75 octets: {part!r}")

    def test_folding_never_splits_a_multibyte_character(self):
        """Folding by character count emits invalid UTF-8.

        Clients handle that by dropping the rest of the line, so the symptom is
        a truncated customer name rather than an error.
        """
        line = "SUMMARY:" + ("é" * 200)   # 2 bytes each in UTF-8
        folded = ical._fold(line)
        for part in folded.split("\r\n"):
            part.encode("utf-8").decode("utf-8")   # raises if split mid-character
            self.assertLessEqual(len(part.encode("utf-8")), 75)

    def test_a_long_client_name_produces_a_parseable_feed(self):
        doc = ical.build_calendar(
            rows((("Bartholomew-Fitzwilliam " * 8).strip(), "2026-11-16", "09:00", 180)))
        for line in doc.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)


class Escaping(unittest.TestCase):
    """A comma in a name ends the field early in a naive generator."""

    def test_comma_is_escaped(self):
        self.assertEqual(ical._escape("Harris, Mrs."), "Harris\\, Mrs.")

    def test_semicolon_and_backslash_are_escaped(self):
        self.assertEqual(ical._escape("a;b"), "a\\;b")
        self.assertEqual(ical._escape("a\\b"), "a\\\\b")

    def test_backslash_is_escaped_first(self):
        """Order matters: escaping ';' first would create a backslash that then
        gets escaped again, doubling it."""
        self.assertEqual(ical._escape("a\\;b"), "a\\\\\\;b")

    def test_newlines_become_literal_escape(self):
        self.assertEqual(ical._escape("line1\nline2"), "line1\\nline2")

    def test_none_is_empty_not_the_string_none(self):
        self.assertEqual(ical._escape(None), "")

    def test_a_comma_in_a_name_does_not_break_the_summary_line(self):
        doc = ical.build_calendar(rows(("Harris, Mrs.", "2026-11-16", "09:00", 180)))
        summary = [l for l in doc.split("\r\n") if l.startswith("SUMMARY:")]
        self.assertEqual(len(summary), 1)
        self.assertIn("Harris\\, Mrs.", summary[0])


class Timezones(unittest.TestCase):

    def test_events_carry_a_tzid_and_the_block_is_present(self):
        """A bare local time is meaningless across DST."""
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)),
                                  tzid="America/Toronto")
        self.assertIn("BEGIN:VTIMEZONE", doc)
        self.assertIn("TZID:America/Toronto", doc)
        self.assertIn("DTSTART;TZID=America/Toronto:20261116T090000", doc)

    def test_a_summer_date_is_the_same_wall_clock_time(self):
        """The whole point: 09:00 in June is 09:00, not 08:00."""
        doc = ical.build_calendar(rows(("A", "2026-07-16", "09:00", 180)),
                                  tzid="America/Toronto")
        self.assertIn("DTSTART;TZID=America/Toronto:20260716T090000", doc)

    def test_an_unknown_zone_falls_back_to_floating_not_a_wrong_block(self):
        """A guessed VTIMEZONE silently shifts every appointment."""
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)),
                                  tzid="Mars/Olympus_Mons")
        self.assertNotIn("BEGIN:VTIMEZONE", doc)
        self.assertIn("DTSTART:20261116T090000", doc)


class UidStability(unittest.TestCase):
    """A UID that changes per export duplicates every event."""

    def test_same_appointment_same_uid_across_exports(self):
        data = rows(("A", "2026-11-16", "09:00", 180))
        first = ical.build_calendar(data)
        second = ical.build_calendar(data)
        uids_first = [l for l in first.split("\r\n") if l.startswith("UID:")]
        uids_second = [l for l in second.split("\r\n") if l.startswith("UID:")]
        self.assertEqual(uids_first, uids_second)

    def test_different_appointments_get_different_uids(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180),
                                       ("B", "2026-11-16", "13:00", 120)))
        uids = [l for l in doc.split("\r\n") if l.startswith("UID:")]
        self.assertEqual(len(uids), 2)
        self.assertNotEqual(uids[0], uids[1])


class Privacy(unittest.TestCase):
    """What must never appear in a calendar that syncs to third parties."""

    def test_no_location_property_on_any_event(self):
        """No LOCATION property — it would put a customer's street address into
        the calendar and, on a subscribed feed, onto a third party's servers.

        Checked as a property, not a substring: X-LIC-LOCATION is a VTIMEZONE
        field and is correct and required.
        """
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        props = [l.split(":", 1)[0].split(";", 1)[0]
                 for l in doc.split("\r\n") if l and not l.startswith(" ")]
        self.assertNotIn("LOCATION", props)

    def test_no_addresses_or_codes_leak_in(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        lowered = doc.lower()
        for forbidden in ("code", "lockbox", "address", "st,", "road,"):
            self.assertNotIn(forbidden, lowered,
                             f"{forbidden!r} should never be in the feed")

    def test_title_modes(self):
        data = rows(("Harris", "2026-11-16", "09:00", 180))
        original = config.ICAL_TITLE_MODE
        try:
            config.ICAL_TITLE_MODE = "full"
            self.assertIn("SUMMARY:Harris", ical.build_calendar(data))
            config.ICAL_TITLE_MODE = "initials"
            self.assertIn("SUMMARY:H — job", ical.build_calendar(data))
            config.ICAL_TITLE_MODE = "generic"
            generic = ical.build_calendar(data)
            self.assertIn("SUMMARY:Job — 3h", generic)
            self.assertNotIn("Harris", generic)
        finally:
            config.ICAL_TITLE_MODE = original


class Structure(unittest.TestCase):

    def test_document_is_balanced_and_complete(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180),
                                       ("B", "2026-11-17", "13:00", 120)))
        self.assertTrue(doc.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(doc.strip().endswith("END:VCALENDAR"))
        self.assertEqual(doc.count("BEGIN:VEVENT"), doc.count("END:VEVENT"))
        self.assertEqual(doc.count("BEGIN:VEVENT"), 2)
        self.assertIn("VERSION:2.0", doc)
        self.assertIn("PRODID:", doc)

    def test_every_event_has_the_required_properties(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        for prop in ("UID:", "DTSTAMP:", "DTSTART", "SUMMARY:"):
            with self.subTest(prop=prop):
                self.assertIn(prop, doc)

    def test_empty_calendar_is_still_valid(self):
        """A subscription to nothing must not be a broken file."""
        doc = ical.build_calendar([])
        self.assertIn("BEGIN:VCALENDAR", doc)
        self.assertIn("END:VCALENDAR", doc)
        self.assertNotIn("BEGIN:VEVENT", doc)

    def test_an_unparseable_row_is_skipped_not_guessed(self):
        doc = ical.build_calendar([
            {"id": 1, "date": "nonsense", "start_local": "09:00",
             "duration_min": 60, "client_name": "Bad"},
            {"id": 2, "date": "2026-11-16", "start_local": "09:00",
             "duration_min": 60, "client_name": "Good"},
        ])
        self.assertEqual(doc.count("BEGIN:VEVENT"), 1)
        self.assertIn("Good", doc)
        self.assertNotIn("Bad", doc)


class EndToEnd(unittest.TestCase):
    """The tool, writing a real file."""

    def setUp(self):
        fresh_db()
        plugin.config.HOME_BASE = "12 Base Rd, Fort Erie, ON"
        plugin.geo.geocode = lambda address, city="", country="Canada": (43.0, -79.0, "stub")

    def test_export_writes_a_real_file(self):
        import json
        import tempfile
        tools = plugin.tools
        tools.sched_client_add({"name": "Calendar Tester", "address": "1 A St",
                                "city": "Fort Erie", "duration": "3 hrs"})
        soon = (date.today() + timedelta(days=3)).isoformat()
        tools.sched_book({"client": "Calendar Tester", "when": soon,
                          "duration": "3 hrs", "start": "09:00"})

        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "test.ics")
            out = json.loads(tools.sched_ical_export({"path": target}))
            self.assertEqual(out["status"], "written")
            self.assertGreaterEqual(out["events"], 1)

            # Read BYTES. read_text() translates CRLF to LF by default, which
            # would destroy the exact property under test — and pass a file that
            # Outlook rejects.
            raw = Path(target).read_bytes()
            written = raw.decode("utf-8")
            self.assertIn("BEGIN:VEVENT", written)
            self.assertIn("Calendar Tester", written)
            self.assertEqual(raw.count(b"\n") - raw.count(b"\r\n"), 0,
                             "file contains bare LF line endings")
            for line in written.split("\r\n"):
                self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_export_defaults_to_the_plugins_own_directory(self):
        """Not the vault. Copying a customer list to every synced device is not
        a decision to make by default."""
        import json
        tools = plugin.tools
        config.ICAL_PATH = ""
        out = json.loads(tools.sched_ical_export({}))
        self.assertEqual(out["status"], "written")
        self.assertIn("plugin-data", out["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class IndependentParser(unittest.TestCase):
    """Parse our output with someone else's library.

    Everything above checks our output against our own understanding of the
    spec. This checks it against an independent implementation, which is the
    only way to catch a shared misunderstanding.

    Skipped when `icalendar` is absent, so the repo still tests cold with no
    dependencies — it is a test-only extra, never a plugin requirement.
    """

    def setUp(self):
        try:
            import icalendar  # noqa: F401
        except ImportError:
            self.skipTest("icalendar not installed (optional test extra)")

    def _parse(self, doc):
        import icalendar
        return icalendar.Calendar.from_ical(doc.encode("utf-8"))

    def test_a_real_library_parses_the_feed(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        cal = self._parse(doc)
        self.assertEqual(len([c for c in cal.walk("VEVENT")]), 1)

    def test_dst_offsets_are_actually_applied(self):
        """The point of the VTIMEZONE block: the same wall-clock time carries a
        different UTC offset in summer and winter. A library that resolves the
        offsets proves the block is right, not merely present.
        """
        doc = ical.build_calendar(rows(("Winter", "2026-11-16", "09:00", 180),
                                       ("Summer", "2026-07-16", "09:00", 180)))
        cal = self._parse(doc)
        offsets = {}
        for event in cal.walk("VEVENT"):
            offsets[str(event["summary"]).split(" — ")[0]] = event["dtstart"].dt.utcoffset()
        self.assertEqual(offsets["Winter"].total_seconds(), -5 * 3600, "winter should be EST")
        self.assertEqual(offsets["Summer"].total_seconds(), -4 * 3600, "summer should be EDT")

    def test_an_escaped_comma_round_trips(self):
        doc = ical.build_calendar(rows(("Harris, Mrs.", "2026-11-16", "09:00", 180)))
        cal = self._parse(doc)
        event = list(cal.walk("VEVENT"))[0]
        # The library unescapes it, so we get the original name back.
        self.assertTrue(str(event["summary"]).startswith("Harris, Mrs."))

    def test_a_folded_long_name_round_trips_intact(self):
        """Folding must be invisible to the consumer: unfold and the name is whole."""
        name = "Bartholomew-Fitzwilliam of the Longest Name in Niagara Region"
        doc = ical.build_calendar(rows((name, "2026-11-16", "09:00", 180)))
        cal = self._parse(doc)
        event = list(cal.walk("VEVENT"))[0]
        self.assertTrue(str(event["summary"]).startswith(name))

    def test_uids_are_stable_through_a_real_parser(self):
        doc = ical.build_calendar(rows(("A", "2026-11-16", "09:00", 180)))
        first = sorted(str(e["uid"]) for e in self._parse(doc).walk("VEVENT"))
        second = sorted(str(e["uid"]) for e in self._parse(doc).walk("VEVENT"))
        self.assertEqual(first, second)
