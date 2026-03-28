"""
Vietnam Race Aggregator — Web UI
Run:  python app.py
Then open: http://localhost:5001
"""
import json
import os
import sys
import logging
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from database.handler import DatabaseHandler, DB_PATH

app    = Flask(__name__, static_folder="static", template_folder="static")
logger = logging.getLogger(__name__)

SEED_FILE = Path(__file__).parent / "seed_data.json"
SYNC_KEY  = os.environ.get("SYNC_KEY", "")   # must be set — endpoint blocked if missing

# ── /api/sync concurrency + rate-limit state ─────────────────────────────────
_sync_lock        = threading.Lock()   # prevents two syncs running at once (#13)
_sync_running     = False              # flag visible to status endpoint
_sync_last_called = 0.0               # epoch seconds of last /api/sync request
SYNC_COOLDOWN_SEC = 300               # minimum 5 min between syncs (#11)


# ── Auto-seed on startup ─────────────────────────────────────────────────────

def _seed_if_empty() -> None:
    """Load seed_data.json into the DB if the races table is empty."""
    if not SEED_FILE.exists():
        return
    with DatabaseHandler() as db:
        count = db.conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
        if count > 0:
            return
        rows  = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        cols  = [c for c in rows[0].keys() if c != "id"]
        ph    = ", ".join("?" * len(cols))
        col_s = ", ".join(cols)
        sql   = f"INSERT OR IGNORE INTO races ({col_s}) VALUES ({ph})"
        db.conn.executemany(sql, [[r[c] for c in cols] for r in rows])
        db.conn.commit()
        logger.info("Seeded %d races from seed_data.json", len(rows))

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


def _run_sync_background() -> dict:
    """
    Run non-Playwright scrapers in a background thread.
    Returns a results dict written to the thread's local scope.
    """
    global _sync_running
    results = {}
    try:
        from scrapers.actiup             import ActiUpScraper
        from scrapers.truerace           import TrueRaceScraper
        from scrapers.vietrace365        import VietRace365Scraper
        from scrapers.vnexpress_schedule import VnExpressScheduleScraper

        scraper_classes = [
            ActiUpScraper,
            TrueRaceScraper,
            VietRace365Scraper,
            VnExpressScheduleScraper,
        ]
        with DatabaseHandler() as db:
            for cls in scraper_classes:
                s = cls(db)
                try:
                    races = s.scrape()
                    for r in races:
                        db.upsert_race(r, source=s.name)
                    results[s.name] = {"ok": True, "count": len(races)}
                    logger.info("sync: %s → %d races", s.name, len(races))
                except Exception as exc:
                    results[s.name] = {"ok": False, "error": str(exc)}
                    logger.warning("sync: %s failed: %s", s.name, exc)
    except Exception as exc:
        logger.error("sync background thread crashed: %s", exc)
    finally:
        _sync_running = False

    return results


@app.post("/api/sync")
def api_sync():
    """
    Trigger a live scrape (non-Playwright sources only).
    Requires X-Sync-Key header matching the SYNC_KEY env var.
    Returns immediately — scraping runs in a background thread.
    Min 5 minutes between calls; concurrent calls are rejected.
    """
    global _sync_running, _sync_last_called

    # ── Auth (#security) ─────────────────────────────────────────────────────
    if not SYNC_KEY or request.headers.get("X-Sync-Key") != SYNC_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    # ── Rate limit: 5-min cooldown (#11) ─────────────────────────────────────
    elapsed = time.time() - _sync_last_called
    if elapsed < SYNC_COOLDOWN_SEC:
        wait = int(SYNC_COOLDOWN_SEC - elapsed)
        return jsonify({"error": f"Too soon. Try again in {wait}s."}), 429

    # ── Concurrency lock (#6 + #13) ───────────────────────────────────────────
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"error": "Sync already in progress."}), 409

    _sync_running     = True
    _sync_last_called = time.time()

    thread = threading.Thread(target=_run_sync_background, daemon=True)
    thread.start()

    # Release lock when thread finishes
    def _release():
        thread.join()
        _sync_lock.release()
    threading.Thread(target=_release, daemon=True).start()

    return jsonify({"status": "started", "note": "Check /api/sync/status for progress."}), 202


@app.get("/api/sync/status")
def api_sync_status():
    """Quick poll endpoint — is a sync currently running?"""
    return jsonify({
        "running":       _sync_running,
        "last_called":   _sync_last_called or None,
        "cooldown_secs": SYNC_COOLDOWN_SEC,
    })


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
