#!/bin/bash
# Daily job (run by launchd at 06:00):
#   1. Scrape all sources → races.db
#   2. Export seed_data.json
#   3. git commit + push → Railway auto-redeploys
#
# Fixes applied:
#   - No set -e: each step runs independently; errors are collected, not fatal
#   - Dynamic Python path: venv > anaconda > system python3
#   - Log rotation: keeps last 1000 lines
#   - Git identity auto-set if missing
#   - macOS notification on completion (success or warning)

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/sync.log"
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
ERRORS=()

# ── Resolve Python ────────────────────────────────────────────────────────────
if   [ -f "$PROJECT_DIR/.venv/bin/python3" ];    then PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -f "/opt/anaconda3/bin/python3" ];         then PYTHON="/opt/anaconda3/bin/python3"
elif [ -f "/opt/homebrew/bin/python3" ];          then PYTHON="/opt/homebrew/bin/python3"
else                                                   PYTHON="$(command -v python3)"; fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# ── Log rotation: keep last 1000 lines ───────────────────────────────────────
if [ -f "$LOG_FILE" ]; then
    tail -n 1000 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
echo "[$TIMESTAMP] ══ Starting sync ($PYTHON) ══════════════════" >> "$LOG_FILE"

# ── Ensure git identity (required for commit) ────────────────────────────────
git config user.email 2>/dev/null | grep -q . || git config user.email "sync@vietnam-race-aggregator.local"
git config user.name  2>/dev/null | grep -q . || git config user.name  "Auto Sync"

# ── Step 1: Scrape ────────────────────────────────────────────────────────────
echo "[$TIMESTAMP] [1/3] Running scrapers..." >> "$LOG_FILE"
if "$PYTHON" main.py sync >> "$LOG_FILE" 2>&1; then
    echo "[$TIMESTAMP] [1/3] Scrapers OK." >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] [1/3] WARNING: scrapers exited with errors (partial data kept)." >> "$LOG_FILE"
    ERRORS+=("scraper-error")
fi

# ── Step 2: Export seed data ─────────────────────────────────────────────────
echo "[$TIMESTAMP] [2/3] Exporting seed_data.json..." >> "$LOG_FILE"
if "$PYTHON" export_seed.py >> "$LOG_FILE" 2>&1; then
    echo "[$TIMESTAMP] [2/3] Export OK." >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] [2/3] FATAL: export_seed.py failed — aborting push." >> "$LOG_FILE"
    ERRORS+=("export-failed")
    osascript -e "display notification \"export_seed.py failed. Check sync.log.\" with title \"🚨 Race Sync Failed\"" 2>/dev/null || true
    exit 1
fi

# ── Step 3: Push to GitHub ────────────────────────────────────────────────────
echo "[$TIMESTAMP] [3/3] Checking for changes..." >> "$LOG_FILE"
if git diff --quiet seed_data.json; then
    echo "[$TIMESTAMP] [3/3] seed_data.json unchanged — skipping push." >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] [3/3] Committing and pushing..." >> "$LOG_FILE"
    if git add seed_data.json \
    && git commit -m "chore: auto-sync races $(date '+%Y-%m-%d')" >> "$LOG_FILE" 2>&1 \
    && git push >> "$LOG_FILE" 2>&1; then
        echo "[$TIMESTAMP] [3/3] Push OK — Railway will redeploy shortly." >> "$LOG_FILE"
    else
        echo "[$TIMESTAMP] [3/3] ERROR: git push failed." >> "$LOG_FILE"
        ERRORS+=("push-failed")
    fi
fi

# ── Notify ────────────────────────────────────────────────────────────────────
if [ ${#ERRORS[@]} -eq 0 ]; then
    echo "[$TIMESTAMP] ══ Done — all OK ══════════════════════════" >> "$LOG_FILE"
    osascript -e "display notification \"Data refreshed and pushed to Railway.\" with title \"✅ Race Sync Done\"" 2>/dev/null || true
else
    ERR_LIST="$(IFS=', '; echo "${ERRORS[*]}")"
    echo "[$TIMESTAMP] ══ Done with issues: $ERR_LIST ═════════════" >> "$LOG_FILE"
    osascript -e "display notification \"Issues: $ERR_LIST. Check sync.log.\" with title \"⚠️ Race Sync Warning\"" 2>/dev/null || true
fi
