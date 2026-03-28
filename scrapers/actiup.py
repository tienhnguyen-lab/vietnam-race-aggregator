"""
ActiUp.net scraper — calls api.actiup.net REST API directly.
Source page: https://actiup.net/vi/events/sports
API:         https://api.actiup.net/v2/content/events/paging
"""
import re
import logging
from typing import Any

import requests

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

API_BASE   = "https://api.actiup.net/v2/content/events/paging"
WEB_BASE   = "https://actiup.net/vi/event"
PAGE_SIZE  = 24

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept":   "application/json",
    "Referer":  "https://actiup.net/vi/events/sports",
    "Origin":   "https://actiup.net",
}


class ActiUpScraper(BaseScraper):
    name       = "actiup"
    source_url = "https://actiup.net/vi/events/sports"

    def scrape(self) -> list[dict[str, Any]]:
        races: list[dict] = []
        offset = 0

        while True:
            url = (
                f"{API_BASE}?event_type=sports"
                f"&limit={PAGE_SIZE}&offset={offset}"
                f"&price=&selling_type=&category_id=&event_time="
            )
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                self.logger.warning("ActiUp API error (offset=%d): %s", offset, exc)
                break

            items = payload.get("result", {}).get("data", [])
            if not items:
                break

            for item in items:
                race = _parse_item(item)
                if race:
                    races.append(race)

            offset += PAGE_SIZE
            total = payload.get("result", {}).get("paging", {}).get("total_item", 0)
            if offset >= total:
                break

        self.logger.info("ActiUp: %d race(s) fetched from API.", len(races))
        return races


# ---------------------------------------------------------------------------
# Item parser
# ---------------------------------------------------------------------------

def _parse_item(item: dict) -> dict | None:
    try:
        race_name = item.get("name", "").strip()
        if not race_name or len(race_name) < 4:
            return None

        slug = item.get("event_slug", "") or item.get("id", "")

        # Date: start_date is local Vietnam time "2026-06-21 00:00:00"
        date_iso = _parse_date(item.get("start_date", ""))

        # Location
        location = (item.get("short_place") or "").strip()

        # Image
        image_url = (item.get("banner_url") or item.get("square_url") or "").strip()

        # Links
        official_website  = f"{WEB_BASE}/{slug}" if slug else ""
        registration_url  = f"{WEB_BASE}/{item.get('id', slug)}/tickets" if slug else ""

        # Status
        selling_type = item.get("selling_type", "")
        status = _map_status(selling_type)

        # Price
        min_price = item.get("min_price")
        currency  = (item.get("currency") or "VND").upper()
        pricing   = {}
        if min_price:
            pricing = {"entry": {"regular": int(min_price), "currency": currency}}

        # Organizer
        organizer = (item.get("merchant_public_name") or "").strip()

        # Distances + race type from name
        distances  = _extract_distances(race_name)
        race_type  = _infer_type(race_name)

        return {
            "race_name":           race_name,
            "date":                date_iso,
            "location":            location,
            "race_type":           race_type,
            "distances":           distances,
            "pricing":             pricing,
            "image_url":           image_url,
            "official_website":    official_website,
            "registration_url":    registration_url,
            "organizer":           organizer,
            "registration_status": status,
        }
    except Exception as exc:
        logger.debug("ActiUp item parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Shared helpers (imported by vietrace365 and other scrapers)
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """Parse various date strings → YYYY-MM-DD."""
    if not raw:
        return ""
    # Already ISO: "2026-06-21 00:00:00" or "2026-06-21"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    # DD/MM/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return ""


def _extract_distances(text: str) -> list[str]:
    """Extract distance tokens like '21km', '42km', '70km', '5K' from text."""
    hits = re.findall(r"\b(\d+\.?\d*)\s*(?:km|K|KM)\b", text, re.IGNORECASE)
    seen, result = set(), []
    for h in hits:
        val = float(h)
        key = f"{int(val)}km" if val == int(val) else f"{val}km"
        if key not in seen:
            seen.add(key)
            result.append(key)
    return sorted(result, key=lambda x: float(re.search(r"[\d.]+", x).group()))


def _parse_price(raw: str) -> int | None:
    """Parse a Vietnamese price string like '450,000' or '450.000' → int."""
    cleaned = re.sub(r"[,\.\s]", "", raw).strip()
    return int(cleaned) if cleaned.isdigit() else None


def _map_status(raw: str) -> str:
    lower = raw.lower()
    if any(w in lower for w in ("selling", "open", "mở", "đang")):
        return "Open"
    if any(w in lower for w in ("sold", "hết", "full")):
        return "Sold Out"
    if any(w in lower for w in ("upcoming", "soon", "sắp")):
        return "Upcoming"
    return "Open" if raw else "Unknown"


def _infer_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("trail", "leo núi", "mountain", "jungle", "ultra")):
        return "Trail"
    if any(w in lower for w in ("triathlon", "ironman", "70.3", "sprint")):
        return "Triathlon"
    if "duathlon" in lower:
        return "Duathlon"
    return "Road"
