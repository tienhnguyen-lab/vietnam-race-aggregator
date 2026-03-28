"""
iRace / ticket.irace.vn scraper.

The site runs the EventOn WordPress plugin. Selectors confirmed from live DOM inspection.
Uses Playwright because the site blocks plain HTTP requests (403).
"""
import re
import logging
from typing import Any
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers.actiup import _parse_price, _map_status
from scrapers.vietrace365 import _infer_type

logger = logging.getLogger(__name__)

BASE_URL   = "https://irace.vn"      # event listing lives here (EventOn plugin)
TICKET_URL = "https://ticket.irace.vn"  # registration links point here

# Vietnamese month names used by EventOn on this site
_VI_MONTHS = {
    "tháng 1": "01", "tháng 2": "02", "tháng 3": "03",
    "tháng 4": "04", "tháng 5": "05", "tháng 6": "06",
    "tháng 7": "07", "tháng 8": "08", "tháng 9": "09",
    "tháng 10": "10", "tháng 11": "11", "tháng 12": "12",
}

DETAIL_PRICE_RE = re.compile(
    r"(\d{1,2}\s*km|full|half|relay)[\s\S]{0,300}?"
    r"(\d{3}[\d,\.]{2,})\s*(VND|vnđ|đ|₫)?",
    re.IGNORECASE,
)


class IRaceScraper(BaseScraper):
    name = "irace"
    source_url = BASE_URL

    def scrape(self) -> list[dict[str, Any]]:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        races: list[dict] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(
                locale="vi-VN",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            try:
                # ── Pass 1: irace.vn homepage (EventOn plugin, rich schema data) ──
                self.logger.info("iRace pass 1: loading %s …", BASE_URL)
                page.goto(BASE_URL, wait_until="networkidle", timeout=40_000)
                for _ in range(6):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    page.wait_for_timeout(1500)

                soup = BeautifulSoup(page.content(), "lxml")
                event_rows = soup.select(".eventon_list_event")
                self.logger.info("iRace pass 1: %d EventOn rows.", len(event_rows))
                for row in event_rows:
                    race = _parse_eventon_row(row, page)
                    if race:
                        races.append(race)

                # ── Pass 2: ticket.irace.vn listing (more events, card-based) ──
                self.logger.info("iRace pass 2: loading %s …", TICKET_URL)
                page.goto(TICKET_URL, wait_until="networkidle", timeout=40_000)
                for _ in range(10):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    page.wait_for_timeout(1500)

                soup2 = BeautifulSoup(page.content(), "lxml")
                cards = soup2.select(".item-event")
                self.logger.info("iRace pass 2: %d ticket cards.", len(cards))
                for card in cards:
                    race = _parse_ticket_card(card, page)
                    if race:
                        races.append(race)

            except PWTimeout:
                self.logger.warning("iRace: page load timed out.")
            except Exception as exc:
                self.logger.warning("iRace error: %s", exc)
            finally:
                browser.close()

        return races


# ---------------------------------------------------------------------------
# EventOn row parser
# ---------------------------------------------------------------------------

def _parse_eventon_row(row, page) -> dict | None:
    try:
        schema = row.select_one(".evo_event_schema")

        # --- Race name: prefer JSON-LD, fall back to visible title element ---
        race_name = _jsonld_field(schema, "name") if schema else ""
        if not race_name:
            title_el = row.select_one(".evcal_event_title")
            race_name = title_el.get_text(strip=True) if title_el else ""
        if not race_name or len(race_name) < 4:
            return None

        # --- Date: read itemprop="startDate" — value is "2026-4-4T04:00+7:00" ---
        start_meta = schema.select_one('meta[itemprop="startDate"]') if schema else None
        date_iso = _parse_schema_date(start_meta["content"] if start_meta else "")

        # --- Location: itemprop="name" inside the Place block ---
        loc_place = schema.select_one('[itemprop="location"] [itemprop="name"]') if schema else None
        location = loc_place.get_text(strip=True) if loc_place else ""
        if not location:
            loc_el = row.select_one(".event_location")
            location = loc_el.get_text(strip=True) if loc_el else ""

        # --- Organizer: meta[itemprop="name"] inside the Organization block ---
        org_meta = schema.select_one('[itemprop="organizer"] meta[itemprop="name"]') if schema else None
        organizer = org_meta["content"] if org_meta else ""

        # --- Links ---
        ticket_el = row.select_one("a[href*='ticket.irace.vn']")
        ticket_href = ticket_el.get("href", "").split("?")[0] if ticket_el else ""

        irace_el = schema.select_one('a[itemprop="url"]') if schema else None
        irace_href = irace_el.get("href", "") if irace_el else ""

        # --- Status (CSS class on the row div) ---
        row_classes = row.get("class", [])
        if "completed-event" in row_classes:
            status = "Sold Out"
        elif "scheduled" in row_classes:
            status = "Open"
        else:
            status = "Unknown"

        # --- Image: meta[itemprop="image"] ---
        img_meta = schema.select_one('meta[itemprop="image"]') if schema else None
        image_url = img_meta["content"] if img_meta else ""

        # --- Distances + pricing: fetch ticket detail page ---
        desc = _jsonld_field(schema, "description") if schema else ""
        distances = _extract_distances(desc + " " + race_name)
        pricing: dict = {}
        if ticket_href:
            extra_distances, pricing = _fetch_ticket_detail(page, ticket_href)
            distances = list(dict.fromkeys(distances + extra_distances)) or distances

        # --- Race type ---
        race_type = _infer_type(race_name + " " + desc)

        return {
            "race_name": race_name,
            "date": date_iso,
            "location": location,
            "race_type": race_type,
            "distances": distances,
            "pricing": pricing,
            "image_url": image_url,
            "official_website": irace_href or ticket_href,
            "registration_url": ticket_href,
            "organizer": organizer,
            "registration_status": status,
        }

    except Exception as exc:
        logger.debug("iRace row parse error: %s", exc)
        return None


def _parse_ticket_card(card, page) -> dict | None:
    """
    Parse a .item-event card from ticket.irace.vn.
    Confirmed DOM structure:
      .card-img-top[data-bgimg]  → image URL
      h3 > a.card-title          → race name
      .item-info i.bx-calendar   → date  (e.g. "30/10 - 01/11/2026")
      .item-info i.bx-map        → location (e.g. "Gia Lai")
      .price .text-primary       → starting price
      a.btn-primary              → registration URL
    """
    try:
        # Skip carousel/section header cards that have no .card-info
        if not card.select(".item-info"):
            return None

        # --- Name ---
        name_el = card.select_one(".card-title, h3 a, h2 a")
        race_name = name_el.get_text(strip=True) if name_el else ""
        if not race_name or len(race_name) < 4:
            return None

        # --- Image ---
        img_el = card.select_one(".card-img-top, [data-bgimg]")
        image_url = ""
        if img_el:
            image_url = img_el.get("data-bgimg", "") or img_el.get("src", "")
        if not image_url:
            img_tag = card.select_one("img[src]")
            image_url = img_tag["src"] if img_tag else ""

        # --- Date & Location: strip <i> icon then read sibling text ---
        date_raw = ""
        location = ""
        for info_el in card.select(".item-info"):
            icon = info_el.select_one("i")
            icon_class = " ".join(icon.get("class", [])) if icon else ""
            if icon:
                icon.extract()
            text = info_el.get_text(strip=True)
            if "calendar" in icon_class:
                date_raw = text
            elif "map" in icon_class:
                location = text

        date_iso = _parse_ticket_date(date_raw)

        # --- Starting price from card ---
        price_el = card.select_one(".price .text-primary, .price span")
        price_text = price_el.get_text(strip=True) if price_el else ""
        starting_price = _parse_price(re.sub(r"[^\d]", "", price_text))

        # --- Registration URL ---
        link_el = card.select_one("a.btn-primary") or card.select_one("a[href*='ticket.irace.vn']")
        href = link_el.get("href", "").split("?")[0] if link_el else ""

        # --- Fetch detail page for distances + full pricing ---
        distances = _extract_distances(race_name)
        pricing: dict = {}
        if starting_price:
            pricing["starting"] = {"regular": starting_price, "currency": "VND"}
        if href:
            extra_distances, detail_pricing = _fetch_ticket_detail(page, href)
            distances = list(dict.fromkeys(distances + extra_distances)) or distances
            pricing = {**pricing, **detail_pricing}

        race_type = _infer_type(race_name)

        return {
            "race_name": race_name,
            "date": date_iso,
            "location": location,
            "race_type": race_type,
            "distances": distances,
            "pricing": pricing,
            "image_url": image_url,
            "official_website": href,
            "registration_url": href,
            "organizer": "",
            "registration_status": "Open" if href else "Unknown",
        }
    except Exception as exc:
        logger.debug("ticket.irace.vn card parse error: %s", exc)
        return None


# ── ticket.irace.vn date parsing ───────────────────────────────────────────

# Handles: "30/10 - 01/11/2026", "22-23/08/2026", "01/05/2026", "23/24/05/2026"
_DATE_RANGE_RE = re.compile(
    r"(\d{1,2})(?:[/\-]\d{1,2})?\s*[-–]\s*\d{1,2}[/\-](\d{1,2})[/\-](\d{4})"  # range
    r"|(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"                                      # single
)


def _parse_ticket_date(text: str) -> str:
    """Return YYYY-MM-DD from date strings on ticket.irace.vn."""
    if not text:
        return ""
    m = _DATE_RANGE_RE.search(text)
    if not m:
        return ""
    if m.group(1):   # range: capture start day, shared month and year
        day, month, year = m.group(1), m.group(2), m.group(3)
    else:            # single date
        day, month, year = m.group(4), m.group(5), m.group(6)
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _fetch_ticket_detail(page, url: str) -> tuple[list[str], dict]:
    """
    Visit a ticket.irace.vn detail page.
    Returns (distances, pricing) extracted from the ticket-type rows.

    Ticket rows look like:
      [distance label]  [tier label]  [price]
    e.g. "42km - Full Marathon"  "Early Bird"  "750.000đ"
    """
    distances: list[str] = []
    pricing: dict = {}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        soup = BeautifulSoup(page.content(), "lxml")

        # --- Ticket type rows (most structured source) ---
        ticket_rows = soup.select(
            ".ticket-type, .ticket-item, .ticket-row, "
            ".list-ticket li, table tr"
        )
        for tr in ticket_rows:
            text = tr.get_text(" ", strip=True)
            dist_m = re.search(r"(\d+\.?\d*)\s*(km|KM|K)\b", text, re.I)
            tier_m = re.search(r"(early|regular|late|normal|sớm)", text, re.I)
            price_m = re.search(r"([\d]{3}[\d,\.]{2,})\s*(đ|VND|₫)?", text)
            if dist_m:
                dist_key = f"{dist_m.group(1)}km"
                if dist_key not in distances:
                    distances.append(dist_key)
                amount = _parse_price(price_m.group(1)) if price_m else None
                if amount:
                    tier = _map_tier(tier_m.group(1) if tier_m else "regular")
                    pricing.setdefault(dist_key, {})
                    pricing[dist_key][tier] = amount
                    pricing[dist_key]["currency"] = "VND"

        # --- Fallback: regex across full body text ---
        if not distances:
            body = soup.get_text(" ")
            for num, _ in re.findall(r"(\d+\.?\d*)\s*(km|KM)\b", body, re.I):
                dist_key = f"{num}km"
                if dist_key not in distances:
                    distances.append(dist_key)

        if not pricing:
            body = soup.get_text(" ")
            for dist, price_raw, _ in DETAIL_PRICE_RE.findall(body):
                dist_key = re.sub(r"\s+", "", dist).lower()
                amount = _parse_price(price_raw)
                if dist_key and amount:
                    pricing.setdefault(dist_key, {})
                    pricing[dist_key]["regular"] = amount
                    pricing[dist_key]["currency"] = "VND"

    except Exception as exc:
        logger.debug("iRace detail fetch error %s: %s", url, exc)

    return distances, pricing


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_schema_date(value: str) -> str:
    """Convert '2026-4-4T04:00+7:00' → '2026-04-04'."""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return ""


def _jsonld_field(schema, field: str) -> str:
    """Extract a top-level field from the JSON-LD <script> inside the schema block."""
    if not schema:
        return ""
    script = schema.select_one("script[type='application/ld+json']")
    if not script:
        return ""
    import json
    try:
        data = json.loads(script.string or "")
        val = data.get(field, "")
        # Strip HTML tags from description
        if val and "<" in val:
            val = re.sub(r"<[^>]+>", " ", val)
            val = re.sub(r"\s+", " ", val).strip()
        return val
    except Exception:
        return ""


def _vi_month(text: str) -> str:
    for key, val in _VI_MONTHS.items():
        if key in text:
            return val
    # Fallback: look for a bare number
    m = re.search(r"\b(\d{1,2})\b", text)
    return m.group(1).zfill(2) if m else ""


def _extract_distances(text: str) -> list[str]:
    found = re.findall(r"(\d+\.?\d*)\s*(km|K|KM)", text)
    seen: set[str] = set()
    result = []
    for num, _ in found:
        label = f"{num}km"
        if label not in seen:
            result.append(label)
            seen.add(label)
    return result


def _map_tier(raw: str) -> str:
    raw = raw.lower()
    if "early" in raw or "sớm" in raw:
        return "early_bird"
    if "late" in raw:
        return "late"
    return "regular"
