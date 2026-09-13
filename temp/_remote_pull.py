#!/usr/bin/env python3
"""Remote helper (runs on papi): dump 9router connections as id<tab>provider<tab>name<tab>apiKey.

Columns id/provider/name live beside the data blob; apiKey is inside it.
Keys are stored plain — "decode" = read columns + blob, strip wrapping
quotes/whitespace. One row per unique key.
"""
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
try:
    rows = con.execute(
        "SELECT id, provider, name, data FROM providerConnections").fetchall()
except Exception:
    rows = [("", "", "", d) for (d,) in
            con.execute("SELECT data FROM providerConnections").fetchall()]


def decode_key(k):
    if not isinstance(k, str):
        return ""
    return k.strip().strip("'\"")


seen = set()
for _id, prov, name, d in rows:
    try:
        obj = json.loads(d) if d else {}
    except Exception:
        continue
    k = decode_key(obj.get("apiKey") or "")
    if not k or k in seen:
        continue
    seen.add(k)
    print(f"{_id}\t{prov}\t{name}\t{k}")
