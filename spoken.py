"""The spoken renderer — M6.

Turns a tool's payload into one sentence a phone can read aloud.

## Why this is its own module

A spoken reply is not a written one with the punctuation filed off. It has
properties the written form does not:

- **It cannot be re-read.** A screen can be glanced at again; a sentence is gone
  the moment it ends. So a spoken reply carries one fact, then stops. It does not
  report eleven ranked days, it names the best one.
- **It cannot be scanned.** Nobody hears a list of times and remembers the
  second one. Numbers become words, and only the ones that matter.
- **It is heard once, in a car, possibly by someone driving.** Density is a
  safety problem, not a style preference.

## Numbers

"2026-11-12" is not speech. "08:00–11:00" is not speech. Both are what the tools
produce, because both are right for a screen and for a database. This module is
the translation, and it is deliberately mechanical: there is no cleverness here
worth having, only consistency.

## Confirmation

Every voice booking is **read back before it is written**. Not because the
parser is unreliable but because the channel is: dictation mishears, and a
misheard date creates a real appointment in a real customer's week. So a spoken
booking is two-phase — stage, read back, then commit on an explicit yes.
"""

from __future__ import annotations

import time

from . import config

# ── number and date speech ───────────────────────────────────────────────────

_ONES = ("twelfth",)  # placeholder; ordinals are generated below

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh",
    12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
    16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
    20: "twentieth", 21: "twenty-first", 22: "twenty-second", 23: "twenty-third",
    24: "twenty-fourth", 25: "twenty-fifth", 26: "twenty-sixth",
    27: "twenty-seventh", 28: "twenty-eighth", 29: "twenty-ninth",
    30: "thirtieth", 31: "thirty-first",
}

_MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}


def _hour(minutes, part_of_day=True):
    """Clock minutes to something sayable.

    8:00 -> "8"        (not "8 o'clock", which sounds like a chime)
    8:30 -> "half past 8"
    Uses 12-hour speech with an explicit part of day, because "fourteen hundred"
    is military and "2" is ambiguous.
    """
    minutes = int(minutes or 0) % (24 * 60)
    hour24, minute = divmod(minutes, 60)
    suffix = ""
    if part_of_day:
        suffix = " in the morning" if hour24 < 12 else (
            " in the afternoon" if hour24 < 17 else " in the evening")
    hour = hour24 % 12 or 12

    if minute == 0:
        clock = f"{hour}"
    elif minute == 30:
        clock = f"half past {hour}"
    elif minute == 15:
        clock = f"quarter past {hour}"
    elif minute == 45:
        clock = f"quarter to {hour + 1}"
    else:
        clock = f"{hour}:{minute:02d}"
    return clock + suffix


def time_range(start_minutes, duration_minutes):
    """'9 in the morning to noon' — one range, not two clock readings."""
    end = int(start_minutes or 0) + int(duration_minutes or 0)
    # A range that ends on the hour keeps its part of day, so "to 11 in the
    # morning" is never mistaken for 11 at night.
    return f"{_hour(start_minutes)} to {_hour(end)}"


def say_date(date_iso):
    """'Thursday the twelfth of November' — a date, in the order it is spoken."""
    try:
        year, month, day = (int(p) for p in str(date_iso).split("-"))
    except (ValueError, AttributeError):
        return str(date_iso)

    import datetime
    weekday = datetime.date(year, month, day).strftime("%A")
    ordinal = _ORDINALS.get(day, str(day))
    return f"{weekday} the {ordinal} of {_MONTHS.get(month, month)}"


def _duration_words(minutes):
    minutes = int(minutes or 0)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} and a half hours" if rest == 30 else f"{hours} hours {rest} minutes"
    if hours:
        return "an hour" if hours == 1 else f"{hours} hours"
    return f"{rest} minutes"


_CLOCK_IN_TEXT = r"\b(\d{1,2}):(\d{2})\b"


def speak_embedded_times(text):
    """Rewrite clock times inside a generated string into speech.

    Tool messages are written for a screen and carry "08:00" and "08:12". read
    aloud, those come out as "eight hundred" or two loose numbers. This translates
    them in place, because the surrounding sentence is already worth keeping.
    """
    import re

    def swap(match):
        hour24, minute = int(match.group(1)), int(match.group(2))
        return _hour(hour24 * 60 + minute)

    out = re.sub(_CLOCK_IN_TEXT, swap, text or "")
    # "24 min" is written shorthand. Spoken, it is "min".
    out = re.sub(r"\bmin\b", "minutes", out)
    out = re.sub(r"\bmins\b", "minutes", out)
    out = out[:150].rstrip()
    if not out:
        return ""
    # A clause starts with a capital and ends with a stop, or it reads as a
    # continuation of the sentence it was appended to.
    return out[0].upper() + out[1:] + ("" if out.endswith((".", "!", "?")) else ".")


def _count(n, singular, plural=None):
    n = int(n or 0)
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n} {word}"


# ── the renderer ─────────────────────────────────────────────────────────────

def render(payload, max_items=None):
    """One payload to one spoken sentence (or two, at most).

    Unrecognised payloads fall back to a generic reply rather than leaking raw
    JSON at someone who is driving.
    """
    if not isinstance(payload, dict):
        return "I did not understand that."

    if payload.get("error"):
        return _error(payload)

    if payload.get("status") == "needs_clarification":
        return payload.get("question") or "I need one more detail."

    if payload.get("status") == "needs_confirmation":
        return payload.get("read_back") or "Should I go ahead?"

    if payload.get("status") == "needs_details":
        return payload.get("question") or "I need a name and an address."

    status = payload.get("status")

    if status == "booked":
        return _booked(payload)
    if status == "moved":
        return _moved(payload)
    if status == "cancelled":
        return _cancelled(payload)

    if "recommended" in payload:
        return _recommendation(payload)
    if "job_count" in payload and "week_start" in payload:
        return _week(payload)
    if "job_count" in payload:
        return _day(payload)
    if "clients" in payload:
        return _clients(payload)

    # Nothing recognised: say something true and short.
    return "Done."


def _error(payload):
    message = str(payload.get("error") or "")
    if "clashes" in message:
        return speak_embedded_times(message) + " Pick another time."
    if "no client matches" in message or "clients match" in message:
        return "I could not tell which client you meant. Say the name again?"
    spoken_message = speak_embedded_times(message)
    if spoken_message:
        # Capitalise the first letter: "That did not work. nothing booked" reads
        # as one broken sentence.
        # Lowercase the first letter only if it is not the pronoun "I".
        if not spoken_message.startswith(("I ", "I'", "I.")):
            spoken_message = spoken_message[0].lower() + spoken_message[1:]
        return "That did not work: " + spoken_message
    return "That did not work."


def clock_to_minutes(clock):
    """'08:00' -> 480. Returns None for anything that is not a clock string.

    Returning None rather than 0 matters: defaulting a missing time to midnight
    is how a renderer states a wrong time as confidently as a right one.
    """
    try:
        hour, minute = str(clock).split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return None


def _booked(payload):
    """A booking confirmation: who, when, and how long. Nothing else.

    The time is read from the clock strings the tool actually emits. If they are
    missing the sentence omits the time rather than inventing midnight.
    """
    client = payload.get("client") or {}
    name = client.get("name") or "the client"
    start = clock_to_minutes(payload.get("start"))
    end = clock_to_minutes(payload.get("end"))

    if start is not None and end is not None:
        when = f"{_hour(start)} to {_hour(end)}"
    else:
        when = _duration_words(payload.get("duration_min"))
    return f"Booked {name} for {say_date(payload.get('date'))}, {when}."


def _moved(payload):
    client = payload.get("client") or {}
    name = client.get("name") or "the client"
    return (f"Moved {name} from {say_date(payload.get('from_date'))} "
            f"to {say_date(payload.get('to_date'))}. "
            f"The time is unchanged.")


def _cancelled(payload):
    client = payload.get("client") or {}
    name = client.get("name") or "the client"
    count = len(payload.get("cancelled") or [])
    if count == 1:
        entry = (payload.get("cancelled") or [{}])[0]
        when = say_date(entry.get("date")) if entry.get("date") else "that day"
        return f"Cancelled {name} on {when}."
    return f"Cancelled {_count(count, 'appointment')} for {name}."


def _recommendation(payload):
    """The ONE best day. A spoken list of five ranked days is unusable."""
    best = (payload.get("recommended") or [None])[0]
    if not best:
        rejected = payload.get("rejected") or []
        if rejected:
            why = rejected[0].get("why")
            if isinstance(why, list):
                why = why[0] if why else ""
            return "Nothing works in that window. " + speak_embedded_times(str(why or ""))
        return "There is no day I would put that on in that window."

    parts = [f"{say_date(best.get('date'))} is best"]
    start = clock_to_minutes(best.get("start"))
    if start is not None:
        parts.append(f"starting {_hour(start, part_of_day=False)}")
    reasons = best.get("reasons") or []
    if reasons:
        # Engine reasons are written for a screen: "adds 24 min of driving".
        reason = speak_embedded_times(str(reasons[0])).rstrip(".")
        parts.append(reason[0].lower() + reason[1:] if reason else "")
    sentence = ", ".join(p for p in parts if p) + "."

    others = len(payload.get("recommended") or []) - 1
    if others > 0:
        # One number, and only because it tells them there is a choice.
        sentence += f" {_count(others, 'other day')} would also work."
    return sentence


def _day(payload):
    count = payload.get("job_count") or 0
    if not count:
        return f"Nothing booked {say_date(payload.get('date'))}."

    parts = [f"{_count(count, 'job')} {say_date(payload.get('date'))}"]
    if payload.get("drive_minutes"):
        parts.append(f"about {_duration_words(payload['drive_minutes'])} of driving")
    sentence = ", ".join(parts) + "."

    if payload.get("problems"):
        # The one thing worth interrupting for — spoken, not spliced raw.
        sentence += " " + speak_embedded_times(str(payload["problems"][0]))
    elif payload.get("fragile"):
        sentence += " It is tight, though."
    return sentence


def _week(payload):
    count = payload.get("job_count") or 0
    if not count:
        return "Nothing booked that week."

    parts = [_count(count, "job") + " that week"]
    if payload.get("hands_on_hours"):
        parts.append(f"{payload['hands_on_hours']} hours on site")
    if payload.get("drive_hours"):
        parts.append(f"{payload['drive_hours']} hours driving")
    sentence = ", ".join(parts) + "."

    if payload.get("infeasible_days"):
        n = len(payload["infeasible_days"])
        first = (payload["infeasible_days"][0] or {}).get("why") or []
        detail = f" {speak_embedded_times(str(first[0]))}" if first else ""
        sentence += f" {_count(n, 'day')} will not fit as booked.{detail}"
    advice = payload.get("improvement_available")
    if advice and advice.get("saving_minutes"):
        sentence += (f" Moving one job would save about "
                     f"{_duration_words(advice['saving_minutes'])} of driving.")
    del advice
    return sentence


def _clients(payload):
    clients = payload.get("clients") or []
    if not clients:
        return "There are no clients yet."
    names = [c.get("name") or "unnamed" for c in clients[:3]]
    if len(clients) <= 3:
        return f"You have {_count(len(clients), 'client')}: " + ", ".join(names) + "."
    return (f"You have {_count(len(clients), 'client')}, starting with "
            + ", ".join(names) + ".")


# ── read-back confirmation ───────────────────────────────────────────────────

# Staged bookings awaiting a spoken yes. In memory on purpose: a pending
# confirmation that survived a restart could be answered days later by someone
# who has forgotten saying anything, and it is one word ("yes") standing between
# a misheard sentence and a real appointment.
_PENDING = {}
_PENDING_TTL_SECONDS = 180


def stage_booking(parsed, client_row, when_iso, duration_min):
    """Hold a voice booking instead of writing it, and return the read-back.

    The read-back uses the operator's OWN date words where we have them, because
    hearing "next Thursday" back is how they catch a mishearing. "2026-11-12" read
    aloud sounds authoritative and confirms nothing.
    """
    token = f"vb{int(time.time() * 1000) % 10_000_000}"
    name = (client_row or {}).get("name") or "the client"

    spoken_when = parsed.get("when_text") or say_date(when_iso)
    import datetime
    try:
        start_hint = datetime.date.fromisoformat(when_iso).strftime("%A")
    except (ValueError, TypeError):
        start_hint = ""

    weekday_already_said = start_hint and start_hint.lower() in spoken_when.lower()
    read_back = (
        f"That is {name}, {spoken_when}"
        + (f" — {start_hint}" if start_hint and not weekday_already_said else "")
        + f", for {_duration_words(duration_min)}. Should I book it?"
    )

    _PENDING[token] = {
        "client": dict(client_row) if client_row else {},
        "when": when_iso,
        "duration_min": duration_min,
        "staged_at": time.time(),
    }
    _expire()
    return token, read_back


def take_booking(token):
    """Pop a staged booking, or None if it is unknown or stale."""
    _expire()
    return _PENDING.pop(token, None)


def _expire():
    cutoff = time.time() - _PENDING_TTL_SECONDS
    for token in [t for t, v in _PENDING.items() if v.get("staged_at", 0) < cutoff]:
        _PENDING.pop(token, None)


def pending_count():
    """For tests, and for a host that wants to report a forgotten stage."""
    _expire()
    return len(_PENDING)
