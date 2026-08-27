#!/usr/bin/env python3
"""th-freeplan.py — aktifkan free models (consent) untuk semua akun
Token Harbor yang belum enabled, lalu verifikasi via API.

Latar belakang: beberapa akun menolak free model dengan
  403 "Free-model consent has changed. Review and enable free models"
karena tombol "Enable free models" tidak sempat diklik (alur verify email
yang lewat /login?verify=success). Script ini login tiap akun, klik tombol
enable free models di dashboard, lalu uji deepseek-v4-flash:free.

Cara pakai:
  python3 th-freeplan.py
  python3 th-freeplan.py --all         # semua akun, bukan hanya yang gagal
  python3 th-freeplan.py --dry-run
  python3 th-freeplan.py --email user@x.com
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th_lib as grok  # noqa: E402
import requests  # noqa: E402

from camoufox.sync_api import Camoufox  # noqa: E402

BASE = "https://tokenharbor.ai"
SITEKEY = "0x4AAAAAADBuC8Knz1EJZx9-"
API = "https://tokenharbor.ai/v1"
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenharbor_keys.txt")
KEY_RE = re.compile(r"thk_live_[A-Za-z0-9_-]{20,}")

INIT = """
(() => {
  let wrapped = false;
  const iv = setInterval(() => {
    if (wrapped) { clearInterval(iv); return; }
    const ts = window.turnstile;
    if (ts && typeof ts.render === 'function') {
      wrapped = true;
      window.turnstile = {
        render(el, opts) { window.__thCb = opts.callback; window.__thAction = opts.action; return undefined; },
        execute: ts.execute, load: ts.load, reset: ts.reset,
        getResponse: ts.getResponse,
      };
    }
  }, 10);
})();
"""


def load_accounts(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            p = line.strip().split("|")
            if len(p) < 2:
                continue
            m = KEY_RE.search(line)
            if not m:
                continue
            out.append({"email": p[0].strip(), "password": p[1].strip(), "key": m.group(0)})
    return out


def free_model_ok(key):
    try:
        r = requests.post(
            f"{API}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "deepseek-v4-flash:free",
                  "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
            timeout=60,
        )
        if r.status_code == 200:
            return True, ""
        if "Free-model consent" in r.text:
            return False, "consent-needed"
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, f"err {e}"


def login_and_enable(page, email, password):
    """Login + klik enable free models. Return 'enabled' | 'already' | 'gagal'."""
    try:
        page.goto(f"{BASE}/login?mode=signin", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return f"gagal: {str(e)[:60]}"
    tok = grok.solve_turnstile_bycf(sitekey=SITEKEY, page_url=f"{BASE}/login?mode=signin")
    if tok and page.evaluate("typeof window.__thCb === 'function'"):
        page.evaluate("(t) => { window.__thCb(t); }", tok)
    try:
        page.wait_for_selector('input[name="email"]', timeout=30000)
    except Exception:
        return "gagal: form login tak muncul"
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    try:
        page.evaluate("""(t) => {
            const form = document.querySelector('form');
            if (!form) return;
            let h = form.querySelector('input[name="cf-turnstile-response"]');
            if (!h) { h = document.createElement('input'); h.type='hidden'; h.name='cf-turnstile-response'; h.value=t; form.appendChild(h); }
            else h.value = t;
        }""", tok)
    except Exception:
        pass
    try:
        page.click('button[type="submit"]', timeout=5000)
    except Exception:
        return "gagal: submit"
    try:
        page.wait_for_url(re.compile(r"/dashboard"), timeout=30000)
    except Exception:
        return "gagal: login (tidak ke dashboard)"

    # klik tombol enable free models (beberapa varian)
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
    ap.add_argument("--file", default=KEYS_FILE)
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
    for a in accounts:
        ok, det = free_model_ok(a["key"])
        a["free_ok"] = ok
        print(f"  {'✓' if ok else '✗'} {a['email']:24s} free_model={'OK' if ok else det}")
        if not ok and (args.all or det == "consent-needed"):
            todo.append(a)

    if not todo:
        print("\nSemua free model sudah aktif. Tidak perlu perubahan.")
        return 0
    print(f"\n[+] Akan enable free models utk {len(todo)} akun")
    if args.dry_run:
        print("    (dry-run, tidak ada perubahan)")
        return 0

    with Camoufox(headless=True) as browser:
        for a in todo:
            ctx = browser.new_context()
            try:
                ctx.add_init_script(INIT)
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
