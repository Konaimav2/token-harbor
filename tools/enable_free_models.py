#!/usr/bin/env python3
"""enable_free_models.py — aktifkan free models (consent) untuk semua akun
Token Harbor yang belum enabled, lalu verifikasi via API.

Latar belakang: beberapa akun menolak free model dengan
  403 "Free-model consent has changed. Review and enable free models"
karena tombol "Enable free models" tidak sempat diklik (alur verify email
yang lewat /login?verify=success). Script ini login tiap akun, klik tombol
enable free models di dashboard, lalu uji deepseek-v4-flash:free.

Cara pakai:
  python3 enable_free_models.py
  python3 enable_free_models.py --all         # semua akun, bukan hanya yang gagal
  python3 enable_free_models.py --dry-run
  python3 enable_free_models.py --email user@x.com
"""

import argparse
import os
import re
import sys
import time

import requests  # noqa: E402

BASE = "https://tokenharbor.ai"
SITEKEY = "0x4AAAAAADBuC8Knz1EJZx9-"
API = "https://tokenharbor.ai/v1"
# NOTE: Turnstile shim removed — tokenharbor login has no Turnstile widget;
# an injected-but-never-read shim only enlarges the fingerprint surface.
KEY_RE = re.compile(r"thk_[A-Za-z0-9_-]{20,}")


def _project_base():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_accounts(path):
    # keystore first (JSON source of truth), legacy pipe files as fallback
    try:
        sys.path.insert(0, _project_base())
        import keystore as _ks
        recs = _ks.store().load()
        out = []
        for r in recs:
            if not isinstance(r, dict):
                continue
            em = (r.get("email") or "").strip()
            k = r.get("api_key") or ""
            if em and k:
                out.append({"email": em, "password": r.get("password") or "", "key": k})
        if out:
            return out
    except Exception as e:
        print(f"  [keystore load: {e}] falling back to legacy pipe files")
    out = []
    cands = [path] if path else []
    cands += [os.path.join(_project_base(), f)
              for f in ("keys.txt", "data/keys.txt", "tokenharbor_keys.txt")]
    seen = set()
    for cand in cands:
        if not cand or not os.path.exists(cand):
            continue
        with open(cand, encoding="utf-8") as f:
            for line in f:
                p = line.strip().split("|")
                if len(p) < 2:
                    continue
                m = KEY_RE.search(line)
                if not m or p[0].strip().lower() in seen:
                    continue
                seen.add(p[0].strip().lower())
                out.append({"email": p[0].strip(), "password": p[1].strip(), "key": m.group(0)})
    return out


def free_model_ok(key):
    try:
        r = requests.post(
            f"{API}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "deepseek-v4-flash:free",
                  "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            timeout=12,  # dry-run budget: 37 keys must fit a 75s soft deadline
        )
        if r.status_code == 200:
            return True, ""
        if "Free-model consent" in r.text:
            return False, "consent-needed"
        if r.status_code == 403:
            return False, "forbidden-403"
        if r.status_code == 402:
            return False, "plan-402"
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, f"err {e}"


def login_and_enable(page, email, password):
    """Login + klik enable free models. Return 'enabled' | 'already' | 'gagal'."""
    try:
        page.goto(f"{BASE}/login?mode=signin", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return f"gagal: {str(e)[:60]}"
    # no Turnstile on tokenharbor login — skip solving entirely
    try:
        page.wait_for_selector('input[name="email"]', timeout=30000)
    except Exception:
        return "gagal: form login tak muncul"
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    try:
        page.click('button[type="submit"]', timeout=5000)
    except Exception:
        return "gagal: submit"
    try:
        page.wait_for_url(re.compile(r"/dashboard"), timeout=30000)
    except Exception:
        return "gagal: login (tidak ke dashboard)"

    # 1. Click "Verify email" banner button if present (fixes 403 / not-verified)
    try:
        vsel = 'button:has-text("Verify email"), a:has-text("Verify email")'
        deadline_v = time.time() + 8
        while time.time() < deadline_v:
            try:
                if page.locator(vsel).count():
                    page.locator(vsel).first.click(timeout=4000)
                    page.wait_for_timeout(2000)
                    return "verify-clicked"
            except Exception:
                pass
            page.wait_for_timeout(800)
    except Exception:
        pass

    # 2. Click the "Enable free models" consent button (fixes 402 / plan)
    sels = [
        'button:has-text("Enable free models")',
        'button:has-text("Review and enable")',
        'a:has-text("Enable free models")',
    ]
    deadline = time.time() + 15
    while time.time() < deadline:
        for sel in sels:
            try:
                if page.locator(sel).count():
                    page.locator(sel).first.click(timeout=4000)
                    page.wait_for_timeout(1500)
                    return "enabled"
            except Exception:
                continue
        page.wait_for_timeout(1200)
    return "already-or-notfound"


def main():
    ap = argparse.ArgumentParser(description="Aktifkan free models di tokenharbor.ai")
    ap.add_argument("--file", default=None)
    ap.add_argument("--all", action="store_true", help="proses semua akun (bukan hanya gagal)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--email", default=None, help="proses satu akun spesifik")
    args = ap.parse_args()

    accounts = load_accounts(args.file)
    if args.email:
        accounts = [a for a in accounts if a["email"] == args.email]
    if not accounts:
        print("✗ tidak ada akun")
        return 1

    print(f"[+] {len(accounts)} akun. Cek status free model via API...")
    todo = []
    t0 = time.monotonic()
    checked = 0
    for a in accounts:
        if args.dry_run and time.monotonic() - t0 > 75:
            print(f"    ... deadline 75s, berhenti (partial: {checked}/{len(accounts)} checked)")
            break
        checked += 1
        ok, det = free_model_ok(a["key"])
        a["free_ok"] = ok
        print(f"  {'✓' if ok else '✗'} {a['email']:24s} free_model={'OK' if ok else det}")
        if not ok and (args.all or det in ("consent-needed", "forbidden-403", "plan-402")):
            todo.append(a)

    if not todo:
        print(f"\nSemua free model sudah aktif. Tidak perlu perubahan. (checked {checked}/{len(accounts)})")
        return 0
    print(f"\n[+] Akan enable free models utk {len(todo)} akun")
    if args.dry_run:
        print("    (dry-run, tidak ada perubahan)")
        return 0

    # launch without default addons (uBlock download may fail on slow networks)
    try:
        from camoufox.sync_api import Camoufox
        from camoufox.addons import DefaultAddons
    except ImportError:
        print("  camoufox not installed — run: pip install camoufox && python -m camoufox fetch")
        return 1
    with Camoufox(headless=True, addons=[], exclude_addons=[DefaultAddons.UBO]) as browser:
        for a in todo:
            ctx = browser.new_context()
            try:
                page = ctx.new_page()
                res = login_and_enable(page, a["email"], a["password"])
                print(f"  {a['email']}: {res}")
                time.sleep(1)
                ok, det = free_model_ok(a["key"])
                print(f"      -> free_model sekarang: {'OK' if ok else det}")
            except Exception as e:
                print(f"  {a['email']}: error {str(e)[:80]}")
            finally:
                ctx.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
