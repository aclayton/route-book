"""The setup wizard — M7.

Walks an operator from a fresh install to a working one, and then gets out of the
way.

## The design rule

A setup wizard that lists everything it needs is a form. This one names **the
single next thing** and stops. Someone installing this is standing between jobs
with a phone in one hand; six bullet points is the same as none.

So: each step reports its own state, the wizard returns the first thing that is
actually missing, and it says what that thing is FOR. "No home base set" is a
chore. "No home base set — without one, a day has no start or end point and no
travel time is meaningful" is a reason to bother.

## Why it is a tool and not a UI

It could be a wizard in a terminal, or a screen in a dashboard. It is a tool
because the agent already knows how to have a conversation, and because the setup
is not a one-off: a home base moves, a service area changes, a new client needs an
address before travel times mean anything. Setup is a state you return to, not a
door you walk through once.
"""

from __future__ import annotations

from . import config, db

# Ordered by what blocks what. The first missing step is the one returned, because
# a service area is useless before there is a home base to measure from.
STEPS = (
    ("home_base", "Where do you start and end the day",
     "Without this a day has no start or end point, so every travel time is "
     "meaningless. Set HOME_BASE to your address or the town you work out of."),
    ("service_area", "Which area you cover",
     "Travel times only exist between clients that have been located. A service "
     "area tells the plugin which towns to expect and lets it check an address "
     "before it becomes a wrong coordinate."),
    ("first_client", "At least one client with an address",
     "A client needs an address before the plugin can work out travel times to "
     "it. Two located clients are the minimum for any routing at all."),
    ("calendar", "Where the calendar feed goes",
     "The .ics feed stays local by default, which is right — it contains customer "
     "names. Point ICAL_PATH at a synced folder to put it on your phone."),
)


def _home_base_step():
    if config.HOME_BASE:
        return {"state": "done", "detail": config.HOME_BASE}
    return {"state": "missing"}


def _service_area_step(conn):
    try:
        areas = db.list_areas(conn)
    except Exception:
        areas = []
    if areas:
        # The column is `name`; `region` is only the argument sched_area_set takes.
        names = [db.as_dict(a).get("name") or "" for a in areas[:3]]
        return {"state": "done", "detail": ", ".join(n for n in names if n)}
    return {"state": "missing"}


def _client_step(conn):
    try:
        clients = db.find_clients(conn)
    except Exception:
        clients = []

    located = [c for c in clients if c["lat"] is not None and c["lon"] is not None]
    if len(located) >= 2:
        return {"state": "done", "detail": f"{len(located)} located clients"}
    if located:
        # One located client is not enough for a matrix, and saying "done" here
        # would leave the operator with a routing engine that reports nothing.
        return {"state": "partial",
                "detail": f"{len(located)} located client — one more is needed "
                          f"before travel times can be worked out"}
    if clients:
        return {"state": "partial",
                "detail": f"{len(clients)} client(s) with no location yet"}
    return {"state": "missing"}


def _calendar_step():
    if config.ICAL_PATH:
        return {"state": "done", "detail": config.ICAL_PATH}
    return {"state": "optional", "detail": "stays in the plugin's own folder"}


def status(conn=None):
    """Every step's state, and the one thing worth doing next.

    The summary is a sentence, not a checklist, because a checklist gets skimmed
    and a sentence gets read.
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        checks = {
            "home_base": _home_base_step,
            "service_area": lambda: _service_area_step(conn),
            "first_client": lambda: _client_step(conn),
            "calendar": _calendar_step,
        }

        steps = []
        for key, title, why in STEPS:
            state = checks[key]()
            steps.append({
                "key": key,
                "title": title,
                "state": state["state"],
                "detail": state.get("detail"),
                "why": why if state["state"] != "done" else None,
            })

        blocking = [s for s in steps if s["state"] in ("missing", "partial")]
        ready = not blocking

        if ready:
            summary = ("Everything is set up. Tell me a client and a day and I "
                       "will work out whether it fits.")
        else:
            first = blocking[0]
            summary = f"Next: {first['title'].lower()}."

        return {
            "ready": ready,
            "steps": steps,
            "next_step": blocking[0]["key"] if blocking else None,
            "summary": summary,
            "blocking": [s["key"] for s in blocking],
        }
    finally:
        if own:
            conn.close()
