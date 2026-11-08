"""The travel matrix — drive times between every point that matters.

This is the expensive artefact and the one that makes everything else fast. It is
built once, cached in SQLite, and rebuilt only when the set of places changes.

**One call, not N².** OSRM's `/table` service returns an entire duration matrix
from a single request, so a 25-client matrix costs one HTTP call rather than 600.
The pairwise approach is what makes naive implementations unusable, and it is
avoidable — so it is avoided.

**Two things this module will not do:**

1. **Invent a number.** If the router is unreachable, the fallback is a
   straight-line estimate at a road factor, and every value carries
   `approximate=True`. A fallback presented as a measurement is worse than no
   measurement: the operator changes their week based on it.
2. **Pretend to know about traffic.** OSRM's public service returns free-flow
   times. A 40-minute drive is 40 minutes at 2am. The relative ranking of days is
   far more trustworthy than the absolute minutes, and callers say so.

**On the public OSRM server:** it is a demo instance with no availability
guarantee. Fine for a personal install, and the README says self-host for
anything busier.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

# The public demo router. Configurable; self-host for real use.
OSRM_BASE = "https://router.project-osrm.org"
USER_AGENT = "route-book/0.4 (Hermes plugin; scheduling for a mobile trade)"

# Straight-line distance underestimates road distance systematically — roughly
# 30-40% on a road network with any structure. Used ONLY in the fallback, and
# flagged, because an unlabelled estimate is a lie.
ROAD_FACTOR = 1.35

# Average speed when estimating: mixed suburban driving.
FALLBACK_SPEED_KMH = 55.0

_REQUEST_TIMEOUT = 20


class MatrixError(RuntimeError):
    """The matrix could not be built at all — not even approximately."""


def _haversine_km(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimate_minutes(a, b):
    """Straight-line estimate with a road factor. Approximate, and labelled so."""
    km = _haversine_km(a, b) * ROAD_FACTOR
    return max(1, int(round(km / FALLBACK_SPEED_KMH * 60)))


def osrm_table(points):
    """Fetch a full duration matrix in one request.

    `points` is a list of (lat, lon). Returns (durations_min, distances_km) as
    2-D lists, or raises MatrixError.

    OSRM expects coordinates as lon,lat — the reverse of every other API, and a
    silent source of wrong answers in the southern hemisphere if reversed by
    accident. Handled once, here.
    """
    if len(points) < 2:
        raise MatrixError("a matrix needs at least two points")

    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
    url = f"{OSRM_BASE}/table/v1/driving/{urllib.parse.quote(coords, safe=';,.-')}"
    req = urllib.request.Request(
        url + "?annotations=duration,distance",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MatrixError(f"routing service returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MatrixError(f"could not reach the routing service: {exc.reason}") from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise MatrixError(f"routing service returned something unusable: {exc}") from exc

    if payload.get("code") != "Ok":
        raise MatrixError(f"routing service said: {payload.get('code')}")

    durations = payload.get("durations")
    if not durations:
        raise MatrixError("routing service returned no durations")

    minutes = [
        [None if v is None else int(round(v / 60.0)) for v in row]
        for row in durations
    ]
    distances = payload.get("distances")
    km = None
    if distances:
        km = [[None if v is None else round(v / 1000.0, 2) for v in row] for row in distances]
    return minutes, km


# ── The cached matrix ───────────────────────────────────────────────────────

def build_matrix(points, force=False):
    """Build or reuse the travel matrix for a set of (lat, lon) points.

    Returns a dict:
      {
        "minutes":  [[...]],        # square, same order as `points`
        "km":       [[...]] or None,
        "approximate": bool,        # True when the fallback was used
        "source":   "osrm" | "estimate",
        "points":   [(lat, lon), ...],
      }

    Cached per point-set signature, so adding one client is a new build but
    merely *booking* against an unchanged set is free.
    """
    if len(points) < 2:
        raise MatrixError("a matrix needs at least two points")

    signature = _signature(points)
    if not force:
        cached = _load_cached(signature)
        if cached:
            return cached

    try:
        minutes, km = osrm_table(points)
        approximate = False
        source = "osrm"
    except MatrixError:
        # Fall back, but never silently: `approximate` travels with the result
        # and every caller surfaces it.
        minutes = [
            [0 if i == j else estimate_minutes(points[i], points[j])
             for j in range(len(points))]
            for i in range(len(points))
        ]
        km = None
        approximate = True
        source = "estimate"

    result = {
        "minutes": minutes,
        "km": km,
        "approximate": approximate,
        "source": source,
        "points": list(points),
    }
    _save_cached(signature, result)
    return result


def _signature(points):
    """A stable key for a point set. Order matters — the matrix is positional."""
    import hashlib
    raw = ";".join(f"{lat:.5f},{lon:.5f}" for lat, lon in points)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _load_cached(signature):
    try:
        with db.open_db() as conn:
            row = conn.execute(
                "SELECT payload, approximate, source FROM travel_matrices WHERE signature = ?",
                (signature,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None
    payload["points"] = [tuple(p) for p in payload.get("points", [])]
    return payload


def _save_cached(signature, result):
    try:
        with db.open_db() as conn:
            conn.execute(
                """INSERT INTO travel_matrices (signature, payload, approximate, source, built_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(signature) DO UPDATE SET
                     payload=excluded.payload, approximate=excluded.approximate,
                     source=excluded.source, built_at=excluded.built_at""",
                (
                    signature,
                    json.dumps({k: v for k, v in result.items() if k != "points"}),
                    1 if result["approximate"] else 0,
                    result["source"],
                    time.time(),
                ),
            )
            conn.commit()
    except Exception:
        # A cache write failure must never fail the caller — the matrix in hand
        # is still correct, it just will not be reused.
        pass


def client_points(conn, include_home=None):
    """Every active client with coordinates, plus optionally the home base.

    Order is stable (by client id) so the matrix indices line up with the list
    the caller keeps. An unstable order here would silently mis-index every
    travel time — the kind of bug that produces confident nonsense.
    """
    rows = conn.execute(
        "SELECT id, name, lat, lon FROM clients "
        "WHERE active = 1 AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY id"
    ).fetchall()
    entries = [{"kind": "client", "id": r["id"], "name": r["name"],
                "lat": r["lat"], "lon": r["lon"]} for r in rows]
    if include_home:
        entries.insert(0, {"kind": "home", "id": None, "name": "home",
                           "lat": include_home[0], "lon": include_home[1]})
    return entries
