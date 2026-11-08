"""Tool handlers.

Every handler follows the same three rules, from the Hermes plugin contract:

1. **Signature** `def handler(args: dict, **kwargs) -> str`
2. **Always return a JSON string** — successes and errors alike
3. **Never raise** — a raise is a broken tool call for the model, so every
   handler catches everything and returns an error object instead

M0 status: these are stubs. Each returns a well-formed not-implemented object
naming the milestone that will fill it in, so the plugin is inspectable and the
tool surface is real before any behaviour exists. That is deliberate — the shape
of the interface is the part worth arguing about early.
"""

import json

PLUGIN = "client-scheduler"
VERSION = "0.1.0"


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


# ── M1 — clients + geography ────────────────────────────────────────────────

def sched_client_add(args, **kwargs):
    return _stub("sched_client_add", "M1", "Will geocode the address and cache coordinates.")


def sched_client_list(args, **kwargs):
    return _stub("sched_client_list", "M1")


def sched_area_set(args, **kwargs):
    return _stub("sched_area_set", "M1", "Resolves a named region or city list to a bounding box.")


def sched_matrix_refresh(args, **kwargs):
    return _stub("sched_matrix_refresh", "M3", "Builds the pairwise drive-time cache via OSRM.")


# ── M2 — booking ────────────────────────────────────────────────────────────

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


# ── M3 — recommendations ────────────────────────────────────────────────────

def sched_recommend(args, **kwargs):
    return _stub(
        "sched_recommend",
        "M3",
        "Ranks days by marginal insertion cost with hard time-feasibility filters.",
    )


# ── M5 — conversational layer ───────────────────────────────────────────────

def sched_ask(args, **kwargs):
    return _stub(
        "sched_ask",
        "M5",
        "Routes natural language to the tools above via ctx.llm, with a deterministic fallback.",
    )
