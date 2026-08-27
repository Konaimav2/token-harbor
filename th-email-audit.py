#!/usr/bin/env python3
"""Fetch all cloudmail catch-all emails and match against keys.txt (TH-registered).

Usage:
  python3 th-email-audit.py              # full audit: all emails + registered status
  python3 th-email-audit.py --unmatched  # only emails NOT in keys.txt (free slots)
  python3 th-email-audit.py --registered # only emails in keys.txt
"""
import sys, os, json, urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent


def get_cloudmail_login():
    """Login to cloudmail, return raw auth token."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("tt", str(BASE / "th-tui.py"))
    m = _ilu.module_from_spec(spec)
    spec.loader.exec_module(m)
    if not m.load_env():
        raise RuntimeError("Failed to load .env")
    req = urllib.request.Request(m.CM_BASE + "/api/login",
        data=json.dumps({"email": m.CM_ADMIN_EMAIL, "password": m.CM_ADMIN_PASSWORD}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return m.CM_BASE, resp["data"]["token"]


def fetch_cloudmail_emails():
    """Fetch all cloudmail account emails (the catch-all inboxes). Retries login on 401."""
    base, token = get_cloudmail_login()
    for attempt in range(5):
        try:
            req = urllib.request.Request(base + "/api/user/list",
                headers={"Authorization": token, "User-Agent": "Mozilla/5.0",
                         "Referer": base + "/"})
            resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
            if resp.get("code") == 200:
                lst = resp.get("data", {}).get("list", [])
                emails = []
                for item in lst:
                    e = item.get("email", "")
                    if "@" in str(e):
                        emails.append(str(e).lower())
                return sorted(set(emails))
            # 401 — re-login and retry
            if resp.get("code") == 401 and attempt < 4:
                import time
                time.sleep(2)
                base, token = get_cloudmail_login()
                continue
            return []
        except Exception as e:
            if attempt == 4:
                print(f"  fetch err: {e}")
                return []
            import time
            time.sleep(2)
    return []


def load_th_keys():
    """Load keys.txt -> {email: {password, api_key, status}}."""
    keys_file = BASE / "keys.txt"
    if not keys_file.exists():
        return {}
    th = {}
    for ln in keys_file.read_text().splitlines():
        p = ln.strip().split("|")
        if len(p) >= 4 and "@" in p[0]:
            th[p[0].lower()] = {
                "password": p[1],
                "api_key": p[2] if p[2].startswith("thk_") else "",
                "status": p[3],
            }
    return th


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    print("=" * 64)
    print("CLOUDMAIL CATCH-ALL EMAIL AUDIT")
    print("=" * 64)

    th = load_th_keys()
    print(f"\nTH-registered (keys.txt): {len(th)}")

    print("\nFetching cloudmail catch-all emails...")
    cm = fetch_cloudmail_emails()
    print(f"Cloudmail emails: {len(cm)}")

    cm_set = set(cm)
    th_set = set(th.keys())

    registered = sorted(cm_set & th_set)          # in both
    only_th = sorted(th_set - cm_set)             # TH but not in cloudmail
    only_cm = sorted(cm_set - th_set)             # cloudmail but not TH (free slots)
    both_count = len(registered)

    print(f"\n  Matched (cloudmail + TH):   {len(registered)}")
    print(f"  TH only (not in cloudmail):  {len(only_th)}")
    print(f"  Cloudmail only (free slots): {len(only_cm)}")

    if mode in ("all", "registered"):
        if registered:
            print(f"\n=== REGISTERED IN BOTH ({len(registered)}) ===")
            for e in registered:
                info = th[e]
                s = "OK" if info["status"] in ("ok", "ok+free") else "!"
                k = "key" if info["api_key"] else "nokey"
                print(f"  [{s}/{k}] {e}")

    if mode in ("all", "unmatched", "free", "list"):
        print(f"\n=== CLOUDMAIL EMAILS — FULL LIST ({len(cm)}) ===")
        # Show each email with its status
        for e in sorted(cm):
            if e in th_set:
                info = th[e]
                s = "OK" if info["status"] in ("ok", "ok+free") else "!"
                k = "key" if info["api_key"] else "nokey"
                st = f"[{s}/{k}] {info['status']}"
                print(f"  {st} USED  {e}")
            else:
                print(f"  [FREE] UNUSED {e}")

    if mode in ("all", "unmatched", "free") and only_cm:
        print(f"\n=== WEBSHARE FREE SLOTS ONLY ({len(only_cm)}) ===")
        for e in only_cm[:50]:  # cap at 50 for readability
            print(f"  {e}")
        if len(only_cm) > 50:
            print(f"  ... and {len(only_cm)-50} more (use --full for all)")

    if mode == "all" and only_th:
        print(f"\n=== TH ONLY — MISSING FROM CLOUDMAIL ({len(only_th)}) ===")
        for e in only_th:
            print(f"  [MISSING] {e}")

    # --save: write unused (free) catch-all emails to webshare-emails.txt for manual webshare use
    if "--save" in sys.argv and only_cm:
        out = BASE / "webshare-emails.txt"
        out.write_text("\n".join(only_cm) + "\n")
        print(f"\n✅ Saved {len(only_cm)} unused catch-all emails to webshare-emails.txt")
        print(f"   Use these as webshare account emails (none are TH-registered)")

    print(f"\n{'='*64}")
    print(f"TOTAL: {len(cm)} cloudmail | {len(th)} TH | {both_count} matched")
    print(f"Free catch-all slots available: {len(only_cm)}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
