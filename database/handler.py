"""
SQLite database handler for Vietnam Endurance Race Aggregator.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


DB_PATH = Path(__file__).parent.parent / "races.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    race_name           TEXT NOT NULL,
    slug                TEXT UNIQUE NOT NULL,          -- normalised key for dedup
    date                TEXT,                          -- ISO-8601 date string
    location            TEXT,
    city                TEXT,                          -- extracted city for filtering
    race_type           TEXT CHECK(race_type IN ('Road','Trail','Triathlon','Ironman','Duathlon','Other')),
    distances           TEXT,                          -- JSON array e.g. ["5km","10km","21km","42km"]
    pricing             TEXT,                          -- JSON: {distance: {early_bird, regular, late, currency}}
    official_website    TEXT,
    registration_url    TEXT,
    organizer           TEXT,
    registration_status TEXT CHECK(registration_status IN ('Open','Sold Out','Upcoming','Unknown'))
                            DEFAULT 'Unknown',
    image_url           TEXT,                          -- primary banner/thumbnail image
    sources             TEXT,                          -- JSON array of source names
    last_updated        TEXT                           -- ISO-8601 datetime
);

CREATE TABLE IF NOT EXISTS scraper_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper     TEXT NOT NULL,
    run_at      TEXT NOT NULL,
    status      TEXT NOT NULL,     -- success / error
    races_found INTEGER DEFAULT 0,
    message     TEXT
);
"""


class DatabaseHandler:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self):
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._apply_schema()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("Call connect() first or use as a context manager.")
        return self._conn

    def _apply_schema(self):
        self.conn.executescript(SCHEMA)
        # Migrations: add columns introduced after initial release
        for col, definition in [
            ("image_url", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE races ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
        self.conn.commit()

    # ------------------------------------------------------------------
    # Upsert a race (insert or update by slug)
    # ------------------------------------------------------------------
    def upsert_race(self, race: dict[str, Any]) -> int:
        """
        Insert a new race or merge with an existing one identified by slug.
        Returns the row id.
        """
        slug = race.get("slug")
        if not slug:
            raise ValueError("race dict must contain a 'slug' key.")

        now = datetime.utcnow().isoformat()
        existing = self._fetch_by_slug(slug)

        if existing is None:
            row = dict(race)
            row.setdefault("registration_status", "Unknown")
            row["last_updated"] = now
            row["distances"] = json.dumps(row.get("distances") or [])
            row["pricing"] = json.dumps(row.get("pricing") or {})
            row["sources"] = json.dumps(row.get("sources") or [])
            cols = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            self.conn.execute(
                f"INSERT INTO races ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
            self.conn.commit()
            return self._fetch_by_slug(slug)["id"]
        else:
            # Merge: fill in missing fields; update pricing / distances if richer
            updates: dict[str, Any] = {}

            for field in ("date", "location", "city", "race_type",
                          "official_website", "registration_url", "organizer",
                          "image_url"):
                if race.get(field) and not existing[field]:
                    updates[field] = race[field]

            # Always prefer newer status
            if race.get("registration_status") and race["registration_status"] != "Unknown":
                updates["registration_status"] = race["registration_status"]

            # Merge distances list
            existing_distances = json.loads(existing["distances"] or "[]")
            new_distances = race.get("distances") or []
            merged_distances = list(dict.fromkeys(existing_distances + new_distances))
            if merged_distances != existing_distances:
                updates["distances"] = json.dumps(merged_distances)

            # Merge pricing (incoming wins per distance key)
            existing_pricing = json.loads(existing["pricing"] or "{}")
            new_pricing = race.get("pricing") or {}
            merged_pricing = {**existing_pricing, **new_pricing}
            if merged_pricing != existing_pricing:
                updates["pricing"] = json.dumps(merged_pricing)

            # Merge sources list
            existing_sources = json.loads(existing["sources"] or "[]")
            new_sources = race.get("sources") or []
            merged_sources = list(dict.fromkeys(existing_sources + new_sources))
            if merged_sources != existing_sources:
                updates["sources"] = json.dumps(merged_sources)

            updates["last_updated"] = now

            set_clause = ", ".join(f"{k} = ?" for k in updates)
            self.conn.execute(
                f"UPDATE races SET {set_clause} WHERE slug = ?",
                list(updates.values()) + [slug],
            )
            self.conn.commit()
            return existing["id"]

    def _fetch_by_slug(self, slug: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM races WHERE slug = ?", (slug,))
        return cur.fetchone()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    def list_races(
        self,
        sort: str = "date",
        race_type: Optional[str] = None,
        location: Optional[str] = None,
        status: Optional[str] = None,
        distance_filter: Optional[str] = None,
    ) -> list[dict]:
        """
        Return races as dicts.  sort: 'date' | 'price' | 'name'
        price sort uses cheapest early-bird price for 21km or 42km distances.
        """
        where_clauses = []
        params: list[Any] = []

        if race_type:
            where_clauses.append("LOWER(race_type) = LOWER(?)")
            params.append(race_type)
        if location:
            where_clauses.append(
                "(LOWER(city) LIKE LOWER(?) OR LOWER(location) LIKE LOWER(?))"
            )
            params += [f"%{location}%", f"%{location}%"]
        if status:
            where_clauses.append("LOWER(registration_status) = LOWER(?)")
            params.append(status)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if sort == "name":
            order_sql = "ORDER BY race_name COLLATE NOCASE ASC"
        elif sort == "price":
            # We post-sort in Python so fetch all first
            order_sql = ""
        else:
            order_sql = "ORDER BY date ASC NULLS LAST"

        cur = self.conn.execute(
            f"SELECT * FROM races {where_sql} {order_sql}",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

        for row in rows:
            row["distances"] = json.loads(row.get("distances") or "[]")
            row["pricing"] = json.loads(row.get("pricing") or "{}")
            row["sources"] = json.loads(row.get("sources") or "[]")

        if sort == "price":
            rows = _sort_by_price(rows, distance_filter)

        return rows

    def get_all_slugs(self) -> list[str]:
        cur = self.conn.execute("SELECT slug FROM races")
        return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Scraper log
    # ------------------------------------------------------------------
    def log_scraper_run(
        self,
        scraper: str,
        status: str,
        races_found: int = 0,
        message: str = "",
    ):
        self.conn.execute(
            "INSERT INTO scraper_log (scraper, run_at, status, races_found, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (scraper, datetime.utcnow().isoformat(), status, races_found, message),
        )
        self.conn.commit()

    def get_scraper_health(self) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT scraper,
                   MAX(run_at)      AS last_run,
                   status,
                   races_found,
                   message
            FROM scraper_log
            GROUP BY scraper
            ORDER BY scraper
            """
        )
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _sort_by_price(rows: list[dict], distance_filter: Optional[str] = None) -> list[dict]:
    """Sort races by cheapest price for a given distance (default: 21km or 42km)."""
    targets = [distance_filter] if distance_filter else ["21km", "42km", "half", "full"]

    def _min_price(row: dict) -> float:
        pricing = row.get("pricing") or {}
        for dist_key, tiers in pricing.items():
            if any(t.lower() in dist_key.lower() for t in targets):
                prices = []
                if isinstance(tiers, dict):
                    for tier_val in tiers.values():
                        try:
                            prices.append(float(str(tier_val).replace(",", "")))
                        except (ValueError, TypeError):
                            pass
                if prices:
                    return min(prices)
        return float("inf")

    return sorted(rows, key=_min_price)
