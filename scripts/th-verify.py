#!/usr/bin/env python3
"""th-verify.py — verifikasi email akun Token Harbor yang
API-nya ditolak 403 ("Verify your email address to use the API"), lalu
re-test key di 9router.

Alur:
  1) baca tokenharbor_keys.txt (email|password|apikey|status)
  2) untuk tiap akun, cek langsung ke https://tokenharbor.ai/v1/models
     (valid = HTTP 200, perlu verifikasi = 403 "Verify your email")
  3) akun yang 403 -> login mail.tm (password default [REDACTED] atau dari file),
     ambil link verify-email terbaru dari inbox, buka di browser camoufox
     sampai redirect verify=success
  4) re-test key ke API sampai HTTP 200
  5) re-test koneksi di 9router (POST /api/providers/<id>/test)

Cara pakai:
  python3 th-verify.py              # semua akun 403
  python3 th-verify.py --all        # tes semua (termasuk valid)
  python3 th-verify.py --dry-run    # preview saja
"""

import argparse
import base64
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th_lib as grok  # noqa: E402
import requests  # noqa: E402

from camoufox.sync_api import Camoufox  # noqa: E402

KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenharbor_keys.txt")
KEY_RE = re.compile(r"thk_live_[A-Za-z0-9_-]{20,}")
VERIFY_RE = re.compile(r"https://tokenharbor\.ai/verify-email\?token=[^\s\"'<>]+")
API_BASE = "https://tokenharbor.ai/v1"
MAIL_TM = "https://api.mail.tm"


def load_accounts(path):
    """Return list of dict(email, password, key)."""
    out = []
    if not os.path.exists(path):
        print(f"✗ file tidak ada: {path}")
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            p = line.split("|")
            if len(p) < 2:
                continue
            email = p[0].strip()
            password = p[1].strip()
            m = KEY_RE.search(line)
            if not m:
                continue
            out.append({"email": email, "password": password, "key": m.group(0)})
    return out


def key_status(key):
    """Return ('ok' | 'unverified' | 'invalid', detail)."""
    try:
        r = requests.get(f"{API_BASE}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except Exception as e:
        return "error", str(e)
    if r.status_code == 200:
        return "ok", "valid"
    if r.status_code == 403 and "Verify your email" in r.text:
        return "unverified", r.text[:120]
    return "invalid", f"HTTP {r.status_code}: {r.text[:120]}"


def mailtm_token(email, password):
    r = requests.post(f"{MAIL_TM}/token",
                      json={"address": email, "password": password}, timeout=15)
    if r.status_code != 200:
        # coba password alternatif
        for alt in (os.environ.get("GROK_DEFAULT_PASSWORD", ""),):
            r = requests.post(f"{MAIL_TM}/token",
                              json={"address": email, "password": alt}, timeout=15)
            if r.status_code == 200:
                return r.json()["token"], alt
        return None, password
    return r.json()["token"], password


def fetch_verify_link(email, password):
    tok, pw = mailtm_token(email, password)
    if not tok:
        return None, None, "mail.tm login gagal"
    h = {"Authorization": f"Bearer {tok}"}
    try:
        r = requests.get(f"{MAIL_TM}/messages", headers=h, timeout=15)
        if r.status_code != 200:
            return None, None, f"inbox {r.status_code}"
        member = r.json().get("hydra:member", [])
    except Exception as e:
        return None, None, f"inbox err {e}"

    # cari email verifikasi terbaru
    for msg in sorted(member, key=lambda x: x.get("id", ""), reverse=True):
        subj = (msg.get("subject") or "")
        if "verif" not in subj.lower():
            continue
        try:
            d = requests.get(f"{MAIL_TM}/messages/{msg['id']}", headers=h, timeout=15)
            if d.status_code != 200:
                continue
            html = d.json().get("html") or ""
            text = d.json().get("text") or ""
            if isinstance(html, list):
                html = "".join(str(x) for x in html)
            if isinstance(text, list):
                text = "".join(str(x) for x in text)
            for _fld, _val in (("html", html), ("text", text)):
                try:
                    _dec = base64.b64decode(_val).decode("utf-8", "ignore")
                    _val = _dec
                except Exception:
                    pass
                m = VERIFY_RE.search(_val or "")
                if m:
                    return m.group(0), pw, subj
        except Exception:
            continue
    return None, pw, "tidak ada link verify di inbox"


def verify_link_via_browser(link):
    """Buka link verify di camoufox sampai redirect verify=success."""
    with Camoufox(headless=True) as browser:
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(link, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 20
            while time.time() < deadline:
                if "verify=success" in page.url:
                    return True, page.url
                page.wait_for_timeout(1500)
            return False, page.url
        except Exception as e:
            return False, str(e)[:120]


def retest_router_connection(conn_id, router_base, token):
    """Trigger test ulang koneksi 9router. Return (ok, msg)."""
    try:
        r = requests.post(
            f"{router_base}/api/providers/{conn_id}/test",
            cookies={"auth_token": token},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        return r.status_code in (200, 201), r.text[:150]
    except Exception as e:
        return False, str(e)[:120]


def main():
    ap = argparse.ArgumentParser(description="Verifikasi email + re-test key Token Harbor")
    ap.add_argument("--file", default=KEYS_FILE)
    ap.add_argument("--all", action="store_true", help="tes semua akun, bukan hanya 403")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--router-base", default=grok.ROUTER_BASE)
    ap.add_argument("--retest-router", action="store_true",
                    help="re-test koneksi di 9router setelah key valid")
    args = ap.parse_args()

    accounts = load_accounts(args.file)
    if not accounts:
        return 1
    print(f"[+] {len(accounts)} akun di {args.file}")

    need = []
    for a in accounts:
        st, det = key_status(a["key"])
        a["status"], a["detail"] = st, det
        if st == "ok":
            print(f"  ✓ {a['email']}: valid")
        else:
            print(f"  ✗ {a['email']}: {st} — {det[:90]}")
            if args.all or st == "unverified":
                need.append(a)

    if not need:
        print("\nTidak ada akun yang perlu diverifikasi.")
        return 0

    print(f"\n[+] Akan verifikasi {len(need)} akun")
    if args.dry_run:
        print("    (dry-run, tidak ada perubahan)")
        return 0

    token = grok.generate_router_token()
    # peta koneksi 9router: nama -> id (untuk re-test)
    conn_by_name = {}
    if args.retest_router:
        try:
            prov = requests.get(f"{args.router_base}/api/providers",
                                cookies={"auth_token": token}, timeout=15).json()
            for c in prov.get("connections", []):
                conn_by_name[c.get("name") or ""] = c.get("id")
        except Exception:
            print("    ⚠ gagal ambil daftar koneksi 9router (re-test dilewati)")

    verified = failed = 0
    for a in need:
        email = a["email"]
        print(f"\n=== {email} ===")
        link, pw, note = fetch_verify_link(email, a["password"])
        if not link:
            print(f"    ✗ {note}")
            failed += 1
            continue
        print(f"    [mail] {note} | link: {link[:90]}...")
        ok, url = verify_link_via_browser(link)
        print(f"    [browser] verify={'success' if ok else 'gagal'} -> {url[:100]}")
        time.sleep(2)
        st, det = key_status(a["key"])
        if st == "ok":
            verified += 1
            print(f"    ✓ KEY VALID sekarang: {a['key'][:16]}...")
            if args.retest_router:
                # cari koneksi dengan key ini di 9router (nama ai N)
                name = None
                for n, cid in conn_by_name.items():
                    pass
                # simpel: trigger test semua koneksi punya baseUrl tokenharbor
                try:
                    prov = requests.get(f"{args.router_base}/api/providers",
                                        cookies={"auth_token": token}, timeout=15).json()
                    for c in prov.get("connections", []):
                        b = (c.get("providerSpecificData") or {}).get("baseUrl", "")
                        if "tokenharbor.ai/v1" in b:
                            rok, rmsg = retest_router_connection(c["id"], args.router_base, token)
                            if not rok:
                                print(f"    ⚠ re-test conn {c.get('name')}: {rmsg[:80]}")
                    print("    [9router] trigger re-test semua koneksi tokenharbor")
                except Exception as e:
                    print(f"    ⚠ re-test 9router gagal: {e}")
        else:
            failed += 1
            print(f"    ✗ masih {st}: {det[:90]}")

    print(f"\n=== Ringkasan ===")
    print(f"  Terverifikasi : {verified}")
    print(f"  Gagal         : {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
