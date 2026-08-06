"""
Standalone check: given one or more area codes, show which phone number they'll
resolve to (direct match vs nearest-neighbor) and the distance for the latter.

Run: python3 scripts/check_area_code.py 213 214 999
With no arguments, prints a full summary of every currently-unmapped area code
that got resolved via nearest-neighbor.
"""
import logging
import os
import sys

logging.disable(logging.CRITICAL)  # quiet — this script prints its own output

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.config  # noqa: triggers load_dotenv()
from repositories.google_sheets_repository import get_client, load_area_code_map
from utils.area_code_geo import load_area_code_coords, find_nearest_area_code, haversine_miles


def _direct_map():
    client = get_client()
    sheet = client.open_by_key("1bk-G0lD3P9J6MSBYmMYLHfA-_aQ1FO-BTe0x20V6_Ok").worksheet("BOT area codes (LIVE)")
    direct = {}
    for row in sheet.get_all_records():
        area = str(row.get("Area Code")).strip()
        if area and row.get("Phone Number ID"):
            direct[area] = [row.get("Phone Number ID"), row.get("Number")]
    return direct


def check_one(area: str, direct: dict, full_map: dict, coords: dict):
    if area in direct:
        phone_id, number = direct[area]
        print(f"{area}: DIRECTLY mapped -> phone {number} ({phone_id})")
        return

    if area in full_map:
        nearest = find_nearest_area_code(area, list(direct.keys()), coords)
        dist = haversine_miles(*coords[area], *coords[nearest]) if nearest and area in coords else None
        phone_id, number = full_map[area]
        dist_str = f", {dist:.1f} mi away" if dist is not None else ""
        print(f"{area}: resolved via NEAREST NEIGHBOR -> area {nearest}{dist_str} -> phone {number} ({phone_id})")
        return

    print(f"{area}: NOT resolved — no geographic data for this area code, will use DEFAULT_PHONE")


def main():
    print("Loading live area code map (direct + nearest-neighbor)...")
    full_map = load_area_code_map()
    direct = _direct_map()
    coords = load_area_code_coords()
    print(f"Direct: {len(direct)} | Total after fill-in: {len(full_map)} | Known coordinates: {len(coords)}")
    print()

    args = sys.argv[1:]
    if args:
        for area in args:
            check_one(area.strip(), direct, full_map, coords)
        return

    # No args: summarize every area code that was filled in via nearest-neighbor
    filled_in = sorted(a for a in full_map if a not in direct)
    print(f"{len(filled_in)} area codes resolved via nearest-neighbor:\n")
    for area in filled_in:
        check_one(area, direct, full_map, coords)


if __name__ == "__main__":
    main()
