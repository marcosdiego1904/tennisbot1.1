#!/usr/bin/env python3
"""
Test script for TennisApi1 (RapidAPI) — validates live score data structure.

Run from project root:
    python scripts/test_live_scores_api.py

What this tests:
  1. GET /api/tennis/events/live → all live matches right now
  2. GET /api/tennis/event/{id}/point-by-point → detailed score for one match
  3. Prints structured output so you can verify the data we need for pivot logic

Required env vars:
  TENNISAPI_KEY  (your x-rapidapi-key)

Or just run it — the key is hardcoded as fallback for testing.
"""

import os
import json
import http.client
import sys

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY  = os.getenv("TENNISAPI_KEY", "563ea39f66msh0512ba3143fdccfp1bb39fjsnde81db4498b1")
API_HOST = "tennisapi1.p.rapidapi.com"

HEADERS = {
    "x-rapidapi-key": API_KEY,
    "x-rapidapi-host": API_HOST,
}


def api_get(path: str) -> dict:
    """Simple GET request, returns parsed JSON."""
    conn = http.client.HTTPSConnection(API_HOST)
    conn.request("GET", path, headers=HEADERS)
    res = conn.getresponse()
    raw = res.read().decode("utf-8")
    conn.close()
    if res.status != 200:
        print(f"  ✗ HTTP {res.status}: {raw[:300]}")
        return {}
    return json.loads(raw)


def test_live_events():
    """Test 1: Fetch all live tennis events."""
    print("=" * 70)
    print("TEST 1: GET /api/tennis/events/live")
    print("=" * 70)

    data = api_get("/api/tennis/events/live")

    if not data:
        print("  ✗ No data returned. API may be down or no live matches right now.")
        print("  TIP: Run this during a tournament (ATP/WTA) for live data.")
        return None

    # Print raw structure of first event to understand the schema
    events = data.get("events", data) if isinstance(data, dict) else data
    if isinstance(events, dict):
        # Maybe the events are nested differently
        print(f"  Raw top-level keys: {list(data.keys())}")
        print(f"  First 2000 chars of response:")
        print(f"  {json.dumps(data, indent=2)[:2000]}")
        return data

    if isinstance(events, list):
        print(f"  ✓ Found {len(events)} live event(s)\n")

        for i, event in enumerate(events[:5]):  # Show first 5
            print(f"  ── Event {i+1} ──")

            # Try common field names
            event_id = event.get("id", event.get("eventId", event.get("matchId", "?")))
            status = event.get("status", event.get("matchStatus", "?"))

            # Players
            home = event.get("homeTeam", event.get("home", event.get("player1", {})))
            away = event.get("awayTeam", event.get("away", event.get("player2", {})))

            if isinstance(home, dict):
                home_name = home.get("name", home.get("shortName", "?"))
            else:
                home_name = str(home)

            if isinstance(away, dict):
                away_name = away.get("name", away.get("shortName", "?"))
            else:
                away_name = str(away)

            # Score
            home_score = event.get("homeScore", event.get("score", {}).get("home", "?"))
            away_score = event.get("awayScore", event.get("score", {}).get("away", "?"))

            print(f"    ID:      {event_id}")
            print(f"    Status:  {status}")
            print(f"    Match:   {home_name} vs {away_name}")
            print(f"    Score:   {home_score} - {away_score}")

            # Print ALL keys for the first event so we know the full schema
            if i == 0:
                print(f"\n    ── Full schema (all keys) ──")
                _print_schema(event, indent=6)

            print()

        return events
    else:
        print(f"  Unexpected response type: {type(events)}")
        print(f"  {json.dumps(data, indent=2)[:2000]}")
        return data


def test_point_by_point(event_id: str = "14232981"):
    """Test 2: Fetch point-by-point data for a specific event."""
    print("=" * 70)
    print(f"TEST 2: GET /api/tennis/event/{event_id}/point-by-point")
    print("=" * 70)

    data = api_get(f"/api/tennis/event/{event_id}/point-by-point")

    if not data:
        print(f"  ✗ No data for event {event_id}. Match may have ended.")
        print("  TIP: Use an event ID from Test 1 for live data.")
        return

    print(f"  ✓ Got point-by-point data\n")
    print(f"  Top-level keys: {list(data.keys())}")
    print(f"\n  Full response (first 3000 chars):")
    print(f"  {json.dumps(data, indent=2)[:3000]}")


def test_other_endpoints():
    """Test 3: Discover other useful endpoints."""
    print("=" * 70)
    print("TEST 3: Exploring other endpoints")
    print("=" * 70)

    # Try common patterns for tennis APIs
    endpoints = [
        ("/api/tennis/events/live/count", "Live event count"),
        ("/api/tennis/tournament/list", "Tournament list"),
        ("/api/tennis/matches/live", "Matches live (alt)"),
    ]

    for path, desc in endpoints:
        print(f"\n  Trying: {desc} ({path})")
        data = api_get(path)
        if data:
            print(f"    ✓ Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            print(f"    Preview: {json.dumps(data, indent=2)[:500]}")
        else:
            print(f"    ✗ No data")


def _print_schema(obj, indent=4):
    """Recursively print the schema of a dict/list."""
    prefix = " " * indent
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, dict):
                print(f"{prefix}{key}: {{dict with {len(val)} keys}}")
                _print_schema(val, indent + 2)
            elif isinstance(val, list):
                print(f"{prefix}{key}: [list of {len(val)} items]")
                if val and isinstance(val[0], dict):
                    _print_schema(val[0], indent + 2)
            else:
                print(f"{prefix}{key}: {repr(val)[:80]}")
    elif isinstance(obj, list) and obj:
        print(f"{prefix}[0]: {type(obj[0]).__name__}")
        if isinstance(obj[0], dict):
            _print_schema(obj[0], indent + 2)


def main():
    print("\n🎾 TennisApi1 Live Score API — Test Suite")
    print(f"   Host: {API_HOST}")
    print(f"   Key:  {API_KEY[:10]}...{API_KEY[-4:]}\n")

    # Test 1: Live events
    events = test_live_events()
    print()

    # Test 2: Point-by-point (use first live event if available, else fallback)
    event_id = "14232981"
    if events and isinstance(events, list) and len(events) > 0:
        first = events[0]
        live_id = first.get("id", first.get("eventId", first.get("matchId")))
        if live_id:
            event_id = str(live_id)
            print(f"  Using live event ID: {event_id}")

    test_point_by_point(event_id)
    print()

    # Test 3: Other endpoints
    test_other_endpoints()

    print("\n" + "=" * 70)
    print("DONE — Copy the output above and share it so I can build the integration.")
    print("=" * 70)


if __name__ == "__main__":
    main()
