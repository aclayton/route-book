---
name: route-book
description: "Use when scheduling a job for a mobile trade — booking, finding the best day, or reading a day plan."
version: 0.1.0
license: MIT
metadata:
  hermes:
    tags: [scheduling, routing, clients, travel, voice, siri]
---

# Client Scheduler

Scheduling for one person driving between customers. The plugin is the tool; this
skill is about **when to reach for it** and how not to get it wrong.

## The two verbs

Everything the operator says is one of these. Classify first.

- **Imperative — book it.** *"bala, next Thursday, 3 hrs"* → `sched_book`.
  A day was named; the job is to record it.
- **Interrogative — advise me.** *"recommend the best day for bala in two weeks"*
  → `sched_recommend`. No day was named; the job is to work one out.

**Getting this backwards is the main failure.** If the operator asks *which* day,
do not pick one and book it. If they name a day, do not second-guess it with a
recommendation.

## Rules that are not negotiable

1. **Never resolve a date yourself.** Pass `"next Thursday"` through verbatim.
   The plugin parses it in Python. You do not know what day it is in the
   operator's timezone, and a confident wrong answer books the wrong week.
2. **Never do the arithmetic.** Travel times, insertion costs, slack — all of it
   comes from the plugin. Do not estimate a drive time from an address.
3. **Always relay the reasoning.** A recommendation comes with its reasons. Give
   them. An operator who can't see why Tuesday won stops trusting the answer the
   first time they disagree.
4. **Never invent a client.** If a name doesn't resolve, call
   `sched_client_list` and offer the closest matches. Do not guess, and do not
   pick the first fuzzy hit — two clients called Harris are indistinguishable by
   name and obvious by town.
5. **One clarifying question, then act.** Ask once if the request is genuinely
   ambiguous. Don't interrogate.

## Spoken answers

When the request arrives by voice — Siri or a voice note — set `spoken: true` on
`sched_ask`. The reply then comes back shaped for the ear: no tables, no
addresses, at most three items, times in relative terms.

Speak what comes back. Do not reformat it, and **do not read an address aloud** —
it is useless at 60 km/h and it is the one thing that identifies a customer. The
job is *"the Harris job"*; navigation is a tap.

## Booking from voice always confirms

A voice-originated booking reads back before it writes: *"Booking Baia, Thursday
the 13th, three hours — correct?"* Speech-to-text mishears names, and the operator
finds out in the wrong driveway. Do not skip this because the operator sounds
confident.

## Reading a day

`sched_day` returns a timeline, not a list: arrive, start, end, depart per job,
the drive legs, the break, and the slack. Lead with anything that looks fragile —
a day with no slack is worth flagging before the operator discovers it on a
Thursday.

## If something is not implemented

This plugin is at **M4**. Everything works except voice (M6) and the
conversational layer (M5). `sched_recommend` answers "which day is best";
`sched_ical_export` puts the schedule into any calendar app with no account.

Relay that plainly and name the milestone. Do not invent an answer to fill the
gap, and do not fall back to doing the scheduling by hand in chat.

When a client is stored **without coordinates**, say so — travel times for that
client will be missing, and the fix is to correct the address.
