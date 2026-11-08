"""Tool handlers.

Three rules, from the Hermes plugin contract — every handler:

1. **Signature** `def handler(args: dict, **kwargs) -> str`
2. **Returns a JSON string**, successes and errors alike
3. **Never raises.** A raise is a broken tool call for the model; catching and
   returning an error object lets it read the problem and try something else.

Status: **M1** — clients, areas and geocoding are real. Booking, day/week views,
recommendations, the matrix and the conversational layer are still stubs and say
so, naming the milestone that fills them.
"""

from __future__ import annotations

import json
import time

from . import db, geo

PLUGIN = "route-book"
VERSION = "0.2.0"


def _ok(**payload):
    return json.dumps(payload, default=str)


def _err(message, **extra):
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload, default=str)


def _stub(tool, milestone, note=""):
    """A well-formed, honest not-implemented reply."""
    payload = {
        "error": "not_implemented",
        "tool": tool,
        "plugin": PLUGIN,
        "version": VERSION,
        "milestone": milestone,
        "message": (
            f"{tool} is declared but not implemented yet; it lands in {milestone}. "
            "The tool surface is stable — this is the skeleton milestone."
        ),
    }
    if note:
        payload["note"] = note
    return json.dumps(payload)


def _client_row(row):
    """A client as the model should see it — no internal bookkeeping."""
    return {
        "id": row["id"],
        "name": row["name"],
        "city": row["city"],
        "address": row["address"],
        "has_coordinates": row["lat"] is not None and row["lon"] is not None,
        "cadence": row["cadence"],
        "default_duration_min": row["default_duration_min"],
        "preferred_days": [d for d in (row["preferred_days"] or "").split(",") if d],
        "preferred_window": row["preferred_window"],
        "notes": row["notes"] or None,
    }


# ── duration parsing ────────────────────────────────────────────────────────

_DURATION_UNITS = {
    "h": 60, "hr": 60, "hrs": 60, "hour": 60, "hours": 60,
    "m": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
}


def _parse_duration(text):
    """'3 hrs' / '90 min' / '3h' -> minutes. None if it cannot be read.

    Returns None rather than a default. A wrong duration silently corrupts every
    downstream time calculation; a missing one is visible and fixable.
    """
    text = (text or "").strip().lower().replace(" ", "")
    if not text:
        return None
    for unit, mult in sorted(_DURATION_UNITS.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(unit):
            number = text[: -len(unit)]
            try:
                return int(float(number) * mult)
            except ValueError:
                return None
    try:
        return int(float(text))  # a bare number means minutes
    except ValueError:
        return None


# ── M1 — clients + geography ────────────────────────────────────────────────

def sched_client_add(args, **kwargs):
    """Add a client, geocoding the address.

    A failed geocode is **not** a failed add. The client is still worth keeping —
    an address fixed later beats a client re-typed — so the record is written
    with no coordinates and the geocode error is reported alongside it. The
    operator decides whether to retry.
    """
    name = (args.get("name") or "").strip()
    address = (args.get("address") or "").strip()
    city = (args.get("city") or "").strip()

    if not name or not address:
        return _err("a client needs both a name and an address",
                    got={"name": bool(name), "address": bool(address)})

    fields = {
        "cadence": args.get("cadence") or "on_demand",
        "notes": args.get("notes") or "",
        "preferred_window": args.get("preferred_window") or "any",
    }
    if args.get("price") is not None:
        fields["default_price"] = args["price"]
    days = args.get("preferred_days")
    if days:
        fields["preferred_days"] = ",".join(days) if isinstance(days, list) else str(days)
    duration = args.get("duration")
    if duration:
        minutes = _parse_duration(str(duration))
        if minutes:
            fields["default_duration_min"] = minutes

    geocode_note = None
    try:
        lat, lon, _label = geo.geocode(address, city)
        fields.update({"lat": lat, "lon": lon,
                       "geocode_source": "nominatim", "geocoded_at": time.time()})
    except geo.GeoError as exc:
        geocode_note = (
            f"stored without coordinates: {exc}. Travel times for this client "
            "will be missing until the address is resolved."
        )

    try:
        conn = db.connect()
        client_id = db.add_client(conn, name=name, address=address, city=city, **fields)
        row = db.get_client(conn, client_id)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"could not store the client: {exc}")

    result = {"status": "added", "client": _client_row(row)}
    if geocode_note:
        result["warning"] = geocode_note
    return _ok(**result)


def sched_client_list(args, **kwargs):
    """List or search clients."""
    try:
        conn = db.connect()
        rows = db.find_clients(
            conn,
            search=args.get("search"),
            city=args.get("city"),
            active_only=args.get("active_only", True),
        )
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"could not read the client list: {exc}")

    clients = [_client_row(r) for r in rows]
    missing = [c["name"] for c in clients if not c["has_coordinates"]]
    result = {"count": len(clients), "clients": clients}
    if missing:
        result["missing_coordinates"] = missing
    return _ok(**result)


def sched_area_set(args, **kwargs):
    """Set the service area — a named region, an explicit city list, or both.

    A named region is resolved to a bounding box. If that fails (informal names
    like "the peninsula" often do not exist in OpenStreetMap) the area is still
    stored with whatever the operator said, so nothing is lost and the intent is
    visible. Only the boundary is missing, and that is reported.
    """
    name = (args.get("name") or "").strip()
    region = (args.get("region") or "").strip()
    cities = args.get("cities") or []
    if not name:
        return _err("give the area a name so it can be referred to later")
    if not region and not cities:
        return _err("say either a region ('Niagara region') or a list of cities")

    kind = "named_area" if region else "city_list"
    payload = region or ", ".join(cities)

    bbox = None
    resolved = None
    warning = None
    if region:
        try:
            bbox = geo.area_bbox(region)
            if bbox is None:
                warning = (
                    f"'{region}' is not a known area, so no boundary was resolved. "
                    "The area is stored, but in-area checks will need a city list."
                )
            else:
                resolved = [round(v, 4) for v in bbox]
        except geo.GeoError as exc:
            warning = f"could not resolve '{region}': {exc}"

    try:
        conn = db.connect()
        db.set_area(conn, name=name, kind=kind, payload=payload, bbox=bbox)
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"could not store the area: {exc}")

    result = {"status": "set", "area": name, "kind": kind, "resolved_from": payload}
    if resolved:
        result["bbox"] = {"west": resolved[0], "south": resolved[1],
                          "east": resolved[2], "north": resolved[3]}
    if cities:
        result["cities"] = cities
    if warning:
        result["warning"] = warning
    return _ok(**result)


# ── Later milestones ────────────────────────────────────────────────────────

def sched_matrix_refresh(args, **kwargs):
    return _stub("sched_matrix_refresh", "M3", "Builds the pairwise drive-time cache via OSRM.")


def sched_book(args, **kwargs):
    return _stub("sched_book", "M2", "Dates are resolved in Python, never by the model.")


def sched_day(args, **kwargs):
    return _stub("sched_day", "M2")


def sched_week(args, **kwargs):
    return _stub("sched_week", "M2", "Will also report the best available improvement.")


def sched_move(args, **kwargs):
    return _stub("sched_move", "M2")


def sched_cancel(args, **kwargs):
    return _stub("sched_cancel", "M2")


def sched_recommend(args, **kwargs):
    return _stub("sched_recommend", "M3",
                 "Ranks days by marginal insertion cost with hard time-feasibility filters.")


def sched_ask(args, **kwargs):
    return _stub("sched_ask", "M5",
                 "Routes natural language to the tools above via ctx.llm, with a deterministic fallback.")
