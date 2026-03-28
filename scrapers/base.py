"""
Abstract base class for all scrapers.
"""
import logging
from abc import ABC, abstractmethod
from typing import Any

from database.handler import DatabaseHandler
from utils.dedup import resolve_slug


logger = logging.getLogger(__name__)


class RaceDict(dict):
    """Typed alias for a race data dictionary."""
    REQUIRED = ("race_name",)


class BaseScraper(ABC):
    """
    All scrapers extend this class.  Subclasses must implement `scrape()`
    and return a list of raw race dicts.
    """

    name: str = "base"          # human-readable scraper identifier
    source_url: str = ""        # root URL being scraped

    def __init__(self, db: DatabaseHandler, headless: bool = True):
        self.db = db
        self.headless = headless
        self.logger = logging.getLogger(f"scraper.{self.name}")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(self) -> int:
        """
        Execute the scraper, deduplicate, persist, and log.
        Returns number of races upserted.
        """
        self.logger.info("Starting %s …", self.name)
        races_found = 0
        message = ""

        try:
            raw_races = self.scrape()
            existing_slugs = self.db.get_all_slugs()

            for raw in raw_races:
                race = self._enrich(raw, existing_slugs)
                self.db.upsert_race(race)
                # After upsert the slug is now in DB; keep list fresh
                if race["slug"] not in existing_slugs:
                    existing_slugs.append(race["slug"])
                races_found += 1

            self.db.log_scraper_run(self.name, "success", races_found)
            self.logger.info("%s finished — %d race(s) upserted.", self.name, races_found)

        except Exception as exc:
            message = str(exc)
            self.logger.exception("Error in %s: %s", self.name, message)
            self.db.log_scraper_run(self.name, "error", races_found, message)

        return races_found

    # ------------------------------------------------------------------
    # To be implemented by each scraper
    # ------------------------------------------------------------------
    @abstractmethod
    def scrape(self) -> list[dict[str, Any]]:
        """
        Fetch races from the source.
        Each dict should contain as many of these keys as available:

            race_name, date, location, city, race_type,
            distances, pricing, official_website,
            registration_url, organizer, registration_status
        """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _enrich(self, raw: dict, existing_slugs: list[str]) -> dict:
        """Add slug and source tag; sanitise types."""
        race = dict(raw)
        race.setdefault("race_type", "Road")
        race.setdefault("registration_status", "Unknown")
        race.setdefault("organizer", "")
        race.setdefault("distances", [])
        race.setdefault("pricing", {})
        race.setdefault("city", _extract_city(race.get("location", "")))

        # Normalise distances to list of strings
        if isinstance(race["distances"], str):
            race["distances"] = [d.strip() for d in race["distances"].split(",") if d.strip()]

        # Resolve / deduplicate slug
        race["slug"] = resolve_slug(
            race["race_name"],
            race.get("date", ""),
            existing_slugs,
        )

        # Tag source
        race["sources"] = [self.name]
        return race


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
_CITY_KEYWORDS = {
    "hanoi": "Hanoi",
    "ha noi": "Hanoi",
    "hcmc": "Ho Chi Minh City",
    "ho chi minh": "Ho Chi Minh City",
    "saigon": "Ho Chi Minh City",
    "da nang": "Da Nang",
    "danang": "Da Nang",
    "hue": "Hue",
    "hội an": "Hoi An",
    "hoi an": "Hoi An",
    "nha trang": "Nha Trang",
    "phu quoc": "Phu Quoc",
    "phú quốc": "Phu Quoc",
    "can tho": "Can Tho",
    "cần thơ": "Can Tho",
    "quy nhon": "Quy Nhon",
    "quy nhơn": "Quy Nhon",
    "vung tau": "Vung Tau",
    "vũng tàu": "Vung Tau",
    "ha long": "Ha Long",
    "hạ long": "Ha Long",
    "sa pa": "Sa Pa",
    "sapa": "Sa Pa",
    "lai chau": "Lai Chau",
    "dong nai": "Dong Nai",
    "binh duong": "Binh Duong",
}


def _extract_city(location: str) -> str:
    if not location:
        return ""
    loc_lower = location.lower()
    for keyword, city in _CITY_KEYWORDS.items():
        if keyword in loc_lower:
            return city
    # Fallback: return everything after the last comma (often the city)
    parts = [p.strip() for p in location.split(",")]
    return parts[-1] if parts else location
