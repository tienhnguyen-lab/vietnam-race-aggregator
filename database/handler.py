"""
Database handler for the Vietnam Endurance Race Aggregator.

Runs on either:
  • local SQLite (default) — for dev and the Playwright CLI (`python main.py sync`)
  • remote Postgres / Neon  — when POSTGRES_URL (or DATABASE_URL) is set, e.g. on Vercel

The two share one code path. SQL is written in SQLite's `?`-placeholder style and
translated to Postgres `%s` at the connection wrapper; the handful of genuinely
dialect-specific bits (id type, upsert-ignore, date/collation expressions) branch
on `self._is_pg`.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


import os
_default_db = Path(__file__).parent.parent / "races.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_default_db)))

# ── Remote Postgres / Neon support (used on Vercel serverless) ───────────────
# Vercel's filesystem is read-only (only ephemeral /tmp), so a local SQLite file
# can't persist across invocations. When a Postgres URL is present we connect to
# Neon instead. Vercel's Neon integration exposes several names; accept the common
# ones. Prefer the POOLED url on serverless (Neon's "-pooler" host).
POSTGRES_URL = (
    os.environ.get("POSTGRES_URL")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_PRISMA_URL")
    or ""
)

# ── Dialect-specific schema DDL (executed statement-by-statement) ────────────
_SCHEMA_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS races (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        race_name           TEXT NOT NULL,
        slug                TEXT UNIQUE NOT NULL,
        date                TEXT,
        location            TEXT,
        city                TEXT,
        race_type           TEXT CHECK(race_type IN ('Road','Trail','Triathlon','Ironman','Duathlon','Other')),
        distances           TEXT,
        pricing             TEXT,
        official_website    TEXT,
        registration_url    TEXT,
        organizer           TEXT,
        registration_status TEXT CHECK(registration_status IN ('Open','Sold Out','Upcoming','Unknown')) DEFAULT 'Unknown',
        image_url           TEXT,
        sources             TEXT,
        last_updated        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scraper_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scraper     TEXT NOT NULL,
        run_at      TEXT NOT NULL,
        status      TEXT NOT NULL,
        races_found INTEGER DEFAULT 0,
        message     TEXT
    )
    """,
    # Legacy migration: image_url added after initial release. SQLite has no
    # ADD COLUMN IF NOT EXISTS, so this may fail on existing DBs — caller ignores.
    "ALTER TABLE races ADD COLUMN image_url TEXT",
]

_SCHEMA_PG = [
    """
    CREATE TABLE IF NOT EXISTS races (
        id                  SERIAL PRIMARY KEY,
        race_name           TEXT NOT NULL,
        slug                TEXT UNIQUE NOT NULL,
        date                TEXT,
        location            TEXT,
        city                TEXT,
        race_type           TEXT CHECK(race_type IN ('Road','Trail','Triathlon','Ironman','Duathlon','Other')),
        distances           TEXT,
        pricing             TEXT,
        official_website    TEXT,
        registration_url    TEXT,
        organizer           TEXT,
        registration_status TEXT CHECK(registration_status IN ('Open','Sold Out','Upcoming','Unknown')) DEFAULT 'Unknown',
        image_url           TEXT,
        sources             TEXT,
        last_updated        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scraper_log (
        id          SERIAL PRIMARY KEY,
        scraper     TEXT NOT NULL,
        run_at      TEXT NOT NULL,
        status      TEXT NOT NULL,
        races_found INTEGER DEFAULT 0,
        message     TEXT
    )
    """,
    "ALTER TABLE races ADD COLUMN IF NOT EXISTS image_url TEXT",
]


class _PgConn:
    """
    Thin wrapper around a psycopg (Postgres) connection giving it the small slice
    of the sqlite3 API this handler uses:

      • execute()/executemany() accept SQLite-style `?` placeholders — translated
        to Postgres `%s` here, so every call site stays dialect-agnostic.
      • execute() returns a cursor (with .description/.fetchone/.fetchall).
      • commit() is a no-op — the connection runs in autocommit mode, which suits
        short-lived serverless invocations and avoids idle-in-transaction hangs.
    """
    def __init__(self, conn):
        self._c = conn

    @staticmethod
    def _translate(sql: str) -> str:
        # No literal '?' or '%' appear in this project's SQL strings, so a plain
        # swap is safe. (Bind values that contain '%' are passed as params, not
        # interpolated into the query text.)
        return sql.replace("?", "%s")

    def execute(self, sql, params=None):
        cur = self._c.cursor()
        if params is None:
            cur.execute(self._translate(sql))
        else:
            cur.execute(self._translate(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._c.cursor()
        cur.executemany(self._translate(sql), list(seq_of_params))
        return cur

    def commit(self):
        pass  # autocommit

    def close(self):
        self._c.close()


class DatabaseHandler:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._is_pg: bool = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self):
        if POSTGRES_URL:
            import psycopg
            conn = psycopg.connect(POSTGRES_URL, autocommit=True)
            # Neon's pooled endpoint runs PgBouncer in transaction mode, which
            # can't keep server-side prepared statements across pooled connections
            # (causes intermittent 'prepared statement already exists' errors).
            # Disabling them makes both the pooled and direct endpoints reliable.
            conn.prepare_threshold = None
            self._conn = _PgConn(conn)
            self._is_pg = True
        else:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._is_pg = False
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

    # ------------------------------------------------------------------
    # Row materialisation (works for both sqlite3.Row and libSQL tuples)
    # ------------------------------------------------------------------
    @staticmethod
    def _rows(cur) -> list[dict]:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    @staticmethod
    def _one(cur) -> Optional[dict]:
        r = cur.fetchone()
        if r is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))

    def _apply_schema(self):
        statements = _SCHEMA_PG if self._is_pg else _SCHEMA_SQLITE
        for stmt in statements:
            try:
                self.conn.execute(stmt)
            except Exception:
                # The only expected failure is the legacy SQLite `ADD COLUMN`
                # when image_url already exists (Postgres uses IF NOT EXISTS).
                pass
        self.conn.commit()

    # ------------------------------------------------------------------
    # Seed the races table from a JSON export if it is currently empty.
    # ------------------------------------------------------------------
    def seed_if_empty(self, seed_path) -> int:
        """Load a seed_data.json export into an empty races table. Returns rows added."""
        seed_path = Path(seed_path)
        if not seed_path.exists():
            return 0
        count = self.conn.execute("SELECT COUNT(*) FROM races").fetchone()[0]
        if count and int(count) > 0:
            return 0
        rows = json.loads(seed_path.read_text(encoding="utf-8"))
        if not rows:
            return 0
        cols = [c for c in rows[0].keys() if c != "id"]
        col_sql = ", ".join(cols)
        ph = ", ".join(["?"] * len(cols))
        conflict = "ON CONFLICT (slug) DO NOTHING" if self._is_pg else "OR IGNORE"
        if self._is_pg:
            sql = f"INSERT INTO races ({col_sql}) VALUES ({ph}) {conflict}"
        else:
            sql = f"INSERT {conflict} INTO races ({col_sql}) VALUES ({ph})"
        self.conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
        self.conn.commit()
        return len(rows)

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

    def _fetch_by_slug(self, slug: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM races WHERE slug = ?", (slug,))
        return self._one(cur)

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

        # Permanent date filter. Dates are stored as ISO 'YYYY-MM-DD' TEXT, so a
        # lexicographic string compare against today works in both dialects.
        today_expr = "CURRENT_DATE::text" if self._is_pg else "DATE('now')"
        where_clauses.append(f"(date = '' OR date >= {today_expr})")

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
            # Postgres has no NOCASE collation; LOWER() is portable.
            order_sql = ("ORDER BY LOWER(race_name) ASC" if self._is_pg
                         else "ORDER BY race_name COLLATE NOCASE ASC")
        elif sort == "price":
            # We post-sort in Python so fetch all first
            order_sql = ""
        else:
            order_sql = "ORDER BY date ASC NULLS LAST"

        cur = self.conn.execute(
            f"SELECT * FROM races {where_sql} {order_sql}",
            params,
        )
        rows = self._rows(cur)

        # Python-side name filters (SQLite LOWER() doesn't handle Vietnamese)
        _BLOCKED = ("chèo sup", "cheo sup", "trek")
        rows = [
            r for r in rows
            if not any(kw in (r.get("race_name") or "").lower() for kw in _BLOCKED)
        ]

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
        # The original SQLite "GROUP BY scraper with bare columns" is invalid in
        # Postgres. A window function picks the latest row per scraper in both.
        cur = self.conn.execute(
            """
            SELECT scraper, run_at AS last_run, status, races_found, message
            FROM (
                SELECT scraper, run_at, status, races_found, message,
                       ROW_NUMBER() OVER (PARTITION BY scraper ORDER BY run_at DESC) AS rn
                FROM scraper_log
            ) t
            WHERE rn = 1
            ORDER BY scraper
            """
        )
        return self._rows(cur)


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
