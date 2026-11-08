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

from . import config, db, geo, nl

PLUGIN = "route-book"
VERSION = "0.3.0"


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
        with db.open_db() as conn:
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
        with db.open_db() as conn:
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
        with db.open_db() as conn:
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


def _today():
    """Today, injected rather than read inline.

    Every date decision flows through here so a caller (or a test) can pin it.
    A parser that reads the clock internally cannot be tested, and an untested
    date parser books the wrong week.
    """
    from datetime import date
    return date.today()


def _minutes_from_hhmm(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm_from_minutes(total):
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def sched_book(args, **kwargs):
    """Book a client on a day.

    The resolved day is always reported back, because "next Thursday" is
    genuinely ambiguous in English and the operator should see which Thursday
    was chosen before it becomes a fact.
    """
    today = _today()
    client_arg = (args.get("client") or "").strip()
    when_arg = args.get("when") or ""
    if not client_arg:
        return _err("which client?")

    try:
        with db.open_db() as conn:
            matches = nl.find_named_clients(client_arg, db.find_clients(conn))
    except Exception as exc:
        return _err(f"could not read the client list: {exc}")

    if not matches:
        return _err(f"no client matches {client_arg!r}",
                    hint="check the name, or add the client first with sched_client_add")
    if len(matches) > 1:
        # Never pick silently — two clients called Harris are indistinguishable
        # by name and obvious by town.
        return _err(
            f"{len(matches)} clients match {client_arg!r}",
            candidates=[{"id": c["id"], "name": c["name"], "city": c["city"]} for c in matches],
            hint="say which one — include the town",
        )
    client = matches[0]

    when = nl.parse_when(when_arg, today)
    if when is None:
        return _err(f"could not read a date from {when_arg!r}",
                    hint="try 'next Thursday', 'in two weeks', 'Nov 20' or a date like 2026-11-20")

    duration = nl.parse_duration(args.get("duration")) or client["default_duration_min"]
    if not duration:
        return _err(
            "no duration given and this client has no default",
            hint="say how long, e.g. '3 hrs'",
        )

    weekday_name = when.strftime("%A")
    if config.WORKING_DAYS and weekday_name not in config.WORKING_DAYS:
        return _err(
            f"{when.isoformat()} is a {weekday_name}, which is not a working day",
            working_days=config.WORKING_DAYS,
        )

    start_local = (args.get("start") or config.WORK_START).strip()

    try:
        with db.open_db() as conn:
            existing = db.appointments_on(conn, when.isoformat())
    except Exception as exc:
        return _err(f"could not read that day: {exc}")

    # Overlap check. Without travel times (M3) this catches only direct clashes,
    # but a double-booking is the one error that is definitely wrong.
    new_start = _minutes_from_hhmm(start_local)
    new_end = new_start + duration
    for appt in existing:
        a_start = _minutes_from_hhmm(appt["start_local"])
        a_end = a_start + appt["duration_min"]
        if new_start < a_end and a_start < new_end:
            return _err(
                f"clashes with {appt['client_name']} at {appt['start_local']}",
                on=when.isoformat(),
                hint="pick another time or day",
            )

    try:
        with db.open_db() as conn:
            appt_id = db.add_appointment(
                conn, client_id=client["id"], date_iso=when.isoformat(),
                start_local=start_local, duration_min=duration,
                tz=config.TIMEZONE, source=args.get("source") or "manual",
            )
    except Exception as exc:
        return _err(f"could not store the appointment: {exc}")

    _end = _hhmm_from_minutes(new_end)
    return _ok(
        status="booked",
        appointment_id=appt_id,
        client={"id": client["id"], "name": client["name"], "city": client["city"]},
        date=when.isoformat(),
        day=weekday_name,
        start=start_local,
        end=_end,
        duration_min=duration,
        readable=f"{client['name']}, {weekday_name} {when.strftime('%d %b')}, "
                 f"{start_local}–{_end}",
    )


def sched_day(args, **kwargs):
    """One day as a timeline: jobs, the break, and the gaps between them.

    **No drive legs yet.** Travel times need the matrix (M3), and inventing them
    from straight-line distance would be worse than omitting them — the gaps
    shown are real, and a gap that looks big enough may not be once driving is
    counted. The tool says so rather than implying a validated day.
    """
    today = _today()
    when = nl.parse_when(args.get("when") or "today", today)
    if when is None:
        return _err(f"could not read a date from {args.get('when')!r}")

    try:
        with db.open_db() as conn:
            appts = db.appointments_on(conn, when.isoformat())
    except Exception as exc:
        return _err(f"could not read that day: {exc}")

    if not appts:
        return _ok(date=when.isoformat(), day=when.strftime("%A"),
                   jobs=[], job_count=0, hands_on_minutes=0,
                   message=f"Nothing booked on {when.strftime('%A')}.",
                   note="Drive times arrive in M3; gaps are real time, not validated.")

    timeline = []
    service_total = 0
    cursor = _minutes_from_hhmm(config.WORK_START)
    break_minutes = config.BREAK_MINUTES
    break_placed = False
    break_not_before = _minutes_from_hhmm(config.BREAK_EARLIEST)
    break_not_after = _minutes_from_hhmm(config.BREAK_LATEST)

    for appt in appts:
        start = _minutes_from_hhmm(appt["start_local"])
        end = start + appt["duration_min"]
        gap = start - cursor
        # Largest gap so far, to place the break in — a break belongs in the
        # emptiest part of the day, not bolted on at a fixed clock time.
        if (not break_placed and gap >= break_minutes
                and break_not_before <= start <= break_not_after):
            timeline.append({"kind": "break", "start": _hhmm_from_minutes(cursor),
                             "end": _hhmm_from_minutes(cursor + break_minutes),
                             "minutes": break_minutes})
            cursor += break_minutes
            break_placed = True
        if gap > 0:
            timeline.append({"kind": "gap", "start": _hhmm_from_minutes(cursor),
                             "end": appt["start_local"], "minutes": gap})
        timeline.append({
            "kind": "job",
            "client": appt["client_name"],
            "city": appt["client_city"],
            "start": appt["start_local"],
            "end": _hhmm_from_minutes(end),
            "minutes": appt["duration_min"],
        })
        service_total += appt["duration_min"]
        cursor = end

    elapsed = cursor - _minutes_from_hhmm(config.WORK_START)
    return _ok(
        date=when.isoformat(),
        day=when.strftime("%A"),
        job_count=len(appts),
        hands_on_minutes=service_total,
        elapsed_minutes=elapsed,
        timeline=timeline,
        note=("Drive times arrive in M3. The gaps shown are real clock time but "
              "have not been checked against travel — do not treat the day as "
              "validated yet."),
    )


def sched_week(args, **kwargs):
    """The week's shape: jobs, hands-on hours, and which days carry the load."""
    from datetime import timedelta
    today = _today()
    anchor = nl.parse_when(args.get("start") or "today", today)
    if anchor is None:
        return _err(f"could not read a date from {args.get('start')!r}")

    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)

    try:
        with db.open_db() as conn:
            appts = db.appointments_between(conn, monday.isoformat(), sunday.isoformat())
    except Exception as exc:
        return _err(f"could not read the week: {exc}")

    per_day = {}
    service_total = 0
    for a in appts:
        entry = per_day.setdefault(a["date"], {"jobs": 0, "minutes": 0, "cities": []})
        entry["jobs"] += 1
        entry["minutes"] += a["duration_min"]
        service_total += a["duration_min"]
        if a["client_city"] and a["client_city"] not in entry["cities"]:
            entry["cities"].append(a["client_city"])

    busiest = max(per_day.items(), key=lambda kv: kv[1]["minutes"])[0] if per_day else None

    return _ok(
        week_start=monday.isoformat(),
        week_end=sunday.isoformat(),
        job_count=len(appts),
        hands_on_minutes=service_total,
        hands_on_hours=round(service_total / 60, 1),
        days_worked=len(per_day),
        busiest_day=busiest,
        per_day=per_day,
        note=("Drive time is not included yet — it arrives with the travel matrix in "
              "M3, along with the saving available from moving anything."),
    )


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
