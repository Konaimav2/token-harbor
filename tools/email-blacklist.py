#!/usr/bin/env python3
"""Email blacklist management for th-farm.
Prevents using personal or already-created emails.
Blacklist sources:
  - keys.txt (all emails ever created)
  - cloudmail existing addresses
  - manual entries in email-blacklist.txt
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
BLACKLIST_FILE = BASE / "email-blacklist.txt"


def load_blacklist():
    """Load blacklist as a set of lowercase emails."""
    bl = set()
    if BLACKLIST_FILE.exists():
        for ln in BLACKLIST_FILE.read_text().splitlines():
            ln = ln.strip().lower()
            if ln and "@" in ln and not ln.startswith("#"):
                bl.add(ln)
    return bl


def add(email):
    """Add an email to the blacklist file."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return False
    bl = load_blacklist()
    if email in bl:
        return False
    with open(BLACKLIST_FILE, "a") as f:
        f.write(email + "\n")
    return True


def auto_seed():
    """Auto-load used emails from keys.txt + cloudmail into the blacklist."""
    keys_file = BASE / "keys.txt"
    added = 0
    if keys_file.exists():
        for ln in keys_file.read_text().splitlines():
            p = ln.strip().split("|")
            if len(p) >= 1 and "@" in p[0]:
                if add(p[0]):
                    added += 1
    # cloudmail existing addresses
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("thtui", str(BASE / "th-tui.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.load_env()
        for a in m.get_cloudmail_addresses():
            if add(a):
                added += 1
    except Exception as e:
        print(f"  cloudmail seed err: {e}")
    return added


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--seed":
        n = auto_seed()
        print(f"Seeded blacklist: +{n} (total {len(load_blacklist())})")
    elif len(sys.argv) > 1 and sys.argv[1] == "--list":
        bl = load_blacklist()
        print(f"{len(bl)} blacklisted emails:")
        for e in sorted(bl):
            print(f"  {e}")
    else:
        print("Usage: python3 email-blacklist.py --seed | --list")
        print("       Or append emails to email-blacklist.txt")
