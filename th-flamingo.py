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

# ── Prefer project venv if it exists ──
_venv_py = BASE / ".venv" / "bin" / "python"
if _venv_py.exists():
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


def _launch_browser(p, exe, headless, proxy_host=""):
    """Real Google Chrome first (better v3 score than Chromium-for-Testing),
    bundled chromium fallback. Returns browser."""
    base_args = ["--no-sandbox", "--disable-dev-shm-usage",
                 "--disable-blink-features=AutomationControlled"]
    if proxy_host:
        base_args.append("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + proxy_host)
    else:
        base_args.append("--disable-ipv6")
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
        executable_path=exe, headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
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


def affiliate_points(pg):
    """Return (available_points:int, has_active_plan:bool) from affiliate page."""
    pg.goto(f"{DASH_BASE}/affiliate", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    body = pg.inner_text("body", timeout=8000)
    pts, active = 0, False
    m = re.search(r"Points to spend\s*(\d+)", body)
    if m:
        pts = int(m.group(1))
    active = bool(re.search(r"GB left|active plan|Active Residential Plans\s*[1-9]", body, re.I))
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
        x, y = _vs.locate_on_page(pg, target, timeout=timeout)
        pg.mouse.click(x, y)
        log(f"vision-clicked '{target}' at ({x},{y})")
        return True
    except Exception as e:
        log(f"vision-click '{target}' failed: {str(e)[:80]}", "warn")
        return False


def claim_50mb(pg):
    """Affiliate page: Claim/Redeem the free 50MB (button text varies). Vision-gated."""
    pg.goto(f"{DASH_BASE}/affiliate", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    for txt in ("Claim", "Redeem"):
        try:
            btns = pg.locator(f"button:has-text('{txt}')")
            if btns.count():
                btns.first.click(timeout=10000)
                pg.wait_for_timeout(4000)
                break
        except Exception:
            pass
    # confirm modal (Confirm/Yes/Claim now) if one appeared
    for txt in ("Confirm", "Yes", "Claim now", "Redeem now", "Confirm redeem"):
        try:
            loc = pg.locator(f"button:has-text('{txt}')").first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=8000)
                pg.wait_for_timeout(4000)
                break
        except Exception:
            pass
    return _vconfirm(pg, "affiliate page showing the free 50MB claimed/redeemed or a success confirmation")


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


def configure_generator(pg, qty=5, sticky_min=2, sticky_max=5, countries=None):
    """Drive the Residential Proxy Generator form. Returns dict used for API call.

    Plan=Standard, Sticky + duration (Rotating->Sticky fallback), Country from
    list w/ random State/City, Qty. Vision-gated at the end.
    """
    countries = countries or GEN_COUNTRIES
    pg.goto(f"{DASH_BASE}/?tab=residential", wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    # generator may live on the plan product page — open Standard card if needed
    try:
        body = (pg.inner_text("body", timeout=8000) or "").lower()
    except Exception:
        body = ""
    if "residential proxy generator" not in body:
        for sel in ["text=Standard Residential", "text=Standard Resident"]:
            try:
                loc = pg.locator(sel).first
                if loc.count():
                    loc.click(timeout=10000)
                    pg.wait_for_timeout(6000)
                    break
            except Exception:
                pass
    try:
        body = (pg.inner_text("body", timeout=8000) or "").lower()
    except Exception:
        body = ""
    if "residential proxy generator" not in body:
        # vision fallback: find + click whatever opens the generator
        _vision_locate_click(pg, "the Standard Residential plan card or button that opens proxy generation")
        pg.wait_for_timeout(5000)
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
    # 3. Country (+ value id) with random State/City
    country_val, country_name, state_val, city_val = "_country-id", "any", "random", "random"
    try:
        sels = scope.locator("select")
        geo = []
        for i in range(sels.count()):
            try:
                opts = sels.nth(i).locator("option")
                texts = [opts.nth(j).inner_text(timeout=1500).strip() for j in range(opts.count())]
                geo.append((i, texts))
            except Exception:
                pass
        # country select = the one containing a wanted country
        for i, texts in geo:
            for want in countries:
                hit = next((t for t in texts if want.lower() in t.lower()), "")
                if hit:
                    val = scope.locator("select").nth(i).locator("option", has_text=hit).first.get_attribute("value")
                    scope.locator("select").nth(i).select_option(value=val)
                    pg.wait_for_timeout(3000)  # state/city options reload
                    country_val, country_name = val, hit
                    break
            if country_val != "_country-id":
                break
        if country_val == "_country-id":
            # keep placeholder-country default, still randomize the rest below
            log("wanted countries not listed — keeping default country")
        # state/city = random non-placeholder option of the remaining selects
        picked = 0
        for i, _ in geo:
            try:
                sel = scope.locator("select").nth(i)
                opts = sel.locator("option")
                vals = []
                for j in range(opts.count()):
                    t = opts.nth(j).inner_text(timeout=1500).strip()
                    v = opts.nth(j).get_attribute("value")
                    if t and v and "select" not in t.lower() and "choose" not in t.lower() and v != country_val:
                        vals.append((v, t))
                if vals and picked < 2:
                    v, t = random.choice(vals)
                    # skip the country select itself
                    cur = sel.input_value(timeout=2000)
                    if cur == country_val:
                        continue
                    sel.select_option(value=v)
                    if picked == 0:
                        state_val = v
                    else:
                        city_val = v
                    picked += 1
                    pg.wait_for_timeout(1500)
            except Exception:
                pass
    except Exception as e:
        log(f"geo config: {str(e)[:60]}", "warn")
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


def generate_proxies_api(pg, plan=2, qty=5, sticky=10, country="_country-id"):
    """Call the dashboard generate-proxies API in-page (session cookies apply).
    Returns list of dicts {host,port,user,pw}."""
    res = pg.evaluate("""async ([payload]) => {
        const r = await fetch('/action/generate-proxies', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify(payload)});
        return await r.json();
    }""", [{"country": country, "state": "random", "city": "random",
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


def gen_account(tui, email, password, proxy_parsed, qty, sticky, country, plan, vnc_mode):
    """Login -> redeem if needed -> generate qty proxies -> save. Returns count saved."""
    from playwright.sync_api import sync_playwright
    exe = tui.preflight_browser(log_it=False, headless=not vnc_mode)
    if not exe:
        return 0
    pm = _proxy_mod()
    b = None
    try:
        with sync_playwright() as p:
            launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                           "--disable-blink-features=AutomationControlled"]
            if proxy_parsed and proxy_parsed[1]:
                launch_args.append("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + proxy_parsed[1])
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
            pts, active = affiliate_points(pg)
            log(f"{email}: points={pts} active_plan={active}")
            if not active:
                log(f"{email}: claiming free 50MB...")
                claim_50mb(pg)
                if pts < 1:
                    # legacy redeem path as fallback
                    redeem_50mb(pg)
                pts, active = affiliate_points(pg)
                log(f"{email}: after claim points={pts} active_plan={active}")
            if not active:
                log(f"{email}: no active plan (points={pts}) — cannot generate yet", "warn")
                return 0
            # configure the visible generator form (discovers country/state/city ids)
            cfg = configure_generator(pg, qty=qty, sticky_min=2,
                                      sticky_max=max(2, min(5, sticky)), countries=GEN_COUNTRIES)
            got = generate_proxies_api(pg, plan=plan, qty=qty, sticky=cfg.get("sticky", sticky),
                                       country=cfg.get("country", country))
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
            _vconfirm(pg, f"dashboard for {email} with an active residential plan")
            return added
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
