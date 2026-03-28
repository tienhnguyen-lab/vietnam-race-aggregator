"""
123Go.vn scraper — JS-rendered, uses Playwright.
URL: https://123go.vn/su-kien/chay-bo
"""
import re
import time
import logging
from typing import Any

from scrapers.base import BaseScraper
from scrapers.actiup import _parse_date, _extract_distances, _parse_price, _map_status

logger = logging.getLogger(__name__)

URLS = [
    ("Road",  "https://123go.vn/su-kien/chay-bo"),
    ("Trail", "https://123go.vn/su-kien/trail-running"),
    ("Triathlon", "https://123go.vn/su-kien/triathlon"),
]


class Go123Scraper(BaseScraper):
    name = "123go"
    source_url = "https://123go.vn"

    def scrape(self) -> list[dict[str, Any]]:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        races: list[dict] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(locale="vi-VN")
            page = context.new_page()

            for race_type, url in URLS:
                self.logger.info("123Go: scraping %s …", url)
                try:
                    page.goto(url, wait_until="networkidle", timeout=30_000)
                    for _ in range(5):
                        page.evaluate("window.scrollBy(0, window.innerHeight)")
                        page.wait_for_timeout(800)

                    # 123Go uses .event-item or similar class names
                    cards = page.query_selector_all(
                        ".event-item, .race-card, [class*='EventItem'], [class*='event-item'], "
                        "a[href*='/su-kien/'], a[href*='/event/']"
                    )
                    self.logger.info("123Go %s: %d cards.", race_type, len(cards))

                    for card in cards:
                        race = _parse_123go_card(card, race_type)
                        if race:
                            races.append(race)

                except PWTimeout:
                    self.logger.warning("123Go: timeout on %s", url)
                except Exception as exc:
                    self.logger.warning("123Go error %s: %s", url, exc)

            browser.close()

        return races


def _parse_123go_card(card, race_type: str) -> dict | None:
    try:
        text = card.inner_text() or ""
        href = card.get_attribute("href") or ""
        if not href:
            link_el = card.query_selector("a[href]")
            href = link_el.get_attribute("href") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://123go.vn" + href

        name_el = card.query_selector("h2, h3, h4, [class*='title'], [class*='name']")
        race_name = name_el.inner_text().strip() if name_el else ""
        if not race_name or len(race_name) < 4:
            return None

        date_el = card.query_selector("[class*='date'], time")
        date_iso = _parse_date(date_el.inner_text().strip() if date_el else "")

        loc_el = card.query_selector("[class*='location'], [class*='place']")
        location = loc_el.inner_text().strip() if loc_el else ""

        distances = _extract_distances(text)
        status_text = ""
        status_el = card.query_selector("[class*='status'], [class*='badge']")
        if status_el:
            status_text = status_el.inner_text().strip().lower()
        status = _map_status(status_text)

        # Pricing extraction from card text
        pricing = _extract_card_pricing(text)

        return {
            "race_name": race_name,
            "date": date_iso,
            "location": location,
            "race_type": race_type,
            "distances": distances,
            "pricing": pricing,
            "official_website": href,
            "registration_url": href,
            "organizer": "",
            "registration_status": status,
        }
    except Exception as exc:
        logger.debug("123Go card parse error: %s", exc)
        return None


def _extract_card_pricing(text: str) -> dict:
    """Extract pricing from card text using regex."""
    pricing: dict = {}
    # Pattern: "21km ... 450,000" or "450.000đ"
    blocks = re.findall(
        r"(\d{1,2}\s*km)[\s\S]{0,200}?(\d{3}[\d,\.]{2,})\s*(VND|vnđ|đ|₫)?",
        text,
        re.IGNORECASE,
    )
    for dist, price_raw, _ in blocks:
        dist_key = re.sub(r"\s+", "", dist).lower()
        amount = _parse_price(price_raw)
        if dist_key and amount:
            pricing.setdefault(dist_key, {})
            pricing[dist_key]["regular"] = amount
            pricing[dist_key]["currency"] = "VND"
    return pricing
