"""
Standalone end-to-end test for the GHL call-transcript pipeline.

Posts a synthetic transcript to /api/ghl/call-transcript on a real running
server (local or deployed), waits for the background pipeline to finish
(real Claude call + real Google Sheets write), then checks the live
"Rubric - Live" sheet to confirm a new row actually landed with the expected
values.

Usage:
    python3 scripts/test_ghl_pipeline.py                        # deployed Railway URL
    python3 scripts/test_ghl_pipeline.py http://127.0.0.1:8000   # local server
"""
import logging
import os
import sys
import time
import uuid

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import config.config  # noqa: triggers load_dotenv()
from repositories.google_sheets_repository import get_client

DEFAULT_BASE_URL = "https://zaps-production-d766.up.railway.app"
GHL_SHEET_ID = "1ZjARCtxcGIzr0ydRutLGYVQ94Ey3u37fUZoACgzEgP8"
GHL_WORKSHEET_ID = 1446116535


def build_payload(call_id: str) -> dict:
    return {
        "contact_id": "test_contact_pipeline_check",
        "customData": {
            "call_id": call_id,
            "call_from": "+15550001111",
            "call_to": "+15550002222",
            "transcript": (
                "Agent: Hi, is this Michael? Michael: Yes, this is Michael speaking. "
                "Agent: Great Michael, I wanted to follow up on the TCPA spam calls you reported. "
                "Do you still have the screenshots of those messages? "
                "Michael: Yes, I still have them saved on my phone from last week. "
                "Agent: Perfect, would you be able to send those over to us today while we are on the call? "
                "Michael: Sure, let me pull them up right now, I can text them to you. "
                "Agent: Great, thank you Michael, I appreciate you sending those over live."
            ),
        },
    }


def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE_URL
    call_id = f"test_call_{uuid.uuid4().hex[:8]}"

    print(f"Target server: {base_url}")
    print(f"Test call_id:  {call_id}")
    print()

    sheet = get_client().open_by_key(GHL_SHEET_ID).get_worksheet_by_id(GHL_WORKSHEET_ID)
    before_count = len(sheet.get_all_values())
    print(f"Rows in 'Rubric - Live' before: {before_count}")

    print("POSTing synthetic transcript...")
    r = requests.post(f"{base_url}/api/ghl/call-transcript", json=build_payload(call_id), timeout=30)
    print(f"Response: {r.status_code} {r.json()}")
    if r.status_code != 200:
        print("FAILED — request itself did not succeed, stopping here.")
        return

    print()
    print("Waiting for the background pipeline (Claude scoring + sheet write)...")
    for attempt in range(30):  # up to ~90s
        time.sleep(3)
        values = sheet.get_all_values()
        if len(values) > before_count:
            headers, new_row = values[0], values[-1]
            if new_row[0] == call_id:
                print(f"\nSUCCESS — new row appeared after {(attempt + 1) * 3}s:\n")
                for h, v in zip(headers, new_row):
                    print(f"  {h:35s} -> {v!r}")
                return
        print(f"  ...still waiting ({(attempt + 1) * 3}s)")

    print("\nTIMED OUT — no matching row appeared within ~90s. Check server logs for the actual error.")


if __name__ == "__main__":
    main()
