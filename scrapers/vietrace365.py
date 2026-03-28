"""
VietRace365.vn scraper — renders the AngularJS page with Playwright and
parses the .marathon-item cards directly from the rendered DOM.

Source: https://vietrace365.vn/marathon
Tabs scraped: "Sắp diễn ra" (upcoming) + "Đang diễn ra" (ongoing)
"""
import re
import logging
from typing import Any
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.actiup import _extract_distances

logger = logging.getLogger(__name__)

SOURCE_URL = "https://vietrace365.vn/marathon"

# Tabs to click through (text visible on the page)
TABS = ["Sắp diễn ra", "Đang diễn ra"]


class VietRace365Scraper(BaseScraper):
    name       = "vietrace365"
    source_url = SOURCE_URL

    def scrape(self) -> list[dict[str, Any]]:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        seen_names: set[str] = set()
        races: list[dict]    = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            )
            page = context.new_page()

            try:
                self.logger.info("VietRace365: loading %s …", SOURCE_URL)
                page.goto(SOURCE_URL, wait_until="networkidle", timeout=40_000)
                page.wait_for_timeout(4_000)

                for tab_text in TABS:
                    self.logger.info("VietRace365: clicking tab '%s' …", tab_text)
                    try:
                        page.click(f"text={tab_text}", timeout=5_000)
                        page.wait_for_timeout(3_000)
                    except PWTimeout:
                        self.logger.debug("Tab '%s' not found — skipping.", tab_text)
                        continue

                    # Paginate through all pages for this tab
                    page_num = 1
                    while True:
                        # Scroll down to trigger lazy-loading
                        for _ in range(4):
                            page.evaluate("window.scrollBy(0, window.innerHeight)")
                            page.wait_for_timeout(800)

                        soup = BeautifulSoup(page.content(), "lxml")
                        items = soup.select(".marathon-item")
                        self.logger.info(
                            "VietRace365 tab='%s' page=%d: %d item(s).",
                            tab_text, page_num, len(items),
                        )

                        for item in items:
                            race = _parse_item(item)
                            if race and race["race_name"] not in seen_names:
                                seen_names.add(race["race_name"])
                                races.append(race)

                        # Try clicking the next-page arrow (›)
                        try:
                            next_btn = page.query_selector("a[ng-click*='next'], .pagination .next a, li.next a")
                            if next_btn and next_btn.is_visible():
                                next_btn.click()
                                page.wait_for_timeout(2_500)
                                page_num += 1
                            else:
                                break
                        except Exception:
                            break

            except PWTimeout:
                self.logger.warning("VietRace365: page load timed out.")
            except Exception as exc:
                self.logger.warning("VietRace365 error: %s", exc)
            finally:
                browser.close()

        self.logger.info("VietRace365: %d unique race(s) scraped.", len(races))
        return races


# ---------------------------------------------------------------------------
# Item parser
# ---------------------------------------------------------------------------

def _parse_item(item) -> dict | None:
    """Parse a single .marathon-item div from the rendered DOM."""
    try:
        # Race name
        name_el = item.select_one(".item-title")
        race_name = name_el.get_text(strip=True) if name_el else ""
        if not race_name or len(race_name) < 4:
            return None

        # Official detail link (first <a> with timve365 event href)
        detail_link = ""
        reg_link    = ""
        for a in item.select("a[href]"):
            href = a.get("href", "")
            if "/registration" in href:
                reg_link = href
            elif "timve365.vn/events/" in href and not detail_link:
                detail_link = href

        # Date: span following icon-time — format "28/11/26 11:00 AM"
        date_iso = ""
        time_span = item.select_one(".cicon.icon-time")
        if time_span:
            sibling = time_span.find_next_sibling("span")
            if sibling:
                date_iso = _parse_date(sibling.get_text(strip=True))

        # Location: span following icon-location
        location = ""
        loc_span = item.select_one(".cicon.icon-location")
        if loc_span:
            sibling = loc_span.find_next_sibling("span")
            if sibling:
                location = sibling.get_text(strip=True)

        # Image
        img_el = item.select_one("img[src]")
        image_url = img_el.get("src", "") if img_el else ""

        # Price: span.ng-binding inside .item-price
        price_el = item.select_one(".item-price .ng-binding")
        pricing  = _parse_sale_price(price_el.get_text(strip=True) if price_el else "")

        # Status: if a registration link exists assume Open
        status = "Open" if reg_link else "Upcoming"

        # Distances + race type from name
        distances = _extract_distances(race_name)
        race_type = _infer_type(race_name)

        return {
            "race_name":           race_name,
            "date":                date_iso,
            "location":            location,
            "race_type":           race_type,
            "distances":           distances,
            "pricing":             pricing,
            "image_url":           image_url,
            "official_website":    detail_link or SOURCE_URL,
            "registration_url":    reg_link or detail_link,
            "organizer":           "",
            "registration_status": status,
        }
    except Exception as exc:
        logger.debug("VietRace365 item parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """
    AngularJS formats the date as "dd/MM/yy h:mm a", e.g. "28/11/26 11:00 AM".
    Convert to YYYY-MM-DD.
    """
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2})", raw.strip())
    if m:
        day, month, yy = m.groups()
        year = 2000 + int(yy)
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def _parse_sale_price(sale_price: str) -> dict:
    """
    "169,000 - 799,000" → {"entry": {"regular": 169000, "late": 799000, "currency": "VND"}}
    "600,000"           → {"entry": {"regular": 600000, "currency": "VND"}}
    """
    if not sale_price:
        return {}
    parts   = [p.strip() for p in sale_price.split("-")]
    amounts = []
    for p in parts:
        cleaned = re.sub(r"[,\.]", "", p).strip()
        if cleaned.isdigit():
            amounts.append(int(cleaned))
    if not amounts:
        return {}
    lo, hi = min(amounts), max(amounts)
    tier: dict = {"regular": lo, "currency": "VND"}
    if hi != lo:
        tier["late"] = hi
    return {"entry": tier}


def _infer_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("trail", "leo núi", "mountain", "jungle", "ultra")):
        return "Trail"
    if any(w in lower for w in ("triathlon", "ironman", "70.3")):
        return "Triathlon"
    if "duathlon" in lower:
        return "Duathlon"
    return "Road"
