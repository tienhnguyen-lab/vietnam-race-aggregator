"""
VnExpress Marathon schedule scraper — server-rendered HTML, BeautifulSoup4.
Source: https://vnexpress.net/the-thao/marathon/lich-giai-chay

Provides: race name, date, location, race type, distances, image.
No registration URL or pricing (editorial listing only).
"""
import re
import logging
from typing import Any

import requests
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.actiup import _extract_distances

logger = logging.getLogger(__name__)

URL = "https://vnexpress.net/the-thao/marathon/lich-giai-chay"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    "Referer": "https://vnexpress.net/the-thao/marathon",
}


class VnExpressScheduleScraper(BaseScraper):
    name       = "vnexpress_schedule"
    source_url = URL

    def scrape(self) -> list[dict[str, Any]]:
        try:
            resp = requests.get(URL, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            self.logger.error("VnExpress request error: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        races = _parse_schedule(soup)
        self.logger.info("VnExpress schedule: %d race(s) parsed.", len(races))
        return races


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_schedule(soup: BeautifulSoup) -> list[dict]:
    races = []
    container = soup.select_one("#content-list-tournament")
    if not container:
        logger.warning("VnExpress: #content-list-tournament not found — page structure may have changed.")
        return races

    for item in container.select(".item"):
        race = _parse_item(item)
        if race:
            races.append(race)

    return races


def _parse_item(item) -> dict | None:
    try:
        # Name: prefer p[title] attribute, fall back to p text
        name_el = item.select_one("p[title]")
        race_name = (name_el.get("title") or name_el.get_text(strip=True)) if name_el else ""
        race_name = race_name.strip()
        if not race_name or len(race_name) < 4:
            return None

        # Date: find nearest preceding h2.date-month for year + month
        h2 = item.find_previous("h2", class_="date-month")
        year = 2026
        month_num = 1
        if h2:
            year_m = re.search(r"(\d{4})", h2.get_text())
            if year_m:
                year = int(year_m.group(1))
            dm = h2.get("data-month")
            if dm and dm.isdigit():
                month_num = int(dm)

        # Date text: "29/03" → day=29
        date_el = item.select_one(".date .month")
        day = 1
        if date_el:
            raw = date_el.get_text(strip=True)   # "29/03"
            parts = raw.split("/")
            if parts and parts[0].isdigit():
                day = int(parts[0])

        date_iso = f"{year}-{month_num:02d}-{day:02d}"

        # Location
        loc_el = item.select_one(".location .address")
        location = loc_el.get_text(strip=True) if loc_el else ""

        # Race type: span inside .flex.note (e.g. "Road", "Trail")
        note_el = item.select_one(".flex.note")
        race_type = "Road"
        if note_el:
            spans = note_el.find_all("span", recursive=False)
            for sp in spans:
                txt = sp.get_text(strip=True).lower()
                if "trail" in txt:
                    race_type = "Trail"
                    break
                if "triathlon" in txt or "ironman" in txt:
                    race_type = "Triathlon"
                    break

        # Distances: each span in .distance
        dist_el = item.select_one(".distance")
        distances = []
        if dist_el:
            for sp in dist_el.select("span"):
                txt = sp.get_text(strip=True)
                extracted = _extract_distances(txt)
                distances.extend(extracted)
            # deduplicate while preserving order
            seen = set()
            distances = [d for d in distances if not (d in seen or seen.add(d))]

        if not distances:
            distances = _extract_distances(race_name)

        # Image
        img_el = item.select_one(".thumb-art img")
        image_url = img_el.get("src", "") if img_el else ""

        return {
            "race_name":           race_name,
            "date":                date_iso,
            "location":            location,
            "race_type":           race_type,
            "distances":           distances,
            "pricing":             {},
            "image_url":           image_url,
            "official_website":    URL,
            "registration_url":    "",
            "organizer":           "",
            "registration_status": "Upcoming",
        }
    except Exception as exc:
        logger.debug("VnExpress item parse error: %s", exc)
        return None
