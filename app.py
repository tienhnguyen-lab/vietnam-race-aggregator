"""
Vietnam Race Aggregator — Web UI
Run:  python app.py
Then open: http://localhost:5001
"""
import json
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from database.handler import DatabaseHandler

app = Flask(__name__, static_folder="static", template_folder="static")


# ── helpers ─────────────────────────────────────────────────────────────────

def _races_to_response(races: list[dict]) -> list[dict]:
    """Strip heavy/internal fields for the API response."""
    keep = (
        "id", "race_name", "date", "location", "city", "race_type",
        "distances", "registration_status", "image_url", "organizer",
        "registration_url", "official_website", "sources",
    )
    return [{k: r[k] for k in keep if k in r} for r in races]


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/races")
def api_races():
    sort     = request.args.get("sort", "date")
    race_type = request.args.get("type")
    city     = request.args.get("city")
    status   = request.args.get("status")
    q        = request.args.get("q", "").strip().lower()

    # Map URL status values → DB values
    status_map = {
        "open": "Open", "upcoming": "Upcoming",
        "sold-out": "Sold Out", "unknown": "Unknown",
    }
    db_status = status_map.get((status or "").lower())

    with DatabaseHandler() as db:
        races = db.list_races(
            sort=sort,
            race_type=race_type,
            location=city,
            status=db_status,
        )

    # Text search (client can also do this, but server-side is faster)
    if q:
        races = [
            r for r in races
            if q in (r.get("race_name") or "").lower()
            or q in (r.get("city") or "").lower()
            or q in (r.get("location") or "").lower()
        ]

    return jsonify(_races_to_response(races))


@app.get("/api/meta")
def api_meta():
    """Return distinct cities and race types for filter dropdowns."""
    with DatabaseHandler() as db:
        races = db.list_races()

    cities = sorted(
        {r["city"] for r in races if r.get("city") and r["city"].strip()},
        key=str.lower,
    )
    types = sorted(
        {r["race_type"] for r in races if r.get("race_type")},
        key=str.lower,
    )
    return jsonify({"cities": cities, "types": types})


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
