"""The scheduling engine — travel cost, feasibility, scoring, recommendations.

This is the module the plugin exists for. Everything before it was setup.

**A day is a timeline, not a list of jobs.** The distinction is not pedantry:
a list of jobs cannot tell you whether you can physically get from one to the
next, and that is the only question that matters when you are standing in a
driveway at 1pm.

Three ideas, in the order they apply:

1. **Feasibility is a filter, never a score.** A day you cannot drive is not a
   low-scoring day, it is not a candidate. Candidates that fail are discarded
   before scoring, so no weighting can quietly resurrect an impossible day.
2. **Cost is marginal, not absolute.** The question is not "how far is this
   client" but "what does adding this job to this day cost" — the extra driving
   the decision actually causes.
3. **The default path is greedy and local, and says so.** It optimises the
   booking in front of it, not the week. `best_improvement()` surfaces the
   global saving as advice, because a scheduler that silently reorganises a week
   gets switched off within a month.
"""

from __future__ import annotations

from . import config


# ── Day model ───────────────────────────────────────────────────────────────

class Stop:
    """One job on a day, as a timeline entry."""

    __slots__ = ("client_id", "name", "city", "start", "duration", "kind")

    def __init__(self, client_id, name, city, start, duration, kind="job"):
        self.client_id = client_id
        self.name = name
        self.city = city
        self.start = start            # minutes from midnight
        self.duration = duration      # minutes of hands-on work
        self.kind = kind

    @property
    def end(self):
        return self.start + self.duration

    @property
    def depart(self):
        """When the van actually leaves — work plus the changeover buffer.

        Without the buffer a timeline is optimistic by roughly a quarter hour
        per job, which is 90 minutes across a six-job day.
        """
        return self.end + config.SERVICE_BUFFER_MIN

    def as_dict(self):
        return {
            "client_id": self.client_id, "name": self.name, "city": self.city,
            "kind": self.kind,
            "start": hhmm(self.start), "end": hhmm(self.end),
            "depart": hhmm(self.depart), "minutes": self.duration,
        }


def _place_break(ordered, start_bound):
    """Insert a break Stop into the first gap that fits, or return None.

    Returns a new list — mutating the caller's stops would make a timeline
    computation change the schedule it was asked to describe.
    """
    window_start = parse_hhmm(config.BREAK_EARLIEST)
    window_end = parse_hhmm(config.BREAK_LATEST)
    running = start_bound

    for index, stop in enumerate(ordered):
        if running >= window_start and running + config.BREAK_MINUTES <= window_end \
                and running + config.BREAK_MINUTES <= stop.start:
            block = Stop(None, "break", "", running, config.BREAK_MINUTES, kind="break")
            return ordered[:index] + [block] + ordered[index:]
        running = stop.depart
    return None


def hhmm(minutes):
    """Minutes from midnight -> 'HH:MM'. Wraps past midnight rather than 25:30."""
    return f"{(int(minutes) // 60) % 24:02d}:{int(minutes) % 60:02d}"


def parse_hhmm(text):
    h, m = str(text).split(":")
    return int(h) * 60 + int(m)


def clock(day_start, day_end):
    """Seconds are meaningless here; everything is minutes from midnight."""
    return parse_hhmm(day_start), parse_hhmm(day_end)


# ── Travel lookup ───────────────────────────────────────────────────────────

class Travel:
    """Drive times keyed by point id, with honest fallbacks.

    Keyed by id rather than matrix position on purpose. A positional lookup
    silently returns the wrong number when two lists disagree about order, and
    the result is confident nonsense — a travel time for the wrong town that
    looks entirely plausible.
    """

    def __init__(self, legs, approximate=False, source="unknown"):
        self._legs = legs                    # {(from_id, to_id): minutes}
        self.approximate = approximate
        self.source = source

    def minutes(self, from_id, to_id):
        if from_id == to_id:
            return 0
        value = self._legs.get((from_id, to_id))
        if value is None:
            # Unknown pair. Returning 0 would understate the day, and inventing
            # a number would be worse. None forces the caller to decide.
            return None
        return value

    def get(self, from_id, to_id, default=None):
        value = self.minutes(from_id, to_id)
        return default if value is None else value

    @classmethod
    def from_matrix(cls, entries, matrix):
        """Build from `client_points()` output plus a `build_matrix()` result.

        `HOME` is a synthetic id so the base participates like any other point.
        """
        points = [{"kind": e.get("kind"), "id": e.get("id")} for e in entries]
        legs = {}
        for i, a in enumerate(points):
            for j, b in enumerate(points):
                if i == j:
                    continue
                key_a = a["id"] if a["id"] is not None else "home"
                key_b = b["id"] if b["id"] is not None else "home"
                legs[(key_a, key_b)] = matrix["minutes"][i][j]
        return cls(legs, approximate=matrix.get("approximate", False),
                   source=matrix.get("source", "unknown"))


# ── Feasibility ─────────────────────────────────────────────────────────────

def timeline(stops, travel, home_id="home", day_start=None, day_end=None):
    """Walk a day and return its legs, totals and slack.

    This is the model everything else reads. Returns a dict rather than raising,
    because "why doesn't this day work" is a question the caller has to answer
    in prose for the operator.
    """
    start_bound = parse_hhmm(day_start or config.WORK_START)
    end_bound = parse_hhmm(day_end or config.WORK_END)
    ordered = sorted(stops, key=lambda s: s.start)

    legs = []
    problems = []
    cursor = start_bound
    previous_id = home_id
    service_total = 0

    for stop in ordered:
        if stop.kind == "break":
            # A break is not a destination: no drive to it, no service at it.
            cursor = max(cursor, stop.start) + stop.duration
            continue
        drive = travel.minutes(previous_id, stop.client_id)
        if drive is None:
            problems.append(
                f"no travel time known between {previous_id} and {stop.name}"
            )
            legs.append({"from": previous_id, "to": stop.client_id,
                         "minutes": None, "approximate": travel.approximate})
            drive = 0
        else:
            legs.append({"from": previous_id, "to": stop.client_id,
                         "minutes": drive, "approximate": travel.approximate})

        arrive = cursor + drive
        # Arriving early is fine — you wait in the van. Arriving late is not.
        if arrive > stop.start:
            problems.append(
                f"cannot reach {stop.name} by {hhmm(stop.start)} — "
                f"arriving {hhmm(arrive)}"
            )
        if drive > config.MAX_LEG_MINUTES:
            problems.append(
                f"{drive} min drive to {stop.name} exceeds the "
                f"{config.MAX_LEG_MINUTES} min limit"
            )
        cursor = max(arrive, stop.start) + stop.duration + config.SERVICE_BUFFER_MIN
        service_total += stop.duration  # break stops never reach here
        previous_id = stop.client_id

    # The return leg counts: max_day_minutes is door to door.
    home_drive = travel.minutes(previous_id, home_id) if ordered else 0
    if ordered and home_drive is None:
        problems.append("no travel time known for the drive home")
    elif ordered:
        legs.append({"from": previous_id, "to": home_id,
                     "minutes": home_drive, "approximate": travel.approximate})
        cursor += home_drive

    elapsed = cursor - start_bound
    if elapsed > config.MAX_DAY_MINUTES:
        problems.append(
            f"day runs {elapsed} min, over the {config.MAX_DAY_MINUTES} min ceiling"
        )
    if service_total > config.MAX_SERVICE_MINUTES:
        problems.append(
            f"{service_total} min of work exceeds the "
            f"{config.MAX_SERVICE_MINUTES} min hands-on ceiling"
        )

    # Slack: the spare minutes that let a job run long without cascading.
    slack = 0
    running = start_bound
    previous = home_id
    for stop in ordered:
        drive = travel.get(previous, stop.client_id, 0)
        planned_arrive = running + drive
        if stop.start > planned_arrive:
            slack += stop.start - planned_arrive
        running = max(planned_arrive, stop.start) + stop.duration + config.SERVICE_BUFFER_MIN
        previous = stop.client_id

    # The break. Placed in the first gap wide enough to hold it, inside the
    # configured window — a break belongs in the emptiest part of the day, not
    # bolted onto a fixed clock time. It is a timeline entry, so it consumes
    # elapsed time and can push everything after it.
    if ordered and (cursor - start_bound) >= config.BREAK_MIN_DAY_MINUTES:
        placed = _place_break(ordered, start_bound)
        if placed:
            ordered = placed

    # Idle gaps: real clock time where nobody is being served and nobody is
    # driving. Reported, because a day made mostly of gaps is worth seeing.
    gaps = []
    running = start_bound
    previous = home_id
    for stop in ordered:
        if stop.kind == "break":
            running = max(running, stop.start) + stop.duration
            continue
        arrive = running + travel.get(previous, stop.client_id, 0)
        if stop.start > arrive:
            gaps.append({"from": hhmm(arrive), "to": hhmm(stop.start),
                         "minutes": stop.start - arrive})
        running = max(arrive, stop.start) + stop.duration + config.SERVICE_BUFFER_MIN
        previous = stop.client_id

    return {
        "stops": [s.as_dict() for s in ordered],
        "gaps": gaps,
        "legs": legs,
        "drive_minutes": sum(l["minutes"] or 0 for l in legs),
        "service_minutes": service_total,
        "elapsed_minutes": elapsed,
        "slack_minutes": slack,
        "problems": problems,
        "feasible": not problems,
        "approximate": travel.approximate,
    }


def feasible(stops, travel, **kwargs):
    """True when the day can physically be worked. Never a score term."""
    return timeline(stops, travel, **kwargs)["feasible"]


# ── Cost ────────────────────────────────────────────────────────────────────

def insertion_cost(stops, client_id, travel, home_id="home"):
    """The extra driving caused by adding one job to a day.

    The number the plugin is built on. Not "how far is this client from home" —
    that answers a question nobody asked:

        cost = travel(prev -> client) + travel(client -> next) - travel(prev -> next)

    A client 40 minutes away can be nearly free when they slot between two
    existing jobs. A client 10 minutes away can be expensive when they sit alone
    on a day that would have finished early.

    Returns (minutes, best_position) or (None, None) when a pair is unknown.
    """
    ordered = sorted(stops, key=lambda s: s.start)
    sequence = [home_id] + [s.client_id for s in ordered] + [home_id]

    best_cost = None
    best_at = None
    for i in range(len(sequence) - 1):
        prev_id, next_id = sequence[i], sequence[i + 1]
        direct = travel.minutes(prev_id, next_id)
        via_a = travel.minutes(prev_id, client_id)
        via_b = travel.minutes(client_id, next_id)
        if direct is None or via_a is None or via_b is None:
            continue
        cost = via_a + via_b - direct
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_at = i
    return best_cost, best_at


# ── Scoring ─────────────────────────────────────────────────────────────────

def score_day(stops, client_id, travel, client=None, day_index=None, home_id="home"):
    """Score a candidate day across the weighted terms.

    Returns (score, reasons). The reasons are not decoration: an operator who
    cannot see why Monday beat Tuesday stops trusting the answer the first time
    they disagree, and then the plugin is unused.
    """
    reasons = []
    weights = config.WEIGHTS

    cost, _ = insertion_cost(stops, client_id, travel, home_id)
    if cost is None:
        return None, ["no travel times available for this day"]

    # Insertion: lower minutes is better. Normalised against a leg the operator
    # would already find irritating, so the scale means something.
    insertion_term = max(0.0, 1.0 - (cost / max(1, config.MAX_LEG_MINUTES)))
    reasons.append(f"adds {cost} min of driving")

    # Anchor: does this day already have work in the same city?
    client = _as_mapping(client)
    client_city = client.get("city") or ""
    cities = {s.city for s in stops if s.city}
    anchor_term = 1.0 if (client_city and client_city in cities) else 0.0
    if anchor_term:
        reasons.append(f"already working in {client_city} that day")

    # Cluster: how many stops are near this client.
    near = 0
    for s in stops:
        leg = travel.minutes(client_id, s.client_id)
        if leg is not None and leg <= 15:
            near += 1
    cluster_term = min(1.0, near / 3.0)
    if near:
        reasons.append(f"{near} other job(s) within 15 min")

    # Slack: survives a job running long. Scored, and reported, because a day
    # with no spare minutes is where one overrun cascades.
    current = timeline(stops, travel, home_id=home_id)
    slack_term = min(1.0, current["slack_minutes"] / max(1, config.OVERRUN_TOLERANCE_MIN))
    if current["slack_minutes"]:
        reasons.append(f"{current['slack_minutes']} min of slack in the day")

    # Balance: prefer the lighter day, so work does not pile onto one.
    busiest = max((s.duration for s in stops), default=0)
    load = sum(s.duration for s in stops)
    balance_term = max(0.0, 1.0 - (load / max(1, config.MAX_SERVICE_MINUTES)))
    if load:
        reasons.append(f"already {load // 60}h{load % 60:02d} of work on it")

    # Window fit: morning/afternoon preference, if the client has one.
    window = (client or {}).get("preferred_window", "any")
    if window == "any" or not stops:
        window_term = 0.5
    else:
        first_hour = min(s.start for s in stops) // 60
        prefers_morning = window == "morning"
        window_term = 1.0 if (prefers_morning == (first_hour < 12)) else 0.0
        if window_term:
            reasons.append(f"matches their {window} preference")

    # Spread: a small penalty for loading the early week.
    spread_term = 1.0
    if day_index is not None and day_index <= 2:
        spread_term = 0.6

    score = (
        weights["insertion"] * insertion_term
        + weights["anchor"] * anchor_term
        + weights["cluster"] * cluster_term
        + weights["slack"] * slack_term
        + weights["balance"] * balance_term
        + weights["window_fit"] * window_term
        + weights["spread"] * spread_term
    )
    return round(score, 4), reasons


# ── Recommendations ─────────────────────────────────────────────────────────

def _as_mapping(row):
    """Accept a sqlite3.Row or a dict, and return a dict.

    sqlite3.Row supports row["key"] but NOT .get(), so code that works with a
    dict raises AttributeError the first time it is handed a row. Normalising at
    the boundary means the rest of the engine has one type to reason about.
    """
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def recommend(client_id, client, days, travel, home_id="home", opts=None):
    """Rank candidate days for one job.

    `days` is {date_iso: [Stop, ...]} — the days in the planning window and what
    is already on each.

    Feasibility first, then score. Days that fail the timeline are returned as
    rejected with their reason, rather than dropped: "why not Thursday" is a
    question the operator will ask, and an unexplained absence is worse than a
    stated reason.
    """

    client = _as_mapping(client)
    opts = opts or {}
    duration = opts.get("duration") or client.get("default_duration_min") or 180
    window = opts.get("window") or client.get("preferred_window") or "any"

    scored = []
    rejected = []

    for date_iso, existing in days.items():
        stops = list(existing)
        candidate_start = _first_sensible_start(stops, duration, window,
                                                 travel=travel, client_id=client_id)
        trial = stops + [Stop(client_id, client.get("name", ""), client.get("city", ""),
                              candidate_start, duration)]

        state = timeline(trial, travel, home_id=home_id)
        if not state["feasible"]:
            rejected.append({"date": date_iso, "why": state["problems"]})
            continue

        value, reasons = score_day(stops, client_id, travel, client=client,
                                   day_index=None, home_id=home_id)
        if value is None:
            rejected.append({"date": date_iso, "why": ["no travel times available"]})
            continue

        scored.append({
            "date": date_iso,
            "score": value,
            "start": hhmm(candidate_start),
            "insertion_minutes": (insertion_cost(stops, client_id, travel, home_id)[0]),
            "reasons": reasons,
            "day": state,
        })

    scored.sort(key=lambda d: d["score"], reverse=True)
    return {
        "recommended": scored[:5],
        "rejected": rejected[:5],
        "approximate": travel.approximate,
        "source": travel.source,
    }


def _first_sensible_start(stops, duration, window, travel=None, client_id=None):
    """Where this job would go if it were added to a day.

    The working window is when the *day* starts, not when the first job starts.
    You leave home at 08:00, so a job 12 minutes away cannot begin until 08:12 —
    and a version that ignored this rejected every recommendation onto an empty
    day with "cannot reach X by 08:00", which is exactly the kind of bug that
    looks like a config problem for a long time.

    Appended after the last job rather than inserted early: a new booking should
    not push an existing client's routine.
    """
    start_bound = parse_hhmm(config.WORK_START)

    def plus_drive(base):
        if travel is None or client_id is None:
            return base
        first_leg = travel.minutes("home", client_id)
        return base + (first_leg or 0)

    if not stops:
        if window == "afternoon":
            return plus_drive(max(start_bound, parse_hhmm("13:00")))
        return plus_drive(start_bound)

    last = max(stops, key=lambda s: s.start)
    # The drive from the previous job also takes time before this one can start.
    gap = 0
    if travel is not None and client_id is not None:
        gap = travel.get(last.client_id, client_id, 0)
    return max(start_bound, last.depart + gap)


def best_improvement(days, travel, home_id="home", max_moves=None):
    """The single best move available this week, as advice.

    Deliberately advice and not an action. A scheduler that silently reorganises
    a week to save 40 minutes gets switched off within a month, so this returns
    a suggestion with its saving and lets the operator decide.
    """
    limit = config.MAX_REBALANCE_MOVES if max_moves is None else max_moves
    current = sum(
        timeline(list(stops), travel, home_id=home_id)["drive_minutes"]
        for stops in days.values()
        if stops
    )

    best = None
    all_dates = list(days.keys())
    for date_iso, stops in days.items():
        for stop in list(stops):
            for target_iso in all_dates:
                if target_iso == date_iso:
                    continue
                moved_from = [s for s in stops if s.client_id != stop.client_id]
                moved_to = list(days[target_iso]) + [stop]
                trial_stops = dict(days)
                trial_stops[date_iso] = moved_from
                trial_stops[target_iso] = moved_to

                total = 0
                for s_list in trial_stops.values():
                    state = timeline(list(s_list), travel, home_id=home_id)
                    if not state["feasible"]:
                        total = None
                        break
                    total += state["drive_minutes"]
                if total is None:
                    continue

                saving = current - total
                if saving > 0 and (best is None or saving > best["saving_minutes"]):
                    best = {
                        "move": stop.name,
                        "from": date_iso,
                        "to": target_iso,
                        "saving_minutes": saving,
                        "advice": (
                            f"Moving {stop.name} from {date_iso} to {target_iso} "
                            f"would save about {saving} min of driving this week."
                        ),
                        "note": "Advice only — nothing moves on its own.",
                        "max_moves_per_rebalance": limit,
                    }
    return best
