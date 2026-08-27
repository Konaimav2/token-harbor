#!/usr/bin/env python3
"""th-reverify — re-verify pending TokenHarbor accounts safely.

- Checks cloudmail inbox FIRST for existing verification email
- Only resends if no email found (slower, avoids rate limits)
- After clicking the verify link, tests API key against /v1/models
- Only marks 'ok' if the API key actually works
- 1 worker, deliberate pacing
"""
import os, sys, time, json, re, traceback
from pathlib import Path
import importlib.util

BASE = Path(__file__).resolve().parent
KEYS_FILE = BASE / "keys.txt"

def load_tui():
    spec = importlib.util.spec_from_file_location("thtui", str(BASE / "th-tui.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def test_key(key, model="deepseek-v4-flash:free"):
    """Test a key — checks /v1/models AND tries a real :free chat completion."""
    import requests
    # 1. models list
    try:
        r = requests.get("https://tokenharbor.ai/v1/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=12)
        if r.status_code != 200:
            return False, f"models={r.status_code}"
    except Exception as e:
        return False, f"models_err={str(e)[:30]}"
    # 2. actual :free chat completion
    try:
        r = requests.post("https://tokenharbor.ai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json={"model": model, "messages": [{"role": "user", "content": "hi"}],
                                "max_tokens": 5}, timeout=20)
        if r.status_code == 200:
            return True, "ok"
        return False, f"completions={r.status_code}:{r.text[:50]}"
    except Exception as e:
        return False, f"completions_err={str(e)[:30]}"

def reverify(m, email, password, key):
    """Smart reverify: check inbox first, resend only if needed, API-test key after."""
    # 1. Ensure inbox exists
    try:
        if m.create_cloudmail_inbox(email):
            m.dlog(f"Inbox ensured for {email}")
    except Exception as e:
        m.dlog(f"create inbox {email}: {e}")

    # 2. Check inbox for existing verification email first
    try:
        msgs = m.read_cloudmail_inbox(email)
        link = m.find_verify_link(msgs)
        if link:
            m.dlog(f"Found existing verification link for {email} — clicking...")
            if m._open_link(link):
                # key accompanies the record in reverify callers; test it if present
                if key:
                    works, why = m._test_key(key)
                    if not works:
                        m.log(f"Email verified but key FAILED ({why})", "warn")
                        return False
                    m.log(f"Verified + key OK: {email}", "ok")
                else:
                    m.log(f"Verified via existing email on {email}", "ok")
                return True
            m.dlog(f"Existing link click failed for {email}")
    except Exception as e:
        m.dlog(f"check inbox {email}: {e}")

    # 3. No existing email — resend (login + click verify)
    m.dlog(f"Resending verification for {email}...")
    try:
        m.resend_verification(email, password)
    except Exception as e:
        m.elog(f"resend {email}: {e}")
        return False

    # 4. Wait for email to arrive (120s max — if not here, signal requeue)
    for i in range(1, 7):  # 6 × 20s = 120s
        try:
            msgs = m.read_cloudmail_inbox(email)
            link = m.find_verify_link(msgs)
            if link:
                m.dlog(f"Link found for {email} (attempt {i})")
                if m._open_link(link):
                    if key:
                        works, why = m._test_key(key)
                        if not works:
                            m.log(f"Email verified but key FAILED ({why})", "warn")
                            return False
                        m.log(f"Verified + key OK: {email}", "ok")
                    else:
                        m.log(f"Verified: {email}", "ok")
                    return True
                # clicked but not persisted → resend a fresh link + keep polling
                m.log(f"Link {i} for {email} did not persist — resending", "warn")
                try:
                    m.resend_verification(email, password)
                except Exception as e:
                    m.dlog(f"resend on retry {email}: {e}")
        except Exception as e:
            m.dlog(f"poll {email}: {e}")
        m.dlog(f"No link for {email} ({i}/6), waiting 20s...")
        time.sleep(20)
    m.log(f"TIMEOUT (120s): {email} — queuing for reverify in 600s", "warn")
    return "timeout"

def main():
    m = load_tui()
    if not m.load_env():
        print("No .env")
        return 1

    accounts = []
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 4 and parts[3] == "pending" and parts[2]:
                accounts.append({"email": parts[0], "password": parts[1], "key": parts[2]})
    print(f"Pending accounts with keys: {len(accounts)}")
    if not accounts:
        print("Nothing to reverify")
        return 0

    # Requeue logic: if an account times out (120s, no email yet),
    # wait 600s (10 min) then try again — the slow email may have arrived.
    pending = list(accounts)
    retry_after = 600  # seconds between retry passes
    max_passes = 3
    ok_count = 0
    fail_count = 0
    pass_no = 0

    while pending and pass_no < max_passes:
        pass_no += 1
        m.log(f"=== Pass {pass_no}/{max_passes}: {len(pending)} accounts ===", "arr")
        still_pending = []
        for i, acc in enumerate(pending, 1):
            email = acc["email"]
            m.log(f"[{i}/{len(pending)}] Checking {email}...", "arr")

            # Quick key test first (models + :free completion)
            works, why = test_key(acc["key"])
            if works:
                m.log(f"Key already works: {email}", "ok")
                _mark_ok(KEYS_FILE, email, acc["key"])
                ok_count += 1
                continue
            m.dlog(f"Key pre-test fail for {email}: {why}")

            # Reverify (returns True / False / "timeout")
            result = reverify(m, email, acc["password"], acc["key"])
            if result is True:
                # Wait then test key with API (models + :free completion)
                time.sleep(8)
                works, why = test_key(acc["key"])
                if works:
                    m.log(f"KEY WORKS: {email} (:free ok)", "ok")
                    _mark_ok(KEYS_FILE, email, acc["key"])
                    ok_count += 1
                else:
                    m.log(f"Verified but KEY FAILS :free: {email} ({why})", "warn")
                    fail_count += 1
            elif result == "timeout":
                m.log(f"Queued for retry: {email}", "warn")
                still_pending.append(acc)
            else:
                fail_count += 1

            # Stagger between accounts to avoid rate limits
            time.sleep(5)

        if still_pending:
            m.log(f"=== {len(still_pending)} accounts need retry — waiting {retry_after}s before pass {pass_no+1} ===", "arr")
            time.sleep(retry_after)
        pending = still_pending

    print(f"\nDone: {ok_count} ok / {fail_count} fail / {len(accounts)} total (unresolved: {len(pending)})")
    return 0

def _mark_ok(path, email, key):
    with _mark_lock:
        lines = path.read_text().splitlines()
        out = []
        for line in lines:
            parts = line.strip().split("|")
            if parts and parts[0].lower() == email.lower():
                parts[3] = "ok"
                out.append("|".join(parts))
            else:
                out.append(line)
        path.write_text("\n".join(out) + "\n")

_mark_lock = __import__("threading").Lock()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)