#!/usr/bin/env python3
"""th-farm — parallel TokenHarbor account farm.

Creates N accounts concurrently across separate proxies (smart balanced),
with auto-verify + free-model + store. This is how large batches run fast
(dozens/hour instead of one-at-a-time).

Usage:
    python3 th-farm.py [count] [--workers N] [--delay S]
"""
import sys
import os
import time
import json
import random
import threading
import traceback
from pathlib import Path
import importlib.util

BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "th-tui-config.json"

# load the TUI as a module (it has create/verify/free-model/store logic)
def _load_tui():
    spec = importlib.util.spec_from_file_location("thtui", str(BASE / "th-tui.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# thread-safe proxy picker
_lock = threading.Lock()
_used_ips = set()
_used_proxies = set()


def pick_proxy(m):
    """Thread-safe smart proxy pick (unique IP per account)."""
    global _used_ips
    with _lock:
        pm = m._load_proxy_mod()
        if not pm:
            return None
        p, ip = pm.smart_pick_proxy(pm.load_proxies(), used_ips=_used_ips)
        if p:
            _used_ips.add(ip)
            _used_proxies.add(p)
            return p
    return None


def run_one(m, c, index, total, email=None, password=None, skip_inbox=False, rmode=None):
    """Create one account (create → verify → free-model → store).
    rmode: None=fresh, 'reuse_unused', 'reverify_pending'."""
    start = time.time()
    tag = f"[{index}/{total}]"
    try:
        if not email:
            email, password, rmode = m.pick_next_email(c)
        # if reusing a pending email that already has an account, skip create + just reverify
        if rmode == "reverify_pending":
            m.log(f"{tag} Re-verifying: {email}", "arr")
            r = m.reverify_flow(c, email, password)
        else:
            m.log(f"{tag} Starting: {email}", "arr")
            r = m.run_full_flow(c, email, password=password, skip_inbox=skip_inbox)
        if r:
            m.log(f"{tag} DONE: {email} key={str(r.get('api_key'))[:12]}... verified={r.get('verified')} free={r.get('free_ok')} ({int(time.time()-start)}s)", "ok")
            return True
        m.log(f"{tag} FAILED: {email}", "no")
        return False
    except Exception as e:
        m.elog(f"{tag} error {email}: {e}", traceback.format_exc()[:200])
        return False


def main():
    m = _load_tui()
    if not m.load_env():
        print("Cannot start: live credentials (.env) missing.")
        return 1
    c = m.load_cfg()

    # parse args
    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else c.get("batch_count", 10)
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 6
    delay = int(sys.argv[sys.argv.index("--delay") + 1]) if "--delay" in sys.argv else (c.get("batch_delay", 15) or 15)

    # resolve emails if mail server is pick-mode; else generate fresh per worker
    ms = m.get_active_mail(c)
    using_tempmail = (not ms) and c.get("proxy", {}).get("use_public_tempmail")
    t = ms.get("type", "") if ms else "tempmail"

    print("=" * 50)
    print(f"  TH-FARM: {count} accounts, {workers} workers, {delay}s delay")
    print(f"  mail: {ms.get('name','tempmail') if ms else 'tempmail'} ({t})")
    print(f"  proxy: enabled={c.get('proxy',{}).get('enabled')} mode={c.get('proxy',{}).get('mode')}")
    print("=" * 50)

    # ─── PHASE 0: Generate emails with blacklist check (skip personal/used) ───
    _emails_created = []
    _use_catchall = c.get("use_catchall", True)
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("eb", str(BASE / "email-blacklist.py"))
        bl_mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(bl_mod)
        BLACKLIST = set(bl_mod.load_blacklist())
    except Exception as e:
        print(f"  Warning: blacklist load failed, continuing without it: {e}")
        BLACKLIST = set()

    if ms and ms.get("type") == "cloudmail" and ms.get("create_new_mail"):
        print()
        domains = ms.get("domains") or []
        # FIRST: check for reusable emails (unused/pending) to not waste — recover BEFORE fresh
        reusable = m.load_reusable_emails()
        n_unused = len(reusable.get("unused", []))
        n_pending = len(reusable.get("pending", []))
        print(f"  Reusable before fresh: {n_unused} unused + {n_pending} pending")
        # Build the reusable queue: unused (inbox ready) then pending (has account, needs verify)
        reusable_queue = [(e, pw, "reuse_unused") for e, pw, _ in reusable.get("unused", [])]
        reusable_queue += [(e, pw, "reverify_pending") for e, pw, _ in reusable.get("pending", [])]
        # Only generate NEW catch-all emails to fill the gap (if count > reusable)
        gap = count - len(reusable_queue)
        if gap > 0:
            print(f"  Generating {gap} fresh emails to fill the batch...")
            for _ in range(gap):
                attempts = 0
                while attempts < 50:
                    pfx = c.get("email_prefix", "") or m._real_prefix(c)
                    dom = m._next_domain(c, domains)
                    email = f"{pfx}@{dom}".lower()
                    if email not in BLACKLIST:
                        break
                    attempts += 1
                else:
                    print("  ERROR: could not find unique email after 50 attempts")
                    sys.exit(1)
                # SAVE AS UNUSED NOW (so if cancelled, reuse next batch — nothing wasted)
                m.save_unused_email(email)
                BLACKLIST.add(email)
                reusable_queue.append((email, "test123", "reuse_unused"))
            print(f"  Phase 0: {len(reusable_queue)} emails ready ({n_unused} unused + {n_pending} pending recovered + {gap} fresh)")
        else:
            print(f"  Phase 0: {len(reusable_queue)} emails ready — all from reusable pool, no fresh generation")
    else:
        reusable_queue = []
    # ─── END PHASE 0 ───
    random.shuffle(reusable_queue)

    # pre-generate email pool for pick-mode mail
    email_pool = []
    if ms and not ms.get("create_new_mail"):
        # pick-mode: gather available emails
        if t == "mailg":
            email_pool = [a for a in m.get_mailg_accounts()]
        elif t == "cloudmail":
            email_pool = [a for a in m.get_cloudmail_addresses()]
        random.shuffle(email_pool)

    done = 0
    ok = 0
    fail = 0
    results = []
    queue = list(range(count))
    # Use reusable_queue directly — it already contains all emails (reuse + fresh)
    reusable_lock = threading.Lock()

    def worker():
        nonlocal done, ok, fail
        while True:
            with _lock:
                if not queue:
                    return
                idx = queue.pop(0)
            # resolve email — pop from reusable_queue (prioritizes unused + pending before fresh)
            email = None
            skip_inbox = False
            rmode = None
            password = "test123"
            with reusable_lock:
                if reusable_queue:
                    email, password, rmode = reusable_queue.pop(0)
                    skip_inbox = (rmode == "reuse_unused")
            m.log(f"[{idx}/{count}] {('Re-verifying' if rmode=='reverify_pending' else 'Starting')} {email} ({rmode or 'fresh'})", "arr")
            success = run_one(m, c, idx + 1, count, email=email, password=password, skip_inbox=skip_inbox, rmode=rmode)
            with _lock:
                done += 1
                if success:
                    ok += 1
                else:
                    fail += 1
            # ratelimit delay
            if delay > 0:
                time.sleep(delay)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, count))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    print("\n" + "=" * 50)
    print(f"  FARM DONE: {ok} ok / {fail} fail / {count} total")
    print(f"  keys in {m.KEYS_FILE}")
    print("=" * 50)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(1)
