"""Defaults, in one place.

These are the numbers the design argues about. They live here rather than inline
so an operator can find them, and so a reviewer can see the whole policy at once.

Nothing here is a magic constant buried in a calculation.
"""

# ── Travel ──────────────────────────────────────────────────────────────────

# Longest single drive accepted between two jobs.
MAX_LEG_MINUTES = 45

# Time to pack up, lock up, load the car, and say goodbye. Not driving, but not
# work either. Without it a timeline is optimistic by ~15 min per job.
SERVICE_BUFFER_MIN = 10

# ── Day limits ──────────────────────────────────────────────────────────────
# Two ceilings, because one is ambiguous: seven hours of cleaning plus two hours
# of driving is a nine-hour day, and those are different numbers.

# The working window and the base every day starts and ends at. M1's setup
# wizard writes these; until then they are the defaults.
WORKING_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
WORK_START = "08:00"       # local, HH:MM
WORK_END = "17:00"
HOME_BASE = ""             # set by setup; travel is measured from here
TIMEZONE = "America/Toronto"   # IANA. Never a bare offset — DST moves twice a year.

MAX_DAY_MINUTES = 540      # elapsed, door to door, including all driving
MAX_SERVICE_MINUTES = 420  # hands-on working time only

# Would this day survive one job running long? A day with no slack cascades.
OVERRUN_TOLERANCE_MIN = 20

# ── Break ───────────────────────────────────────────────────────────────────

BREAK_MINUTES = 30
BREAK_EARLIEST = "11:30"
BREAK_LATEST = "14:00"
BREAK_MIN_DAY_MINUTES = 300  # only days at least this long get a break

# ── Scoring weights ─────────────────────────────────────────────────────────
# Must sum to 1.0. Insertion cost dominates because travel is the cost that is
# purely a function of scheduling.

WEIGHTS = {
    "insertion": 0.45,   # marginal drive minutes this job adds
    "anchor": 0.15,      # does this day already have work in that city
    "cluster": 0.15,     # clients within 15 min of this one
    "slack": 0.10,       # spare minutes — survives a job running long
    "balance": 0.10,     # don't overfill one day and starve another
    "window_fit": 0.05,  # matches the client's morning/afternoon preference
    "spread": 0.05,      # small penalty for clustering all work Mon-Wed
}

# ── Thresholds ──────────────────────────────────────────────────────────────

# Insertion cost under this means just take the day; most bookings end here.
GOOD_ENOUGH_MINUTES = 12

# 2-opt weekly rebalance will never move more than this many appointments
# without asking. A scheduler that silently reshuffles six routines is dead.
MAX_REBALANCE_MOVES = 3

# ── Voice ───────────────────────────────────────────────────────────────────

VOICE_MAX_ITEMS = 3          # items in a spoken list before summarising
VOICE_RELATIVE_TIMES = True  # "in 25 minutes" rather than "at 09:40"
VOICE_HOT_PATH_TIMEOUT_MS = 1500  # Siri abandons slow Shortcuts
