"""route-book — a Hermes plugin for travel-aware scheduling.

Status: M0 skeleton. The plugin registers its full tool surface and validates
clean, but the handlers are stubs. See docs/DESIGN.md for what each milestone
adds.

    M0  skeleton                      <- this repo is here
    M1  clients + geography
    M2  booking
    M3  recommendations               <- the plugin becomes useful
    M4  .ics calendar feed
    M5  conversational layer
    M6  Siri voice
    M7  polish + catalog submission

Nothing here writes to the network, and no tool has side effects yet.
"""

import logging

from . import schemas, tools

logger = logging.getLogger(__name__)

# Tools this plugin contributes. Kept in one place so `register()` and the
# tests can assert the same list.
TOOL_NAMES = [
    "sched_client_add",
    "sched_client_list",
    "sched_area_set",
    "sched_book",
    "sched_recommend",
    "sched_day",
    "sched_week",
    "sched_move",
    "sched_cancel",
    "sched_matrix_refresh",
    "sched_ask",
]


def register(ctx):
    """Wire tool schemas to handlers.

    Called exactly once at startup. If this raises, the plugin is disabled and
    Hermes carries on — so it must not reference anything unavailable.
    """
    for name in TOOL_NAMES:
        ctx.register_tool(
            name=name,
            toolset="route_book",
            schema=getattr(schemas, name.upper()),
            handler=getattr(tools, name),
        )

    # The skill teaches the agent WHEN to reach for this plugin and how to ask a
    # clarifying question rather than guessing. Registered as
    # `route-book:route-book`.
    try:
        ctx.register_skill("route-book", "skills/route-book")
    except Exception as exc:  # pragma: no cover - registration is optional
        logger.debug("skill registration skipped: %s", exc)

    logger.info("route-book: registered %d tools", len(TOOL_NAMES))
