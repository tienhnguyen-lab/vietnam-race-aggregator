"""
TrueRace.org scraper — calls timing-api.truerace.org REST API directly.
Source page: https://truerace.org
API:         https://timing-api.truerace.org/api/events
"""
import re
import logging
from typing import Any

import requests

from scrapers.base import BaseScraper
from scrapers.actiup import _extract_distances, _infer_type

logger = logging.getLogger(__name__)

API_BASE = "https://timing-api.truerace.org/api/events"
WEB_BASE = "https://truerace.org"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept":  "application/json",
    "Referer": "https://truerace.org/",
    "Origin":  "https://truerace.org",
}

# Sections to fetch — include ended (TrueRace is a timing platform; all events end up here)
SECTIONS = ("featured", "live", "upcoming", "opening", "ended")

# Only keep events from this year onward
MIN_YEAR = 2026


class TrueRaceScraper(BaseScraper):
    name       = "truerace"
    source_url = "https://truerace.org"

    def scrape(self) -> list[dict[str, Any]]:
        seen_ids: set[int] = set()
        races: list[dict]  = []

        for section in SECTIONS:
            page = 1
            while True:
                url = f"{API_BASE}?section={section}&page={page}"
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=20)
                    resp.raise_for_status()
                    payload = resp.json()
                except Exception as exc:
                    self.logger.warning("TrueRace API error (section=%s page=%d): %s", section, page, exc)
                    break

                items = payload.get("data", [])
                if not items:
                    break

                stop_early = False
                for item in items:
                    eid = item.get("event_id")
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)

                    # Skip events older than MIN_YEAR
                    date_str = _parse_date(item.get("event_startdate", ""))
                    if date_str and int(date_str[:4]) < MIN_YEAR:
                        stop_early = True   # results are sorted newest-first; stop paginating
                        continue

                    race = _parse_item(item)
                    if race:
                        races.append(race)

                last_page = payload.get("last_page", 1)
                if page >= last_page or stop_early:
                    break
                page += 1

        self.logger.info("TrueRace: %d race(s) fetched.", len(races))
        return races


# ---------------------------------------------------------------------------
# Item parser
# ---------------------------------------------------------------------------

def _parse_item(item: dict) -> dict | None:
    try:
        race_name = (item.get("event_name_vi") or "").strip()
        if not race_name or len(race_name) < 4:
            return None

        slug = item.get("slug", "") or str(item.get("event_id", ""))

        # Date: "15/03/2026" → "2026-03-15"
        date_iso = _parse_date(item.get("event_startdate", ""))

        # Location
        location = (item.get("place_vi") or "").strip()

        # Image: prefer banner, fall back to poster
        image_url = (item.get("event_banner") or item.get("event_poster") or "").strip()

        # Links
        official_website = f"{WEB_BASE}/{slug}" if slug else ""
        registration_url = official_website   # TrueRace links to event page; reg may be external

        # Status
        status = _map_status(item.get("status") or "")

        # Sport type → race type
        sport = (item.get("sport") or "").lower()
        if "trail" in sport or "leo núi" in sport or "mountain" in sport:
            race_type = "Trail"
        elif "triathlon" in sport or "ironman" in sport:
            race_type = "Triathlon"
        else:
            race_type = _infer_type(race_name)

        distances = _extract_distances(race_name)

        return {
            "race_name":           race_name,
            "date":                date_iso,
            "location":            location,
            "race_type":           race_type,
            "distances":           distances,
            "pricing":             {},          # TrueRace is a timing platform — no pricing data
            "image_url":           image_url,
            "official_website":    official_website,
            "registration_url":    registration_url,
            "organizer":           "",
            "registration_status": status,
        }
    except Exception as exc:
        logger.debug("TrueRace item parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """DD/MM/YYYY → YYYY-MM-DD."""
    if not raw:
        return ""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw.strip())
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def _map_status(raw: str) -> str:
    lower = raw.lower()
    if any(w in lower for w in ("sắp", "upcoming", "soon", "opening", "mở đăng ký")):
        return "Upcoming"
    if any(w in lower for w in ("đang", "live", "diễn ra")):
        return "Open"
    if any(w in lower for w in ("kết thúc", "ended", "finished")):
        return "Unknown"
    return "Upcoming"
