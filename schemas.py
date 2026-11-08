"""Tool schemas — what the model sees.

Two rules held throughout:

1. **Descriptions state when to use the tool, not just what it does.** The model
   picks a tool from the description; "list clients" and "find a client" are
   different jobs and the description is the only thing distinguishing them.

2. **No tool takes a resolved date.** Every date parameter is an expression
   passed through verbatim ("next Thursday", "in two weeks") and resolved in
   Python. An LLM asked what day it is will answer confidently and wrongly, and
   the operator has no way to notice.
"""

_CLIENT = {
    "type": "string",
    "description": (
        "Client name or alias as the operator said it, e.g. 'bala' or "
        "'Mrs Harris'. Partial names and speech-to-text spellings are fine — "
        "resolution happens locally against the client list."
    ),
}

_WHEN = {
    "type": "string",
    "description": (
        "Date expression exactly as spoken or typed: 'next Thursday', "
        "'in two weeks', '2026-11-20'. Never resolve this yourself — pass the "
        "phrase through and the plugin will parse it."
    ),
}

_DURATION = {
    "type": "string",
    "description": (
        "Duration as stated: '3 hrs', '90 min', 'half a day'. Omit to use the "
        "client's default."
    ),
}


SCHED_CLIENT_ADD = {
    "name": "sched_client_add",
    "description": (
        "Add a client, or update one that exists. Geocodes the address and "
        "caches the coordinates. Use this when the operator gives you a new "
        "customer, or corrects an address on an existing one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Client name."},
            "address": {"type": "string", "description": "Street address."},
            "city": {"type": "string", "description": "City or town."},
            "cadence": {
                "type": "string",
                "enum": ["weekly", "biweekly", "monthly", "on_demand"],
                "description": "How often this client is serviced.",
            },
            "duration": _DURATION,
            "price": {"type": "number", "description": "Usual price, optional."},
            "preferred_days": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Days this client prefers, e.g. ['Tuesday'].",
            },
            "notes": {"type": "string", "description": "Access notes, pets, anything useful."},
        },
        "required": ["name", "address"],
    },
}

SCHED_CLIENT_LIST = {
    "name": "sched_client_list",
    "description": (
        "List or search clients. Use this to answer 'who is in Fort Erie' or to "
        "check a name before booking. Never guess a client's details — read them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Optional text to match against name or city."},
            "city": {"type": "string", "description": "Restrict to one city."},
            "active_only": {"type": "boolean", "default": True},
        },
        "required": [],
    },
}

SCHED_AREA_SET = {
    "name": "sched_area_set",
    "description": (
        "Set the area the operator works in. Accepts a named region "
        "('Niagara region') or an explicit list of cities. Defines which clients "
        "are considered in-area."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Label for this area, e.g. 'home patch'."},
            "region": {"type": "string", "description": "A named region to resolve, e.g. 'Niagara region'."},
            "cities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicit city list, e.g. ['Niagara Falls', 'Fort Erie'].",
            },
        },
        "required": ["name"],
    },
}

SCHED_BOOK = {
    "name": "sched_book",
    "description": (
        "Book an appointment for a client on a specific day. Use when the "
        "operator names the day ('book bala next Thursday, 3 hrs'). If they ask "
        "WHICH day is best instead, use sched_recommend — do not pick a day "
        "yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {"client": _CLIENT, "when": _WHEN, "duration": _DURATION},
        "required": ["client", "when"],
    },
}

SCHED_RECOMMEND = {
    "name": "sched_recommend",
    "description": (
        "Rank candidate days for a job by travel cost, and explain the ranking. "
        "Use when the operator asks which day or when they should do a job "
        "('recommend the best day for bala in two weeks'). Always relay the "
        "reasons, not just the winner — an operator who can't see why Monday beat "
        "Tuesday stops trusting it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "client": _CLIENT,
            "within": {
                "type": "string",
                "description": "How far ahead to search, as stated: 'two weeks', 'this month'. Default 'two weeks'.",
            },
            "duration": _DURATION,
        },
        "required": ["client"],
    },
}

SCHED_DAY = {
    "name": "sched_day",
    "description": (
        "Show one day as a timeline: arrive, start, end and depart per job, the "
        "drive legs between them, the break, and the slack left over. Use for "
        "'what does Tuesday look like'."
    ),
    "parameters": {
        "type": "object",
        "properties": {"when": _WHEN},
        "required": ["when"],
    },
}

SCHED_WEEK = {
    "name": "sched_week",
    "description": (
        "Show the week: jobs, total hands-on hours, total drive time, and the "
        "single best improvement available. Use for 'how does my week look' and "
        "when the operator is deciding whether to move anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "Week containing this date expression. Default: this week."}
        },
        "required": [],
    },
}

SCHED_MOVE = {
    "name": "sched_move",
    "description": (
        "Move an existing appointment to another day or time. Confirm the "
        "destination day back to the operator before or after writing — a "
        "misheard day moves a customer's routine."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "client": _CLIENT,
            "from_when": {"type": "string", "description": "Which appointment, as stated: 'this week', 'Tuesday'."},
            "to_when": _WHEN,
        },
        "required": ["client", "to_when"],
    },
}

SCHED_CANCEL = {
    "name": "sched_cancel",
    "description": "Cancel an appointment. State which one back to the operator.",
    "parameters": {
        "type": "object",
        "properties": {"client": _CLIENT, "when": _WHEN},
        "required": ["client"],
    },
}

SCHED_MATRIX_REFRESH = {
    "name": "sched_matrix_refresh",
    "description": (
        "Rebuild the cached travel matrix (drive times between every active "
        "client). Needed after adding clients or correcting addresses; otherwise "
        "run automatically. Slow — it makes one routing lookup per pair."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SCHED_ASK = {
    "name": "sched_ask",
    "description": (
        "The natural-language front door. Use when the operator's request is a "
        "sentence rather than a structured command ('bala, next Thursday, 3 hrs' "
        "or 'recommend the best day for bala in two weeks'). Routes to the right "
        "tool. If the request is ambiguous, ask one clarifying question instead "
        "of guessing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The operator's request, verbatim."},
            "spoken": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Set when the answer will be heard rather than read — Siri or a "
                    "voice note. Returns prose shaped for the ear: no tables, no "
                    "addresses, at most three items."
                ),
            },
        },
        "required": ["text"],
    },
}


SCHED_ICAL_EXPORT = {
    "name": "sched_ical_export",
    "description": (
        "Write the calendar feed (.ics) so the schedule shows up in whichever "
        "calendar app the operator already uses. No account, key or OAuth — any "
        "app can subscribe to the file or import it. Use when asked to get the "
        "schedule into a calendar, or after a batch of changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Where to write the file. Omit to use the configured location. "
                    "A synced folder puts it on the phone; the default stays local, "
                    "because copying a customer list to every device is not a "
                    "decision to make by accident."
                ),
            },
            "days_ahead": {
                "type": "integer",
                "description": "How far ahead to publish. Default 180 days.",
            },
        },
        "required": [],
    },
}
