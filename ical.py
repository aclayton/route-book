"""iCalendar export — the M4 feed.

Writes one `.ics` file that Apple Calendar, Google Calendar, Fastmail, Nextcloud
and Outlook can all subscribe to or import. **No OAuth, no API key, nothing to
register** — that is the whole reason this was chosen for v1.

The hard part is not generating a calendar. It is generating one that every
client parses, and the four things that silently break them are all in here:

1. **Line folding.** RFC 5545 caps a line at 75 octets and continues it with a
   leading space. Clients that meet an over-long line variously truncate it,
   mangle it, or reject the file. Folding is octet-aware, not character-aware,
   because a client name with an accent is more than one byte per character and
   folding mid-character produces invalid UTF-8.
2. **CRLF endings.** The spec says CRLF. Unix LF works in Apple Calendar and
   fails in Outlook, which is the worst combination — the author sees it working.
3. **Escaping.** A comma in a client's name ends the field early; a semicolon
   ends it differently; a backslash escapes what follows. `Harris, Mrs.` breaks
   a naive generator, and the failure is a garbled calendar, not an error.
4. **Timezones.** A bare local time is meaningless across DST. The feed emits
   `DTSTART;TZID=...` with a matching `VTIMEZONE` block so the wall-clock time
   the operator booked is the wall-clock time their phone shows, in June and in
   December.

**UID stability matters as much as validity.** A subscribed client re-reads the
file and matches events by UID. A generator that derives UIDs from position or
from a timestamp will duplicate every event on each export.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from . import config, db

PRODID = "-//route-book//Hermes plugin//EN"

# A minimal but correct VTIMEZONE for the configured zone. Only the rules for
# the zone actually in use are emitted; a wrong timezone block is worse than none.
_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:{tzid}
X-LIC-LOCATION:{tzid}
BEGIN:DAYLIGHT
TZOFFSETFROM:{std_offset}
TZOFFSETTO:{dst_offset}
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:{dst_offset}
TZOFFSETTO:{std_offset}
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def _fold(line: str) -> str:
    """Fold a content line to 75 octets, continuation lines start with a space.

    Octet-aware on purpose: folding by character count would split a multi-byte
    character and emit invalid UTF-8, which some clients handle by dropping the
    rest of the line — a corruption that looks like a data problem.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    out = []
    current = b""
    for char in line:
        chunk = char.encode("utf-8")
        # 75 on the first line, 74 on continuations (the leading space counts).
        limit = 75 if not out else 74
        if len(current) + len(chunk) > limit:
            out.append(current.decode("utf-8"))
            current = chunk
        else:
            current += chunk
    if current:
        out.append(current.decode("utf-8"))
    return "\r\n ".join(out)


def _escape(text) -> str:
    """Escape a TEXT value per RFC 5545 §3.3.11.

    Order matters: the backslash must be escaped first, or escaping a later
    character would introduce a backslash that then gets escaped again.
    """
    if text is None:
        return ""
    text = str(text)
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def _uid(appointment_id, date_iso, slot) -> str:
    """A stable UID for an appointment.

    Derived from the row id, not from position or from export time. A UID that
    changes between exports makes a subscribed client show every job twice, and
    the operator then deletes the calendar rather than the duplicates.
    """
    raw = f"{appointment_id}:{date_iso}:{slot}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:16]
    return f"{digest}@route-book"


def _event(appt, tzid: str):
    """One VEVENT. Times are local wall-clock with a TZID, never bare."""
    date_iso = appt["date"]
    start_local = appt["start_local"]
    duration = appt["duration_min"] or 180

    try:
        start = datetime.strptime(f"{date_iso} {start_local}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None  # unparseable rows are skipped, never guessed at

    end = start + timedelta(minutes=duration)

    # The title is the customer's name, by explicit decision (see the design
    # doc): legible on a lock screen, and it travels to whatever server the
    # calendar app syncs through. `initials` is one setting away if that changes.
    name = appt["client_name"] if appt["client_name"] is not None else "Job"
    title_mode = getattr(config, "ICAL_TITLE_MODE", "full")
    if title_mode == "initials":
        summary = f"{name[:1]} — job"
    elif title_mode == "generic":
        summary = f"Job — {duration // 60}h" + (f"{duration % 60:02d}" if duration % 60 else "")
    else:
        summary = f"{name} — job"

    # No LOCATION field, deliberately: it would put a customer's street address
    # into the calendar, and on a subscribed feed, onto a third party's servers.
    description = "Booked by route-book. Details stay on the machine that made this."

    lines = [
        "BEGIN:VEVENT",
        f"UID:{_uid(appt['id'], date_iso, start_local)}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;TZID={tzid}:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND;TZID={tzid}:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "SEQUENCE:0",
        "END:VEVENT",
    ]
    return lines


def build_calendar(rows, tzid=None, calendar_name="route-book") -> str:
    """Render appointment rows into an iCalendar document.

    `rows` are sqlite rows with id, date, start_local, duration_min,
    client_name. Returns the complete document as a string with CRLF endings.
    """
    tzid = tzid or config.TIMEZONE

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        f"X-WR-TIMEZONE:{tzid}",
        # Refresh hint: clients treat this loosely, which is fine — the file is
        # regenerated on every change anyway.
        "X-PUBLISHED-TTL:PT15M",
    ]

    if tzid == "America/Toronto":
        lines.extend(_VTIMEZONE.format(
            tzid=tzid, std_offset="-0500", dst_offset="-0400").splitlines())
    else:
        # A zone we have no rules for: emit floating times rather than a wrong
        # VTIMEZONE. A wrong timezone block silently shifts every appointment.
        tzid = None

    for row in rows:
        if tzid is None:
            appt = dict(row)
            appt["start_local"] = appt["start_local"]
            event = _event_floating(appt)
        else:
            event = _event(dict(row), tzid)
        if event:
            lines.extend(event)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _event_floating(appt):
    """A VEVENT with no timezone — used only when the zone is unknown.

    Floating times mean 'whatever the local clock says', which is right for a
    single-zone operator and honest about what we know. Emitting a guessed
    VTIMEZONE would be a confident lie.
    """
    date_iso, start_local = appt["date"], appt["start_local"]
    duration = appt["duration_min"] or 180
    try:
        start = datetime.strptime(f"{date_iso} {start_local}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    end = start + timedelta(minutes=duration)
    name = appt["client_name"] or "Job"
    return [
        "BEGIN:VEVENT",
        f"UID:{_uid(appt['id'], date_iso, start_local)}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
        f"SUMMARY:{_escape(name + ' — job')}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
    ]


def export(conn=None, path=None, days_ahead=180) -> dict:
    """Write the feed to disk. Returns a summary.

    Defaults to the plugin's own data directory rather than the vault: writing
    into a synced folder by default would copy a customer list to every device
    the operator owns without them asking. Point `ICAL_PATH` at the vault to do
    that deliberately.
    """
    from plugins.plugin_storage import plugin_data_dir

    own_conn = conn is None
    conn = conn or db.connect()
    try:
        today = date.today()
        end = today + timedelta(days=days_ahead)
        rows = db.appointments_between(conn, today.isoformat(), end.isoformat())
        document = build_calendar(rows)

        target = path or config.ICAL_PATH
        if not target:
            target = str(plugin_data_dir("route-book") / "route-book.ics")

        from pathlib import Path
        out = Path(target)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8", newline="")

        return {
            "status": "written",
            "path": str(out),
            "events": len(rows),
            "bytes": len(document.encode("utf-8")),
            "timezone": config.TIMEZONE,
        }
    finally:
        if own_conn:
            conn.close()
