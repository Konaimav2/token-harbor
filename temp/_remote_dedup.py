#!/usr/bin/env python3
"""Remote helper (runs on papi): dump unique apiKeys from the 9router DB."""
import os
import sqlite3
import json

cands = [
    os.path.expanduser("~/.9router/db/data.sqlite"),
    os.path.expanduser("~/.9router/data.sqlite"),
    "/var/lib/9router/db/data.sqlite",
]
if os.environ.get("DATA_DIR"):
    cands.insert(0, os.path.join(os.environ["DATA_DIR"], "db", "data.sqlite"))
db = next((p for p in cands if os.path.exists(p)), None)
if not db:
    raise SystemExit("9router DB not found")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = con.execute("SELECT data FROM providerConnections").fetchall()
keys = set()
for (d,) in rows:
    if not d:
        continue
    try:
        obj = json.loads(d)
    except Exception:
        continue
    k = obj.get("apiKey")
    if k and isinstance(k, str):
        k = k.strip().strip("'\"")
        if k:
            keys.add(k)
print("\n".join(sorted(keys)))
