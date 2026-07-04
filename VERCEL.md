# Deploying to Vercel (Neon Postgres)

The web tier runs as Vercel serverless functions; scraping runs on a daily cron.
DB is **Neon Postgres** in the cloud, **SQLite** locally (auto-selected by env).

**Deploy:** push to GitHub, import at [vercel.com/new](https://vercel.com/new)
(Flask auto-detected — no build command), set the env vars below.

| Env var | Required | Notes |
|---|---|---|
| `POSTGRES_URL` / `DATABASE_URL` | ✅ | Neon **pooled** string. The Vercel–Neon integration sets this for you. |
| `CRON_SECRET` | ✅ | `openssl rand -hex 32`. Without it (or `SYNC_KEY`), `/api/cron/sync` returns 503. |
| `SYNC_KEY` | optional | Manual sync via the `X-Sync-Key` header. |

- **No migrations.** Schema + the 52-row seed from `seed_data.json` are created
  lazily on the first request to an empty DB.
- **Isolated schema.** On Postgres the app keeps its tables in a dedicated
  `race_aggregator` schema (override with `PG_SCHEMA`), so a Neon database can be
  safely shared with another project — no `public`-table collisions.
- **Cron** (`/api/cron/sync`, daily `0 19 * * *`) is wired in `vercel.json` and
  runs only the `requests`-based scrapers. Playwright sources (`123go`, `irace`,
  `vietrace365`) don't run on Vercel — run `python main.py sync` locally
  (point `POSTGRES_URL` at Neon to write to the cloud, or commit a fresh
  `seed_data.json`).

**Routes:** `GET /` (frontend) · `GET /api/races` (`?sort=&type=&city=&status=&q=`)
· `GET /api/meta` · `GET/POST /api/cron/sync` · `POST /api/sync` (legacy).

**Local dev:** `pip install -r requirements.txt && python app.py` → localhost:5001
(uses SQLite; leave `POSTGRES_URL` unset).
