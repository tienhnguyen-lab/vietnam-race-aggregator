"""
Vietnam Race Aggregator — Web UI
Run:  python app.py
Then open: http://localhost:5001
"""
import os
import sys
import logging
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from database.handler import DatabaseHandler, DB_PATH

app    = Flask(__name__, static_folder="static", template_folder="static")
logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).parent / "seed_data.json"
SYNC_KEY  = os.environ.get("SYNC_KEY", "")   # must be set — endpoint blocked if missing

# ── /api/sync rate-limit state (best-effort; per warm instance) ──────────────
_sync_last_called = 0.0               # epoch seconds of last manual sync request
SYNC_COOLDOWN_SEC = 300               # minimum 5 min between manual syncs


# ── Auto-seed on startup ─────────────────────────────────────────────────────

def _seed_if_empty() -> None:
    """Load seed_data.json into the DB if the races table is empty."""
    with DatabaseHandler() as db:
        added = db.seed_if_empty(SEED_FILE)
        if added:
            logger.info("Seeded %d races from seed_data.json", added)

with app.app_context():
    _seed_if_empty()


# ── helpers ──────────────────────────────────────────────────────────────────

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
    sort      = request.args.get("sort", "date")
    race_type = request.args.get("type")
    city      = request.args.get("city")
    status    = request.args.get("status")
    q         = request.args.get("q", "").strip().lower()

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


def _run_scrapers_sync() -> dict:
    """
    Run the serverless-safe scrapers synchronously and persist results.

    Only `requests`/BeautifulSoup scrapers are included — Playwright-based
    sources (123Go, iRace, VietRace365) need a headless Chromium binary that
    isn't available in Vercel's serverless runtime, so they're intentionally
    excluded here. Run those locally via `python main.py sync` and commit the
    refreshed seed_data.json instead.

    Each scraper's `.run()` handles enrich → dedup → upsert → scraper_log,
    and swallows its own errors, so one failing source won't abort the rest.
    """
    from scrapers.actiup             import ActiUpScraper
    from scrapers.truerace           import TrueRaceScraper
    from scrapers.vnexpress_schedule import VnExpressScheduleScraper

    scraper_classes = [ActiUpScraper, TrueRaceScraper, VnExpressScheduleScraper]
    results: dict = {}
    with DatabaseHandler() as db:
        for cls in scraper_classes:
            s = cls(db)
            try:
                count = s.run()
                results[s.name] = {"ok": True, "count": count}
                logger.info("sync: %s → %d races", s.name, count)
            except Exception as exc:  # run() normally swallows, but be defensive
                results[s.name] = {"ok": False, "error": str(exc)}
                logger.warning("sync: %s failed: %s", s.name, exc)
    return results


def _sync_authorized() -> bool:
    """
    Authorize a sync request. Vercel Cron sends `Authorization: Bearer $CRON_SECRET`;
    manual callers may instead send the `X-Sync-Key` header. At least one of the two
    secrets must be configured, otherwise the endpoint stays closed.
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    if not cron_secret and not SYNC_KEY:
        return False
    if cron_secret and request.headers.get("Authorization") == f"Bearer {cron_secret}":
        return True
    if SYNC_KEY and request.headers.get("X-Sync-Key") == SYNC_KEY:
        return True
    return False


@app.route("/api/cron/sync", methods=["GET", "POST"])
def api_cron_sync():
    """
    Live scrape of the serverless-safe sources. Invoked on a schedule by Vercel
    Cron (GET + Bearer CRON_SECRET), or manually with the X-Sync-Key header.
    Runs synchronously — serverless functions are frozen after the response is
    returned, so a background thread would never finish.
    """
    global _sync_last_called

    if not _sync_authorized():
        # 503 if the server has no secret configured at all; 401 if the caller
        # simply presented the wrong one.
        if not os.environ.get("CRON_SECRET") and not SYNC_KEY:
            return jsonify({"error": "Sync not configured (set CRON_SECRET or SYNC_KEY)."}), 503
        return jsonify({"error": "Unauthorized"}), 401

    _sync_last_called = time.time()
    results = _run_scrapers_sync()
    return jsonify({"status": "ok", "results": results})


@app.post("/api/sync")
def api_sync():
    """
    Backwards-compatible manual trigger. Same behaviour as /api/cron/sync but
    accepts POST + X-Sync-Key only, and keeps the 5-minute cooldown.
    """
    global _sync_last_called

    if not SYNC_KEY or request.headers.get("X-Sync-Key") != SYNC_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    elapsed = time.time() - _sync_last_called
    if elapsed < SYNC_COOLDOWN_SEC:
        wait = int(SYNC_COOLDOWN_SEC - elapsed)
        return jsonify({"error": f"Too soon. Try again in {wait}s."}), 429

    _sync_last_called = time.time()
    results = _run_scrapers_sync()
    return jsonify({"status": "ok", "results": results})


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
