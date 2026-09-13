#!/usr/bin/env python3
"""th-flamingo — farm FlamingoProxies accounts under a referral code.

Each account registered through your referral link earns the referrer
1 GB of Flamingo Basic Residentials. Registration traffic goes through
webshare proxies from proxy.txt (rotation, per-proxy caps).

Flow per account (all in ONE browser context so the referral cookie sticks):
1. GET https://auth.flamingoproxies.com/register?ref=CODE (stores attribution)
2. Fill name / email / password / confirm + ToS checkbox, submit.
   reCAPTCHA v3 is invisible — the page's own JS scores the session, no
   manual solving needed (headless works).
3. Verify tab appears -> poll cloudmail inbox for the 6-digit code ->
   fill code -> Verify & Create Account -> dashboard redirect.

Usage:
  python3 th-flamingo.py --count 1                    # single test account
  python3 th-flamingo.py --count 5 --proxy-order random
  python3 th-flamingo.py --count 5 --vnc               # headed (debug)

Files:
  flamingo_accounts.txt  email|password|name|status   (registered|verified|exists|failed)
  flamingo_used.txt      emails already attempted (dedup ledger, never delete)
"""
import os, sys, time, json, random, re, argparse, traceback, secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent

# ── Prefer project venv if it exists (script runs only; never on import:
# a re-exec under importlib would re-run this file as __main__ with the
# importer's argv and take live actions as a side effect of importing) ──
_venv_py = BASE / ".venv" / "bin" / "python"
if __name__ == "__main__" and _venv_py.exists():
    _venv_py = str(_venv_py.resolve())
    if os.path.realpath(sys.executable) != os.path.realpath(_venv_py):
        os.execv(_venv_py, [_venv_py, os.path.abspath(__file__)] + sys.argv[1:])

AUTH_BASE = "https://auth.flamingoproxies.com"
DASH_BASE = "https://dashboard.flamingoproxies.com"
V3_SITEKEY = "6LeQUB8sAAAAAF1dsVInisFV-UO6hZQj6cowDm48"  # recaptcha v3 (invisible)

STEALTH_JS = """() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = window.chrome || {runtime: {}};
  } catch (e) {}
}"""


def _chromium_args(proxy_host=""):
    """ONE Chromium flag list for every browser-launch path in this file.

    Union of the stealth flag (--disable-blink-features=AutomationControlled)
    and the hardening flags th-tui.py sets (ipv6 off, WebRTC leak guards,
    DNS HTTPS-SVCB off). Every launch fallback MUST use this — no inline
    ad-hoc arg lists — so fallbacks can never drift (e.g. silently
    re-enabling automation hints or leaking WebRTC IPs).
    HRR only for IP-literal proxy hosts: with MAP * ~NOTFOUND Chromium
    cannot resolve a proxy HOSTNAME and every tunnel fails."""
    import ipaddress as _ipa
    _ip_literal = False
    if proxy_host:
        try:
            _ipa.ip_address(proxy_host)
            _ip_literal = True
        except Exception:
            pass
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=UseDnsHttpsSvcbAlpn",
            "--disable-ipv6",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-rtc-smoothness-algorithm"]
    if proxy_host and _ip_literal:
        args.append("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + proxy_host)
    return args


def _launch_browser(p, exe, headless, proxy_host=""):
    """Real Google Chrome first (better v3 score than Chromium-for-Testing),
    bundled chromium fallback. Returns browser."""
    base_args = _chromium_args(proxy_host)
    try:
        return p.chromium.launch(channel="chrome", headless=headless, args=base_args)
    except Exception:
        pass
    try:
        import shutil
        real = shutil.which("google-chrome") or "/opt/google/chrome/google-chrome"
        return p.chromium.launch(executable_path=real, headless=headless, args=base_args)
    except Exception:
        pass
    return p.chromium.launch(
        executable_path=exe, headless=headless, args=_chromium_args(proxy_host))
DEFAULT_REF = "FLGCEJ36R52S"

ACCOUNTS_FILE = BASE / "flamingo_accounts.txt"
USED_FILE = BASE / "flamingo_used.txt"

# backend reject phrases that mean "rotate proxy, retry same email"
ROTATE_PHRASES = [
    "too many", "rate limit", "rate-limit", "try again", "blocked",
    "suspicious", "unusual traffic", "access denied",
]
# phrases that mean "this email is dead, move on"
DEAD_PHRASES = [
    "already registered", "already exists", "already in use", "taken",
    "invalid email", "disposable", "not allowed",
]


def log(msg, icon="info"):
    print(f"  - {msg}", flush=True)


def _tui():
    """Load th-tui as a helper module (browser resolver, mail, names)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("thtui", str(BASE / "th-tui.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _proxy_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("thproxy", str(BASE / "th-proxy.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── email pool (cloudmail catch-all, real-name local parts) ──
def _real_name(tui):
    try:
        pfx = tui._real_prefix({}).replace(".", "").replace("_", "").lower()
        pfx = re.sub(r"[^a-z]", "", pfx) or "flamingo"
        if not any(ch.isdigit() for ch in pfx):
            pfx += str(random.randint(11, 999))
        return pfx
    except Exception:
        return "flamingo" + str(random.randint(100, 99999))


def _cloud_domains(tui):
    doms = []
    try:
        for s in (tui.load_cfg().get("mail_servers", []) or []):
            if str(s.get("type", "")).lower() in ("cloudmail", "cloud-mail"):
                for d in (s.get("domains") or ([s.get("domain")] if s.get("domain") else [])):
                    if d and d.strip().lower() not in doms:
                        doms.append(d.strip().lower())
    except Exception as e:
        print(f"[swallow th-flamingo] domains: {e}")
    for d in ["furries.my.id", "konaima.qzz.io", "konaima.tech", "fascir.my.id",
              "arraffi.my.id", "arqonara.web.id", "berapi.eu.cc"]:
        if d not in doms:
            doms.append(d)
    return doms


def load_used():
    used = set()
    for f in (USED_FILE, ACCOUNTS_FILE):
        if f.exists():
            for ln in f.read_text().splitlines():
                e = ln.strip().split("|")[0].lower()
                if "@" in e:
                    used.add(e)
    return used


def ref_code(raw):
    """Accept a bare code or a full affiliate URL -> code."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = re.search(r"[?&]ref=([A-Za-z0-9_-]+)", raw)
    return m.group(1) if m else raw


def unmark_used(email):
    email = email.lower()
    if USED_FILE.exists():
        try:
            lines = [l for l in USED_FILE.read_text().splitlines()
                     if l.strip().lower() != email]
            USED_FILE.write_text("\n".join(lines) + ("\n" if lines else ""))
        except Exception:
            pass


def mark_used(email):
    """Log an email as used/attempted so it's skipped next run (deduped)."""
    email = email.lower()
    if USED_FILE.exists():
        try:
            if email in set(USED_FILE.read_text().splitlines()):
                return
        except Exception:
            pass
    with open(USED_FILE, "a") as f:
        f.write(email + "\n")


def save_account(email, password, name, status):
    lines = ACCOUNTS_FILE.read_text().splitlines() if ACCOUNTS_FILE.exists() else []
    kept, seen = [], False
    for ln in lines:
        if ln.strip().split("|")[0].lower() == email.lower():
            if not seen:
                kept.append(f"{email}|{password}|{name}|{status}")
                seen = True
        elif ln.strip():
            kept.append(ln)
    if not seen:
        kept.append(f"{email}|{password}|{name}|{status}")
    ACCOUNTS_FILE.write_text("\n".join(kept) + "\n")


def gen_password(n=12):
    alpha = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        pw = "".join(secrets.choice(alpha) for _ in range(n))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def gen_name(tui):
    pfx = _real_name(tui)
    base = re.sub(r"\d+$", "", pfx)
    if len(base) >= 6:
        mid = len(base) // 2
        return base[:mid].capitalize() + " " + base[mid:].capitalize()
    return base.capitalize() + " Flamingo"


# ── proxy pool (webshare proxies from proxy.txt) ──
class ProxyPool:
    def __init__(self, path, order="top", max_per_proxy=0):
        self.pm = _proxy_mod()
        self.order = order
        self.max_per_proxy = max_per_proxy
        self.proxies = []
        if Path(path).exists():
            for ln in Path(path).read_text().splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                try:
                    p = self.pm.parse_proxy(ln)
                    # relay:// entries are HTTP-relay shims, not socket proxies —
                    # browsers can't use them (ERR_NO_SUPPORTED_PROXIES)
                    if p and p[0] != "relay":
                        self.proxies.append(p)
                except Exception:
                    pass
        self.use_count = {}
        self.fail_count = {}
        self._idx = 0

    def _key(self, p):
        return f"{p[0]}://{p[1]}:{p[2]}"

    def pick(self):
        cands = [p for p in self.proxies
                 if self.fail_count.get(self._key(p), 0) < 3
                 and (not self.max_per_proxy or self.use_count.get(self._key(p), 0) < self.max_per_proxy)]
        if not cands:
            return None
        if self.order == "random":
            p = random.choice(cands)
        else:
            p = cands[self._idx % len(cands)]
            self._idx += 1
        self.use_count[self._key(p)] = self.use_count.get(self._key(p), 0) + 1
        return p

    def mark_fail(self, p):
        if p:
            self.fail_count[self._key(p)] = self.fail_count.get(self._key(p), 0) + 1


# ── verification code poll (cloudmail) ──
def poll_verify_code(tui, email, timeout=180):
    pat = re.compile(r"(?<!\d)(\d{6})(?!\d)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs = tui.read_cloudmail_inbox(email) or []
        except Exception:
            msgs = []
        for msg in msgs:
            subj = str(msg.get("subject", "") or msg.get("title", ""))
            body = str(msg.get("content", "") or msg.get("text", "") or msg.get("html", ""))
            if "flamingo" not in (subj + body).lower():
                continue
            m = pat.search(subj + "\n" + body)
            if m:
                return m.group(1)
        time.sleep(6)
    return ""


def _solve_v3(api_key, provider, page_url, min_score="0.3", timeout=300):
    """Paid reCAPTCHA v3 token (2captcha/AZCaptcha). Returns token or ''."""
    base = "https://2captcha.com" if provider == "2captcha" else "https://azcaptcha.com"
    import requests
    try:
        r = requests.post(base + "/in.php", data={
            "key": api_key, "method": "userrecaptcha", "version": "v3",
            "action": "submit", "min_score": min_score,
            "googlekey": V3_SITEKEY, "pageurl": page_url, "json": 1}, timeout=30)
        j = r.json()
        if j.get("status") != 1:
            log(f"captcha submit rejected: {str(j.get('request'))[:60]}", "warn")
            return ""
        cid = j["request"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            r2 = requests.get(base + "/res.php", params={
                "key": api_key, "action": "get", "id": cid, "json": 1}, timeout=15)
            j2 = r2.json()
            if j2.get("status") == 1:
                return j2["request"]
            if "CAPCHA_NOT_READY" not in str(j2.get("request", "")):
                log(f"captcha failed: {str(j2.get('request'))[:60]}", "warn")
                return ""
    except Exception as e:
        log(f"captcha solver err: {str(e)[:80]}", "warn")
    return ""


def _gmail_cookies_for(stem_or_email):
    """Load google-session cookies for a gmail (stem or full address)."""
    stem = stem_or_email.split("@")[0].replace(".", "_") + "_gmail_com.json"
    raw = json.loads((Path("/root/projects/gmail-inbox/cookies") / stem).read_text())
    return [{"name": c["name"], "value": c["value"], "domain": c["domain"],
             "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
             "httpOnly": bool(c.get("httpOnly", False)), "sameSite": c.get("sameSite", "Lax")}
            for c in raw if "google.com" in c.get("domain", "") or "youtube.com" in c.get("domain", "")]


def _oauth_click_text(pg, txt):
    for sel in [f"button:has-text('{txt}')", f"a:has-text('{txt}')",
                f"div[role='button']:has-text('{txt}')"]:
        try:
            loc = pg.locator(sel).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=8000)
                return True
        except Exception:
            pass
    return False


def _oauth_click_text(pg, txt):
    for sel in [f"button:has-text('{txt}')", f"a:has-text('{txt}')",
                f"div[role='button']:has-text('{txt}')"]:
        try:
            loc = pg.locator(sel).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=8000)
                return True
        except Exception:
            pass
    return False


def oauth_login_session(tui, gmail, vnc_mode=False):
    """OAuth login, return (stop_fn, pg, ctx) with an authed dashboard session.

    Caller must call stop_fn() (closes browser). Returns (None,None,None) on failure.
    """
    from playwright.sync_api import sync_playwright
    exe = tui.preflight_browser(log_it=False, headless=not vnc_mode)
    if not exe:
        return None, None, None
    try:
        gcookies = _gmail_cookies_for(gmail)
    except Exception:
        return None, None, None
    pw = sync_playwright().start()
    try:
        # DIRECT (no proxy): same unified flags as every other launch path.
        try:
            b = pw.chromium.launch(channel="chrome", headless=not vnc_mode,
                                   args=_chromium_args(""))
        except Exception:
            b = pw.chromium.launch(executable_path=exe, headless=not vnc_mode,
                                   args=_chromium_args(""))
    except Exception:
        pw.stop()
        return None, None, None
    import functools
    def stop():
        try: b.close()
        except Exception: pass
        try: pw.stop()
        except Exception: pass
    try:
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        ctx.add_init_script(STEALTH_JS)
        ctx.add_cookies(gcookies)
        pg = ctx.new_page()
        for i in range(3):
            try:
                pg.goto(f"{AUTH_BASE}/action/auth/google?remember=1",
                        wait_until="commit", timeout=90000)
                break
            except Exception:
                pg.wait_for_timeout(5000)
        for _ in range(30):
            pg.wait_for_timeout(5000)
            url = pg.url or ""
            if "flamingoproxies.com" in url and "accounts.google" not in url \
                    and "auth.flamingoproxies" not in url:
                break
            if "accountchooser" in url:
                try:
                    tile = pg.locator(f'div[data-email="{gmail}"]').first
                    if tile.count():
                        tile.click(timeout=6000)
                        pg.wait_for_timeout(4000)
                        continue
                except Exception:
                    pass
            if "consent" in url or "oauth" in url:
                if _oauth_click_text(pg, "Izinkan") or _oauth_click_text(pg, "Allow") \
                        or _oauth_click_text(pg, "Continue") or _oauth_click_text(pg, "Lanjutkan"):
                    pg.wait_for_timeout(5000)
                    continue
        else:
            stop()
            return None, None, None
        return stop, pg, ctx
    except Exception:
        stop()
        return None, None, None


def goto_retry(pg, url, tries=4, timeout=60000):
    """GET with retries for the flaky egress. Returns True on domcontentloaded."""
    import time as _t
    for i in range(tries):
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except Exception as e:
            log(f"nav {i+1}/{tries} {url[:60]}: {str(e)[:50]}", "warn")
            try:
                pg.wait_for_timeout(5000)
            except Exception:
                _t.sleep(5)
    return False


def verify_flamingo_email(pg, tui, gmail, timeout=240):
    """Verify + earn the newsletter point. Two mail shapes:
    - email/password accounts: 6-digit code -> submit in the verify tab.
    - any account: newsletter 'Confirm Subscription' link with a token ->
      open it in-session (also flips Verified + grants +1pt).
    Returns True when verified."""
    if not goto_retry(pg, f"{DASH_BASE}/settings"):
        return False
    pg.wait_for_timeout(5000)
    clicked = False
    # the pink Verify Email button inside the Email Verification card
    try:
        card = pg.locator("div:has-text('Once verified, you will receive 50 MB')").first
        if card.count():
            btn = card.locator("button:has-text('Verify Email')").first
            if not btn.count():
                btn = pg.locator("button:has-text('Verify Email')").first
        else:
            btn = pg.locator("button:has-text('Verify Email')").first
        if btn.count():
            btn.click(timeout=10000)
            clicked = True
    except Exception:
        pass
    if not clicked:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "tools"))
        import vision_solve as _vs
        try:
            pt = _vs.locate_on_page(pg, "the pink Verify Email button in the Email Verification card")
            if pt is None:
                raise RuntimeError("vision returned no coordinates (strict parse) — refusing (0,0) click")
            x, y = pt
            pg.mouse.click(x, y)
            clicked = True
        except Exception as e:
            log(f"verify button not found: {str(e)[:80]}", "warn")
            return False
    log("verify mail requested — polling inbox (code or newsletter link)...")
    import re as _re
    deadline = time.time() + timeout
    code, nlink = "", ""
    while time.time() < deadline:
        try:
            msgs = tui.read_mailg_inbox(gmail) or []
        except Exception:
            try:
                msgs = tui.read_cloudmail_inbox(gmail) or []
            except Exception:
                msgs = []
        for msg in msgs:
            subj = str(msg.get("subject", "") or "")
            body = str(msg.get("content", "") or msg.get("text", ""))
            if "flamingo" not in (subj + body).lower():
                continue
            if not code:
                m = _re.search(r"(?<!\d)(\d{6})(?!\d)", subj + "\n" + body)
                if m and "newsletter" not in (subj + body).lower():
                    code = m.group(1)
            if not nlink:
                for u in set(_re.findall(r"https?://[^\s\"'<>]+", body)):
                    if "flamingoproxies.com/verify-email?token=" in u:
                        nlink = u.replace("&amp;", "&").split("&")[0]
                        break
            if code or nlink:
                break
        if code or nlink:
            break
        time.sleep(8)
    if nlink:
        # newsletter confirm: verifies email AND grants +1pt
        try:
            pg.goto(nlink, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(6000)
        except Exception as e:
            log(f"confirm link open: {str(e)[:60]}", "warn")
    elif code:
        # 6-digit code submit in the verify tab
        try:
            inp = pg.locator("input[inputmode='numeric'], input[name='code'], input[placeholder*='123456']").first
            if inp.count():
                inp.fill(code, timeout=10000)
                try:
                    pg.locator("button:has-text('Verify')").first.click(timeout=8000)
                except Exception:
                    pass
                pg.wait_for_timeout(6000)
        except Exception as e:
            log(f"code submit: {str(e)[:80]}", "warn")
    else:
        log("no verify mail arrived", "warn")
        return False
    ok = _vconfirm(pg, f"settings showing {gmail} as Verified (no Not Verified badge)")
    return ok


def _totp_for_gmail(gmail):
    """TOTP code from .2fa-secrets (email|base32), computed locally (RFC 6238)."""
    try:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("thredo", str(BASE / "th_redo.py"))
        if spec and spec.loader:
            r = _ilu.module_from_spec(spec)
            spec.loader.exec_module(r)
            if hasattr(r, "_totp_for"):
                return r._totp_for(gmail) or ""
    except Exception:
        pass
    return ""


def _gmail_password(gmail):
    """Gmail password from inbox ledgers (loggedmail.txt pipe, accounts.txt tab).
    Runtime use only — callers must NEVER log it."""
    g = gmail.strip().lower()
    for path, sep in (("/root/projects/gmail-inbox/loggedmail.txt", "|"),
                      ("/root/projects/gmail-inbox/accounts.txt", "\t")):
        try:
            with open(path) as f:
                for ln in f:
                    p = ln.rstrip("\n").split(sep)
                    if len(p) >= 2 and p[0].strip().lower() == g and p[1].strip():
                        return p[1].strip()
        except Exception:
            pass
    return ""


def _fl_click_text(pg, *texts):
    for txt in texts:
        for sel in [f"button:has-text('{txt}')", f"a:has-text('{txt}')",
                    f"div[role='button']:has-text('{txt}')"]:
            try:
                loc = pg.locator(sel).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.click(timeout=8000)
                    return txt
            except Exception:
                pass
    return ""


def _click_recaptcha_checkbox(pg, timeout=8000):
    """Frame-aware reCAPTCHA checkbox click. Returns True when clicked.

    The v2 checkbox lives inside the cross-origin recaptcha anchor iframe,
    so a top-document querySelector can never see it (always-false). Find
    the anchor frame first and act inside it; keep a top-document attempt
    only as a last resort for same-origin embeds."""
    try:
        frames = list(pg.frames or [])
    except Exception:
        frames = []
    anchors = [fr for fr in frames
               if "recaptcha/api2/anchor" in ((getattr(fr, "url", "") or ""))]
    recaps = [fr for fr in frames
              if "recaptcha" in ((getattr(fr, "url", "") or "")) and fr not in anchors]
    for fr in anchors + recaps:
        for sel in (".recaptcha-checkbox-border", "#recaptcha-anchor",
                    '[role="checkbox"]', ".recaptcha-checkbox"):
            try:
                loc = fr.locator(sel).first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.click(timeout=timeout)
                    return True
            except Exception:
                pass
        try:
            hit = fr.evaluate("(() => { const cb=document.querySelector("
                              "'.recaptcha-checkbox-border,#recaptcha-anchor,"
                              "[role=checkbox],.recaptcha-checkbox');"
                              " if(cb){cb.click();return true;} return false; })()")
            if hit:
                return True
        except Exception:
            pass
    # last resort: same-origin embed visible from the top document
    for sel in (".recaptcha-checkbox-border", "#recaptcha-anchor",
                '[role="checkbox"]'):
        try:
            loc = pg.locator(sel).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=timeout)
                return True
        except Exception:
            pass
    try:
        return bool(pg.evaluate("(() => { const cb=document.querySelector("
                                "'.recaptcha-checkbox-border,#recaptcha-anchor,"
                                "[role=checkbox]');"
                                " if(cb){cb.click();return true;} return false; })()"))
    except Exception:
        return False


def flamingo_challenge_loop(pg, gmail, max_s=300):
    """run-batch.mjs-style Google challenge handler. Returns True on dashboard.

    - phone tap: relays the tap-code LOUD (user taps on their phone, VNC live)
    - TOTP: auto-fills from .2fa-secrets (local RFC 6238, no network)
    - reCAPTCHA checkbox: auto-click, else human in VNC
    - wizards (recovery/selfie/home): auto-skip
    - unknown screens: human-assist wait, capped (no infinite loop)
    """
    import time as _t
    t0 = _t.time()
    finding_logged = code_logged = False
    acted_on, acted_at = None, 0
    human_waits = 0
    last_unknown, last_unknown_at = "", 0

    def acted(url):
        nonlocal acted_on, acted_at
        if acted_on == url and _t.time() - acted_at < 25:
            return True
        acted_on, acted_at = url, _t.time()
        pg.wait_for_timeout(2000)
        return False

    def body():
        try:
            return (pg.inner_text("body", timeout=5000) or "")[:4000]
        except Exception:
            return ""

    while _t.time() - t0 < max_s:
        pg.wait_for_timeout(2500)
        try:
            url = pg.url or ""
        except Exception:
            continue
        # OUTCOME: flamingo dashboard (not google/auth)
        if "flamingoproxies.com" in url and "accounts.google" not in url \
                and "auth.flamingoproxies" not in url:
            return True
        T = body()
        # account chooser (first-time oauth for this browser profile)
        if "accountchooser" in url or re.search(r"Choose an account|Pilih akun", T):
            try:
                tile = pg.locator(f'div[data-email="{gmail}"]').first
                if tile.count():
                    tile.click(timeout=6000)
                    log("chooser clicked")
                    pg.wait_for_timeout(4000)
                    continue
            except Exception:
                pass
            try:
                t2 = pg.get_by_text(gmail, exact=False).first
                if t2.count():
                    t2.click(timeout=6000)
                    log("chooser clicked (text)")
                    pg.wait_for_timeout(4000)
                    continue
            except Exception:
                pass
        # oauth consent (signin/oauth/id consent variants included)
        if "consent" in url or "oauth" in url or re.search(
                r"ingin mengakses|wants to access|mengizinkan|login ke ", T, re.I):
            if _oauth_click_text(pg, "Izinkan") or _oauth_click_text(pg, "Allow") \
                    or _oauth_click_text(pg, "Continue") or _oauth_click_text(pg, "Lanjutkan"):
                log("consent allowed")
                pg.wait_for_timeout(5000)
                continue
        # phone tap ("Verify it's you") — relay EVERY new code LOUD, user taps
        if re.search(r"Verify it'?s you|Check your", T):
            if not finding_logged:
                log("challenge: Verify-it's-you — watch VNC, tap on phone", "warn")
                finding_logged = True
            m = re.search(r"(?:tap|click|pilih|ketuk)[^0-9]{0,30}(\d{1,3})\b", T, re.I)
            if m and m.group(1) != code_logged:
                code_logged = m.group(1)
                log(f">>> TAP {m.group(1)} ON YOUR PHONE NOW <<<", "warn")
            try:
                pg.screenshot(path=str(BASE / "debug_shots" / f"flamingo_{gmail.split('@')[0]}_tap.png"))
            except Exception:
                pass
            continue
        # 2SV chooser -> Google Authenticator
        if "/challenge/selection" in url and re.search(
                r"Google Authenticator|verification code from the Google Authenticator", T):
            if acted(url):
                continue
            log("2SV chooser -> Google Authenticator app")
            try:
                tgt = pg.evaluate("""(() => {
                    const opts=[...document.querySelectorAll('li, [role="option"]')]
                      .filter(x=>/Google Authenticator|verification code from the Google Authenticator/i
                        .test((x.innerText||'').trim()) && x.offsetParent!==null
                        && (x.innerText||'').trim().length < 120);
                    if(!opts.length) return null;
                    const a=opts[0].querySelector('a,[role="link"],[jsaction],button') || opts[0];
                    const b=a.getBoundingClientRect(); return {x:b.x+b.width/2, y:b.y+b.height/2};
                })()""")
                if tgt and tgt.get("x") is not None:
                    pg.mouse.click(tgt["x"], tgt["y"])
                    pg.wait_for_timeout(2500)
                    continue
            except Exception:
                pass
            continue
        # TOTP / one-time-code entry
        if re.search(r"Enter the code|Enter code|one-time-code|verification code|"
                     r"Enter security code|Get a code to sign in|g\.co/sc", T):
            if re.search(r"Get a code to sign in|g\.co/sc", T):
                log("g.co/sc screen -> switching to authenticator method")
                _fl_click_text(pg, "Try another way")
                pg.wait_for_timeout(2500)
                continue
            code = _totp_for_gmail(gmail)
            if code:
                try:
                    inp = pg.locator("input[type='tel'], input[autocomplete='one-time-code'], "
                                     "input[name*='code']").first
                    if inp.count():
                        inp.fill(code)
                        pg.wait_for_timeout(300)
                        _fl_click_text(pg, "Next", "Verify", "Continue")
                        log(f"TOTP auto-filled for {gmail}")
                        pg.wait_for_timeout(2000)
                        continue
                except Exception:
                    pass
            else:
                log("code screen but no TOTP secret — solve in VNC (or add secret to .2fa-secrets)", "warn")
                pg.wait_for_timeout(5000)
                continue
        # recovery phone/email prompt -> cancel
        if re.search(r"Enter phone|Add a recovery phone|recovery email|"
                     r"Make sure you can always sign in", T):
            log("recovery prompt -> Cancel")
            if acted(url):
                continue
            _fl_click_text(pg, "Cancel", "Not now", "not now")
            continue
        # selfie -> skip
        if re.search(r"Selfie", T):
            log("selfie screen -> skip (manual in VNC if needed)")
            _fl_click_text(pg, "not now", "Not now", "Skip", "Done", "No thanks")
            pg.wait_for_timeout(3000)
            continue
        # onboarding wizards -> skip
        if re.search(r"recovery|protect your account|google one|set up|profile|"
                     r"personalize|Save your password|Welcome", T) and re.search(
                     r"Skip|Done|Not now|Later|No thanks", T):
            if acted(url):
                continue
            log("onboarding wizard -> skip")
            _fl_click_text(pg, "Skip", "Not now", "Done", "No thanks")
            pg.wait_for_timeout(1500)
            continue
        # reCAPTCHA checkbox -> auto-click inside its anchor iframe, else human
        if re.search(r"reCAPTCHA|I'?m not a robot|Verify you are human|not a robot", T, re.I):
            log("reCAPTCHA -> auto-click checkbox (VNC backup)")
            try:
                if _click_recaptcha_checkbox(pg):
                    pg.wait_for_timeout(5000)
                    continue
            except Exception:
                pass
            pg.wait_for_timeout(4000)
            continue
        # password field (stale cookies) — full login fallback via ledger password
        try:
            pwf = pg.locator("input[type='password']").first
            has_pw = pwf.count() and pwf.is_visible(timeout=2000)
        except Exception:
            has_pw = False
        if has_pw:
            pw = _gmail_password(gmail)
            if pw:
                log("stale cookies — password fallback login")
                try:
                    pwf.fill(pw, timeout=10000)
                    pg.wait_for_timeout(500)
                    _fl_click_text(pg, "Next", "Berikutnya", "Continue")
                    pg.wait_for_timeout(3000)
                    continue
                except Exception:
                    pass
            else:
                log("password required but no ledger password — solve in VNC", "warn")
                pg.wait_for_timeout(30000)
                continue
        # wrong creds -> fail fast
        if re.search(r"password was incorrect|couldn'?t sign you in|"
                     r"couldn'?t find your google account|Wrong password", T):
            log("wrong password / bad creds -> fail", "warn")
            return False
        # blocked -> human wait in VNC
        if re.search(r"This browser or app may not be secure|Sign in blocked|"
                     r"Access blocked|Account disabled", T):
            log("security flag — check VNC (30s)", "warn")
            pg.wait_for_timeout(30000)
            continue
        # unknown screen: human-assist, capped at 3
        uk = (url or "")[:120]
        if uk != last_unknown:
            log(f"challenge: unknown screen, waiting in VNC ({uk[:60]})...", "warn")
            last_unknown, last_unknown_at = uk, _t.time()
        elif _t.time() - last_unknown_at > 60:
            human_waits += 1
            if human_waits >= 3:
                log("unknown screen persists after 3 waits — giving up", "warn")
                try:
                    pg.screenshot(path=str(BASE / "debug_shots" / f"flamingo_{gmail.split('@')[0]}_stuck.png"))
                except Exception:
                    pass
                return False
            log(f"unknown screen {human_waits}/3 — solve in VNC, continuing watch...", "warn")
            last_unknown_at = _t.time()
    log("challenge loop timed out", "warn")
    return False


def create_oauth(tui, gmail, ref, vnc_mode=False):
    """Register via Google OAuth using a gmail session. Returns (flamingo_email, status).

    DIRECT connection only (proxies can't reach accounts.google.com).
    Attribution via affiliate_ref cookie, asserted before starting.
    """
    from playwright.sync_api import sync_playwright
    exe = tui.preflight_browser(log_it=False, headless=not vnc_mode)
    if not exe:
        return gmail, "failed"
    try:
        gcookies = _gmail_cookies_for(gmail)
    except Exception as e:
        log(f"oauth cookies for {gmail} unavailable: {str(e)[:60]}", "warn")
        return gmail, "failed"
    b = None
    try:
        with sync_playwright() as p:
            # DIRECT (no proxy): same unified flags as every other launch path.
            try:
                b = p.chromium.launch(channel="chrome", headless=not vnc_mode,
                                      args=_chromium_args(""))
            except Exception:
                b = p.chromium.launch(executable_path=exe, headless=not vnc_mode,
                                      args=_chromium_args(""))
            ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
            ctx.add_init_script(STEALTH_JS)
            ctx.add_cookies(gcookies)
            pg = ctx.new_page()
            code = ref_code(ref)
            if code:
                for i in range(3):
                    try:
                        pg.goto(f"{DASH_BASE}/affiliate-link?ref={code}",
                                wait_until="domcontentloaded", timeout=60000)
                        break
                    except Exception:
                        pg.wait_for_timeout(5000)
                pg.wait_for_timeout(3000)
                if not any(c["name"] == "affiliate_ref" for c in ctx.cookies()):
                    log("NO affiliate_ref cookie — refusing unattributed oauth", "warn")
                    return gmail, "noattr"
                log(f"referral attached: {code}", "ok")
            for i in range(3):
                try:
                    pg.goto(f"{AUTH_BASE}/action/auth/google?remember=1",
                            wait_until="commit", timeout=90000)
                    break
                except Exception as e:
                    log(f"oauth nav {i+1}/3: {str(e)[:50]}", "warn")
                    pg.wait_for_timeout(5000)
            # full run-batch-style challenge loop until dashboard
            # (chooser/consent/phone-tap/TOTP/recaptcha/wizards/VNC human-wait)
            if not flamingo_challenge_loop(pg, gmail, max_s=300):
                log("oauth challenge unresolved — skip account", "warn")
                return gmail, "failed"
            pg.wait_for_timeout(5000)
            ok = _vconfirm(pg, f"Flamingo dashboard logged in via Google as {gmail}")
            return gmail, ("verified" if ok else "failed")
    except Exception as e:
        log(f"oauth {gmail}: {str(e)[:100]}", "warn")
        return gmail, "failed"
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass


def create_one(tui, email, name, password, proxy_parsed, ref, vnc_mode=False,
               captcha_key="", captcha_provider="2captcha", gmail_cookies=None):  # noqa: C901
    """Register one Flamingo account. Returns status string."""
    from playwright.sync_api import sync_playwright
    exe = tui.preflight_browser(log_it=False, headless=not vnc_mode)
    if not exe:
        log("no usable browser executable", "warn")
        return "failed"
    pm = _proxy_mod()
    b = None
    try:
        with sync_playwright() as p:
            b = _launch_browser(p, exe, headless=not vnc_mode,
                                proxy_host=(proxy_parsed[1] if proxy_parsed and proxy_parsed[1] else ""))
            ctx_kwargs = {"viewport": {"width": 1280, "height": 800},
                          "locale": "en-US", "timezone_id": "Asia/Jakarta",
                          "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/126.0.0.0 Safari/537.36")}
            if proxy_parsed:
                ctx_kwargs["proxy"] = pm.proxy_to_playwright(proxy_parsed)
            ctx = b.new_context(**ctx_kwargs)
            ctx.add_init_script(STEALTH_JS)
            if gmail_cookies:
                try:
                    ctx.add_cookies(gmail_cookies)
                    log("injected google session cookies (v3 score boost)")
                    # act like the session owner: a logged-in Google visit with
                    # real dwell/read behavior before touching the target site.
                    try:
                        gpg = ctx.new_page()
                        gpg.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=30000)
                        time.sleep(4)
                        gpg.mouse.move(random.randint(200, 900), random.randint(200, 500))
                        time.sleep(3)
                        gpg.mouse.wheel(0, 300)
                        time.sleep(4)
                        gpg.close()
                    except Exception:
                        pass
                except Exception as e:
                    log(f"cookie inject failed: {str(e)[:60]}", "warn")
            pg = ctx.new_page()
            # ATTRIBUTION FIRST: the affiliate link sets the affiliate_ref cookie
            # (Domain=.flamingoproxies.com, shared with auth). ?ref= on the
            # register page does NOTHING — registering without this cookie earns
            # zero referral credit. Abort BEFORE burning the email if missing.
            code = ref_code(ref)
            if code:
                try:
                    pg.goto(f"{DASH_BASE}/affiliate-link?ref={code}",
                            wait_until="domcontentloaded", timeout=45000)
                    time.sleep(3)
                except Exception as e:
                    log(f"affiliate link visit failed: {str(e)[:60]}", "warn")
                attached = any(c["name"] == "affiliate_ref" and code in (c["value"] or "")
                               for c in ctx.cookies())
                if not attached:
                    log("NO affiliate_ref cookie — refusing to register unattributed", "warn")
                    return "noattr"
                log(f"referral attached: {code}", "ok")
            reg_url = f"{AUTH_BASE}/register"
            # warm-up: v3 scores session behavior — dwell on the main site with
            # human-like mouse/scroll before touching the register form.
            try:
                pg.goto("https://flamingoproxies.com/", wait_until="domcontentloaded", timeout=45000)
                for _ in range(4):
                    pg.mouse.move(random.randint(100, 1100), random.randint(100, 700))
                    time.sleep(2)
                    pg.mouse.wheel(0, random.randint(200, 600))
                    time.sleep(3)
                pg.goto(reg_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
            except Exception as e:
                log(f"warm-up skipped: {str(e)[:60]}", "warn")
                try:
                    pg.goto(reg_url, wait_until="domcontentloaded", timeout=45000)
                    time.sleep(3)
                except Exception:
                    pass
            try:
                pg.wait_for_selector("#register-email", state="visible", timeout=30000)
            except Exception:
                log("register form never loaded — rotating proxy", "warn")
                return "blocked"
            time.sleep(2)
            pg.fill("#register-name", name)
            pg.fill("#register-email", email)
            pg.fill("#register-password", password)
            pg.fill("#confirm-password", password)
            # ToS checkbox enables the Register button
            try:
                pg.locator("#tos-agree").first.check(timeout=8000)
            except Exception:
                try:
                    pg.evaluate("() => document.getElementById('tos-agree').click()")
                except Exception as e:
                    log(f"tos checkbox failed: {str(e)[:60]}", "warn")
                    return "failed"
            time.sleep(1)
            pg.locator("#register-button").first.click(timeout=10000)
            log("Register submitted — waiting for verify tab / redirect...")
            outcome, waited = "", 0
            tried_vision = 0
            while waited < 60:
                time.sleep(3)
                waited += 3
                try:
                    url = pg.url or ""
                    if "dashboard" in url:
                        outcome = "redirect"
                        break
                    cls = pg.locator("#verify-signup-form").get_attribute("class") or ""
                    if "active" in cls:
                        outcome = "verify-tab"
                        break
                    err = (pg.locator("#register-error").inner_text(timeout=2000) or "").strip().lower()
                    if err:
                        outcome = "error:" + err[:200]
                        break
                    # visible bot challenge (checkbox/slider) -> vision-coordinate solve
                    if tried_vision < 2:
                        try:
                            import sys as _sys
                            _sys.path.insert(0, str(BASE / "tools"))
                            import vision_solve as _vs
                            kind = _vs.visible_challenge_kind(pg)
                            if kind == "slider":
                                log("visible slider challenge — vision drag...")
                                _vs.solve_slider(pg)
                                tried_vision += 1
                            elif kind in ("checkbox", "recaptcha"):
                                log(f"visible {kind} challenge — vision click...")
                                _vs.click_target(pg, "the checkbox (small square box, do NOT click text)")
                                tried_vision += 1
                        except Exception as e:
                            log(f"vision solve err: {str(e)[:80]}", "warn")
                            tried_vision += 1
                except Exception:
                    pass
            if outcome == "redirect":
                log("registered + auto-logged in (dashboard redirect)", "ok")
                return "verified"
            if outcome.startswith("error:"):
                msg = outcome[6:]
                log(f"register rejected: {msg[:120]}", "warn")
                if "recaptcha" in msg and captcha_key:
                    # retry the SAME session with a paid v3 token (no re-fill needed)
                    log("solving paid v3 + resubmitting in-session...")
                    tok = _solve_v3(captcha_key, captcha_provider, reg_url)
                    if tok:
                        try:
                            res = pg.evaluate("""async ([url, payload]) => {
                                const r = await fetch(url, {method: 'POST',
                                    headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify(payload)});
                                return await r.json();
                            }""", [f"{AUTH_BASE}/action/register", {
                                "name": name, "email": email, "password": password,
                                "confirm_password": password, "newsletter": False,
                                "tos": True, "recaptcha_token": tok}])
                            log(f"resubmit: {str(res)[:150]}")
                            if isinstance(res, dict) and res.get("ok"):
                                outcome = ("verify-tab" if res.get("requires_verification")
                                           else "redirect" if res.get("redirect") else "registered-ok")
                            else:
                                msg = str((res or {}).get("message", "")) if isinstance(res, dict) else ""
                                log(f"resubmit rejected: {msg[:120]}", "warn")
                        except Exception as e:
                            log(f"resubmit err: {str(e)[:80]}", "warn")
                if outcome in ("redirect", "registered-ok"):
                    pass  # fall through to verify/redirect handling below
                elif outcome == "verify-tab":
                    pass
                else:
                    if "recaptcha" in msg:
                        # v3 score reject: NO account was created, the email is
                        # still free on Flamingo's side — keep it retryable.
                        log("v3 score reject — email kept fresh for a later run", "warn")
                        return "score"
                    if any(p in msg for p in DEAD_PHRASES):
                        return "exists"
                    if any(p in msg for p in ROTATE_PHRASES):
                        return "blocked"
                    return "failed"
            if outcome == "redirect":
                log("registered + auto-logged in (dashboard redirect)", "ok")
                return "verified"
            if outcome == "registered-ok":
                return "registered"
            if outcome != "verify-tab":
                log("no verify tab and no redirect after 60s", "warn")
                return "failed"
            # 2. verification code (with one in-session resend if mail is slow)
            log("verify tab shown — polling inbox for 6-digit code...")
            code = poll_verify_code(tui, email)
            if not code:
                log("no code yet — resending once in-session...")
                try:
                    res = pg.evaluate("""async ([em]) => {
                        const tok = await grecaptcha.execute(
                            '6LeQUB8sAAAAAF1dsVInisFV-UO6hZQj6cowDm48', {action: 'submit'});
                        const csrf = document.querySelector("input[name='csrf_token']").value;
                        const r = await fetch('/action/resend-signup-code', {method: 'POST',
                            headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
                            body: JSON.stringify({email: em, recaptcha_token: tok})});
                        return await r.json();
                    }""", [email])
                    log(f"resend: {str(res)[:120]}")
                    code = poll_verify_code(tui, email, timeout=180)
                except Exception as e:
                    log(f"resend err: {str(e)[:80]}", "warn")
            if not code:
                log("no verification code within 6min", "warn")
                return "registered"
            pg.fill("#verify-signup-code", code)
            pg.locator("#verifySignupForm button[type='submit']").first.click(timeout=10000)
            time.sleep(5)
            for _ in range(10):
                try:
                    if "dashboard" in (pg.url or ""):
                        log("email VERIFIED (dashboard reached)", "ok")
                        return "verified"
                    err = (pg.locator("#verify-signup-error").inner_text(timeout=2000) or "").strip()
                    if err:
                        log(f"verify rejected: {err[:120]}", "warn")
                        return "registered"
                except Exception:
                    pass
                time.sleep(3)
            log("verify submit ambiguous — checking dashboard...", "warn")
            try:
                pg.goto("https://dashboard.flamingoproxies.com/", wait_until="domcontentloaded", timeout=30000)
                time.sleep(4)
                if "dashboard" in (pg.url or "") and "login" not in (pg.url or "").lower():
                    return "verified"
            except Exception:
                pass
            return "registered"
    except Exception as e:
        log(f"create {email}: {str(e)[:100]}", "warn")
        return "failed"
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass


def _vconfirm(pg, expectation):
    """Vision gate on the live page. Returns True/False (logs reason)."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "tools"))
        import vision_solve as _vs
        ok, why = _vs.confirm_page(pg, expectation)
        log(f"vision: {'YES' if ok else 'NO'} - {why}", "ok" if ok else "warn")
        return ok
    except Exception as e:
        log(f"vision gate err: {str(e)[:80]}", "warn")
        return False


def flamingo_login(pg, email, password):
    """Login via the auth page tabs. Returns True when dashboard reached."""
    ok = False
    for i in range(3):
        try:
            pg.goto(f"{AUTH_BASE}/register", wait_until="domcontentloaded", timeout=60000)
            ok = True
            break
        except Exception as e:
            log(f"login nav {i+1}/3: {str(e)[:50]}", "warn")
            pg.wait_for_timeout(5000)
    if not ok:
        return False
    pg.wait_for_timeout(5000)
    try:
        pg.wait_for_function("() => typeof toggleTab === 'function'", timeout=20000)
        pg.evaluate("() => toggleTab('login')")
    except Exception:
        pg.locator(".tab:has-text('Login')").first.click(timeout=15000)
    pg.wait_for_selector("#login-email", state="visible", timeout=20000)
    pg.fill("#login-email", email)
    pg.fill("#login-password", password)
    try:
        pg.locator("#loginForm button[type='submit']").first.click(timeout=8000)
    except Exception:
        pass  # click raced a navigation — that's success, not failure
    pg.wait_for_timeout(8000)
    if "dashboard" not in (pg.url or ""):
        return False
    return _vconfirm(pg, f"Flamingo dashboard logged in as {email}, sidebar and account area visible")


def plan_active(pg, plan_name="Standard"):
    """True if the named plan has data balance (e.g. '0.00 GB / 0.05 GB').

    NOTE: the card can still say 'Not active — click to buy' while holding
    redeemed data — that label tracks subscription state, not balance.
    What matters for the generator is the data allotment."""
    try:
        goto_retry(pg, f"{DASH_BASE}/?tab=residential")
        pg.wait_for_timeout(5000)
        body = pg.inner_text("body", timeout=8000)
        idx = body.find(plan_name)
        if idx < 0:
            return False
        region = body[idx:idx + 600]
        m = re.search(r"(\d+\.\d+)\s*GB\s*/\s*(\d+\.\d+)\s*GB", region)
        if m and float(m.group(2)) > 0:
            log(f"plan {plan_name}: {m.group(1)}/{m.group(2)} GB available")
            return True
        return False
    except Exception:
        return False
        region = body[idx:idx + 600]
        return "not active" not in region.lower()
    except Exception:
        return False


def affiliate_points(pg):
    """Return (available_points:int, has_active_plan:bool) from affiliate page."""
    goto_retry(pg, f"{DASH_BASE}/affiliate")
    pg.wait_for_timeout(5000)
    pts, active = 0, False
    try:
        html = pg.content()
        m = (re.search(r'points-available[^>]*>\s*(\d+)', html)
             or re.search(r'legend-available[^>]*>\s*(\d+)', html))
        if m:
            pts = int(m.group(1))
        else:
            body = pg.inner_text("body", timeout=8000)
            m2 = re.search(r"Points to spend\s*(\d+)", body)
            if m2:
                pts = int(m2.group(1))
    except Exception:
        pass
    try:
        body = pg.inner_text("body", timeout=8000)
        active = bool(re.search(r"GB left|active plan|Active Residential Plans\s*[1-9]", body, re.I))
    except Exception:
        pass
    return pts, active


def redeem_50mb(pg):
    """Click Redeem on the 50MB Standard card (+ confirm modal). Vision-gated."""
    try:
        card = pg.locator("div:has-text('50MB Standard')").first
        btn = pg.locator("button:has-text('Redeem')")
        # pick the Redeem button nearest the 50MB card
        target = None
        for i in range(btn.count()):
            try:
                box = btn.nth(i).bounding_box(timeout=2000)
                cb = card.bounding_box(timeout=2000)
                if box and cb and abs(box["y"] - cb["y"]) < 400:
                    target = btn.nth(i)
                    break
            except Exception:
                pass
        target = target or btn.first
        target.click(timeout=10000)
        pg.wait_for_timeout(4000)
        # confirm modal (Confirm/Yes/Redeem) if one appeared
        for txt in ("Confirm", "Yes, redeem", "Redeem now", "Confirm redeem"):
            try:
                loc = pg.locator(f"button:has-text('{txt}')").first
                if loc.count() and loc.is_visible(timeout=2000):
                    loc.click(timeout=8000)
                    pg.wait_for_timeout(4000)
                    break
            except Exception:
                pass
        return _vconfirm(pg, "points shop showing the 50MB plan redeemed or a success confirmation")
    except Exception as e:
        log(f"redeem err: {str(e)[:80]}", "warn")
        return False


GEN_COUNTRIES = ["Indonesia", "Malaysia", "Singapore", "India"]


def _vision_locate_click(pg, target, timeout=120):
    """Last-resort click by vision coordinates. Returns True on click."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(BASE / "tools"))
        import vision_solve as _vs
        pt = _vs.locate_on_page(pg, target, timeout=timeout)
        if pt is None:
            log(f"vision-click '{target}': no coordinates (strict parse) — skipping click", "warn")
            return False
        x, y = pt
        pg.mouse.click(x, y)
        log(f"vision-clicked '{target}' at ({x},{y})")
        return True
    except Exception as e:
        log(f"vision-click '{target}' failed: {str(e)[:80]}", "warn")
        return False


def claim_50mb(pg):
    """Click Redeem on the 50MB Standard Points-Shop card (+ confirm modal).

    NOTE: #redeem-key-btn is the *gift code* redeem — never click that.
    Vision-gated on success."""
    goto_retry(pg, f"{DASH_BASE}/affiliate")
    pg.wait_for_timeout(5000)
    try:
        target = None
        for b in pg.locator("button.redeem-btn").all():
            try:
                card = b.evaluate("""(el) => {
                    let n = el, depth = 0, txt = '';
                    while (n && depth < 6) { n = n.parentElement; depth++;
                        if (n && /50MB|1GB/i.test(n.innerText || '')) { txt = n.innerText; break; } }
                    return txt.slice(0, 200);
                }""")
                if "50MB" in card:
                    target = b
                    break
            except Exception:
                pass
        (target or pg.locator("button.redeem-btn").nth(1)).click(timeout=10000)
        pg.wait_for_timeout(4000)
    except Exception as e:
        log(f"50MB redeem click: {str(e)[:80]}", "warn")
    # confirm modal (Confirm/Yes) if one appeared
    for txt in ("Confirm", "Yes", "Claim now", "Redeem now", "Confirm redeem"):
        try:
            loc = pg.locator(f"button:has-text('{txt}')").first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=8000)
                pg.wait_for_timeout(4000)
                break
        except Exception:
            pass
    return _vconfirm(pg, "points shop showing the 50MB plan redeemed or a success confirmation")


def _combo_controls(pg, scope):
    """Map type-and-select dropdowns: [{idx, label}] for text inputs, combobox
    roles and contenteditables, labelled by nearest label text. DOM order."""
    try:
        return pg.evaluate("""(root) => {
            const scopeEl = root || document;
            const ctrls = [...scopeEl.querySelectorAll(
                'input[role="combobox"], input[aria-expanded], [role="combobox"], ' +
                '[contenteditable="true"], input[type="text"]:not([name="csrf_token"])')];
            return ctrls.filter(el => {
                const r = el.getBoundingClientRect(); return r.width > 40 && r.height > 10;
            }).slice(0, 12).map((el, idx) => {
                let label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
                if (!label) {
                    let n = el, depth = 0;
                    while (n && depth < 5) {
                        n = n.parentElement; depth++;
                        const lab = n ? n.querySelector('label') : null;
                        if (lab && lab.innerText.trim()) { label = lab.innerText.trim(); break; }
                    }
                }
                if (!label && el.id) {
                    const lab = document.querySelector(`label[for="${el.id}"]`);
                    if (lab) label = lab.innerText.trim();
                }
                return {idx, label: (label || '').slice(0, 60)};
            });
        }""", scope)
    except Exception:
        return []


def ensure_generator_visible(pg, tries=3):
    """Open the Residential Proxy Generator (product page). Returns True."""
    for _ in range(tries):
        try:
            body = (pg.inner_text("body", timeout=8000) or "")
            if "Residential Proxy Generator" in body:
                return True
            loc = pg.locator("text=Standard Residential").first
            if loc.count():
                loc.click(timeout=10000)
                pg.wait_for_timeout(6000)
        except Exception:
            pass
    try:
        body = (pg.inner_text("body", timeout=8000) or "")
        return "Residential Proxy Generator" in body
    except Exception:
        return False


def _labeled_control(pg, scope, label):
    """Find the form control under an exact label text. Returns dict
    {kind: 'select'|'box', index} or None. scope limits the search."""
    try:
        return pg.evaluate("""([label]) => {
            const labs = [...document.querySelectorAll('label, div, span, p')]
                .filter(e => (e.innerText || '').trim() === label
                    && e.children.length === 0);
            for (const lab of labs) {
                let root = lab.parentElement;
                for (let d = 0; d < 4 && root; d++) {
                    const sel = root.querySelector('select');
                    if (sel) {
                        const all = [...document.querySelectorAll('select')];
                        return {kind: 'select', index: all.indexOf(sel)};
                    }
                    const box = [...root.querySelectorAll('div')]
                        .find(e => /^(Random|.+)$/.test((e.innerText || '').trim())
                            && e.getBoundingClientRect().height > 20
                            && e.getBoundingClientRect().height < 70
                            && e.children.length <= 2);
                    if (box) {
                        const all = [...document.querySelectorAll('div')]
                            .filter(e => { const r = e.getBoundingClientRect();
                                return r.height > 20 && r.height < 70; });
                        return {kind: 'box', index: all.indexOf(box)};
                    }
                    root = root.parentElement;
                }
            }
            return null;
        }""", [label])
    except Exception:
        return None


def _pick_from_control(pg, scope, label, want_text=None):
    """Select want_text (or random option) in the control under label.
    Returns (value, text)."""
    info = _labeled_control(pg, scope, label)
    if not info:
        return None, ""
    if info["kind"] == "select":
        try:
            sel = pg.locator("select").nth(info["index"])
            opts = sel.locator("option")
            texts = [opts.nth(j).inner_text(timeout=1500).strip() for j in range(opts.count())]
            if want_text:
                hit = next((t for t in texts if want_text.lower() in t.lower()), "")
                if not hit:
                    return None, ""
            else:
                pool = [t for t in texts if t and "select" not in t.lower()
                        and "choose" not in t.lower() and "random" not in t.lower()]
                if not pool:
                    return None, ""
                hit = random.choice(pool)
            val = sel.locator("option", has_text=hit).first.get_attribute("value")
            sel.select_option(value=val)
            pg.wait_for_timeout(2500)
            return val, hit
        except Exception:
            return None, ""
    # custom div dropdown: click box, then the option
    try:
        boxes = pg.locator("div").all()
        box = boxes[info["index"]]
        box.click(timeout=6000)
        pg.wait_for_timeout(1500)
        opts = pg.locator("[role='option'], ul li, .dropdown-item, .option")
        texts = []
        for j in range(min(opts.count(), 80)):
            try:
                t = opts.nth(j).inner_text(timeout=1000).strip()
                if t:
                    texts.append((j, t))
            except Exception:
                pass
        if want_text:
            hit = next(((j, t) for j, t in texts if want_text.lower() in t.lower()), None)
        else:
            pool = [(j, t) for j, t in texts if "select" not in t.lower()
                    and "choose" not in t.lower() and "random" not in t.lower()]
            hit = random.choice(pool) if pool else None
        if hit is None:
            try:
                pg.keyboard.press("Escape")
            except Exception:
                pass
            return None, ""
        j, t = hit
        try:
            opts.nth(j).click(timeout=6000)
        except Exception:
            pg.evaluate("""(t) => { const items=[...document.querySelectorAll(
                '[role="option"], ul li, .dropdown-item, .option')];
                const el=items.find(x=>(x.innerText||'').includes(t)); if(el) el.click(); }""", t[:30])
        pg.wait_for_timeout(2000)
        return t, t
    except Exception:
        return None, ""


def _pick_from_control(pg, scope, label, want_text=None):
    """Select want_text (or random) in the control under exact `label`.
    Handles native <select> and custom click-to-open div dropdowns.
    Returns (value, text)."""
    # 1. native select under the label
    try:
        info = pg.evaluate("""([label]) => {
            const labs = [...document.querySelectorAll('label')]
                .filter(e => (e.innerText || '').trim() === label);
            for (const lab of labs) {
                let root = lab.parentElement;
                for (let d = 0; d < 4 && root; d++) {
                    const sel = root.querySelector('select');
                    if (sel) {
                        const all = [...document.querySelectorAll('select')];
                        return {kind: 'select', index: all.indexOf(sel)};
                    }
                    root = root.parentElement;
                }
            }
            return null;
        }""", [label])
    except Exception:
        info = None
    if info and info.get("kind") == "select" and info.get("index", -1) >= 0:
        try:
            sel = pg.locator("select").nth(info["index"])
            opts = sel.locator("option")
            texts = [opts.nth(j).inner_text(timeout=1500).strip() for j in range(opts.count())]
            if want_text:
                hit = next((t for t in texts if want_text.lower() in t.lower()), "")
                if not hit:
                    return None, ""
            else:
                pool = [t for t in texts if t and "select" not in t.lower()
                        and "choose" not in t.lower() and "random" not in t.lower()]
                if not pool:
                    return None, ""
                hit = random.choice(pool)
            val = sel.locator("option", has_text=hit).first.get_attribute("value")
            sel.select_option(value=val)
            pg.wait_for_timeout(2500)
            return val, hit
        except Exception:
            pass
    # 2. custom div dropdown: click the value box under the label, then option
    try:
        box_idx = pg.evaluate("""([label]) => {
            const labs = [...document.querySelectorAll('label, div, span, p')]
                .filter(e => (e.innerText || '').trim() === label && e.children.length === 0);
            for (const lab of labs) {
                let root = lab.parentElement;
                for (let d = 0; d < 5 && root; d++) {
                    const boxes = [...root.querySelectorAll('div')].filter(e => {
                        const r = e.getBoundingClientRect();
                        const t = (e.innerText || '').trim();
                        return r.height > 20 && r.height < 70 && t.length > 0 && t.length < 60;
                    });
                    if (boxes.length) {
                        const all = [...document.querySelectorAll('div')];
                        return all.indexOf(boxes[0]);
                    }
                    root = root.parentElement;
                }
            }
            return -1;
        }""", [label])
        if box_idx is None or box_idx < 0:
            return None, ""
        pg.locator("div").nth(box_idx).click(timeout=6000)
        pg.wait_for_timeout(1500)
        opts = pg.locator("[role='option'], ul li, .dropdown-item, .option")
        texts = []
        for j in range(min(opts.count(), 100)):
            try:
                t = opts.nth(j).inner_text(timeout=1000).strip()
                if t:
                    texts.append((j, t))
            except Exception:
                pass
        if want_text:
            hit = next(((j, t) for j, t in texts if want_text.lower() in t.lower()), None)
        else:
            pool = [(j, t) for j, t in texts if "select" not in t.lower()
                    and "choose" not in t.lower() and "random" not in t.lower()]
            hit = random.choice(pool) if pool else None
        if hit is None:
            try:
                pg.keyboard.press("Escape")
            except Exception:
                pass
            return None, ""
        j, t = hit
        try:
            opts.nth(j).click(timeout=6000)
        except Exception:
            pg.evaluate("""(t) => { const items=[...document.querySelectorAll(
                '[role="option"], ul li, .dropdown-item, .option')];
                const el=items.find(x=>(x.innerText||'').includes(t)); if(el) el.click(); }""", t[:30])
        pg.wait_for_timeout(2000)
        return t, t
    except Exception:
        return None, ""


def _dropdown_pick(pg, scope, label_re, want_text=None):
    """Pick an option from a labelled dropdown (native <select> OR custom
    click-to-open div). want_text=None picks a random non-placeholder option.
    Returns (value, text)."""
    # 1. native select first
    try:
        sels = scope.locator("select")
        for i in range(sels.count()):
            try:
                lab = ""
                try:
                    lab = sels.nth(i).evaluate(
                        """(el) => { const l = el.closest('div')?.parentElement?.querySelector('label')?.innerText
                            || el.getAttribute('aria-label') || el.getAttribute('name') || ''; return l; }""")
                except Exception:
                    pass
                opts = sels.nth(i).locator("option")
                texts = [opts.nth(j).inner_text(timeout=1500).strip() for j in range(opts.count())]
                if label_re and not re.search(label_re, lab or " ".join(texts[:3]), re.I):
                    continue
                pool = [(opts.nth(j).get_attribute("value"), t) for j, t in enumerate(texts)
                        if t and "select" not in t.lower() and "choose" not in t.lower() and "random" not in t.lower()]
                if want_text:
                    hit = next(((v, t) for v, t in pool if want_text.lower() in t.lower()), None)
                    if hit:
                        sels.nth(i).select_option(value=hit[0])
                        return hit
                elif pool:
                    v, t = random.choice(pool)
                    sels.nth(i).select_option(value=v)
                    return v, t
            except Exception:
                pass
    except Exception:
        pass
    # 2. custom div dropdown: click the box showing current value, then the option
    try:
        boxes = scope.locator("div:has-text('Random')")
        for i in range(min(boxes.count(), 8)):
            try:
                box = boxes.nth(i)
                # nearest label above the box
                lab = box.evaluate("""(el) => { let n = el.parentElement;
                    for (let d = 0; d < 4 && n; d++, n = n.parentElement) {
                        const m = (n.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                        if (m.length) return m[0];
                    } return ''; }""")
                if label_re and not re.search(label_re, lab, re.I):
                    continue
                box.click(timeout=6000)
                pg.wait_for_timeout(1500)
                opts = pg.locator("[role='option'], ul li, .dropdown-item, .option")
                texts = []
                for j in range(min(opts.count(), 60)):
                    try:
                        t = opts.nth(j).inner_text(timeout=1000).strip()
                        if t:
                            texts.append((j, t))
                    except Exception:
                        pass
                if want_text:
                    hit = next(((j, t) for j, t in texts if want_text.lower() in t.lower()), None)
                else:
                    pool = [(j, t) for j, t in texts
                            if "select" not in t.lower() and "choose" not in t.lower() and "random" not in t.lower()]
                    hit = random.choice(pool) if pool else None
                if hit is None:
                    try:
                        pg.keyboard.press("Escape")
                    except Exception:
                        pass
                    continue
                j, t = hit
                try:
                    opts.nth(j).click(timeout=6000)
                except Exception:
                    pg.evaluate("""(t) => { const items=[...document.querySelectorAll(
                        '[role="option"], ul li, .dropdown-item, .option')];
                        const el=items.find(x=>(x.innerText||'').includes(t)); if(el) el.click(); }""", t[:30])
                pg.wait_for_timeout(2000)
                return t, t  # custom dropdowns: display text doubles as value
            except Exception:
                pass
    except Exception:
        pass
    return None, ""


def _combo_select(pg, scope, label_re, want_text=None):
    """Type-and-select in a combobox labelled like label_re. want_text=None
    picks a random non-placeholder option. Returns (value, text)."""
    ctrls = _combo_controls(pg, scope)
    target = next((c for c in ctrls if re.search(label_re, c.get("label", ""), re.I)), None)
    if not target:
        return None, ""
    # resolve the element handle again inside the page for interaction
    info = pg.evaluate("""([scopeSel, idx, want]) => {
        const scopeEl = document;
        const ctrls = [...scopeEl.querySelectorAll(
            'input[role="combobox"], input[aria-expanded], [role="combobox"], ' +
            '[contenteditable="true"], input[type="text"]:not([name="csrf_token"])')]
            .filter(el => { const r = el.getBoundingClientRect(); return r.width > 40 && r.height > 10; });
        const el = ctrls[idx];
        if (!el) return {ok: false};
        el.scrollIntoView({block: 'center'});
        el.click();
        el.focus();
        return {ok: true};
    }""", [None, target["idx"], want_text or ""])
    if not info or not info.get("ok"):
        return None, ""
    pg.wait_for_timeout(1200)
    if want_text:
        try:
            pg.keyboard.type(want_text, delay=60)
            pg.wait_for_timeout(2000)
        except Exception:
            pass
    opts = pg.evaluate("""(() => {
        const items = [...document.querySelectorAll(
            '[role="option"], [role="listbox"] [role="option"], ul li, .dropdown-item, .option')]
            .filter(el => { const r = el.getBoundingClientRect();
                return r.width > 20 && r.height > 8 && (el.innerText || '').trim(); });
        return items.slice(0, 60).map(el => ({
            text: (el.innerText || '').trim().slice(0, 60),
            value: el.getAttribute('data-value') || el.getAttribute('value') || '' }));
    })()""")
    if not opts:
        return None, ""
    if want_text:
        pick = next((o for o in opts if want_text.lower() in o["text"].lower()), None)
    else:
        pool = [o for o in opts if "select" not in o["text"].lower() and "choose" not in o["text"].lower()]
        pick = random.choice(pool or opts)
    if not pick:
        return None, ""
    try:
        pg.locator(f"[role='option']:has-text('{pick['text'][:30]}')").first.click(timeout=6000)
    except Exception:
        try:
            pg.evaluate("""(t) => {
                const items = [...document.querySelectorAll('[role="option"], ul li, .dropdown-item, .option')];
                const el = items.find(x => (x.innerText || '').includes(t));
                if (el) el.click();
            }""", pick["text"][:30])
        except Exception:
            return None, ""
    pg.wait_for_timeout(2000)
    return pick["value"] or pick["text"], pick["text"]


def _select_option_by_names(pg, scope, names):
    """Pick first matching visible option text in a <select>. Returns (value, text)."""
    sels = scope.locator("select")
    for i in range(sels.count()):
        try:
            opts = sels.nth(i).locator("option")
            texts = [opts.nth(j).inner_text(timeout=1500).strip() for j in range(opts.count())]
            for want in names:
                for t in texts:
                    if want.lower() in t.lower() or t.lower() in want.lower():
                        val = opts.filter(has_text=t).first.get_attribute("value")
                        sels.nth(i).select_option(value=val)
                        return val, t
        except Exception:
            pass
    return None, ""


def _js_select(pg, css, value, timeout=10000):
    """Set a (possibly hidden/custom-overlay) <select> by value via JS +
    change event. Returns the selected option text."""
    return pg.evaluate("""([css, val]) => {
        const sel = document.querySelector(css);
        if (!sel) return '';
        sel.value = val;
        sel.dispatchEvent(new Event('input', {bubbles: true}));
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        const opt = sel.querySelector(`option[value="${val}"]`);
        return opt ? opt.textContent.trim() : '';
    }""", [css, value])


def configure_generator(pg, qty=5, sticky_min=2, sticky_max=5, countries=None):
    """Drive the Residential Proxy Generator form. Returns dict used for API call.

    Plan=Standard, Sticky + duration (Rotating->Sticky fallback), Country from
    list w/ random State/City, Qty. Vision-gated at the end.
    """
    countries = countries or GEN_COUNTRIES
    goto_retry(pg, f"{DASH_BASE}/?tab=residential")
    pg.wait_for_timeout(5000)
    if not ensure_generator_visible(pg):
        log("generator form never appeared", "warn")
        return {"country": "_country-id", "state": "random", "city": "random",
                "sticky": random.randint(sticky_min, sticky_max), "confirmed": False}
    try:
        pg.wait_for_selector("select#generate-country-select, select[name='generate-country-select']", state="attached", timeout=30000)
        pg.wait_for_timeout(2000)
    except Exception:
        pass
    _vconfirm(pg, "Residential Proxy Generator form visible with plan, proxy type, country and quantity controls")
    gen = pg.locator("text=/Residential Proxy Generator/i").first
    scope = pg.locator("body")
    try:
        # scope to the generator container when identifiable
        cont = pg.locator("div:has-text('Residential Proxy Generator')").last
        if cont.count():
            scope = cont
    except Exception:
        pass
    # 1. Plan = Standard
    try:
        _select_option_by_names(scope, ["Standard Residential", "Standard Resident", "Standard"])
    except Exception:
        pass
    # 2. Proxy type Sticky (+ duration); fallback Rotating->Sticky
    sticky_val = random.randint(sticky_min, sticky_max)
    try:
        for mode in ("Sticky",):
            for sel in (f"input[value='{mode}' i]", f"button:has-text('{mode}')",
                        f"label:has-text('{mode}')"):
                try:
                    loc = scope.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=2000):
                        loc.click(timeout=8000)
                        pg.wait_for_timeout(2000)
                        break
                except Exception:
                    pass
        dur = scope.locator("input[type='number']").first
        dur_set = False
        if dur.count():
            try:
                dur.fill(str(sticky_val), timeout=8000)
                dur_set = True
            except Exception:
                pass
        if not dur_set:
            # toggle Rotating -> back to Sticky, duration input often appears then
            for mode in ("Rotating", "Sticky"):
                try:
                    loc = scope.locator(f"label:has-text('{mode}'), button:has-text('{mode}')").first
                    if loc.count() and loc.is_visible(timeout=2000):
                        loc.click(timeout=8000)
                        pg.wait_for_timeout(2000)
                except Exception:
                    pass
            try:
                dur = scope.locator("input[type='number']").first
                if dur.count():
                    dur.fill(str(sticky_val), timeout=8000)
            except Exception:
                pass
    except Exception as e:
        log(f"sticky config: {str(e)[:60]}", "warn")
    # 3. Country (+ value id) with random City.
    # Real controls (stable names): generate-country-select,
    # generate-city-select (ONE combined State/City dropdown).
    country_val, country_name, state_val, city_val = "_country-id", "any", "random", "random"
    country_css = ("select#generate-country-select, select[name='generate-country-select']")
    city_css = ("select#generate-city-select, select[name='generate-city-select']")
    def _opt_list(css):
        rows = pg.evaluate("""(css) => {
            const sel = document.querySelector(css);
            if (!sel) return [];
            return [...sel.options].map(o => [(o.textContent || '').trim(), o.value]);
        }""", [css]) or []
        return [(str(v), str(t)) for t, v in rows]
    try:
        texts = _opt_list(country_css)
        log(f"country options: {[t for _, t in texts][:30]}")
        hit = ""
        for want in countries:
            hit = next(((v, t) for v, t in texts if want.lower() in t.lower()), ("", ""))
            if hit[1]:
                break
        if hit[1]:
            v, t = hit
            shown = _js_select(pg, country_css, v)
            pg.wait_for_timeout(3000)  # city options reload
            country_val, country_name = v, shown or t
            log(f"country: {country_name} (val={str(v)[:40]})")
        else:
            log("wanted countries not listed — keeping default country")
    except Exception as e:
        log(f"country select: {str(e)[:60]}", "warn")
    try:
        texts = _opt_list(city_css)
        pool = [(v, t) for v, t in texts
                if t and v and "select" not in t.lower() and "choose" not in t.lower()
                and "random" not in t.lower() and "all" not in t.lower()]
        if pool:
            v, t = random.choice(pool)
            _js_select(pg, city_css, v)
            city_val = v
            log(f"city: {t}")
            pg.wait_for_timeout(1500)
    except Exception as e:
        log(f"city select: {str(e)[:60]}", "warn")
    # 4. Qty
    try:
        qty_in = scope.locator("input[name*='qty' i], input[name*='quantity' i], input[name*='amount' i]").first
        if not qty_in.count():
            # numeric input nearest the Generate button
            qty_in = scope.locator("input[type='number']").last
        if qty_in.count():
            qty_in.fill(str(qty), timeout=8000)
    except Exception as e:
        log(f"qty config: {str(e)[:60]}", "warn")
    ok = _vconfirm(pg, f"generator form set: Standard plan, Sticky {sticky_val}min, {country_name}, qty {qty}")
    return {"country": country_val, "state": state_val, "city": city_val,
            "sticky": sticky_val, "confirmed": ok}


def generate_proxies_api(pg, plan=2, qty=5, sticky=10, country="_country-id",
                         state="random", city="random"):
    """Call the dashboard generate-proxies API in-page (session cookies apply).
    Returns list of dicts {host,port,user,pw}."""
    res = pg.evaluate("""async ([payload]) => {
        const r = await fetch('/action/generate-proxies', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify(payload)});
        return await r.json();
    }""", [{"country": country, "state": state, "city": city,
             "proxy_plan": plan, "proxy_amount": qty, "proxy_type": "sticky",
             "format": "user:pass@ip:port", "ttl": sticky}])
    out = []
    if isinstance(res, dict) and res.get("success") and res.get("proxies"):
        for ln in str(res["proxies"]).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(":")
            if len(parts) >= 4:
                out.append({"host": parts[0], "port": parts[1],
                            "user": parts[2], "pw": ":".join(parts[3:])})
    return out


def save_generated(proxies, path=None):
    """Append generated proxies to proxy list as http://user:pass@host:port. Returns added."""
    path = Path(path or BASE / "proxy.txt")
    existing = set()
    if path.exists():
        for ln in path.read_text().splitlines():
            if ln.strip():
                existing.add(ln.strip())
    added = 0
    with open(path, "a") as f:
        for p in proxies:
            if not (p.get("host") and p.get("port") and p.get("user") and p.get("pw")):
                continue
            line = f"http://{p['user']}:{p['pw']}@{p['host']}:{p['port']}"
            if line not in existing:
                f.write(line + "\n")
                existing.add(line)
                added += 1
    return added


def _gen_logged_in(pg, pm, email, qty, sticky, country, plan):
    """Affiliate claim -> configure generator -> generate -> save. pg is authed."""
    pts, _ = affiliate_points(pg)
    log(f"{email}: points={pts}")
    if pts >= 1:
        log(f"{email}: claiming free 50MB...")
        claim_50mb(pg)
    active = plan_active(pg)
    log(f"{email}: active_plan={active}")
    if not active:
        log(f"{email}: no active plan — cannot generate yet", "warn")
        return 0
    # configure the visible generator form (discovers country/state/city ids)
    cfg = configure_generator(pg, qty=qty, sticky_min=2,
                              sticky_max=max(2, min(5, sticky)), countries=GEN_COUNTRIES)
    got = generate_proxies_api(pg, plan=plan, qty=qty, sticky=cfg.get("sticky", sticky),
                               country=cfg.get("country", country),
                               state=cfg.get("state", "random"), city=cfg.get("city", "random"))
    log(f"{email}: generated {len(got)} proxies")
    if not got:
        return 0
    added = save_generated(got)
    # functional proof: live-check a sample (stronger than any screenshot)
    try:
        checked = 0
        for g in got[:2]:
            r = pm.check_proxy(("http", g["host"], int(g["port"]), g["user"], g["pw"]), timeout=10)
            if r:
                checked += 1
        log(f"{email}: live-checked {checked}/{min(2, len(got))} sample OK")
    except Exception as e:
        log(f"live-check err: {str(e)[:60]}", "warn")
    return added


def gen_account(tui, email, password, proxy_parsed, qty, sticky, country, plan, vnc_mode):
    """Login -> redeem if needed -> generate qty proxies -> save. Returns count saved."""
    from playwright.sync_api import sync_playwright
    # OAuth-created accounts have no password: login via stored gmail session
    if password.startswith("oauth:"):
        gmail = email if "@" in email else password.split("oauth:", 1)[1]
        stop, pg, ctx = oauth_login_session(tui, gmail, vnc_mode)
        if not pg:
            log(f"{email}: oauth login failed", "warn")
            return 0
        try:
            pm = _proxy_mod()
            return _gen_logged_in(pg, pm, email, qty, sticky, country, plan)
        except Exception as e:
            log(f"gen {email}: {str(e)[:100]}", "warn")
            return 0
        finally:
            stop()
    exe = tui.preflight_browser(log_it=False, headless=not vnc_mode)
    if not exe:
        return 0
    pm = _proxy_mod()
    b = None
    try:
        with sync_playwright() as p:
            launch_args = _chromium_args(proxy_parsed[1] if proxy_parsed and proxy_parsed[1] else "")
            try:
                b = p.chromium.launch(channel="chrome", headless=not vnc_mode, args=launch_args)
            except Exception:
                b = p.chromium.launch(executable_path=exe, headless=not vnc_mode, args=launch_args)
            ctx_kwargs = {"viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
            if proxy_parsed:
                ctx_kwargs["proxy"] = pm.proxy_to_playwright(proxy_parsed)
            ctx = b.new_context(**ctx_kwargs)
            ctx.add_init_script(STEALTH_JS)
            pg = ctx.new_page()
            if not flamingo_login(pg, email, password):
                log(f"{email}: login failed", "warn")
                return 0
            return _gen_logged_in(pg, pm, email, qty, sticky, country, plan)
    except Exception as e:
        log(f"gen {email}: {str(e)[:100]}", "warn")
        return 0
    finally:
        try:
            if b is not None:
                b.close()
        except Exception:
            pass


def gen_main(args, tui):
    """Gen mode: for each farm account -> login, redeem, generate, save."""
    pool = ProxyPool(args.proxy, order=args.proxy_order, max_per_proxy=0)
    accts = []
    if ACCOUNTS_FILE.exists():
        for ln in ACCOUNTS_FILE.read_text().splitlines():
            p = ln.strip().split("|")
            if len(p) >= 4 and p[3] in ("verified", "registered", "registered-nomail", "score-retry"):
                accts.append((p[0], p[1]))
    if args.gen_accounts.strip():
        want = {e.strip().lower() for e in args.gen_accounts.split(",")}
        accts = [(e, pw) for e, pw in accts if e.lower() in want]
    if not accts:
        print("No farm accounts eligible for gen mode")
        return 1
    print(f"  TH-FLAMINGO-GEN: {len(accts)} accounts x {args.gen_qty} proxies "
          f"(sticky {args.sticky}min, plan {args.gen_plan})")
    if args.watch > 0:
        # watch mode: repeat rounds until every account yielded proxies
        pending = list(accts)
        rnd = 0
        grand = 0
        while pending and (args.rounds <= 0 or rnd < args.rounds):
            rnd += 1
            log(f"watch round {rnd}: {len(pending)} pending, checking points...")
            still = []
            for (email, pw) in pending:
                proxy = None if args.no_proxy else pool.pick()
                try:
                    n = gen_account(tui, email, pw, proxy, args.gen_qty, args.sticky,
                                    args.country, args.gen_plan, args.vnc)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    log(f"{email} gen err: {str(e)[:80]}", "warn")
                    n = 0
                if n > 0:
                    grand += n
                    log(f"{email}: +{n} proxies DONE")
                else:
                    still.append((email, pw))
            pending = still
            if pending:
                log(f"round {rnd}: {len(pending)} still waiting on points — sleeping {args.watch}min")
                try:
                    time.sleep(args.watch * 60)
                except KeyboardInterrupt:
                    raise
        print(f"\n  WATCH DONE: +{grand} proxies -> proxy.txt, {len(pending)} still pending")
        return 0
    total = 0
    for i, (email, pw) in enumerate(accts, 1):
        proxy = None if args.no_proxy else pool.pick()
        log(f"[{i}/{len(accts)}] {email} via " + ("DIRECT" if args.no_proxy else f"{proxy[1]}:{proxy[2]}"))
        try:
            n = gen_account(tui, email, pw, proxy, args.gen_qty, args.sticky,
                            args.country, args.gen_plan, args.vnc)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"{email} gen err: {str(e)[:80]}", "warn")
            n = 0
        total += n
        log(f"[{i}/{len(accts)}] {email}: +{n} proxies")
        if i < len(accts) and args.delay > 0:
            time.sleep(args.delay)
    print(f"\n  GEN DONE: +{total} proxies -> proxy.txt")
    return 0


def oauth_main(args, tui):
    """OAuth farm: one Flamingo account per gmail session (DIRECT only)."""
    flag = args.oauth_gmail.strip().lower()
    if flag == "auto":
        cdir = Path("/root/projects/gmail-inbox/cookies")
        gmails = sorted(p.stem[:-len("_gmail_com")] for p in cdir.glob("*_gmail_com.json")) if cdir.exists() else []
        # stem mangling is lossy (dots->underscores); resolve via DB below
        gmails = _resolve_gmails(gmails)
    else:
        gmails = [e.strip() for e in args.oauth_gmail.split(",") if "@" in e]
    gmails = gmails[:max(1, args.count)]
    print(f"  TH-FLAMINGO-OAUTH: {len(gmails)} gmails (direct, ref={args.ref})")
    used = load_used()
    ok = fail = 0
    # terminal states are never retried; everything else (failed/noattr/
    # score-retry/registered*) is fair game for another attempt
    terminal = set()
    if ACCOUNTS_FILE.exists():
        for ln in ACCOUNTS_FILE.read_text().splitlines():
            p = ln.strip().split("|")
            if len(p) >= 4 and p[3] in ("verified", "exists"):
                terminal.add(p[0].lower())
    for i, gmail in enumerate(gmails, 1):
        if gmail.lower() in terminal:
            log(f"[{i}/{len(gmails)}] {gmail} already done — skip")
            continue
        log(f"[{i}/{len(gmails)}] OAuth {gmail} ...")
        try:
            _, st = create_oauth(tui, gmail, args.ref, vnc_mode=args.vnc)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"{gmail} oauth err: {str(e)[:80]}", "warn")
            st = "failed"
        mark_used(gmail)
        save_account(gmail, "oauth:" + gmail.split("@")[0], gmail.split("@")[0], st)
        if st == "verified":
            ok += 1
        else:
            fail += 1
        if i < len(gmails) and args.delay > 0:
            time.sleep(args.delay)
    print(f"\n  OAUTH DONE: {ok} ok / {fail} fail — accounts in {ACCOUNTS_FILE}")
    return 0 if fail == 0 else 1


def verify_main(args, tui):
    """Verify email on existing OAuth farm accounts (→ +50MB bonus)."""
    gmails = [e.strip() for e in args.verify_email.split(",") if "@" in e]
    ok = fail = 0
    for i, gmail in enumerate(gmails, 1):
        log(f"[{i}/{len(gmails)}] verify {gmail} ...")
        try:
            stop, pg, ctx = oauth_login_session(tui, gmail, vnc_mode=args.vnc)
            if not pg:
                log(f"{gmail}: oauth login failed", "warn")
                fail += 1
                continue
            try:
                if verify_flamingo_email(pg, tui, gmail):
                    ok += 1
                    log(f"{gmail}: VERIFIED", "ok")
                else:
                    fail += 1
            finally:
                stop()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"{gmail} verify err: {str(e)[:80]}", "warn")
            fail += 1
        if i < len(gmails) and args.delay > 0:
            time.sleep(args.delay)
    print(f"\n  VERIFY DONE: {ok} ok / {fail} fail")
    return 0 if fail == 0 else 1


def _resolve_gmails(stems):
    """Map cookie-file stems back to real gmail addresses via inbox DB."""
    try:
        import sqlite3
        db = sqlite3.connect("/root/projects/gmail-inbox/inbox.db")
        rows = db.execute("SELECT email FROM accounts ORDER BY email").fetchall()
        db.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser(description="Farm FlamingoProxies referral accounts via webshare proxies")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--proxy", default=str(BASE / "proxy.txt"))
    ap.add_argument("--proxy-order", choices=["top", "random"], default="top")
    ap.add_argument("--max-per-proxy", type=int, default=2)
    ap.add_argument("--delay", type=int, default=20)
    ap.add_argument("--password", default="")
    ap.add_argument("--captcha-key", default="")
    ap.add_argument("--captcha-provider", default="2captcha")
    ap.add_argument("--gmail-cookie", default="",
                    help="gmail address for google session cookies (v3 boost), or 'auto' to rotate")
    ap.add_argument("--oauth-gmail", default="",
                    help="register via Google OAuth with gmail session(s): one address, 'auto', or comma list (DIRECT, skips email/password+v3)")
    ap.add_argument("--verify-email", default="",
                    help="verify email on existing OAuth farm account(s): gmail address or comma list")
    ap.add_argument("--no-proxy", action="store_true",
                    help="direct connection (exposes host IP — use for score tests)")
    ap.add_argument("--gen-only", action="store_true",
                    help="skip signup: login farm accounts, redeem, generate proxies -> proxy.txt")
    ap.add_argument("--gen-accounts", default="",
                    help="comma-separated farm emails for gen mode (default: all verified/registered)")
    ap.add_argument("--gen-qty", type=int, default=5, help="proxies per account (default 5)")
    ap.add_argument("--sticky", type=int, default=5, help="sticky minutes 2-5 (default 5)")
    ap.add_argument("--country", default="_country-id", help="country id or _country-id for any")
    ap.add_argument("--gen-plan", type=int, default=2, help="proxy_plan id (default 2=Standard)")
    ap.add_argument("--watch", type=int, default=0,
                    help="gen-only: re-check points every N minutes until all accounts generated (0=off)")
    ap.add_argument("--rounds", type=int, default=0, help="max watch rounds (0=unlimited)")
    ap.add_argument("--vnc", action="store_true")
    args = ap.parse_args()

    tui = _tui()
    if not tui.load_env():
        print("Cannot start: live credentials (.env) missing.")
        return 1
    tui.load_cfg()
    if not tui.preflight_browser(log_it=True, headless=not args.vnc):
        return 1

    if args.gen_only:
        return gen_main(args, tui)

    if args.oauth_gmail.strip():
        return oauth_main(args, tui)

    if args.verify_email.strip():
        return verify_main(args, tui)

    pool = ProxyPool(args.proxy, order=args.proxy_order, max_per_proxy=args.max_per_proxy)
    if not args.no_proxy and not pool.proxies:
        print(f"No proxies in {args.proxy}")
        return 1
    print(f"  TH-FLAMINGO: {args.count} accounts, ref={args.ref}, "
          + ("DIRECT (no proxy)" if args.no_proxy else f"proxies={len(pool.proxies)} ({args.proxy_order})"))

    used = load_used()
    doms = _cloud_domains(tui)
    gcookies = []
    cookie_pool = []
    if args.gmail_cookie.lower() == "auto":
        cdir = Path("/root/projects/gmail-inbox/cookies")
        if cdir.exists():
            cookie_pool = sorted(cdir.glob("*_gmail_com.json"))
            log(f"cookie rotation pool: {len(cookie_pool)} gmail sessions")
    elif args.gmail_cookie:
        try:
            cf = (Path("/root/projects/gmail-inbox/cookies")
                  / (args.gmail_cookie.split("@")[0].replace(".", "_") + "_gmail_com.json"))
            raw = json.loads(cf.read_text())
            gcookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                         "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
                         "httpOnly": bool(c.get("httpOnly", False)),
                         "sameSite": c.get("sameSite", "Lax")}
                        for c in raw if "google.com" in c.get("domain", "")]
            log(f"loaded {len(gcookies)} google cookies for {args.gmail_cookie}")
        except Exception as e:
            log(f"gmail cookies unavailable: {str(e)[:60]}", "warn")
    ok = fail = 0
    hard_ips = []
    for i in range(1, args.count + 1):
        # fresh cloudmail address
        email, name, password = "", "", args.password or gen_password()
        for _ in range(100):
            e = f"{_real_name(tui)}@{random.choice(doms)}".lower()
            if e not in used:
                email = e
                break
        if not email:
            log("email pool exhausted", "warn")
            break
        name = gen_name(tui)
        try:
            tui.create_cloudmail_inbox(email)
        except Exception:
            pass
        if cookie_pool:
            # rotate a fresh google session per account (spreads v3 risk)
            cf = cookie_pool[(i - 1) % len(cookie_pool)]
            try:
                raw = json.loads(cf.read_text())
                gcookies = [{"name": c["name"], "value": c["value"], "domain": c["domain"],
                             "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
                             "httpOnly": bool(c.get("httpOnly", False)),
                             "sameSite": c.get("sameSite", "Lax")}
                            for c in raw if "google.com" in c.get("domain", "")]
                log(f"rotated cookies: {cf.stem} ({len(gcookies)})")
            except Exception as e:
                log(f"cookie load {cf.name} failed: {str(e)[:50]}", "warn")
                gcookies = []
        proxy = None if args.no_proxy else pool.pick()
        if not proxy and not args.no_proxy:
            log("proxy pool exhausted (all capped/failed) — stopping", "warn")
            break
        log(f"[{i}/{args.count}] Registering {email} via "
            + ("DIRECT" if args.no_proxy else f"{proxy[1]}:{proxy[2]}") + " ...")
        st = create_one(tui, email, name, password, proxy, args.ref, vnc_mode=args.vnc,
                        captcha_key=args.captcha_key, captcha_provider=args.captcha_provider,
                        gmail_cookies=gcookies or None)
        if st == "noattr":
            unmark_used(email)
            log(f"[{i}/{args.count}] {email} skipped (no attribution — email NOT burned)", "warn")
            fail += 1
            continue
        if st == "score":
            unmark_used(email)
            save_account(email, password, name, "score-retry")
            log(f"[{i}/{args.count}] {email} v3-rejected, kept fresh for retry", "warn")
            fail += 1
            continue
        mark_used(email)
        save_account(email, password, name, st)
        if st == "verified":
            ok += 1
            log(f"[{i}/{args.count}] DONE {email} verified", "ok")
        elif st == "registered":
            ok += 1
            log(f"[{i}/{args.count}] DONE {email} registered (unverified — recheck mail)", "ok")
        elif st == "exists":
            log(f"[{i}/{args.count}] {email} already exists — next", "warn")
            fail += 1
        elif st == "blocked":
            pool.mark_fail(proxy)
            hip = proxy[1]
            if hip not in hard_ips:
                hard_ips.append(hip)
            fail += 1
            if len(hard_ips) >= 3:
                log(f"backend blocking across {len(hard_ips)} IPs — stopping batch", "warn")
                break
        else:
            pool.mark_fail(proxy)
            fail += 1
        if i < args.count and args.delay > 0:
            log(f"waiting {args.delay}s...")
            time.sleep(args.delay)
    print(f"\n  FLAMINGO DONE: {ok} ok / {fail} fail / {args.count} — accounts in {ACCOUNTS_FILE}")
    print("  NOTE: confirm the +1 GB credit on the referrer dashboard.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(1)
