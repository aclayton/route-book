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

**Complete.** Setup, clients, booking, recommendations, a subscribable `.ics`
feed, natural-language routing, voice, and data export. 192 tests, all offline.

| Milestone | What it adds |
|---|---|
| M0 | Skeleton, tool surface, config defaults |
| M1 | Clients, service areas, geocoding |
| M2 | Booking, the date parser, day and week views |
| **M3** | Recommendations — travel matrix, insertion cost, explanations |
| **M4** | Calendar — `.ics` feed, subscribable everywhere |
| **M5** | Conversational — one sentence in, one tool call out |
| **M6** | Voice — ask Siri, hear the answer, confirm out loud |
| **M7** | **Setup, export, docs** — *you are here* |
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

## Setting it up

```
hermes sched_setup
```

It tells you **the one thing to do next**, and why it matters. Not a checklist —
a checklist gets skimmed, and someone installing this is standing between jobs
with a phone in one hand.

The three that matter:

1. **A home base.** Where you start and end the day. Without it a day has no
   start or end point, so every travel time is meaningless.
2. **A service area.** Which towns you cover. Travel times only exist between
   located clients, and an area lets an address be checked before it becomes a
   wrong coordinate.
3. **Two clients with addresses.** Two is the minimum for any routing at all —
   one located client cannot make a matrix. The wizard says "partial" rather than
   "done" at one, because a routing engine that silently reports nothing is worse
   than one that admits what it is missing.

Run it again whenever something changes. A home base moves, an area changes, a
new client needs an address. **Setup is a state you return to, not a door you
walk through once.**

## Getting your data out

```
hermes sched_export
```

Writes `clients.csv`, `appointments.csv` and one JSON bundle — plain text,
openable in anything. Your customer list and visit history are your business, and
they should not be trapped in one program's database. It is also the honest
answer to *"what happens if I stop using this"*, which is worth answering before
anyone has to ask.

**No door codes, alarm codes or entry instructions are in an export, because the
plugin never stores them.** That is a design rule, not a disclaimer — the export
cannot leak what was never written down.

## Asking by voice

```
"Bala, next Thursday, 3 hrs"
"Recommend the best day for Bala in two weeks"
"What does my week look like"
```

Say those to Siri and hear the answer. Two Shortcuts, ten minutes to build, no
speech-to-text or text-to-speech to install — Siri does both. The full build is in
[`docs/VOICE.md`](docs/VOICE.md).

**A spoken booking is read back before it is written:**

> *"That is Bala Construction, next Thursday, for 3 hours. Should I book it?"*

Say yes and it books. Say nothing and nothing happens. Dictation mishears, and a
misheard date puts a real appointment in a real customer's week — you are listening,
not reading, so hearing it back is your only chance to catch it.

## Getting the schedule into a calendar

```
hermes sched_ical_export          # writes route-book.ics
```

Point your calendar app at the file, or import it. Apple Calendar, Google
Calendar, Fastmail, Nextcloud and Outlook all read it. There is no account to
create and no key to store — which is the entire reason this is the calendar
story for v1 rather than an API integration.

**No street addresses are ever written into an event.** The calendar says who and
when; where stays on the machine that made the file. Set `ICAL_TITLE_MODE:
initials` and it says `H — job` instead of a surname — worth knowing if the feed
syncs through a third-party server, because on a subscribed feed the customer
name travels with it.

By default the file stays in the plugin's own data directory, because copying a
customer list to every device the operator owns is not a decision to make by
accident. Set `ICAL_PATH` to a synced folder to put it on your phone.

## Install

Not yet — this is pre-release and not on the catalog.

To develop against it, point a Hermes install at the checkout:

```bash
hermes plugins validate /path/to/route-book
hermes plugins doctor   /path/to/route-book   # loads it in a temp HERMES_HOME
```

## License

MIT. See [LICENSE](LICENSE).
