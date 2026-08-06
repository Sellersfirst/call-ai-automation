import csv
import logging
import math
import os

logger = logging.getLogger("area_code_geo")

_GEO_CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "us_area_code_geo.csv")

_coords_cache: dict[str, tuple[float, float]] | None = None


def load_area_code_coords() -> dict[str, tuple[float, float]]:
    """
    area_code -> (latitude, longitude), sourced from NANPA + Geonames
    (github.com/ravisorg/Area-Code-Geolocation-Database, public domain).
    """
    global _coords_cache
    if _coords_cache is not None:
        return _coords_cache

    coords: dict[str, tuple[float, float]] = {}
    with open(_GEO_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) != 3:
                continue
            area, lat, lon = row
            coords[area.strip()] = (float(lat), float(lon))

    _coords_cache = coords
    return coords


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in miles."""
    r = 3958.8  # Earth radius in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_nearest_area_code(area_code: str, candidate_area_codes, coords: dict[str, tuple[float, float]]) -> str | None:
    """
    Given an area_code and an iterable of candidate area codes (e.g. ones that
    already have a phone number assigned), return the geographically closest
    candidate, or None if neither the target nor any candidate has known coordinates.
    """
    target = coords.get(area_code)
    if not target:
        return None

    nearest = None
    nearest_dist = None
    for candidate in candidate_area_codes:
        candidate_coord = coords.get(candidate)
        if not candidate_coord:
            continue
        dist = haversine_miles(target[0], target[1], candidate_coord[0], candidate_coord[1])
        if nearest_dist is None or dist < nearest_dist:
            nearest = candidate
            nearest_dist = dist

    return nearest
