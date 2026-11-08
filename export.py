"""Data export — M7.

Everything the plugin knows about a business, in files the operator can read,
open in a spreadsheet, and take elsewhere.

## Why this is a feature and not a checkbox

A scheduling tool accumulates a customer list, a history of visits, and the record
of which towns are worth driving to. That is the operator's business, and it
should not be trapped in one program's private database. Export is how that stays
true — and it is also the honest answer to "what happens if I stop using this",
which is a question worth answering before someone has to ask it.

Exports are **CSV and JSON**, both plain text, both openable by anything.

## What is not exported

No door codes, alarm codes, or entry instructions, because **the plugin never
stores them** — that is a design rule, not a disclaimer. The export cannot leak
what was never written down.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

from . import config, db

_CLIENT_COLUMNS = (
    "id", "name", "address", "city", "postal",
    "lat", "lon", "default_duration_min", "default_price",
    "cadence", "preferred_days", "preferred_window", "active",
)

_APPOINTMENT_COLUMNS = (
    "id", "client_id", "client_name", "client_city",
    "date", "start_local", "duration_min", "price", "status", "notes",
)


def _dicts(rows, columns):
    """Rows to plain dicts, keeping only the columns we intend to publish.

    Filtered deliberately: a column added later by schema migration should not
    appear in an export until someone decides it belongs there.
    """
    out = []
    for row in rows:
        mapping = db.as_dict(row)
        out.append({col: mapping.get(col) for col in columns})
    return out


def clients(conn):
    return _dicts(db.find_clients(conn, active_only=False), _CLIENT_COLUMNS)


def appointments(conn, days_back=365, days_ahead=365):
    """Every appointment, past and future, with the client's name attached.

    The name is joined in rather than left as an id, because an export full of
    foreign keys is a database dump rather than a record a person can read.
    """
    start = (date.today() - timedelta(days=days_back)).isoformat()
    end = (date.today() + timedelta(days=days_ahead)).isoformat()
    rows = db.appointments_between(conn, start, end, include_cancelled=True)

    by_id = {}
    for client in db.find_clients(conn, active_only=False):
        by_id[client["id"]] = db.as_dict(client)

    enriched = []
    for row in rows:
        record = db.as_dict(row)
        client = by_id.get(row["client_id"]) or {}
        record["client_name"] = client.get("name")
        record["client_city"] = client.get("city")
        enriched.append(record)
    return _dicts(enriched, _APPOINTMENT_COLUMNS)


def to_csv(records, columns):
    """One CSV, with a header even when there are no rows.

    An empty file with no header looks like a failure. A header with no rows
    looks like an empty schedule, which is what it is.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(record)
    return buffer.getvalue()


def export(conn=None, directory=None, days_back=365, days_ahead=365):
    """Write both tables as CSV and as one JSON bundle. Returns a summary."""
    from plugins.plugin_storage import plugin_data_dir

    own = conn is None
    conn = conn or db.connect()
    try:
        target = Path(directory) if directory else plugin_data_dir("route-book") / "export"
        target.mkdir(parents=True, exist_ok=True)

        client_records = clients(conn)
        appointment_records = appointments(conn, days_back, days_ahead)

        written = {}

        clients_csv = target / "clients.csv"
        clients_csv.write_text(to_csv(client_records, _CLIENT_COLUMNS),
                               encoding="utf-8", newline="")
        written["clients_csv"] = str(clients_csv)

        appts_csv = target / "appointments.csv"
        appts_csv.write_text(to_csv(appointment_records, _APPOINTMENT_COLUMNS),
                             encoding="utf-8", newline="")
        written["appointments_csv"] = str(appts_csv)

        bundle = target / "route-book.json"
        stamps = date.today().isoformat()
        bundle.write_text(json.dumps({
            "exported": stamps,
            "timezone": config.TIMEZONE,
            "note": ("No door codes, alarm codes or entry instructions exist in "
                     "this data — the plugin does not store them."),
            "clients": client_records,
            "appointments": appointment_records,
        }, indent=2, default=str), encoding="utf-8")
        written["json"] = str(bundle)

        return {
            "status": "written",
            "directory": str(target),
            "files": written,
            "counts": {"clients": len(client_records),
                       "appointments": len(appointment_records)},
        }
    finally:
        if own:
            conn.close()
