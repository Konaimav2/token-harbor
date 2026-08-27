#!/usr/bin/env python3
"""th-create.py — auto-signup tokenharbor.ai (alur resmi) lalu buat API key.

Alur per akun:
  1) buat inbox tempmail (mail.tm, fallback generator.email)
  2) signup via /login?mode=signup (Turnstile diselesaikan via BYCF, token
     diinjeksi ke callback React karena widget tidak render di headless)
  3) accept analytics cookies (jika ada)
  4) enable free models
  5) verify email (baca link dari inbox)  -> wajib agar API key bisa dipakai
  6) buat API key di /dashboard/api-keys  -> format thk_live_...
  7) simpan  email|password|apikey  ke file txt

Pembatasan per jaringan (IP): "Too many sign-ups from this network.
Please try again in an hour." -> script mendeteksi dan mencatat status
"ratelimited" tanpa menunggu; jalankan lagi setelah kuota reset.
"""

import argparse
import base64
import os
import random
import re
import string
import sys
import threading
import time

MIN_PY = (3, 10)  # dibutuhkan camoufox
if sys.version_info < MIN_PY:
    sys.exit(
        f"Python >= {MIN_PY[0]}.{MIN_PY[1]} diperlukan (terdeteksi {sys.version_info[0]}.{sys.version_info[1]}).\n"
        "Di Ubuntu 20.04/18.04 python3 bawaan terlalu tua. Jalankan dulu:\n"
        "  bash install.sh\n"
        "(install.sh otomatis memasang Python 3.11 via PPA deadsnakes bila perlu.)"
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import th_lib as grok  # noqa: E402

from camoufox.sync_api import Camoufox  # noqa: E402

BASE = "https://tokenharbor.ai"
SITEKEY = "0x4AAAAAADBuC8Knz1EJZx9-"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokenharbor_keys.txt")

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

VERIFY_RE = re.compile(r"https://tokenharbor\.ai/verify-email\?token=[^\s\"'<>]+")
KEY_RE = re.compile(r"\bthk_live_[A-Za-z0-9_-]{20,}\b")
RATELIMIT_RE = re.compile(r"Too many sign-ups from this network")


def _b64_part(part):
    if not part:
        return ""
    if isinstance(part, list):
        parts = []
        for x in part:
            if isinstance(x, str):
                parts.append(_b64_part(x))
        return " ".join(parts)
    try:
        return base64.b64decode(part).decode("utf-8", "ignore")
    except Exception:
        return str(part)


class MailReader:
    """Baca email dari MailTmBox (API) atau TempEmail (generator.email)."""

    def __init__(self, box):
        self.box = box

    def read(self):
        """Return list of (subject, text, html)."""
        box = self.box
        out = []
        if isinstance(box, grok.MailTmBox):
            try:
                resp = box._get("/messages")
                if resp.status_code != 200:
                    return out
                body = resp.json()
                member = body.get("hydra:member", []) if isinstance(body, dict) else body
            except Exception:
                return out
            for m in member or []:
                try:
                    mr = box._get(f"/messages/{m['id']}")
                    if mr.status_code != 200:
                        continue
                    d = mr.json()
                    out.append(
                        (d.get("subject", ""),
                         _b64_part(d.get("text")),
                         _b64_part(d.get("html")))
                    )
                except Exception:
                    continue
        else:
            try:
                out.append(("(inbox)", "", box.get_inbox_page()))
            except Exception:
                pass
        return out

    def wait_for_verify_link(self, timeout=90, poll=2):
        deadline = time.time() + timeout
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            for subj, txt, html in self.read():
                m = VERIFY_RE.search(html or txt or "")
                if m:
                    return m.group(0)
            if attempts % 10 == 0:
                print(f"    ... waiting for email ({attempts * poll}s)")
            time.sleep(poll)
        return None


def _buttons(page):
    return [(b.inner_text() or "").strip()[:60] for b in page.locator("button").all() if (b.inner_text() or "").strip()]


def _click_button(page, label):
    for b in page.locator("button").all():
        if label in (b.inner_text() or ""):
            try:
                b.click()
                return True
            except Exception:
                pass
    return False


def _enable_free_models_modal(page, timeout=8000):
    """Aktifkan free-model consent bila muncul di dashboard.

    Token Harbor menampilkan tombol/CTA "Enable free models" (varian:
    "Free-model consent has changed. Review and enable"). Tanpa diklik,
    API free model (deepseek-v4-flash:free, mimo-v2.5:free) menolak 403.
    Return True bila sudah aktif / berhasil diklik, False bila tidak ketemu.
    """
    selectors = [
        'button:has-text("Enable free models")',
        'button:has-text("Review and enable")',
        'button:has-text("Enable free models")',
        'a:has-text("Enable free models")',
    ]
    deadline = time.time() + timeout/1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).count():
                    page.locator(sel).first.click(timeout=3000)
                    page.wait_for_timeout(800)
                    print("    [free models] klik tombol enable")
                    return True
            except Exception:
                continue
        page.wait_for_timeout(800)
    return False


def _capture_turnstile(page, timeout=15):
    """Tunggu sampai callback turnstile tertangkap oleh wrapper init-script."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate("typeof window.__thCb === 'function'"):
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def _wait_selector(page, selector, timeout=15000):
    """Tunggu elemen muncul. Return True/False tanpa throw."""
    try:
        page.wait_for_selector(selector, timeout=timeout)
        return True
    except Exception:
        return False


def _wait_url(page, pattern, timeout=15000):
    """Tunggu URL cocok dengan regex. Return True/False tanpa throw."""
    try:
        page.wait_for_url(re.compile(pattern), timeout=timeout)
        return True
    except Exception:
        return False


def _wait_dashboard(page, timeout=15000):
    """Tunggu redirect ke /dashboard (URL direct ataupun proxy)."""
    return _wait_url(page, r"/dashboard(?:\?|$)", timeout=timeout)


def _solve_turnstile_async(sitekey, page_url, timeout=120):
    """Solve turnstile BYCF di thread terpisah (paralel dengan navigasi).

    Return dict dengan kunci 'tok' atau 'err' setelah thread selesai.
    """
    holder = {}

    def _run():
        try:
            holder["tok"] = grok.solve_turnstile_bycf(
                sitekey=sitekey, page_url=page_url, timeout=timeout)
        except Exception as e:
            holder["err"] = str(e)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return holder, th


PROXYIUM_URL = "https://proxyium.com/"
PROXYIUM_COUNTRIES = ["pl", "us", "sg"]


def _page_is_tokenharbor(page):
    """Deteksi halaman tokenharbor.ai (URL proxy berisi IP, jadi cek juga teks body)."""
    try:
        if "tokenharbor" in page.url:
            return True
        body = (page.evaluate("document.body.innerText") or "")
        low = body.lower()
        return "token harbor" in low or "tokenharbor" in low
    except Exception:
        return False


def _open_via_proxyium(page, target_url):
    """Buka target_url lewat web-proxy proxyium.com.

    Alur (sesuai halaman proxyium.com):
      1) goto https://proxyium.com/
      2) isi input[name=url] dengan target_url
      3) submit form (POST ke cdn.proxyium.com/proxyrequest.php)
      4) proxyium redirect (location.href) ke server proxy -> halaman target

    Return True bila halaman target benar-benar terbuka.
    """
    target = target_url.split("?")[0]
    try:
        if not _wait_selector(page, 'input[name="url"]', timeout=15000):
            try:
                page.goto(PROXYIUM_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            _wait_selector(page, 'input[name="url"]', timeout=15000)
        try:
            page.fill('input[name="url"]', target_url)
        except Exception:
            return False
        for country in PROXYIUM_COUNTRIES:
            try:
                page.evaluate(
                    "document.querySelector('select[name=proxy_country]').value=arguments[0]",
                    country,
                )
            except Exception:
                pass
            try:
                page.click('button[type="submit"]', timeout=4000)
            except Exception:
                continue
            # redirect ke server proxy bisa cepat; polling pendek setiap 1.5s
            for _ in range(14):
                low = ""
                try:
                    low = (page.evaluate("document.body.innerText") or "").lower()
                except Exception:
                    pass
                if "request failed" in low or "blocked" in low:
                    break
                if _page_is_tokenharbor(page):
                    return True
                page.wait_for_timeout(1500)
            print(f"    [!] proxyium ({country}) gagal membuka {target}, coba negara lain...")
    except Exception as e:
        print(f"    [!] proxyium error: {str(e)[:120]}")
    return False


def _open_signup_tab_via_proxy(page):
    """Buka form signup lewat proxyium dengan alur yang andal:
      1) buka halaman utama https://tokenharbor.ai
      2) klik 'Sign in' (di header)
      3) klik tab 'Sign up'

    Menghindari /login?mode=signup karena saat lewat proxy mode-nya tidak
    terbaca sehingga form email/password tidak dirender.
    """
    if not _open_via_proxyium(page, f"{BASE}/"):
        return False
    clicked = False
    for sel in ('a:has-text("Sign in")', 'a[href="/login"]'):
        if _wait_selector(page, sel, timeout=8000):
            try:
                page.click(sel, timeout=5000)
                clicked = True
                break
            except Exception:
                continue
    if not clicked:
        print("    [!] link 'Sign in' tidak ditemukan")
        return False
    for label in ("Sign up", "Create an account"):
        if _wait_selector(page, f'button:has-text("{label}")', timeout=8000):
            try:
                page.locator(f'button:has-text("{label}")').first.click(timeout=4000)
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    print("    [!] tab 'Sign up' tidak ditemukan")
    return False


def _inject_turnstile(page, tok):
    """Suntik token turnstile: lewat callback React bila widget render,
    atau lewat hidden input cf-turnstile-response bila widget tidak render."""
    try:
        if page.evaluate("typeof window.__thCb === 'function'"):
            page.evaluate("(t) => { window.__thCb(t); }", tok)
            page.wait_for_timeout(1500)
            return
    except Exception:
        pass
    try:
        page.evaluate("""(t) => {
            const form = document.querySelector('form');
            if (!form) return;
            let h = form.querySelector('input[name="cf-turnstile-response"]');
            if (!h) {
                h = document.createElement('input');
                h.type = 'hidden'; h.name = 'cf-turnstile-response'; h.value = t;
                form.appendChild(h);
            } else { h.value = t; }
        }""", tok)
    except Exception:
        pass


def signup(page, email, password, via_proxy=False):
    """Lakukan signup UI. Return 'ok' | 'ratelimited' | 'blocked' | 'error'."""
    raw_errors = []

    def _on_response(res):
        try:
            if res.request.method == "POST" and "/login" in res.url:
                raw_errors.append(res.text())
        except Exception:
            pass

    page.on("response", _on_response)
    for attempt in range(3):
        holder, th = _solve_turnstile_async(SITEKEY, f"{BASE}/login?mode=signup")

        if via_proxy:
            if not _open_signup_tab_via_proxy(page):
                return "error"
        else:
            try:
                page.goto(f"{BASE}/login?mode=signup", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"    [!] gagal buka halaman login (attempt {attempt + 1}): {str(e)[:80]}")
                continue

        th.join(timeout=120)
        tok = holder.get("tok")
        if not tok:
            print(f"    [!] BYCF gagal (attempt {attempt + 1}): {holder.get('err', 'timeout')}")
            continue

        if _wait_selector(page, 'input[name="email"]', timeout=3000):
            _wait_selector(page, 'input[name="password"]', timeout=2000)
        else:
            if _capture_turnstile(page, timeout=10000):
                _inject_turnstile(page, tok)
            if not _wait_selector(page, 'input[name="email"]', timeout=15000):
                print(f"    [!] form email/password tidak muncul (attempt {attempt + 1})")
                continue
            _wait_selector(page, 'input[name="password"]', timeout=5000)

        try:
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password)
        except Exception as e:
            print(f"    [!] gagal isi form (attempt {attempt + 1}): {str(e)[:80]}")
            continue

        _inject_turnstile(page, tok)
        try:
            page.click('button[type="submit"]', timeout=4000)
        except Exception as e:
            print(f"    [!] tombol submit tidak bisa diklik (attempt {attempt + 1}): {str(e)[:80]}")
            continue

        if _wait_dashboard(page, timeout=15000):
            return "ok"

        raw = "\n".join(r or "" for r in raw_errors)
        if "Too many sign-ups" in raw:
            return "ratelimited"
        try:
            body = page.evaluate("document.body.innerText") or ""
        except Exception:
            body = ""
        if "couldn't create your account" in raw or "couldn't create your account" in body:
            return "blocked"
        detail = " | ".join((r or "")[-200:] for r in raw_errors if r)
        print(f"    [!] hasil tak dikenal (attempt {attempt + 1}): {body[-180:].replace(chr(10), ' | ')}"
              + (f"\n        POST resp: {detail}" if detail else ""))
    return "error"


def post_signup(page, reader):
    """Accept analytics, enable free models, verify email. Return bool verified.

    Email verifikasi dikirim OTOMATIS saat signup. Jangan pernah menganggap
    akun sudah terverifikasi hanya karena tombol "Verify email" tidak muncul —
    itu sumber key invalid (403 "Verify your email address to use the API").
    Selalu baca link verify dari inbox dan buka sampai verify=success.
    """
    _click_button(page, "Accept analytics")
    page.wait_for_timeout(500)
    _click_button(page, "Enable free models")
    page.wait_for_timeout(600)

    # Cek apakah dashboard sudah ada tombol "Verify email"
    try:
        body = page.evaluate("document.body.innerText") or ""
        if "Verify your email" in body or "verify your email" in body:
            print("    [dashboard] email belum terverifikasi, klik tombol 'Send verification email'")
            try:
                _click_button(page, "Send verification email")
                page.wait_for_timeout(2000)
            except Exception:
                pass
    except Exception:
        pass

    link = reader.wait_for_verify_link(timeout=90, poll=2)
    if not link:
        print("    [!] email verifikasi tidak ditemukan di inbox setelah 90s -> akun unverified")
        return False
    print(f"    [verify] link: {link[:80]}...")
    try:
        page.goto(link, wait_until="domcontentloaded", timeout=30000)
        _wait_url(page, r"verify=success", timeout=15000)
    except Exception:
        pass
    print(f"    [verify] result url: {page.url}")
    return "verify=success" in page.url


def create_api_key(page, label):
    """Buat API key.

    Struktur UI (dari pemeriksaan langsung /dashboard/api-keys):
      - tombol "+ New key" (class btn-float-primary) membuka modal
        `section.card-float` (BUKAN [role=dialog]).
      - di dalam modal: input label `input-float`
        (placeholder "e.g. Cursor, Production, Side project", React controlled)
        dan tombol "Create key" yang start disabled; menjadi enabled begitu
        input label terisi.
      - setelah klik, key thk_live_... muncul di body halaman.
    """
    got = False
    for attempt in range(3):
        try:
            page.goto(f"{BASE}/dashboard/api-keys", wait_until="domcontentloaded", timeout=45000)
            got = True
            break
        except Exception as e:
            print(f"    [!] gagal buka halaman API keys (attempt {attempt + 1}/3): {str(e)[:60]}")
            page.wait_for_timeout(2000)
    if not got:
        return None

    # Jika halaman menanyakan "Enable free models", klik dulu
    _enable_free_models_modal(page, timeout=10000)
    
    if _wait_selector(page, 'button:has-text("+ New key")', timeout=20000):
        try:
            page.locator('button:has-text("+ New key")').first.click(timeout=4000)
        except Exception as e:
            print(f"    [!] klik '+ New key' gagal: {str(e)[:80]}")
            return None
    else:
        try:
            body = page.evaluate("document.body.innerText") or ""
            if "login" in page.url.lower():
                print("    [!] sesi hilang, redirect ke /login (proxy mode?)")
                return None
        except Exception:
            pass
        print("    [!] tombol '+ New key' tidak ditemukan pada halaman")
        return None

    label_inp = page.locator('input[placeholder*="Cursor"]')
    if not _wait_selector(page, 'input[placeholder*="Cursor"]', timeout=8000):
        print("    [!] input label key tidak muncul")
        return None
    if not label_inp.first.is_visible():
        page.wait_for_timeout(400)
    btn = page.locator('button:has-text("Create key")').first

    clicked = False
    deadline = time.time() + 8
    while time.time() < deadline and not clicked:
        try:
            cur = label_inp.first.input_value()
            if cur != label:
                label_inp.first.fill(label)
            elif not btn.is_enabled():
                label_inp.first.fill("")
                label_inp.first.fill(label)
        except Exception:
            pass
        try:
            if btn.is_enabled():
                btn.click(timeout=2000)
                clicked = True
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if not clicked:
        try:
            btn.click(timeout=1500, force=True)
            clicked = True
        except Exception:
            pass
    if not clicked:
        print("    [!] tombol 'Create key' tidak bisa diklik (disabled/timeout)")
        return None

    deadline = time.time() + 8
    body = ""
    while time.time() < deadline:
        try:
            body = page.evaluate("document.body.innerText") or ""
        except Exception:
            body = ""
        m = KEY_RE.search(body)
        if m:
            return m.group(0)
        page.wait_for_timeout(800)
    print("    [!] key tidak tertangkap; body:", (body or "")[-400:].replace("\n", " | "))
    return None


def signin_and_create_key(page, email, password, label):
    """Sign-in langsung ke tokenharbor.ai (pakai email/password) lalu buat API key.

    Dipakai untuk mode --proxy: setelah signup + verify email, sesi langsung
    di tokenharbor.ai belum ada, jadi login ulang pakai kredensial akun.
    """
    # Pastikan kita di halaman login tokenharbor (proxy mungkin redirect)
    got_login = False
    for attempt in range(2):
        try:
            if "/login" not in page.url.lower():
                page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=45000)
            got_login = True
            break
        except Exception as e:
            print(f"    [!] gagal buka halaman login (attempt {attempt + 1}/2): {str(e)[:60]}")
            page.wait_for_timeout(2000)
    if not got_login:
        return None

    holder, th = _solve_turnstile_async(SITEKEY, f"{BASE}/login?mode=signin")
    th.join(timeout=120)
    tok = holder.get("tok")
    if not tok:
        print(f"    [!] BYCF gagal (login): {holder.get('err', 'timeout')}")
        return None

    if _wait_selector(page, 'input[name="email"]', timeout=3000):
        _wait_selector(page, 'input[name="password"]', timeout=2000)
    else:
        if _capture_turnstile(page, timeout=10000):
            _inject_turnstile(page, tok)
        if not _wait_selector(page, 'input[name="email"]', timeout=15000):
            print("    [!] form login tidak muncul")
            return None
        _wait_selector(page, 'input[name="password"]', timeout=5000)

    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    _inject_turnstile(page, tok)
    try:
        page.click('button[type="submit"]', timeout=4000)
    except Exception as e:
        print(f"    [!] submit login gagal: {str(e)[:80]}")
        return None
    if _wait_dashboard(page, timeout=15000):
        _enable_free_models_modal(page, timeout=5000)
        return create_api_key(page, label)
    try:
        b = page.evaluate("document.body.innerText") or ""
    except Exception:
        b = ""
    print(f"    [!] login langsung gagal: {b[-200:].replace(chr(10), ' | ')}")
    return None


def create_account(browser, label, via_proxy=False):
    """Satu akun penuh (context baru di browser yang dibagi). Return dict."""
    password = "".join(random.choices(string.ascii_letters + string.digits, k=18))
    box = grok.create_mailbox(None)
    email = box.email
    print(f"[+] mailbox: {email} ({type(box).__name__})")
    reader = MailReader(box)

    ctx = browser.new_context()
    try:
        ctx.add_init_script(INIT)
        page = ctx.new_page()

        status = signup(page, email, password, via_proxy=via_proxy)
        print(f"    [signup] -> {status} ({page.url})")
        if status != "ok":
            return {"email": email, "password": password, "api_key": "", "status": status}

        verified = post_signup(page, reader)
        print(f"    [email verified]: {verified}")

        if not verified:
            print("    [!] email TIDAK terverifikasi -> akun disimpan sebagai 'unverified'")
            return {"email": email, "password": password, "api_key": "", "status": "unverified"}

        if via_proxy:
            # sesi langsung di tokenharbor.ai belum ada -> sign-in pakai kredensial,
            # lalu buat API key.
            api_key = signin_and_create_key(page, email, password, label)
        else:
            _enable_free_models_modal(page, timeout=8000)
            api_key = create_api_key(page, label)
        print(f"    [api key]: {api_key}")
    finally:
        ctx.close()

    status = "ok" if api_key else "no-key"
    return {"email": email, "password": password, "api_key": api_key or "", "status": status}


def main():
    ap = argparse.ArgumentParser(description="Auto-signup tokenharbor.ai + buat API key (cepat)")
    ap.add_argument("-c", "--count", type=int, default=1, help="jumlah akun (default 1)")
    ap.add_argument("--label", default="router-prod", help="label API key")
    ap.add_argument("--wait", type=int, default=60, help="jeda antar akun (detik, default 60)")
    ap.add_argument("--proxy", action="store_true", help="browse lewat web-proxy proxyium.com "
                    "(untuk hindari rate-limit signup per IP)")
    ap.add_argument("--fast", action="store_true", help="mode cepat (jeda 30 detik)")
    ap.add_argument("--turbo", action="store_true", help="mode turbo (jeda 15 detik, risiko rate-limit)")
    ap.add_argument("-t", "--threads", type=int, default=1,
                    help="jumlah akun paralel (default 1, pakai --threads 3 untuk 3x cepat)")
    args = ap.parse_args()

    if args.turbo:
        args.wait = 15
    elif args.fast:
        args.wait = 30

    args.threads = max(1, args.threads)

    results = []

    def _create_one(_idx):
        with Camoufox(headless=True) as _browser:
            return create_account(_browser, args.label, via_proxy=args.proxy)

    if args.threads > 1:
        # Jalankan beberapa browser paralel sekaligus
        import concurrent.futures as _cf
        print(f"\n🚀 Mode paralel: {args.threads} worker untuk {args.count} akun")
        with _cf.ThreadPoolExecutor(max_workers=args.threads) as _ex:
            futures = {_ex.submit(_create_one, i): i for i in range(args.count)}
            for _f in _cf.as_completed(futures):
                _i = futures[_f]
                try:
                    _res = _f.result()
                except Exception as _e:
                    _res = {"email": "error", "password": "", "api_key": "", "status": f"error:{_e}"}
                results.append(_res)
                _line = f"{_res['email']}|{_res['password']}|{_res['api_key']}|{_res['status']}"
                with open(OUTPUT_FILE, "a", encoding="utf-8") as _of:
                    _of.write(_line + "\n")
                print(f"    SAVED: {_line}")
    else:
        with Camoufox(headless=True) as browser:
            for i in range(args.count):
                print(f"\n=== akun {i + 1}/{args.count} ===")
                res = create_account(browser, args.label, via_proxy=args.proxy)
                results.append(res)
                line = f"{res['email']}|{res['password']}|{res['api_key']}|{res['status']}"
                with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                print(f"    SAVED: {line}")

                if res["status"] in ("blocked", "ratelimited"):
                    print(f"    [!] {res['status']} — hentikan, IP ini perlu jeda (coba lagi nanti)")
                    break

                if i + 1 < args.count:
                    time.sleep(args.wait)

    print(f"\n=== ringkasan ({OUTPUT_FILE}) ===")
    for r in results:
        print(f"  {r['status']:10s} {r['email']}  key={'YES' if r['api_key'] else 'no'}")


if __name__ == "__main__":
    main()