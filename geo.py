"""Geography: geocoding and service areas.

**This is the only module that sends data off the machine.** It sends addresses
to Nominatim (OpenStreetMap) to turn them into coordinates, and it says so
plainly in the README and in setup. Two consequences shape the code:

1. **Cache forever, call once.** An address is geocoded when it is entered and
   never again unless it changes. A 25-client list is 25 calls, once.
2. **Honour the rate limit.** Nominatim's terms allow 1 request/second, and
   ignoring that gets the user's IP blocked — a failure that looks like the
   plugin being broken. The limiter below is not optional.

Coordinates are never invented. If geocoding fails, the client is stored with
no coordinates and the failure is reported, because a wrong coordinate is worse
than a missing one: it silently corrupts every travel time computed from it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

# Nominatim's usage policy requires a real identifying User-Agent. A generic one
# gets blocked, and a blocked geocoder looks like a broken plugin.
USER_AGENT = "route-book/0.2 (Hermes plugin; scheduling for a mobile trade)"

NOMINATIM = "https://nominatim.openstreetmap.org"

# Policy: absolute maximum one request per second. Kept module-level so it holds
# across calls, which is the point — the limit is per client, not per function.
_MIN_INTERVAL_S = 1.0
_last_call = [0.0]


class GeoError(RuntimeError):
    """Geocoding could not be completed. Never a silent fallback to 0,0."""


def _throttle():
    elapsed = time.time() - _last_call[0]
    if elapsed < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - elapsed)
    _last_call[0] = time.time()


def _get(path, params, timeout=15):
    _throttle()
    url = f"{NOMINATIM}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GeoError(f"geocoding service returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GeoError(
            "could not reach the geocoding service — check the network. "
            "The plugin works without it, but travel times will be missing."
        ) from exc
    except (json.JSONDecodeError, TimeoutError) as exc:
        raise GeoError(f"geocoding service returned something unusable: {exc}") from exc


def geocode(address, city="", country="Canada"):
    """Resolve an address to (lat, lon, display_name).

    Raises GeoError rather than returning a guess. The caller decides whether a
    missing coordinate is fatal — for adding a client it is not, for computing
    travel it is.
    """
    query = ", ".join(p for p in (address, city, country) if p)
    results = _get("/search", {"q": query, "format": "json", "limit": 1,
                               "addressdetails": 0})
    if not results:
        raise GeoError(f"no match for {query!r}")
    top = results[0]
    return float(top["lat"]), float(top["lon"]), top.get("display_name", "")


def suggest(address, city="", country="Canada", limit=5):
    """Return candidate matches, newest-first, for disambiguating an address.

    Used instead of silently taking the top hit. Two towns can share a street
    name, and booking the wrong one costs a wasted trip — so ambiguous addresses
    are surfaced to the operator, not resolved behind their back.
    """
    query = ", ".join(p for p in (address, city, country) if p)
    results = _get("/search", {"q": query, "format": "json", "limit": limit,
                               "addressdetails": 0})
    return [
        {"lat": float(r["lat"]), "lon": float(r["lon"]),
         "label": r.get("display_name", ""), "type": r.get("type", "")}
        for r in results
    ]


def area_bbox(region):
    """Resolve a named region to a bounding box (west, south, east, north).

    Returns None when Nominatim has no such area, which is common for informal
    names ("the peninsula"). The caller falls back to the city list the operator
    supplied rather than inventing bounds.
    """
    results = _get("/search", {"q": region, "format": "json", "limit": 1})
    if not results:
        return None
    box = results[0].get("boundingbox")
    if not box or len(box) != 4:
        return None
    south, north, west, east = (float(v) for v in box)
    return (west, south, east, north)


def bbox_contains(bbox, lat, lon):
    """Is a point inside a bounding box?

    A bounding box is a coarse test and deliberately so: this answers "is this
    client in the area the operator described", and a rectangle is what a person
    means by "the Niagara region". Precise polygon containment would reject
    clients the operator considers in-area, which is the worse error.
    """
    if not bbox or lat is None or lon is None:
        return None
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north
