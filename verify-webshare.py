#!/usr/bin/env python3
"""Batch-activate ALL webshare accounts: inbox -> activation link -> GET. No browser needed."""
import importlib.util
import re
import sys
import time
from pathlib import Path
import requests

BASE = Path("/root/temp/token-harbor")
spec = importlib.util.spec_from_file_location("tt", BASE / "th-tui.py")
m = importlib.util.module_from_spec(spec)
sys.modules["tt"] = m
spec.loader.exec_module(m)
m.load_env()

accts = [l.split(":")[0].strip() for l in (BASE / "ws_accounts.txt").read_text().splitlines() if ":" in l]
print(f"{len(accts)} accounts to check")

ok = fail = none = 0
for em in accts:
    try:
        msgs = m.read_cloudmail_inbox(em) or []
    except Exception as e:
        print(f"  {em}: inbox error {str(e)[:40]}")
        fail += 1
        continue
    url = None
    for msg in msgs:
        body = str(msg.get("text", "")) + " " + str(msg.get("html", ""))
        vu = [u for u in re.findall(r'https?://[^\s"<>]+', body)
              if any(w in u.lower() for w in ["activation", "/verify", "confirm", "activate"])]
        if vu:
            url = vu[0]
            break
    if not url:
        print(f"  {em}: no activation mail")
        none += 1
        continue
    try:
        r = requests.get(url, timeout=30, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
        txt = r.text.lower()
        if any(w in txt for w in ["success", "verified", "activated", "thank you"]):
            print(f"  {em}: ACTIVATED ✓")
            ok += 1
        elif any(w in txt for w in ["invalid", "expired", "already been used"]):
            # already used = was activated before; treat as verified
            if "already been used" in txt or "already" in txt:
                print(f"  {em}: link already used (was verified earlier)")
                ok += 1
            else:
                print(f"  {em}: REJECTED ({r.url[:50]})")
                fail += 1
        else:
            print(f"  {em}: unclear ({r.status_code} {str(r.url)[:50]})")
            none += 1
    except Exception as e:
        print(f"  {em}: request failed {str(e)[:40]}")
        fail += 1
    time.sleep(1)

print(f"\nDONE: activated/verified={ok}, rejected/errors={fail}, no-mail/unclear={none}")
