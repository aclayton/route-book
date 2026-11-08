"""Natural language, resolved deterministically in Python.

The LLM layer (M5) turns a sentence into an intent. **This module is what
resolves it** — and it does so with no model, no network and no clock it didn't
ask for.

Three rules, and each exists because the alternative is a silent wrong answer:

1. **`today` is always injected, never read from the system clock.** A parser
   that calls `datetime.now()` internally cannot be tested, and a test for
   "next Thursday" that passes only on some days is worse than no test.
2. **The grammar is bounded.** Anything it cannot read returns `None`. A parser
   that guesses is a parser that books the wrong week, and the operator finds
   out by standing in an empty driveway.
3. **"next Thursday" means the next Thursday strictly after today.** This is
   genuinely ambiguous in English — British usage often means "the Thursday of
   next week", American often means "the coming Thursday". The strict-after
   reading is chosen, documented here, and reported in the tool output so the
   operator can see which day it picked before confirming.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Weekday name -> Python weekday() index (Monday=0)
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "a": 1, "an": 1, "couple": 2,
}


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower().rstrip(".,!?"))


# ── Dates ───────────────────────────────────────────────────────────────────

def parse_when(text, today):
    """Resolve a date expression to a `date`, or None.

    None means "I do not know what day that is", and the caller must ask. It
    never means "probably today".
    """
    if not text:
        return None
    t = _clean(text)

    # ISO date — unambiguous, so it wins over everything else.
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # today / tomorrow / tonight / the day after tomorrow
    if t in {"today", "tonight"}:
        return today
    if t in {"tomorrow", "tmrw", "tomorow"}:
        return today + timedelta(days=1)
    if t in {"the day after tomorrow", "day after tomorrow"}:
        return today + timedelta(days=2)

    # in N days / weeks / months
    m = re.fullmatch(r"in (\d+|[a-z]+) (day|days|week|weeks|month|months)", t)
    if m:
        n = _as_number(m.group(1))
        if n is None:
            return None
        unit = m.group(2)
        if unit.startswith("day"):
            return today + timedelta(days=n)
        if unit.startswith("week"):
            return today + timedelta(weeks=n)
        # Months are not a fixed number of days. 30-day approximation is wrong
        # often enough that it is refused rather than approximated.
        return None

    # "(a) week on Tuesday" / "Tuesday week" — British, means the named weekday
    # in the week after next occurrence.
    m = re.fullmatch(r"(?:a )?week (?:on |come )?([a-z]+)", t) or \
        re.fullmatch(r"([a-z]+) week", t)
    if m and m.group(1) in _WEEKDAYS:
        base = _next_weekday(today, _WEEKDAYS[m.group(1)], strictly_after=True)
        return base + timedelta(weeks=1)

    # next week / this week / next month
    if t in {"next week", "the week after"}:
        return today + timedelta(days=7 - today.weekday()) if t == "next week" else None
    if t == "next month":
        # Refused for the same reason as "in N months" — a month is not 30 days.
        return None

    # tomorrow/friday-style: (this|next|coming|on)? <weekday>
    m = re.fullmatch(r"(this |next |coming |on )?([a-z]+)", t)
    if m and m.group(2) in _WEEKDAYS:
        idx = _WEEKDAYS[m.group(2)]
        qualifier = (m.group(1) or "").strip()
        if qualifier == "this":
            # "this Thursday" = the coming one, which may be today.
            return _next_weekday(today, idx, strictly_after=False)
        if qualifier in {"next", "coming"}:
            return _next_weekday(today, idx, strictly_after=True)
        return _next_weekday(today, idx, strictly_after=True)

    # "on the 13th" / "the 13th"
    m = re.fullmatch(r"(?:on )?the (\d{1,2})(?:st|nd|rd|th)", t)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            return None
        candidate = _safe_date(today.year, today.month, day)
        if candidate and candidate >= today:
            return candidate
        # Roll to next month if this month's has passed.
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return _safe_date(year, month, day)

    # "Nov 20" / "November 20" / "20 Nov"
    m = re.fullmatch(r"([a-z]+) (\d{1,2})", t) or re.fullmatch(r"(\d{1,2}) ([a-z]+)", t)
    if m:
        a, b = m.group(1), m.group(2)
        month_name, day_str = (a, b) if a in _MONTHS else (b, a)
        if month_name in _MONTHS:
            month, day = _MONTHS[month_name], int(day_str)
            candidate = _safe_date(today.year, month, day)
            if candidate and candidate >= today:
                return candidate
            return _safe_date(today.year + 1, month, day)

    # Unreadable. Ask, don't guess.
    return None


def _as_number(token):
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_weekday(today, target_idx, strictly_after):
    """The next occurrence of a weekday.

    strictly_after=False allows today to match ("this Thursday" on a Thursday).
    strictly_after=True skips today, which is what "next Thursday" means here.
    """
    delta = (target_idx - today.weekday()) % 7
    if delta == 0 and strictly_after:
        delta = 7
    return today + timedelta(days=delta)


# ── Durations ───────────────────────────────────────────────────────────────

_DURATION_UNITS = {
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
}

_NAMED_DURATIONS = {"half a day": 240, "all day": 480, "a full day": 480}


def parse_duration(text):
    """'3 hrs' / '90 min' / 'half a day' -> minutes, or None."""
    t = _clean(text)
    if not t:
        return None
    if t in _NAMED_DURATIONS:
        return _NAMED_DURATIONS[t]

    # Spelled-out numbers first. "three hours" would otherwise reach the numeric
    # loop below, fail float("three"), and be reported as unreadable — a silent
    # None for a perfectly good duration.
    m = re.fullmatch(r"([a-z]+) ?(hour|hours|hr|hrs|minute|minutes|min|mins)", t)
    if m:
        n = _as_number(m.group(1))
        if n is not None:
            return n * _DURATION_UNITS[m.group(2)]
        return None

    compact = t.replace(" ", "")
    for unit, mult in sorted(_DURATION_UNITS.items(), key=lambda kv: -len(kv[0])):
        if compact.endswith(unit):
            number = compact[: -len(unit)]
            try:
                return int(float(number) * mult)
            except ValueError:
                return None

    try:
        return int(float(compact))  # a bare number means minutes
    except ValueError:
        return None


# ── Client resolution ───────────────────────────────────────────────────────

def _edit_distance(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def find_named_clients(text, clients, max_matches=3):
    """Find clients named anywhere inside a sentence.

    `resolve_client` matches a whole string against a name. That is right when
    the input *is* the name, and useless when it is a sentence — "recommend the
    best day for bala in two weeks" is not equal to "bala".

    So this scans candidate spans instead: every single word, then every
    adjacent pair (which is what catches "mrs harris"). The best-ranked match
    wins, and ties are returned together — never a single pick.
    """
    if not text:
        return []
    words = re.findall(r"[A-Za-z][A-Za-z.\-']*", text)
    if not words:
        return []

    candidates = [(w,) for w in words]
    candidates += [(words[i], words[i + 1]) for i in range(len(words) - 1)]

    best_rank = None
    found = []
    for span in candidates:
        matches = resolve_client(" ".join(span), clients, max_matches=max_matches)
        if not matches:
            continue
        # Rank by how strong the match was: an exact hit beats a fuzzy one, so a
        # pair that resolves exactly should not be displaced by a looser single.
        rank = _match_rank(" ".join(span), matches[0])
        if best_rank is None or rank < best_rank:
            best_rank = rank
            found = matches
    return found


def _match_rank(needle, client):
    """How good a match this was. Lower is better."""
    n = _clean(needle)
    c = dict(client)
    if n == _clean(c["name"]):
        return 0
    if n in [_clean(a) for a in (c.get("aliases") or "").split(",") if a.strip()]:
        return 1
    if n in [_clean(a) for a in (c.get("spoken_names") or "").split(",") if a.strip()]:
        return 2
    return 3


def resolve_client(text, clients, max_matches=3):
    """Find the clients a spoken or typed name could refer to.

    Returns a list, never a single pick. Two clients called Harris are
    indistinguishable by name and obvious by town, so the caller is handed both
    and decides — or asks. Colour is deliberately absent: this is a lookup, not
    a decision.

    Order of confidence: exact name, alias, speech-to-text spelling, then fuzzy.
    """
    if not text:
        return []
    needle = _clean(text)
    if not needle:
        return []

    exact, alias, spoken, fuzzy = [], [], [], []

    for c in clients:
        name = _clean(c["name"])
        aliases = [_clean(a) for a in (dict(c).get("aliases") or "").split(",") if a.strip()]
        spoken_names = [_clean(s) for s in (dict(c).get("spoken_names") or "").split(",") if s.strip()]

        if needle == name:
            exact.append(c)
            continue
        if needle in aliases:
            alias.append(c)
            continue
        if needle in spoken_names:
            spoken.append(c)
            continue

        # Token match: "mrs harris" should find "Mrs. Harris".
        name_tokens = set(re.findall(r"[a-z]+", name))
        needle_tokens = set(re.findall(r"[a-z]+", needle))
        if needle_tokens and needle_tokens <= name_tokens:
            alias.append(c)
            continue

        if _edit_distance(needle, name) <= 2 or any(
            _edit_distance(needle, t) <= 1 for t in name_tokens if len(t) > 2
        ):
            fuzzy.append(c)

    ordered = exact + alias + spoken + fuzzy
    # Deduplicate, preserving order
    seen, out = set(), []
    for c in ordered:
        key = dict(c)["id"]
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:max_matches]


# ── The verb ────────────────────────────────────────────────────────────────

_RECOMMEND_PATTERNS = [
    r"\brecommend\b", r"\bbest day\b", r"\bwhen should\b", r"\bwhich day\b",
    r"\bwhat day\b", r"\bwhen can\b", r"\bwhen'?s good\b", r"\bshould i do\b",
]
_LIST_PATTERNS = [
    r"\bwhat does\b", r"\bwhat'?s on\b", r"\bshow me\b", r"\bmy day\b",
    r"\bmy week\b", r"\bhow does .* look\b",
]


# Patterns for intents the original rules had no words for. Kept beside
# parse_verb so the whole vocabulary is in one place.
# "what's on today", "what does tomorrow look like" — a question about the diary
# with no verb word at all. Without this the rules return unknown and the whole
# sentence goes to the model, which is both slower and less private.
_DIARY_QUESTION_PATTERNS = [
    r"\bwhat.?s (on|happening|booked|going on)\b",
    r"\bwhat (is|have i got|do i have)\b.{0,20}\b(on|booked|tomorrow|today)\b",
    r"\bmy (day|week|schedule)\b",
]

_CLIENT_PATTERNS = [
    r"\b(list|show|who).{0,12}\bclients?\b",
    r"\bmy clients\b",
    r"\bcustomer list\b",
]

_CALENDAR_PATTERNS = [
    r"\b(ical|ics|calendar feed)\b",
    r"\b(subscribe|export).{0,12}\bcalendar\b",
]

_DIARY_PATTERNS = [
    r"\b(add|new|create).{0,12}\bclient\b",
    r"\bnew customer\b",
]


def parse_verb(text):
    """Classify the request. Returns one of:

    book | recommend | list | move | cancel | unknown

    Getting this backwards is the main failure mode: if the operator asks
    *which* day, picking one and booking it is worse than asking.
    """
    t = _clean(text)
    if not t:
        return "unknown"
    if re.search(r"\bmove\b|\breschedule\b|\bpush\b|\bchange .* to\b", t):
        return "move"
    if re.search(r"\bcancel\b|\bdelete\b|\bskip\b", t):
        return "cancel"
    for p in _RECOMMEND_PATTERNS:
        if re.search(p, t):
            return "recommend"
    for p in _CLIENT_PATTERNS:
        if re.search(p, t):
            return "list_clients"
    for p in _CALENDAR_PATTERNS:
        if re.search(p, t):
            return "export_calendar"
    for p in _DIARY_PATTERNS:
        if re.search(p, t):
            # "add a client" is a write, so it is checked before the generic
            # booking patterns — otherwise "add" would classify it as a booking.
            return "add_client"
    for p in _DIARY_QUESTION_PATTERNS:
        if re.search(p, t):
            return "list"
    for p in _LIST_PATTERNS:
        if re.search(p, t):
            return "list"
    if re.search(r"\bbook\b|\bschedule\b|\badd\b|\bbring\b", t):
        return "book"
    return "unknown"


def parse_request(text, today, clients):
    """The whole deterministic path: one sentence in, one intent out.

    This is the fallback for the conversational layer and the test oracle for
    it — being pure code, it can be exercised exhaustively, which a model
    cannot.
    """
    t = _clean(text)
    verb = parse_verb(t)

    # A client name may be the whole input ("bala") or buried in a sentence
    # ("recommend the best day for bala in two weeks"). find_named_clients
    # handles both, so there is one resolution path rather than two that drift.
    matches = find_named_clients(t, clients) if clients else []

    when = None
    duration = None
    when_text = None

    # Everything after the first comma is treated as the detail clause, which is
    # how the shorthand is actually spoken.
    detail = t
    if "," in t:
        head, _, tail = t.partition(",")
        detail = tail
        if not matches:
            matches = find_named_clients(head, clients)

    for chunk in re.split(r",| and ", detail):
        chunk = chunk.strip()
        if not chunk:
            continue
        if duration is None:
            d = parse_duration(chunk)
            if d:
                duration = d
                continue
        if when is None:
            w = parse_when(chunk, today)
            if w:
                when = w
                # Keep the words the date came FROM. A read-back that says
                # "Thursday the twelfth of November" sounds authoritative and
                # confirms nothing; "next Thursday" is what a mishearing shows
                # up in.
                when_text = chunk
                continue

    # If the detail clause gave nothing, try the whole string for a date — and
    # try PHRASES, not just single words. "in two weeks" is three words, so a
    # word-by-word search never sees it; the phrase has to be offered intact.
    if when is None:
        words = re.split(r"[\s,]+", t)
        for size in (4, 3, 2, 1):
            for start in range(len(words) - size + 1):
                phrase = " ".join(words[start:start + size])
                w = parse_when(phrase, today)
                if w:
                    when = w
                    when_text = phrase
                    break
            if when is not None:
                break

    # The shorthand — "bala, next Thursday, 3 hrs" — carries no verb word at all.
    # If a client and a date resolved, the operator is booking; that is the only
    # thing that sentence can mean. Inferring it here is what makes the flagship
    # form work, and it cannot misfire on an interrogative because those were
    # classified above and never reach this branch.
    if verb == "unknown" and matches and when is not None:
        verb = "book"

    return {
        "verb": verb,
        "clients": [dict(c) for c in matches],
        "when": when.isoformat() if when else None,
        # The expression, verbatim, so a read-back can echo what was actually
        # said rather than an authoritative-sounding resolved date.
        "when_text": when_text,
        "duration_min": duration,
        "raw": text,
    }
