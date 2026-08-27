#!/usr/bin/env python3
"""Remote helper (runs on papi): dump unique apiKeys from the 9router DB."""
import sqlite3
import json

con = sqlite3.connect("/root/.9router/db/data.sqlite")
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
        keys.add(k)
print("\n".join(sorted(keys)))
