# Deploying to Vercel + Neon

The web tier runs as Vercel serverless functions; scraping runs on a daily Vercel
Cron. The database is **Neon Postgres** in the cloud and **SQLite** locally — the
handler picks automatically based on `POSTGRES_URL`.

## Architecture notes
- **DB selection:** if `POSTGRES_URL` / `DATABASE_URL` is set → Neon Postgres; else
  local SQLite (`races.db`). So local dev needs no database setup.
- **Isolated schema:** on Postgres the app keeps its tables in a dedicated
  **`race_aggregator`** schema (override with `PG_SCHEMA`), *not* `public`. A Neon
  database can therefore be shared with another project with zero table collisions.
- **No migrations:** schema + the 52-row seed from `seed_data.json` are created
  lazily on the first request to an empty DB.
- **Cron:** `/api/cron/sync` runs daily (`0 19 * * *`, from `vercel.json`), authed
  by `CRON_SECRET`. It runs only the `requests`-based scrapers — Playwright sources
  (`123go`, `irace`, `vietrace365`) can't run on Vercel.

---

## Step-by-step

### 1. Provision / connect the Neon database
In the Vercel project (`vietnam-race-aggregator`) → **Storage**:
- If a Neon DB isn't attached yet, **Create Database → Neon** (or **Connect Database**
  to attach an existing one). Sharing an existing Neon DB with another project is
  safe here thanks to the dedicated schema.
- On the Install Integration dialog: Environments **Production + Preview**, "Create
  database branch for deployment" **unchecked**, **Custom Prefix empty** (so it
  creates `POSTGRES_URL` / `DATABASE_URL`, which the app reads), Sensitive **on** →
  **Connect**.

> If you see *"already has an existing environment variable NEON_AUTH_BASE_URL"*, the
> DB is already connected — just close the dialog. Check **Settings → Environment
> Variables** for `POSTGRES_URL`; if it's there, you're done with this step.

`POSTGRES_URL` is Neon's **pooled** endpoint — correct for serverless, and the code
is hardened for it (prepared statements disabled). The direct string
(`POSTGRES_URL_NON_POOLING`) also works if you prefer it.

### 2. Add the cron secret
**Settings → Environment Variables** → add:
- `CRON_SECRET` = output of `openssl rand -hex 32` (Production at least)
- *(optional)* `SYNC_KEY` = any random string, to trigger a sync manually
- *(optional)* `PG_SCHEMA` = a different schema name (defaults to `race_aggregator`)

### 3. Deploy
Git source = `tienhnguyen-lab/vietnam-race-aggregator`, branch **`main`**. Flask is
auto-detected (no build command). **Deployments → Redeploy**, or push to `main`.

### 4. Verify
```bash
curl https://<app>.vercel.app/api/races      # JSON list — auto-seeds 52 races on first hit
curl https://<app>.vercel.app/api/meta
curl -H "Authorization: Bearer <CRON_SECRET>" \
     https://<app>.vercel.app/api/cron/sync   # {"status":"ok","results":{…}}
```
Then open `https://<app>.vercel.app/` — the race browser UI should load. If
`/api/races` 500s, check **Observability → Runtime Logs** (usually a missing/renamed
DB env var).

### 5. Confirm the cron
**Settings → Cron Jobs** lists `/api/cron/sync` at `0 19 * * *` (daily; Hobby max).
Vercel attaches `CRON_SECRET` automatically.

---

## Routes
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Frontend (static `index.html`) |
| GET | `/api/races` | Race list (`?sort=&type=&city=&status=&q=`) |
| GET | `/api/meta` | Distinct cities + types |
| GET/POST | `/api/cron/sync` | Scheduled/manual scrape of the `requests` sources |
| POST | `/api/sync` | Legacy manual sync (`X-Sync-Key`, 5-min cooldown) |

## Refreshing Playwright sources (ongoing)
The daily cron covers `actiup`, `truerace`, `vnexpress_schedule`. For `123go`,
`irace`, `vietrace365`, run locally:
```bash
pip install -r requirements.txt -r requirements-scraper.txt
playwright install chromium
export POSTGRES_URL="<neon-url>"   # writes straight to Neon; omit to use local SQLite
python main.py sync
```
If you sync into local SQLite instead, run `python export_seed.py` and commit
`seed_data.json` (it seeds an empty cloud DB on first boot).

## Local development
```bash
pip install -r requirements.txt
python app.py            # http://localhost:5001 — uses local SQLite
```
Leave `POSTGRES_URL`/`DATABASE_URL` unset for SQLite; set it to develop against Neon.
