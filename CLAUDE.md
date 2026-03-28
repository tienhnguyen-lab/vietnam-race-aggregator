# Vietnam Endurance Race Aggregator — Project Status

## Overview
A Python/SQLite CLI tool that aggregates 2026–2027 endurance race data (Road, Trail, Triathlon, Ironman) from Vietnamese aggregator sites and organizer pages, with deduplication, pricing tier tracking, and rich terminal output.

## Architecture

```
vietnam-race-aggregator/
├── main.py                  # CLI entry point (click + rich)
├── requirements.txt
├── races.db                 # SQLite database (auto-created on first run)
├── database/
│   ├── __init__.py
│   └── handler.py           # Schema, upsert/merge logic, query helpers
├── scrapers/
│   ├── __init__.py
│   ├── base.py              # BaseScraper ABC + city-extraction helper
│   ├── actiup.py            # ActiUp.net — Playwright (JS-rendered)
│   ├── go123.py             # 123Go.vn — Playwright (JS-rendered)
│   ├── vietrace365.py       # VietRace365.vn — BeautifulSoup4
│   ├── irace.py             # iRace.vn — BeautifulSoup4 + detail page pricing
│   └── organizers.py        # Sunrise Events, VnExpress Marathon, VTS, DHA, Pulse Active
└── utils/
    ├── __init__.py
    └── dedup.py             # Fuzzy slug generation + match resolution (thefuzz)
```

## Database Schema

### `races` table
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| race_name | TEXT | Display name |
| slug | TEXT UNIQUE | Normalised dedup key (ASCII, hyphenated, +year) |
| date | TEXT | ISO-8601 (YYYY-MM-DD) |
| location | TEXT | Raw venue string |
| city | TEXT | Extracted city for filtering |
| race_type | TEXT | Road / Trail / Triathlon / Ironman / Duathlon / Other |
| distances | TEXT | JSON array e.g. `["5km","10km","21km","42km"]` |
| pricing | TEXT | JSON: `{dist: {early_bird, regular, late, currency}}` |
| official_website | TEXT | |
| registration_url | TEXT | Direct registration link |
| organizer | TEXT | |
| registration_status | TEXT | Open / Sold Out / Upcoming / Unknown |
| sources | TEXT | JSON array of scraper names that contributed |
| last_updated | TEXT | ISO-8601 datetime |

### `scraper_log` table
Tracks every scraper run: scraper name, timestamp, success/error, races found, error message.

## CLI Commands

```bash
# Sync all scrapers
python main.py sync

# Sync a single scraper (useful for debugging)
python main.py sync --scraper actiup

# Show browser window for JS scrapers
python main.py sync --visible

# List races (default: sorted by date)
python main.py list

# Sort by price (cheapest 21km/42km first)
python main.py list --sort price

# Sort by name
python main.py list --sort name

# Filter by race type
python main.py list --filter-type trail
python main.py list --filter-type road

# Filter by city
python main.py list --location "Da Nang"
python main.py list --location "Hanoi"
python main.py list --location "Ho Chi Minh"

# Filter by registration status
python main.py list --status open
python main.py list --status upcoming

# Combine filters
python main.py list --sort price --filter-type trail --location "Da Nang" --distance 21km

# JSON output for scripting
python main.py list --json

# Scraper health dashboard
python main.py health
```

## Setup

```bash
cd vietnam-race-aggregator
pip install -r requirements.txt
playwright install chromium   # Required for JS-rendered sites (ActiUp, 123Go)
```

## Scraper Health

Run `python main.py health` after syncing to see the health dashboard.

| Scraper | Engine | Target | Priority |
|---|---|---|---|
| `actiup` | Playwright | https://actiup.net | HIGH |
| `123go` | Playwright | https://123go.vn | HIGH |
| `vietrace365` | BeautifulSoup4 | https://vietrace365.vn | HIGH |
| `irace` | BeautifulSoup4 | https://irace.vn | HIGH |
| `sunrise_events` | BeautifulSoup4 | https://sunriseevents.vn | MEDIUM |
| `vnexpress_marathon` | BeautifulSoup4 | https://marathon.vnexpress.net | MEDIUM |
| `vietnam_trail_series` | BeautifulSoup4 | https://vietnamtrailseries.com | MEDIUM |
| `dha_vietnam` | BeautifulSoup4 | https://dhavietnam.vn | MEDIUM |
| `pulse_active` | BeautifulSoup4 | https://pulseactive.vn | MEDIUM |

## Deduplication Logic

1. Each race name is normalised: lowercased, diacritics stripped, punctuation removed, spaces collapsed.
2. A **slug** is formed: `{normalised-name}-{year}` (e.g. `vnexpress-marathon-da-nang-2026`).
3. Before insertion, the candidate slug is fuzzy-matched (token sort ratio ≥ 82) against all existing slugs.
4. If a match is found, data is **merged** into the existing row (fill blanks, merge distances list, merge pricing, append source name).
5. If no match, a new row is inserted.

## Pricing JSON Format

```json
{
  "21km": {
    "early_bird": 450000,
    "regular": 550000,
    "late": 650000,
    "currency": "VND"
  },
  "42km": {
    "early_bird": 750000,
    "regular": 900000,
    "currency": "VND"
  }
}
```

## Known Limitations & Maintenance Notes

- **ActiUp / 123Go**: CSS selectors may break if the sites redesign their React components.
  Re-inspect with browser devtools and update the `query_selector_all` patterns in the respective scraper.

- **Pricing extraction**: Uses regex against raw page text. Structured data (tables, JSON-LD) is preferred when available.

- **Rate limiting**: No explicit delays between requests on BS4 scrapers. Add `time.sleep(1)` loops if you see 429 errors.

- **VTS / DHA / Pulse Active**: URL patterns assumed; verify against actual live sites and update `URLS` lists accordingly.

- **iRace detail-page pricing**: Makes one HTTP request per card — can be slow for large listings. Consider caching or async fetching.

## TODO / Roadmap

- [ ] Add async scraping (aiohttp + asyncio) for BS4 scrapers to speed up detail-page pricing
- [ ] Add price alert / watchlist feature (email/webhook when early bird opens)
- [ ] Export to CSV / Google Sheets
- [ ] Add Docker support for scheduled cron sync
- [ ] Scraper for XTERRA Vietnam, Merrel Vietnam Trail
- [ ] Add `--year` filter to CLI
