"""
Export all races from local races.db → seed_data.json
Run this locally before pushing to Railway to pre-populate the cloud DB.
"""
import json, sqlite3
from pathlib import Path

DB   = Path(__file__).parent / "races.db"
OUT  = Path(__file__).parent / "seed_data.json"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute("SELECT * FROM races ORDER BY date").fetchall()
con.close()

data = [dict(r) for r in rows]
OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2))
print(f"Exported {len(data)} races → {OUT}")
