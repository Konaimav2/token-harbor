"""Map DB TH rows (rowid, apiKey, createdAt) to API names via createdAt."""
import os
import sqlite3
import json
import sys

con = sqlite3.connect(os.path.expanduser("~/.9router/db/data.sqlite"))
rows = con.execute("SELECT rowid, data FROM providerConnections").fetchall()
print("=== DB TH rows (rowid | key | createdAt) ===")
for rowid, d in rows:
    if not d:
        continue
    try:
        obj = json.loads(d)
    except Exception:
        continue
    if (obj.get("providerSpecificData") or {}).get("baseUrl", "").strip().rstrip("/") != "https://tokenharbor.ai/v1":
        continue
    print(f"{rowid} | {str(obj.get('apiKey'))[:40]} | {obj.get('createdAt')}")
