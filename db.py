"""Storage: schema, migrations, and query helpers.

The database lives in Hermes's per-plugin data directory, which survives
`hermes plugins update` and `remove`. It is deliberately **not** the plugin's
install directory — that gets deleted on remove and git-pulled on update.

Two tables in M1. Clients are the expensive thing to type in, so the schema is
generous about what it accepts and strict about what it requires: a name and a
place. Everything else can arrive later.

Coordinates are cached forever. Geocoding is the one call that leaves the
machine, so it happens once per address and never again unless the address
changes.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time

from plugins.plugin_storage import plugin_db, plugin_data_dir  # noqa: F401

PLUGIN_NAME = "route-book"

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    aliases         TEXT NOT NULL DEFAULT '',   -- comma-separated
    spoken_names    TEXT NOT NULL DEFAULT '',   -- comma-separated; STT spellings
    address         TEXT NOT NULL,
    city            TEXT NOT NULL DEFAULT '',
    lat             REAL,
    lon             REAL,
    geocode_source  TEXT,                       -- provider that resolved it
    geocoded_at     REAL,
    default_duration_min INTEGER,
    default_price   REAL,
    cadence         TEXT NOT NULL DEFAULT 'on_demand',
    preferred_days  TEXT NOT NULL DEFAULT '',
    preferred_window TEXT NOT NULL DEFAULT 'any',
    notes           TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(name);
CREATE INDEX IF NOT EXISTS idx_clients_city ON clients(city);

CREATE TABLE IF NOT EXISTS appointments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id),
    date          TEXT NOT NULL,              -- ISO YYYY-MM-DD, LOCAL date
    start_local   TEXT NOT NULL,              -- HH:MM, local wall clock
    tz            TEXT NOT NULL,              -- IANA zone, e.g. America/Toronto
    duration_min  INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'planned',
    source        TEXT NOT NULL DEFAULT 'manual',
    notes         TEXT NOT NULL DEFAULT '',
    gcal_event_id TEXT,                       -- makes calendar sync idempotent (M4)
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appt_date ON appointments(date);
CREATE INDEX IF NOT EXISTS idx_appt_client ON appointments(client_id);

CREATE TABLE IF NOT EXISTS travel_matrices (
    signature   TEXT PRIMARY KEY,   -- hash of the ordered point set
    payload     TEXT NOT NULL,      -- the durations (and distances) as JSON
    approximate INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,
    built_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,                  -- named_area | city_list | radius
    payload     TEXT NOT NULL DEFAULT '',       -- what the user said
    west        REAL, south REAL, east REAL, north REAL,
    created_at  REAL NOT NULL
);
"""


@contextlib.contextmanager
def open_db():
    """Handlers use this. Closes the connection on the way out.

    `plugin_db()` returns a live connection and nothing closed it, so every tool
    call leaked one. Under a long-running gateway that is a file-descriptor leak
    that only shows up after hours of use — which is exactly when it is hardest
    to attribute.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def connect() -> sqlite3.Connection:
    """Open the plugin database, applying migrations.

    `plugin_db` handles the directory, WAL mode and thread-safety. Row access is
    by column name here because every caller reads two or three named fields and
    indexing by position is how those silently swap.
    """
    conn = plugin_db(PLUGIN_NAME)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply forward migrations.

    Idempotent: `CREATE TABLE IF NOT EXISTS` plus a version row means a fresh
    database and an existing one take the same path. A migration that only runs
    once is a migration that has never been tested from scratch.
    """
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


# ── Clients ─────────────────────────────────────────────────────────────────

def add_client(conn, *, name, address, city="", **fields):
    """Insert a client and return its id.

    Raises ValueError on the two things that are genuinely required — a name and
    an address — rather than writing a half-record that fails later at the point
    of use, where the error is much harder to explain.
    """
    if not (name or "").strip():
        raise ValueError("a client needs a name")
    if not (address or "").strip():
        raise ValueError("a client needs an address — it is what travel is computed from")

    now = time.time()
    cols = {
        "name": name.strip(),
        "address": address.strip(),
        "city": (city or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    for key in (
        "aliases", "spoken_names", "lat", "lon", "geocode_source", "geocoded_at",
        "default_duration_min", "default_price", "cadence", "preferred_days",
        "preferred_window", "notes", "active",
    ):
        if key in fields and fields[key] is not None:
            cols[key] = fields[key]

    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT INTO clients ({','.join(cols)}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    conn.commit()
    return cur.lastrowid


def update_client(conn, client_id, **fields):
    """Update named fields on a client. Unknown keys are ignored, not fatal."""
    allowed = {
        "name", "aliases", "spoken_names", "address", "city", "lat", "lon",
        "geocode_source", "geocoded_at", "default_duration_min", "default_price",
        "cadence", "preferred_days", "preferred_window", "notes", "active",
    }
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return 0
    sets["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = ?" for k in sets)
    cur = conn.execute(
        f"UPDATE clients SET {assignments} WHERE id = ?",
        (*sets.values(), client_id),
    )
    conn.commit()
    return cur.rowcount


def find_clients(conn, search=None, city=None, active_only=True):
    """Search clients by name, alias or city.

    Deliberately a LIKE scan rather than FTS: a working list is dozens of rows,
    and a substring match behaves the way a person expects when they type half a
    name. FTS would need tokenising that gets *less* predictable, not more.
    """
    sql = "SELECT * FROM clients WHERE 1=1"
    params: list = []
    if active_only:
        sql += " AND active = 1"
    if city:
        sql += " AND city LIKE ?"
        params.append(f"%{city}%")
    if search:
        sql += " AND (name LIKE ? OR aliases LIKE ? OR spoken_names LIKE ? OR city LIKE ?)"
        params.extend([f"%{search}%"] * 4)
    sql += " ORDER BY name COLLATE NOCASE"
    return conn.execute(sql, params).fetchall()


def get_client(conn, client_id):
    return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def ungeocoded(conn):
    """Clients with no coordinates — the ones a matrix refresh would have to call out for."""
    return conn.execute(
        "SELECT * FROM clients WHERE active = 1 AND (lat IS NULL OR lon IS NULL)"
    ).fetchall()


# ── Areas ───────────────────────────────────────────────────────────────────

def set_area(conn, *, name, kind, payload="", bbox=None):
    """Create or replace a named service area.

    Replacing rather than erroring on a duplicate name: an operator who re-runs
    setup with a corrected region means to correct it, not to collect a second
    area with the same label.
    """
    west = south = east = north = None
    if bbox:
        west, south, east, north = bbox
    conn.execute(
        """INSERT INTO areas (name, kind, payload, west, south, east, north, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             kind=excluded.kind, payload=excluded.payload,
             west=excluded.west, south=excluded.south,
             east=excluded.east, north=excluded.north""",
        (name, kind, payload, west, south, east, north, time.time()),
    )
    conn.commit()


def list_areas(conn):
    return conn.execute("SELECT * FROM areas ORDER BY name COLLATE NOCASE").fetchall()


# ── Appointments ────────────────────────────────────────────────────────────

def add_appointment(conn, *, client_id, date_iso, start_local, duration_min,
                    tz, source="manual", notes=""):
    """Insert an appointment. Returns its id.

    Date is a LOCAL date and the zone is stored alongside it. Storing a bare UTC
    instant instead would shift every appointment by an hour twice a year, and
    the operator would discover it by arriving early.
    """
    now = time.time()
    cur = conn.execute(
        """INSERT INTO appointments
             (client_id, date, start_local, tz, duration_min, source, notes,
              created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (client_id, date_iso, start_local, tz, duration_min, source, notes, now, now),
    )
    conn.commit()
    return cur.lastrowid


def appointments_on(conn, date_iso, include_cancelled=False):
    """A day's appointments, in clock order."""
    sql = """SELECT a.*, c.name AS client_name, c.city AS client_city
             FROM appointments a JOIN clients c ON c.id = a.client_id
             WHERE a.date = ?"""
    if not include_cancelled:
        sql += " AND a.status = 'planned'"
    sql += " ORDER BY a.start_local"
    return conn.execute(sql, (date_iso,)).fetchall()


def appointments_between(conn, start_iso, end_iso, include_cancelled=False):
    sql = """SELECT a.*, c.name AS client_name, c.city AS client_city
             FROM appointments a JOIN clients c ON c.id = a.client_id
             WHERE a.date >= ? AND a.date <= ?"""
    if not include_cancelled:
        sql += " AND a.status = 'planned'"
    sql += " ORDER BY a.date, a.start_local"
    return conn.execute(sql, (start_iso, end_iso)).fetchall()


def cancel_appointment(conn, appointment_id):
    cur = conn.execute(
        "UPDATE appointments SET status='cancelled', updated_at=? WHERE id=?",
        (time.time(), appointment_id),
    )
    conn.commit()
    return cur.rowcount


def appointments_for_client(conn, client_id, include_cancelled=False):
    sql = "SELECT * FROM appointments WHERE client_id = ?"
    if not include_cancelled:
        sql += " AND status = 'planned'"
    sql += " ORDER BY date, start_local"
    return conn.execute(sql, (client_id,)).fetchall()
