# route-book

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for
scheduling a solo mobile trade — cleaning, window washing, pet care, landscaping,
appliance repair. Anything where one person drives between customers.

It does one thing worth having: **given a job that needs scheduling, say which day
costs the least travel while still being a reasonable day to work.**

```
"bala, next Thursday, 3 hrs"                 -> books it
"recommend the best day for bala in two weeks" -> works it out, and says why
```

## Status

**M3 — recommendations.** The plugin does the thing it exists for: rank days by
travel cost and explain the ranking. The calendar feed and voice are not built.

| Milestone | What it adds |
|---|---|
| M0 | Skeleton, tool surface, config defaults |
| M1 | Clients, service areas, geocoding |
| M2 | Booking, the date parser, day and week views |
| **M3** | **Recommendations** — travel matrix, insertion cost, explanations — *you are here* |
| M4 | `.ics` calendar feed |
| M5 | Conversational layer (`ctx.llm`, with a deterministic fallback) |
| M6 | Siri voice |
| M7 | Polish, docs, catalog submission |

## Why it exists

A solo mobile service is a one-vehicle routing problem with human constraints.
The operator already knows *what* has to be done. The expensive question is
*when*, and specifically **which day each job lands on**, because that decides
the driving. Three clients in three towns on three days costs six border
crossings; the same three on one day costs one loop.

## The measuring stick

Not *"how far is this client from home?"* but **what does adding this job to
this day cost?**

```
insertion_cost(day, client) = travel(prev -> client)
                            + travel(client -> next)
                            - travel(prev -> next)
```

A client 40 minutes away can be nearly free if they slot between two existing
jobs. One 10 minutes away can be expensive if they sit alone on a day that would
otherwise have ended early.

**The default path is greedy and local, and the docs say so.** It optimises the
booking in front of it, not the week as a whole; the weekly view surfaces the
best improvement it can find as *advice*. An operator who can't see why Monday
beat Tuesday stops trusting the answer, so every recommendation carries its
reasoning.

## Time feasibility

A day is a timeline, not a list. The rule that prevents a broken day:

```
depart(i) + travel(i -> i+1) <= start(i+1)
where depart(i) = end(i) + service_buffer_min
```

A 40-minute drive passes a 45-minute leg cap and is still impossible if the gap
is 20 minutes. Days that fail this are **discarded before scoring**, never
penalised — a day you cannot physically drive is not a low-scoring day, it is
not a candidate.

Includes changeover buffers, a mid-day break that counts toward elapsed time but
not working time, and a slack score so a day survives one job running long.

## Privacy

- **Everything local by default.** SQLite in the plugin's own data directory.
- **Two outbound calls, both optional and disclosed:** geocoding (addresses) and
  routing (coordinates only). Self-hostable, and the plugin works with neither —
  it just falls back and labels its estimates as approximate.
- **The LLM layer never carries the database.** Client names resolve to IDs
  locally; only the resolved intent reaches a model. The model is never asked
  what day it is.
- **No door codes, no alarm codes, ever.** A scheduling tool does not need them,
  and storing them turns a convenience into a liability.

## Install

Not yet — this is pre-release and not on the catalog.

To develop against it, point a Hermes install at the checkout:

```bash
hermes plugins validate /path/to/route-book
hermes plugins doctor   /path/to/route-book   # loads it in a temp HERMES_HOME
```

## License

MIT. See [LICENSE](LICENSE).
