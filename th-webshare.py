#!/usr/bin/env python3
"""th-webshare — farm Webshare proxy accounts → feed proxy.txt.

Approach (captcha):
- vnc_mode ON: browser visible via VNC, human solves reCAPTCHA by eye,
  we poll for grecaptcha token (cheap + vision-assisted).
- vnc_mode OFF: try paid 2Captcha/AZCaptcha if key provided.
Each free Webshare account gives ~10 residential proxies.

Usage:
  python3 th-webshare.py --count 5               # 5 accounts
  python3 th-webshare.py --count 5 --vnc         # visible browser (solve captcha manually)
  python3 th-webshare.py --count 5 --captcha-key XXX --captcha-provider 2captcha
"""
import os, sys, time, json, random, re, argparse, traceback
import importlib.util
from pathlib import Path
import socket  # for proxy IP resolution

BASE = Path(__file__).resolve().parent
PROXY_LIST = BASE / "proxy.txt"

# ── Prefer project venv if it exists (isolates deps from system python) ──
_venv_py = BASE / ".venv" / "bin" / "python"
if _venv_py.exists():
    _venv_py = str(_venv_py.resolve())
    if os.path.realpath(sys.executable) != os.path.realpath(_venv_py):
        # Re-exec ourselves with the venv python
        os.execv(_venv_py, [_venv_py, os.path.abspath(__file__)] + sys.argv[1:])

# ── Auto-bootstrap: ensure playwright is importable, install if missing ──
def _bootstrap():
    try:
        import playwright  # noqa
        return
    except ImportError:
        pass
    print("  [deps] playwright not found — installing...")
    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "--quiet"])
        print("  [deps] playwright installed")
    except Exception as e:
        print(f"  [deps] auto-install failed: {e}")
        print(f"  [deps] run manually: {sys.executable} -m pip install playwright")
        sys.exit(1)

_bootstrap()

REGISTER_URL = "https://dashboard.webshare.io/register?source=login_signup_link"
API_BASE = "https://proxy.webshare.io/api/v2"
RECAPTCHA_SITEKEY = "6LeHZ6UUAAAAAKat_YS--O2tj_by3gv3r_l03j9d"


def log(msg, icon="info"):
    print(f"  - {msg}", flush=True)


def _load_mail_servers():
    """Read mail_servers from the TUI's config.json."""
    try:
        cfg = json.loads((BASE / "config.json").read_text())
        return cfg.get("mail_servers", []) or []
    except Exception:
        return []


def _cloud_domains(servers=None):
    """Catch-all cloud-mail domains from config + legacy fallback list."""
    doms = []
    for s in (servers if servers is not None else _load_mail_servers()):
        if str(s.get("type", "")).lower() in ("cloudmail", "cloud-mail") and s.get("domain"):
            d = s["domain"].strip().lower()
            if d not in doms:
                doms.append(d)
    for d in ["furries.my.id", "konaima.qzz.io", "konaima.tech", "fascir.my.id",
              "arraffi.my.id", "arqonara.web.id", "berapi.eu.cc", "modrinth.my.id"]:
        if d not in doms:
            doms.append(d)
    return doms


def _real_name_email(domain):
    """Fresh real-name local part — NO dots, NO random junk.
    Webshare flags 'first.last' dots and random strings as suspicious;
    plain concatenated names (markdavis471) pass cleanly."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tt", str(BASE / "th-tui.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        pfx = m._real_prefix({})
        pfx = pfx.replace(".", "").replace("_", "").lower()
        import random as _r
        if not any(ch.isdigit() for ch in pfx):
            pfx += str(_r.randint(11, 999))
        return f"{pfx}@{domain}"
    except Exception:
        return None


def _build_mail_pool(mails_arg, used):
    """Return (pool, source-label) per --mails selection. Empty pool = fall back to file/gen."""
    servers = _load_mail_servers()
    arg = (mails_arg or "").strip().lower()

    def _fresh(lst):
        out = []
        for a in lst:
            a = a.strip().lower()
            if a and "@" in a and a not in used and a not in out:
                out.append(a)
        return out

    if not arg or arg in ("cloud-mail", "cloudmail"):
        # real-name addresses across ALL catch-all domains
        doms = _cloud_domains(servers)
        pool = []
        for d in doms:
            for _ in range(50):  # up to 50 fresh real-name candidates per domain
                e = _real_name_email(d)
                if e and e not in used and e not in pool:
                    pool.append(e)
                    break
        log(f"--mails cloud-mail: {len(pool)} fresh real-name addresses across {len(doms)} domains")
        return pool, "cloud-mail"

    if arg == "mailg":
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("tt", str(BASE / "th-tui.py"))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            accs = _fresh(m.get_mailg_accounts() or [])
            log(f"--mails mailg: {len(accs)} fresh gmail accounts")
            return accs, "mailg"
        except Exception as e:
            log(f"mailg pool unavailable: {str(e)[:60]}", "warn")
            return [], "mailg"

    # specific server name
    srv = next((s for s in servers if s.get("name", "").lower() == arg), None)
    if not srv:
        log(f"mail server '{arg}' not found in config.json — falling back to file pool", "warn")
        return [], arg
    stype = str(srv.get("type", "")).lower()
    if stype == "mailg":
        return _build_mail_pool("mailg", used)
    d = (srv.get("domain") or "").strip().lower()
    if not d:
        log(f"server '{arg}' has no domain — falling back", "warn")
        return [], arg
    pool = []
    for _ in range(50):
        e = _real_name_email(d)
        if e and e not in used and e not in pool:
            pool.append(e)
    log(f"--mails {arg}: {len(pool)} fresh addresses @{d}")
    return pool, arg


def gen_email():
    """Real-looking email (name-based), reused from th-tui's generator."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tt", str(BASE / "th-tui.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        prefix = m._real_prefix({})
        return f"{prefix}@outlook.com"
    except Exception:
        import random, string
        return "".join(random.choices(string.ascii_lowercase, k=8)) + "@outlook.com"


def gen_pass():
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=8)) + \
        random.choice("0123456789") + random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + "x"


WS_USED_FILE = BASE / "ws_used.txt"


def load_used_emails():
    """Load set of webshare emails already attempted/used (from ws_used.txt + ws_accounts.txt)."""
    used = set()
    for f in (WS_USED_FILE, BASE / "ws_accounts.txt"):
        if f.exists():
            for ln in f.read_text().splitlines():
                e = ln.strip().split(":")[0].lower()
                if "@" in e:
                    used.add(e)
    return used


def _mark_used(email):
    """Log an email as used/attempted so it's skipped next run (deduped)."""
    email = email.lower()
    # dedupe: skip if already logged
    if WS_USED_FILE.exists():
        try:
            if email in set(WS_USED_FILE.read_text().splitlines()):
                return
        except Exception:
            pass
    with open(WS_USED_FILE, "a") as f:
        f.write(email + "\n")
    log(f"⊘ used/rejected: {email}", "warn")


def pick_fresh_email(pool, used):
    """Pick an email not yet used for webshare. If pool exhausted, generate a random catch-all one."""
    # 1. try pool (unused from mail source)
    while pool:
        e = pool.pop(0).strip().lower()
        if e and "@" in e and e not in used:
            return e
    # 2. fallback: random catch-all on known good domains (avoid flagged ones)
    import random as _r
    domains = ["furries.my.id", "konaima.qzz.io", "konaima.tech", "fascir.my.id",
               "arraffi.my.id", "arqonara.web.id", "berapi.eu.cc"]
    for _ in range(50):
        e = f"ws{_r.randint(10,99999)}{_r.choice(['','a','b','c','d','e'])}@{_r.choice(domains)}"
        if e not in used:
            return e
    return None


def save_proxies(proxies, path=PROXY_LIST):
    """Append webshare proxies to the list in the SAME scheme:// form as the
    rest of proxy.txt (http://user:pass@host:port) — bare host:port:user:pass
    mixed formats broke downstream parsing/display. Dedupe by full identity."""
    if not proxies:
        return
    existing = set()
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                existing.add(line.strip())
    added = 0
    with open(path, "a") as f:
        for p in proxies:
            host = p.get("proxy_address", "")
            port = p.get("port", "")
            user = p.get("username", "")
            pw = p.get("password", "")
            if not (host and port and user and pw):
                continue
            line = f"http://{user}:{pw}@{host}:{port}"
            if line not in existing:
                f.write(line + "\n")
                existing.add(line)
                added += 1
    return added


def _verify_in_browser(pg, email):
    """Open the Webshare verification link in the SAME browser (keeps cookies/session).
    Quick single inbox poll, then navigate the existing page to the verify URL."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tt", str(BASE / "th-tui.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.load_env()
        # poll patiently — activation mail often lands 30-90s AFTER signup.
        # Read from BOTH providers: catch-all domains hit cloudmail, MailG-sourced
        # accounts receive in their gmail inbox.
        msgs = []
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                msgs = m.read_cloudmail_inbox(email) or []
            except Exception:
                msgs = []
            if not msgs:
                try:
                    msgs = m.read_mailg_inbox(email) or []
                except Exception:
                    msgs = []
            if msgs:
                break
            time.sleep(6)
        if not msgs:
            log(f"No verification email for {email} within 2min — banner will persist", "warn")
            return False
        for msg in msgs:
            body = str(msg.get("text", "")) + " " + str(msg.get("html", ""))
            urls = re.findall(r'https?://[^\s"<>]+', body)
            verify_urls = [u for u in urls if any(w in u.lower() for w in ["verify", "confirm", "activate"])]
            if verify_urls:
                url = verify_urls[0]
                log(f"Verification link found for {email}, opening in browser...", "ok")
                pg.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    pg.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(3)
                # READ THE OUTCOME — loading the URL is not the same as activating
                try:
                    txt = (pg.inner_text("body") or "").lower()
                except Exception:
                    txt = ""
                if any(w in txt for w in ["invalid", "expired", "already been used"]):
                    log(f"Activation REJECTED for {email}: {txt[:80]}", "warn")
                    return False
                if any(w in txt for w in ["verified", "activated", "thank you", "success"]):
                    log(f"Email VERIFIED for {email}", "ok")
                    return True
                # ambiguous — force a dashboard reload; banner state is authoritative there
                try:
                    pg.goto("https://dashboard.webshare.io/dashboard/proxy", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
                    txt = (pg.inner_text("body") or "").lower()
                    if "verify your email" not in txt:
                        log(f"Banner gone after reload — {email} verified", "ok")
                        return True
                    log(f"Banner STILL present for {email} after activation visit", "warn")
                except Exception as e:
                    log(f"Dashboard reload check failed: {str(e)[:50]}", "warn")
                return True
        return False
    except Exception as e:
        log(f"Verify in browser for {email}: {str(e)[:50]}", "warn")
        return False


def _verify_webshare_email(email, timeout=15):
    """Quick check if Webshare sent a verification email and click the link.
    One fast poll (10s) — Webshare free tier rarely needs verification, so don't block.
    Non-blocking — logs result."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tt", str(BASE / "th-tui.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.load_env()
        # ONE quick poll (Webshare free tier doesn't usually verify email)
        for i in range(1, 2):
            msgs = m.read_cloudmail_inbox(email)
            if not msgs:
                return False
            # Find any link in the email body
            for msg in msgs:
                body = str(msg.get("text", "")) + " " + str(msg.get("html", ""))
                urls = re.findall(r'https?://[^\s"<>]+', body)
                verify_urls = [u for u in urls if any(w in u.lower() for w in ["verify", "confirm", "activate", "email"])]
                if verify_urls:
                    url = verify_urls[0]
                    log(f"Verification link found for {email}, clicking...", "ok")
                    import requests
                    requests.get(url, timeout=15)
                    log(f"Verification link clicked for {email}", "ok")
                    return True
            return False
        return False
    except Exception as e:
        log(f"Verify check for {email}: {str(e)[:60]}", "warn")
        return False


def create_one(vnc_mode, proxy_parsed=None, captcha_key=None, captcha_provider="2captcha", email=None, auto_mode=False):
    """auto_mode=True: headed browser but NEVER wait for a human — solver/retry/rotate only."""
    """Create one Webshare account, return (email, proxies_saved_count) or None.
    Returns ('REGISTERED', None) if the email is already taken (caller should retry with fresh email)."""
    email = email or gen_email()
    password = gen_pass()
    log(f"Registering {email} ...")

    os.environ.setdefault("DISPLAY", ":99" if vnc_mode else "")
    sys.path.insert(0, str(BASE))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch_kwargs = {"executable_path": "/usr/bin/chromium-browser",
                         "headless": not vnc_mode,
                         "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
        b = p.chromium.launch(**launch_kwargs)
        ctx_kwargs = {"viewport": {"width": 1280, "height": 720}}
        if proxy_parsed:
            import importlib.util
            spec = importlib.util.spec_from_file_location("tp", str(BASE / "th-proxy.py"))
            tp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tp)
            ctx_kwargs["proxy"] = tp.proxy_to_playwright(proxy_parsed)
        ctx = b.new_context(**ctx_kwargs)
        pg = ctx.new_page()
        try:
            # Use domcontentloaded (networkidle times out on chatty pages, wastes 60s+)
            log(f"Navigating to register page (proxy: {proxy_parsed[1] if proxy_parsed else 'none'})...")
            nav_ok = False
            for attempt in range(1, 4):
                try:
                    pg.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=45000)
                    nav_ok = True
                    break
                except Exception as e:
                    log(f"Navigate attempt {attempt}/3 failed: {str(e)[:40]}", "warn")
                    if attempt < 3:
                        time.sleep(2)
            if not nav_ok:
                log(f"Navigation failed 3x — proxy likely dead/blocked, rotating", "warn")
                try:
                    b.close()
                except Exception:
                    pass
                return ("BLOCKED", None)

            # Wait for email input to be visible — hard-fail if form never loads
            form_ok = False
            try:
                pg.wait_for_selector("#email-input, input[type='email']", state="visible", timeout=30000)
                form_ok = True
            except Exception:
                try:
                    pg.wait_for_selector("button[type='submit'], input[type='email'], input[type='password']",
                                         state="visible", timeout=45000)
                    form_ok = True
                except Exception:
                    form_ok = False
            if not form_ok:
                log(f"Register form never loaded (page broken/slow) — rotating proxy", "warn")
                try:
                    b.close()
                except Exception:
                    pass
                return ("BLOCKED", None)

            time.sleep(2)  # give the form time to fully render (JS hydration)
            
            # Import humanize helpers for anti-bot interactions
            import importlib.util as _ihu
            _hz_spec = _ihu.spec_from_file_location("humanize", str(BASE / "humanize.py"))
            _hz = _ihu.module_from_spec(_hz_spec); _hz_spec.loader.exec_module(_hz)
            _human_type = _hz.human_type
            _human_click = _hz.human_click
            
            # Fill email with human-like typing (curved mouse + variable speed + typos)
            em = pg.locator("#email-input, input[type='email']").first
            _human_type(pg, em, email)
            
            # Verify email was filled
            time.sleep(0.3)
            filled_email = em.input_value()
            if filled_email != email:
                log(f"Email fill mismatch, retrying (got: {filled_email[:20]})", "warn")
                em.fill("")
                time.sleep(0.3)
                em.fill(email)
                time.sleep(0.5)
            
            # Fill password with human-like typing
            pw = pg.locator("input[type='password']").first
            _human_type(pg, pw, password)
            
            # Verify password was filled
            time.sleep(0.3)
            filled_pw = pw.input_value()
            if filled_pw != password:
                log(f"Password fill mismatch, retrying", "warn")
                pw.fill("")
                time.sleep(0.3)
                pw.fill(password)
                time.sleep(0.5)
            try:
                cb = pg.locator("input[type='checkbox']").first
                if cb.count():
                    # human click with hesitation before agreeing to ToS
                    _hz.rand_delay(0.3, 0.8)
                    _human_click(pg, cb)
            except Exception:
                pass
            time.sleep(0.3)
            # human click on submit with a pause before it
            _hz.rand_delay(0.4, 1.2)
            _human_click(pg, pg.locator("button[type='submit']").first)
            log("Sign up clicked — waiting for captcha solve (" +
                ("solve it in VNC browser" if vnc_mode else "auto/paid") + ")")

            # Wait for page to settle after submit (domcontentloaded, not networkidle)
            try:
                pg.wait_for_load_state("domcontentloaded", timeout=45000)
            except Exception:
                pass
            time.sleep(2)  # let the result render

            # DETECT "email already registered" BEFORE waiting on captcha
            # poll a few seconds so a slow post-submit transition is caught
            body = ""
            for _ in range(5):
                try:
                    body = pg.inner_text("body", timeout=3000).lower()
                    if any(w in body for w in ["automated queries", "already registered", "already exists",
                                               "recaptcha", "not a robot", "sign up", "create account",
                                               "captcha", "cannot be used", "cannot use", "not a valid email",
                                               "invalid email", "different email", "does not support",
                                               "already in use", "already taken"]):
                        break
                except Exception:
                    pass
                time.sleep(1)
            try:
                # Google automated-queries block — rotate proxy immediately
                if any(w in body for w in ["automated queries", "can't process your request",
                                           "cannot process your request", "unusual traffic",
                                           "our systems have detected", "sending automated queries",
                                           "protect our users"]):
                    log(f"Blocked page detected on this proxy (post-submit) — rotating...", "warn")
                    try:
                        b.close()
                    except Exception:
                        pass
                    return ("BLOCKED", None)
                if any(w in body for w in ["already registered", "already exists", "already in use",
                                           "email already", "already taken", "has already been"]):
                    log(f"Email already registered: {email}", "warn")
                    # mark as used so we skip it next time
                    _mark_used(email)
                    try:
                        b.close()
                    except Exception:
                        pass
                    return ("REGISTERED", None)
                if any(w in body for w in ["cannot be used", "not a valid email", "invalid email",
                                           "please try a different email", "does not support"]):
                    log(f"Email cannot be used (blocked): {email}", "warn")
                    _mark_used(email)
                    try:
                        b.close()
                    except Exception:
                        pass
                    return ("REGISTERED", None)  # treat as unusable, retry with fresh
            except Exception:
                pass

            # SOLVE CAPTCHA
            token = None
            # Enterprise often PASSES silently (no widget at all) based on risk score.
            # Poll for an already-present token BEFORE trying any solver.
            for _w in range(10):
                try:
                    t0 = pg.evaluate("() => (window.grecaptcha && grecaptcha.getResponse()) || ''")
                except Exception:
                    t0 = ""
                if t0:
                    log("Captcha auto-passed (invisible Enterprise) — token ready", "ok")
                    token = t0
                    break
                time.sleep(1)
            # auto-solve with audio solver first (free — no API key needed)
            if not token:
                log("Trying audio solver...", "info")
            try:
                if _solve_recaptcha_audio(pg):
                    t = pg.evaluate("() => (window.grecaptcha && grecaptcha.getResponse()) || ''")
                    if t:
                        token = t
            except Exception:
                pass
            if not token and vnc_mode and auto_mode:
                # --vnc-auto: VNC stays up as a WITNESS, but nobody will solve.
                # Rotate proxy immediately instead of blocking on a human.
                log("Audio solver failed (--vnc-auto) — rotating proxy instead of waiting", "warn")
                return None
            if not token and vnc_mode:
                log("Audio solver didn't work — waiting for manual solve in VNC...", "warn")
                log("  (press Q to skip/rotate this account's proxy)", "info")
                import select
                import termios as _term, tty as _tty
                for _ in range(300):  # up to 10 min, but detects block early
                    # manual skip keybind: Q -> rotate proxy for this account
                    try:
                        if select.select([sys.stdin], [], [], 0)[0]:
                            _k = sys.stdin.read(1)
                            if _k in ("q", "Q"):
                                log(f"Manual skip pressed — rotating proxy for {email}", "warn")
                                _mark_used(email)
                                try:
                                    b.close()
                                except Exception:
                                    pass
                                return ("SKIP", None)
                    except Exception:
                        pass
                    try:
                        # detect Google "automated queries" block — rotate proxy instead of waiting
                        _txt = ""
                        _html = ""
                        try:
                            _txt = pg.inner_text("body", timeout=2000).lower() if pg else ""
                        except Exception:
                            pass
                        try:
                            _html = pg.content().lower() if pg else ""
                        except Exception:
                            pass
                        _all = _txt + " " + _html
                        if any(w in _all for w in [
                            "automated queries", "can't process your request", "cannot process your request",
                            "unusual traffic", "our systems have detected", "sending automated queries",
                            "protect our users",
                        ]):
                            log(f"Blocked page detected on this proxy — rotating...", "warn")
                            try:
                                b.close()
                            except Exception:
                                pass
                            return ("BLOCKED", None)
                        # email rejected (cannot be used / invalid) — mark used + retry fresh
                        if any(w in _all for w in [
                            "cannot be used", "cannot use", "not a valid email", "invalid email",
                            "different email", "does not support", "has already been used",
                        ]):
                            log(f"Email rejected by webshare: {email}", "warn")
                            _mark_used(email)
                            try:
                                b.close()
                            except Exception:
                                pass
                            return ("REGISTERED", None)
                    except Exception:
                        pass
                    try:
                        t = pg.evaluate("() => (window.grecaptcha && grecaptcha.getResponse()) || ''")
                        if t:
                            token = t
                            break
                    except Exception:
                        pass
                    time.sleep(2)
            elif captcha_key:
                token = _solve_paid(captcha_key, captcha_provider)
            if not token:
                # second chance after paid/manual paths skipped
                log("Retrying audio solver...", "info")
                try:
                    if _solve_recaptcha_audio(pg):
                        t = pg.evaluate("() => (window.grecaptcha && grecaptcha.getResponse()) || ''")
                        if t:
                            token = t
                except Exception:
                    pass
            if not token:
                log("Captcha not solved — account abandoned")
                return None

            log("Captcha solved — submitting...")
            time.sleep(5)  # give browser time to navigate

            # WAIT FOR DASHBOARD TO LOAD (registration may show a startup/onboarding screen)
            # Poll for the dashboard URL or a token in localStorage up to ~60s
            at = ""
            for _ in range(60):
                at = pg.evaluate("() => localStorage.getItem('token') || ''") or ""
                if at:
                    break
                # also check cookie token
                at = pg.evaluate("() => (document.cookie.match(/token=([^;]+)/)||[])[1] || ''") or ""
                if at:
                    break
                time.sleep(1)
            if not at:
                log("No token found after dashboard load — trying API register")
            # NOTE: browser stays open here — we need it for the verification link
            # b.close() happens AFTER verification (below)

            import requests
            sess = requests.Session()
            if not at:
                # try API register
                resp = sess.post(f"{API_BASE}/register/",
                                 json={"email": email, "password": password,
                                       "recaptcha": token, "tos_accepted": True,
                                       "marketing_email_accepted": False},
                                 headers={"Content-Type": "application/json",
                                          "Origin": "https://proxy.webshare.io",
                                          "Referer": "https://proxy.webshare.io/register"},
                                 timeout=25)
                if resp.status_code in (200, 201):
                    at = resp.json().get("token", "")
                else:
                    body = resp.text[:200]
                    log(f"API register: {resp.status_code} {body}")
                    # Return specific error types so caller can react
                    low = body.lower()
                    if resp.status_code == 400 and ("suspicious email" in low or "cannot sign up" in low):
                        log(f"Email flagged suspicious by webshare: {email}", "warn")
                        return ("SUSPICIOUS", None)
                    if resp.status_code == 429 or ("throttl" in low):
                        # extract wait seconds
                        import re as _re
                        m = _re.search(r"(\d+)", low)
                        wait = int(m.group(1)) if m else 60
                        log(f"Webshare throttled — need to wait {wait}s", "warn")
                        return ("THROTTLED", wait)
                    return None
            if not at:
                log("No token obtained")
                return None
            # fetch proxies
            r2 = sess.get(f"{API_BASE}/proxy/list/", headers={"Authorization": f"Token {at}"},
                          params={"mode": "direct", "page": 1, "page_size": 100}, timeout=15)
            if r2.status_code != 200:
                log(f"Proxy fetch: {r2.status_code}")
                return None
            results = r2.json().get("results", [])
            added = save_proxies(results)
            log(f"Account {email}: {added} new proxies saved")
            # save account
            with open(BASE / "ws_accounts.txt", "a") as f:
                f.write(f"{email}:{password}:{at}\n")
            # verify in the SAME browser (before close) — opens the link with cookies/session
            try:
                _verify_in_browser(pg, email)
            except Exception as e:
                log(f"Verification check skipped: {str(e)[:40]}", "info")
            try:
                b.close()
            except Exception:
                pass
            return (email, added)
        except Exception as e:
            log(f"Error: {str(e)[:100]}")
            try:
                b.close()
            except Exception:
                pass
            return None


def _solve_recaptcha_audio(pg, max_attempts=4):
    """Solve reCAPTCHA v2 via the audio challenge, fully logged.

    Frame map (reCAPTCHA v2):
      anchor iframe (recaptcha/api2/anchor)  -> checkbox
      bframe iframe (recaptcha/api2/bframe)  -> challenge (image OR audio controls)

    Audio chain: bframe #audio-source (MP3) -> ffmpeg -> WAV -> flac -> Google SR.
    Returns True when a g-recaptcha-response token appears on the page."""
    try:
        import speech_recognition as sr
        rec = sr.Recognizer()
    except Exception as e:
        log(f"Audio solver unavailable: {str(e)[:80]}", "warn")
        return False

    def _frames():
        # Webshare uses reCAPTCHA ENTERPRISE: /recaptcha/enterprise/anchor|bframe
        # Classic v2 uses /recaptcha/api2/anchor|bframe. Match both.
        anchor = bf = None
        for fr in pg.frames:
            u = (fr.url or "")
            if "/anchor" in u and "recaptcha" in u:
                anchor = fr
            elif ("/bframe" in u or "imageframe" in u) and "recaptcha" in u:
                bf = fr
        return anchor, bf

    for attempt in range(1, max_attempts + 1):
        try:
            # iframes are injected async by recaptcha/api.js — poll up to ~8s
            anchor = bf = None
            for _w in range(16):
                anchor, bf = _frames()
                if anchor:
                    break
                time.sleep(0.5)
            if not anchor:
                # captcha JS can take 20s+ to render through a slow proxy —
                # keep waiting within THIS attempt before giving the slot up
                log(f"Audio solve {attempt}/{max_attempts}: captcha iframe not up yet "
                    "(slow proxy render) — extending wait", "info")
                for _x in range(24):  # +24s beyond the initial 8s poll
                    time.sleep(1)
                    anchor, bf = _frames()
                    if anchor:
                        break
                if not anchor:
                    log(f"Audio solve {attempt}: still no captcha after extended wait", "warn")
                    continue

            # 1. tick the checkbox if present and not already green.
            # Enterprise direct-challenge mode has NO checkbox — challenge opens straight away.
            if anchor and not anchor.evaluate(
                    "() => !!document.querySelector('.recaptcha-checkbox-checked')"):
                cb = anchor.locator(".recaptcha-checkbox-border").first
                if cb.count():
                    cb.click()
                    time.sleep(2.5)
                else:
                    log(f"Audio solve {attempt}: checkbox element missing", "info")
            else:
                log(f"Audio solve {attempt}: checkbox already green — token pending", "info")
                time.sleep(1)

            # 2. challenge frame appears after the click — poll for it
            # (slow proxies render the challenge many seconds late)
            anchor, bf = None, None
            for _w in range(16):
                anchor, bf = _frames()
                if bf:
                    break
                time.sleep(0.5)
            if not bf:
                log(f"Audio solve {attempt}: no challenge frame appeared after click "
                    "(waited 8s)", "info")
                continue

            # 3. switch to audio. The button may live in ANY recaptcha frame
            # (bframe vs imageframe) depending on challenge state — scan all.
            ab = None
            for fr in pg.frames:
                if "recaptcha" not in (fr.url or ""):
                    continue
                try:
                    loc = fr.locator("[title='Get an audio challenge'], [aria-label='Get an audio challenge']").first
                    if loc.count():
                        ab = loc
                        break
                except Exception:
                    continue
            if ab is not None:
                try:
                    ab.click(timeout=6000)
                    log(f"Audio solve {attempt}: audio-switch CLICKED (trusted)", "info")
                except Exception as e:
                    log(f"Audio solve {attempt}: trusted click failed: {str(e)[:70]}", "warn")
            else:
                log(f"Audio solve {attempt}: audio control not in any frame this round", "info")
            time.sleep(3)
            src = ""
            for fr in pg.frames:
                if "recaptcha" not in (fr.url or ""):
                    continue
                try:
                    s2 = fr.evaluate("""() => {
                        const a = document.getElementById('audio-source');
                        if (a && a.src) return a.src;
                        const au = document.querySelector('audio');
                        if (au) return au.currentSrc || au.src || '';
                        const s = document.querySelector('audio source');
                        return s ? s.src : '';
                    }""")
                    if s2:
                        src = s2
                        break
                except Exception:
                    continue
            if not src:
                _dbg = bf.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button,[role=button]')].map(b => b.title || b.getAttribute('aria-label') || b.className).filter(Boolean);
                    const aud = [...document.querySelectorAll('audio,source')].map(a => a.tagName + ':' + (a.src || a.currentSrc || '').slice(0, 60));
                    return JSON.stringify({btns: btns.slice(0, 8), aud});
                }""")
                log(f"Audio solve {attempt}: no audio source yet. frame={_dbg[:200]}", "info")
                continue

            # 4. download MP3 and convert to WAV (sr cannot read MP3)
            import requests as _rq
            audio_bytes = _rq.get(src, timeout=15,
                                  headers={"User-Agent": "Mozilla/5.0"}).content
            mp3 = str(BASE / "_captcha_audio.mp3")
            wav = str(BASE / "_captcha_audio.wav")
            with open(mp3, "wb") as f:
                f.write(audio_bytes)
            from pydub import AudioSegment
            AudioSegment.from_file(mp3).export(wav, format="wav")

            # 5. transcribe
            with sr.AudioFile(wav) as source:
                audio = rec.record(source)
            try:
                answer = rec.recognize_google(audio, language="en-US")
            except sr.UnknownValueError:
                log(f"Audio solve {attempt}: transcript empty (reload + retry)", "info")
                rl = bf.locator("#recaptcha-reload-button").first
                if rl.count():
                    rl.click()
                    time.sleep(2)
                continue
            except Exception as e:
                log(f"Audio solve {attempt}: transcription failed: {str(e)[:90]}", "warn")
                continue
            answer = answer.lower().strip()
            log(f"Audio solve {attempt}: heard '{answer[:40]}'", "info")
            if not answer:
                continue

            # 6. submit
            inp = vb = None
            for fr in pg.frames:
                if "recaptcha" not in (fr.url or ""):
                    continue
                try:
                    l1 = fr.locator("#audio-response").first
                    if l1.count():
                        inp = l1
                        v = fr.locator("#recaptcha-verify-button").first
                        vb = v if v.count() else None
                        break
                except Exception:
                    continue
            if inp is None:
                log(f"Audio solve {attempt}: #audio-response missing", "info")
                continue
            inp.fill(answer)
            time.sleep(0.6)
            if vb is not None:
                vb.click()
            time.sleep(3.5)

            # 7. token?
            tok = pg.evaluate(
                "() => (window.grecaptcha && grecaptcha.getResponse()) || ''")
            if tok:
                return True
            log(f"Audio solve {attempt}: submitted '{answer[:20]}' but token still empty "
                "(wrong digits or rejected — retrying)", "warn")
        except Exception as e:
            log(f"Audio solve {attempt}/{max_attempts} error: {str(e)[:100]}", "warn")
            time.sleep(2)
    return False


def _solve_paid(api_key, provider):
    """Paid captcha solve (2captcha/azcaptcha). Returns token or None."""
    if provider == "2captcha":
        submit = "https://2captcha.com/in.php"
        result = "https://2captcha.com/res.php"
    else:
        submit = "https://azcaptcha.com/in.php"
        result = "https://azcaptcha.com/res.php"
    import requests
    try:
        r = requests.post(submit, data={"key": api_key, "method": "userrecaptcha",
                                        "googlekey": RECAPTCHA_SITEKEY,
                                        "pageurl": REGISTER_URL, "json": 1}, timeout=30)
        j = r.json()
        if j.get("status") != 1:
            return None
        cid = j["request"]
        for _ in range(60):
            time.sleep(5)
            r2 = requests.get(result, params={"key": api_key, "action": "get",
                                              "id": cid, "json": 1}, timeout=15)
            j2 = r2.json()
            if j2.get("status") == 1:
                return j2["request"]
            if "CAPCHA_NOT_READY" not in str(j2.get("request", "")):
                return None
    except Exception:
        pass
    return None


def _start_vnc_stack():
    """Ensure Xvfb(:99) + x11vnc + websockify are running for headed mode."""
    import subprocess as _sp
    # 1. Xvfb
    if not (_sp.call("pgrep -x Xvfb >/dev/null 2>&1", shell=True) == 0):
        _sp.Popen(["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        time.sleep(2)
        log("Started Xvfb :99", "ok")
    os.environ.setdefault("DISPLAY", ":99")
    # 2. x11vnc
    if not (_sp.call("pgrep -f 'x11vnc -display :99' >/dev/null 2>&1", shell=True) == 0):
        vnc_pw = os.environ.get("VNC_PASSWORD", "")
        cmd = ["x11vnc", "-display", ":99", "-forever", "-shared", "-nopw"]
        if vnc_pw:
            cmd += ["-passwdfile", "/dev/stdin"]
            p = _sp.Popen(cmd, stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            p.communicate(vnc_pw.encode())
        else:
            _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        time.sleep(1)
        log("Started x11vnc :99", "ok")
    # 3. websockify noVNC
    if not (_sp.call("pgrep -f 'websockify' >/dev/null 2>&1", shell=True) == 0):
        _sp.Popen(["websockify", "--web=/opt/noVNC", "6080", "127.0.0.1:5900"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        time.sleep(1)
        log("Started websockify :6080", "ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--vnc", action="store_true", help="visible browser (manual captcha)")
    ap.add_argument("--cleanup-vnc", action="store_true",
                    help="kill leftover Chromium + Xvfb/x11vnc/websockify on exit "
                         "(free the :99 display for run-batch)"),
    ap.add_argument("--vnc-auto", dest="vnc_auto", action="store_true",
                    help="headed browser on VNC display, FULLY automatic: audio solver -> retry -> rotate; never blocks for a human")
    ap.add_argument("--proxy", default=None, help="proxy for WS registration (optional)")
    ap.add_argument("--captcha-key", default=None)
    ap.add_argument("--captcha-provider", default="2captcha")
    ap.add_argument("--email-file", default="webshare-emails.txt", help="File with catch-all emails to use (default webshare-emails.txt)")
    ap.add_argument("--mails", default=None,
                    help="Mail source: 'cloud-mail' (catch-all domains from config.json), "
                         "'mailg' (gmail-inbox accounts), or a mail-server name from config.json")
    ap.add_argument("--proxy-order", default="random", choices=["random", "top", "least"],
                    help="Proxy pick order: random (default), top (sequential), least-used")
    ap.add_argument("--skip-wait-throttle", action="store_true", default=False,
                    help="Don't wait on throttle — just swap proxy immediately (aggressive)")
    ap.add_argument("--max-per-proxy", type=int, default=0,
                    help="Rotate to a new proxy after N accounts (0=unlimited/sticky until throttle)")
    args = ap.parse_args()

    # Auto-start Xvfb + VNC stack if headed mode requested (--vnc / --vnc-auto)
    if args.vnc or args.vnc_auto:
        _start_vnc_stack()

    # Load emails from file if provided
    email_pool = None
    used = load_used_emails()
    # load the personal/used blacklist — personal emails must never hit webshare
    try:
        _bl = {line.strip().lower() for line in open(BASE / "email-blacklist.txt") if line.strip() and "@" in line}
        used |= _bl
    except Exception:
        pass
    # MailG accounts (gmail-inbox :8790) join the pool alongside the catch-all file.
    # Real Gmail inboxes receive webshare verification mail reliably; the verify step
    # below tries BOTH readers so provenance doesn't matter.
    try:
        spec_mg = __import__("importlib").util.spec_from_file_location(
            "tt", str(BASE / "th-tui.py"))
        _tt = __import__("importlib").util.module_from_spec(spec_mg)
        spec_mg.loader.exec_module(_tt)
        mg = [a.strip().lower() for a in (_tt.get_mailg_accounts() or []) if "@" in a]
        mg = [a for a in mg if a not in used]
        if mg:
            log(f"MailG pool: {len(mg)} unused accounts available", "ok")
    except Exception as _e:
        mg = []
        log(f"MailG pool unavailable: {str(_e)[:60]}", "warn")
    mails_pool, mails_src = (None, None)
    if args.mails:
        mails_pool, mails_src = _build_mail_pool(args.mails, used)
    if args.mails and not mails_pool:
        log("--mails produced no addresses — falling back to email file/generator", "warn")
    if args.mails and mails_pool:
        random.shuffle(mails_pool)
        email_pool = mails_pool
    elif args.email_file:
        try:
            pool = [e.strip().lower() for e in open(args.email_file).readlines() if e.strip() and "@" in e]
            if not pool:
                log(f"Warning: {args.email_file} is empty, will generate new emails")
            else:
                log(f"Loaded {len(pool)} emails from {args.email_file}")
                # Shuffle the pool so we don't always start from the top (randomized order each run)
                random.shuffle(pool)
                # Filter out already-used emails + TH-registered (real account emails)
                before = len(pool)
                pool = [e for e in pool if e not in used]
                # merge mailg accounts (dedup) — only when no explicit --mails choice
                if not args.mails:
                    for a in mg:
                        if a not in pool and a not in used:
                            pool.append(a)
                    random.shuffle(pool)
                email_pool = pool if pool else None
                log(f"Filtered: {len(email_pool or [])} fresh emails available (dropped {before - len(pool)} used)", "warn" if email_pool and len(email_pool) < before * 0.5 else "ok")
        except FileNotFoundError:
            log(f"Email file {args.email_file} not found, will generate new emails")

    proxy_list = None
    proxy_usage = {}  # usage count per proxy index
    
    if args.proxy:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tp", str(BASE / "th-proxy.py"))
        tp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tp)
        
        # Check if it's a file path or single proxy URL
        proxy_path = args.proxy.strip()
        if proxy_path.endswith(".txt") and os.path.exists(proxy_path):
            # Load from file with rotation
            try:
                proxies = [line.strip() for line in open(proxy_path).readlines() if line.strip() and not line.startswith("#")]
                if proxies:
                    log(f"Loading {len(proxies)} proxies from {proxy_path} for rotation")
                    proxy_list = [(tp.parse_proxy(p), idx) for idx, p in enumerate(proxies)]  # (parsed, index)
                    for idx in range(len(proxy_list)):
                        proxy_usage[idx] = 0
                    log(f"Proxy rotation enabled: {len(proxies)} proxies available")
                else:
                    log("Warning: proxy file empty, proceeding without proxy")
            except Exception as e:
                log(f"Failed to read proxy file {proxy_path}: {e}")
                proxy_list = None
        else:
            # Single proxy - use it for all accounts
            proxy_parsed_single = tp.parse_proxy(proxy_path)
            if proxy_parsed_single:
                log(f"Using single proxy for all accounts: {proxy_parsed_single[1]}:{proxy_parsed_single[2]}")
                proxy_list = [(proxy_parsed_single, 0)]
                proxy_usage[0] = args.count
    
    ok = 0
    used = load_used_emails()

    # Proxy strategy: stick with one proxy until it THROTTLES, then swap.
    # Keeps a proxy warm for as many accounts as Webshare allows before rotating.
    # Proxy pick order: --proxy-order random|top|least  (default random)
    # Throttle behavior: --skip-wait-throttle skips waiting (just swaps immediately), default waits
    proxy_order = getattr(args, "proxy_order", "random")
    skip_throttle_wait = getattr(args, "skip_wait_throttle", False)
    current_proxy = None       # the parsed proxy we're currently using
    current_proxy_idx = None   # index into proxy_list
    proxy_blocked = set()      # indices temporarily blocked (throttled)

    def _usable_indices():
        if not proxy_list:
            return []
        return [i for i in range(len(proxy_list)) if i not in proxy_blocked]

    def _swap_proxy():
        """Pick the next proxy based on ordering. Returns parsed proxy or None."""
        nonlocal current_proxy, current_proxy_idx
        if not proxy_list:
            current_proxy, current_proxy_idx = None, None
            return None
        usable = _usable_indices()
        if not usable:
            log("All proxies currently throttled/blocked — clearing block list to reuse", "warn")
            proxy_blocked.clear()
            usable = list(range(len(proxy_list)))
        # Choose index based on ordering
        if proxy_order == "top":
            # sequential from top: first usable index
            best = usable[0]
        elif proxy_order == "least":
            # least-used
            best = min(usable, key=lambda i: proxy_usage[i])
        else:
            # random among usable (default)
            best = random.choice(usable)
        current_proxy_idx = best
        current_proxy = proxy_list[best][0]
        proxy_usage[best] += 1
        # resolve and log proxy IP
        host = current_proxy[1]
        port = current_proxy[2]
        user = current_proxy[3] or ""
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = host
        user_str = f" ({user})" if user else ""
        log(f"Swapped to proxy[{best}]: {host}:{port}{user_str} (IP: {ip}, used {proxy_usage[best]}x, order={proxy_order})")
        return current_proxy

    def pick_proxy():
        """Return current proxy; if none, pick one. Swap happens on throttle/error."""
        if current_proxy is None:
            return _swap_proxy()
        return current_proxy

    def on_throttle(wait):
        """Handle throttle: block current proxy, optionally wait, swap to a new one."""
        nonlocal current_proxy_idx
        log(f"Proxy throttled — blocking it and swapping to a fresh one", "warn")
        if current_proxy_idx is not None:
            proxy_blocked.add(current_proxy_idx)
        # optionally wait out the cooldown so the new proxy isn't rushed (unless --skip-wait-throttle)
        if not skip_throttle_wait:
            time.sleep(min(wait, 30))  # cap wait at 30s so we keep moving
        return _swap_proxy()

    for i in range(args.count):
        log(f"=== Account {i+1}/{args.count} ===")
        # Pick a fresh email (randomized from shuffled pool, skipping used) OR fallback random
        email_to_use = None
        if email_pool:
            # pop from shuffled pool; skip any that became used
            while email_pool:
                candidate = email_pool.pop(0)
                if candidate not in used:
                    email_to_use = candidate
                    break
            if email_to_use:
                log(f"Using email from pool: {email_to_use}")
        else:
            # pool empty — let pick_fresh_email generate a random one
            email_to_use = pick_fresh_email([], used)
            if email_to_use:
                log(f"Using generated email: {email_to_use}")

        # Retry loop with sticky proxy: stick with ONE proxy until it THROTTLES, then swap
        attempts = 0
        r = None
        proxy_for_this = pick_proxy()  # initial proxy
        if proxy_for_this:
            _host = proxy_for_this[1]
            _port = proxy_for_this[2]
            _user = proxy_for_this[3] or ""
            try:
                _ip = socket.gethostbyname(_host)
            except Exception:
                _ip = _host
            _u_str = f" ({_user})" if _user else ""
            log(f"Using proxy: {_host}:{_port}{_u_str} (IP: {_ip})")
        
        while attempts < 10:
            if not email_to_use:
                email_to_use = pick_fresh_email([], used)
                if not email_to_use:
                    log("No emails available — exhausted")
                    break
            
            r = create_one(args.vnc or args.vnc_auto, proxy_for_this, args.captcha_key,
                          args.captcha_provider, email=email_to_use,
                          auto_mode=args.vnc_auto)
            
            status = r[0] if isinstance(r, tuple) else "ERROR"
            if status == "REGISTERED":
                log(f"✗ REJECTED {email_to_use} — already registered. Marking used, picking fresh (attempt {attempts+1}/10)", "warn")
                used.add(email_to_use)
                _mark_used(email_to_use)
                email_to_use = None
                attempts += 1
                continue
            
            if status == "SUSPICIOUS":
                log(f"✗ REJECTED {email_to_use} — flagged suspicious by webshare. Marking used, skipping permanently", "warn")
                used.add(email_to_use)
                _mark_used(email_to_use)
                email_to_use = None
                attempts += 1
                continue
            
            if status == "BLOCKED":
                log(f"Proxy blocked by captcha (automated queries) — rotating proxy (attempt {attempts+1}/10)", "warn")
                proxy_for_this = on_throttle(3)  # block this proxy + swap
                attempts += 1
                continue

            if status == "SKIP":
                log(f"Manual skip — rotating proxy (attempt {attempts+1}/10)", "warn")
                proxy_for_this = on_throttle(2)
                attempts += 1
                continue
            
            if status == "THROTTLED":
                wait = r[1] if len(r) > 1 else 60
                log(f"Proxy throttled — swapping to a fresh one (wait {min(wait,30)}s)...", "warn")
                proxy_for_this = on_throttle(wait)
                used.add(email_to_use)
                _mark_used(email_to_use)
                email_to_use = None
                attempts += 1
                continue
            
            break  # success or non-special error

        # only a real registration counts: r = (email, added)
        if r and isinstance(r[0], str) and "@" in r[0]:
            ok += 1
            # rotate proxy after N accounts (if --max-per-proxy set)
            if args.max_per_proxy and current_proxy_idx is not None and proxy_usage.get(current_proxy_idx, 0) >= args.max_per_proxy:
                log(f"Reached --max-per-proxy {args.max_per_proxy} — rotating proxy", "info")
                proxy_for_this = on_throttle(1)
        if i + 1 < args.count:
            log("Waiting 5s...")
            time.sleep(5)
    print(f"\nDone: {ok}/{args.count} webshare accounts, proxies appended to {PROXY_LIST}")
    _cleanup_vnc(force=True if (args.vnc or args.vnc_auto) else args.cleanup_vnc)
    if args.vnc or args.vnc_auto or args.cleanup_vnc:
        log("Cleaned up browser/VNC processes", "ok")
    return 0


def _cleanup_vnc(force=False):
    """Kill leftover Chromium on the webshare display + (optionally) the VNC stack.

    Problem: webshare's browser has no --remote-debugging-port, so run-batch's
    pkill misses it. If webshare crashes mid-run its Chromium survives on :99 and
    blocks run-batch's Xvfb from starting. Always kill our own Chromium leftovers;
    only tear down Xvfb/x11vnc/websockify when --cleanup-vnc is passed (they may
    be shared with other tooling)."""
    import subprocess as _sp
    if force:
        _sp.run('pkill -f "[c]hromium-browser.*--no-sandbox" ; pkill -f "[X]vfb :99" ; '
                'pkill -f "[x]11vnc -display :99" ; pkill -f "[w]ebsockify 6080" ; true',
                shell=True)
    else:
        # only our own browser leftovers (DISPLAY :99 headed chromium)
        _sp.run('pkill -f "[c]hromium-browser --no-sandbox" ; true', shell=True)
    return True


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        # tear down VNC stack we started (leave other tooling alone)
        import subprocess as _sp
        _sp.run('pkill -f "[c]hromium-browser.*--no-sandbox" ; pkill -f "[X]vfb :99" ; '
                'pkill -f "[x]11vnc -display :99" ; pkill -f "[w]ebsockify 6080" ; true',
                shell=True)
        sys.exit(1)