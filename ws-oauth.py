#!/usr/bin/env python3
"""Temp script: sign up webshare via Google OAuth using mailg cookie sessions.
Bypasses email verification + captcha because Google OAuth is trusted.
Usage: python3 ws-oauth.py [count] [--vnc]
"""
import sys, os, time, json, random, glob
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DISPLAY", ":99")

VNC = "--vnc" in sys.argv
COUNT = 3
for a in sys.argv[1:]:
    if a.isdigit(): COUNT = int(a)

COOKIE_DIR = Path("/root/projects/gmail-inbox/cookies")

def load_proxy():
    import importlib.util as _iu
    tp = _iu.module_from_spec(_iu.spec_from_file_location("tp", str(BASE/"th-proxy.py")))
    _iu.spec_from_file_location("tp", str(BASE/"th-proxy.py")).loader.exec_module(tp)
    proxies = [l.strip() for l in open(BASE/"proxy"/"proxy.txt") if l.strip() and not l.startswith("#") and "relay" not in l.lower() and "@niceproxy" in l]
    if not proxies:
        proxies = [l.strip() for l in open(BASE/"proxy"/"proxy.txt") if l.strip() and not l.startswith("#") and "relay" not in l.lower()]
    if not proxies: return None, None
    raw = random.choice(proxies)
    return tp.parse_proxy(raw), tp

# Find which cookie files have been used already
DONE_FILE = BASE / "ws_oauth_done.txt"

def used_emails():
    used = set()
    p = BASE / "ws_oauth_used.txt"
    if p.exists():
        used = set(p.read_text().strip().split("\n"))
    # a successful OAuth (done) also counts as used — never reuse the cookie session
    if DONE_FILE.exists():
        used |= set(DONE_FILE.read_text().strip().split("\n"))
    return used

def mark_used(email):
    p = BASE / "ws_oauth_used.txt"
    with open(p, "a") as f:
        f.write(email + "\n")

def mark_done(email):
    """Record a successful OAuth signup — same email/cookie never reused."""
    DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DONE_FILE, "a") as f:
        f.write(email + "\n")

def cookie_for_email(cookie_file):
    """Extract email from cookie filename: aaingsukatb_gmail_com.json -> aaingsukatb@gmail.com"""
    name = cookie_file.replace(".json","")
    # pattern: user_gmail_com
    parts = name.split("_")
    # find gmail or the domain suffix
    try:
        g = parts.index("gmail")
        user = "_".join(parts[:g])
        return user + "@gmail.com"
    except ValueError:
        # e.g. PT_TunangIndah_gmail_com
        user = "_".join(parts[:-2])
        return user + "@gmail.com"

print(f"=== WebShare OAuth Signup (count={COUNT}, vnc={VNC}) ===")
print(f"Cookies dir: {COOKIE_DIR} ({len(list(COOKIE_DIR.glob('*.json')))} files)")

from playwright.sync_api import sync_playwright

def _launch_browser(pw, headless, args):
    """Chromium fallback chain: bundled absolute path → channel="chrome" → system path via which."""
    import shutil
    # 1. bundled chromium absolute path first
    try:
        _bundled = Path(str(pw.chromium.executable_path)).resolve()
        if _bundled.exists():
            try:
                return pw.chromium.launch(executable_path=str(_bundled), headless=headless, args=args)
            except Exception as e:
                print(f"  bundled chromium launch fail: {str(e)[:80]} — trying channel=chrome")
    except Exception as e:
        print(f"[swallow ws-oauth.py:bundled] {e}")
    # 2. channel="chrome"
    try:
        return pw.chromium.launch(channel="chrome", headless=headless, args=args)
    except Exception as e:
        print(f"  channel=chrome launch fail: {str(e)[:80]} — trying system chrome")
    # 3. absolute system path via shutil.which
    for _cand in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        _p = shutil.which(_cand)
        if _p:
            try:
                return pw.chromium.launch(executable_path=_p, headless=headless, args=args)
            except Exception as e:
                print(f"  system chrome {_p} launch fail: {str(e)[:80]}")
                continue
    raise SystemExit("no usable chromium found (bundled chromium missing, channel=chrome unavailable, no google-chrome/chromium in PATH)")

used = used_emails()
success = 0
attempts = 0

with sync_playwright() as pw:
    for cookie_file in sorted(COOKIE_DIR.glob("*.json")):
        if success >= COUNT or attempts >= COUNT * 4:
            break
        email = cookie_for_email(cookie_file.name)
        if email in used:
            continue
        attempts += 1
        print(f"\n--- [{success+1}/{COUNT}] {email} ---")

        # load cookies
        try:
            cookies = json.loads(cookie_file.read_text())
        except Exception as e:
            print(f"  cookie parse fail: {e}")
            continue

        # proxy
        proxy_parsed, tp = load_proxy()
        proxy_cfg = {}
        use_proxy = bool(proxy_parsed) and not os.environ.get("WS_OAUTH_NO_PROXY")
        launch_args = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                       "--disable-ipv6",
                       "--webrtc-ip-handling-policy=disable_non_proxied_udp"]
        if use_proxy:
            proxy_cfg = tp.proxy_to_playwright(proxy_parsed)
            # HRR only for IP-literal proxy hosts; hostname proxies use system DNS.
            # Proxyless → omit --host-resolver-rules entirely.
            try:
                import ipaddress as _ip
                _ip.ip_address(proxy_parsed[1])
            except ValueError:
                pass  # hostname proxy host → system DNS, no HRR flag
            except Exception as _e: print(f"[swallow ws-oauth.py:hrr] {_e}")
            else:
                launch_args.append("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE " + proxy_parsed[1])

        b = _launch_browser(pw, headless=not VNC, args=launch_args)
        ctx = b.new_context(viewport={"width":1280,"height":720}, **({"proxy": proxy_cfg} if proxy_cfg else {}))
        pg = ctx.new_page()
        ok = False
        try:
            # 1. set google cookies
            g_cookies = []
            for c in cookies:
                name = c.get("name","")
                # only google domains — avoid weird ones
                dom = c.get("domain","")
                if "google.com" not in dom and "youtube.com" not in dom and "gstatic.com" not in dom and "googleusercontent.com" not in dom:
                    continue
                g_cookies.append({
                    "name": name,
                    "value": c.get("value",""),
                    "domain": dom,
                    "path": c.get("path","/"),
                    "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "sameSite": c.get("sameSite","Lax"),
                })
            if not g_cookies:
                print("  no google cookies in file — skip")
                b.close(); continue
            ctx.add_cookies(g_cookies)
            print(f"  injected {len(g_cookies)} google cookies")

            # 2. verify google session works: hit accounts.google.com
            pg.goto("https://accounts.google.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            url = pg.url
            if "ServiceLogin" in url or "signin" in url.lower():
                print("  ❌ Google session dead (redirected to login)")
                b.close(); continue
            print(f"  ✅ Google session alive: {url[:60]}")

            # 3. goto webshare register + click Sign up with Google
            pg.goto("https://dashboard.webshare.io/register?source=login_signup_link",
                    wait_until="domcontentloaded", timeout=40000)
            time.sleep(4)
            # click google button — expect OAuth popup
            try:
                gbtn = pg.locator("button:has-text('Sign up with Google'), a:has-text('Sign up with Google')").first
                gbtn.wait_for(state="visible", timeout=15000)
                with ctx.expect_page(timeout=20000) as popup_info:
                    gbtn.click()
                popup = popup_info.value
                print("  OAuth popup opened:", (popup.url or "")[:70])
                popup.wait_for_load_state("domcontentloaded", timeout=25000)
                time.sleep(5)
                print("  popup url:", (popup.url or "")[:70])
                try:
                    popup.screenshot(path=f"/tmp/ws_oauth_popup_{email[:15].replace('@','')}.png")
                except Exception as _e:
                    print(f"[swallow ws-oauth.py:165] {_e}")
                    pass
                # maybe account chooser — click matching account if needed
                try:
                    acct_btn = popup.locator("div[data-email], li[role='presentation'], div[role='link']").first
                    if acct_btn.count() and "accounts.google.com" in (popup.url or ""):
                        acct_btn.click()
                        print("  clicked account chooser option")
                        popup.wait_for_load_state("domcontentloaded", timeout=25000)
                        time.sleep(5)
                        print("  after chooser:", (popup.url or "")[:70])
                except Exception as _e:
                    print(f"[swallow ws-oauth.py:176] {_e}")
                    pass
            except Exception as e:
                print(f"  google btn/popup fail: {str(e)[:80]}")
                # fallback: maybe it redirected same-page — check pages
                time.sleep(3)
                # no popup, could be same-page redirect
                try:
                    pg.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception as _e: print(f"[swallow ws-oauth.py:185] {_e}")

            # 4. wait for OAuth flow: popup or redirect. Poll for dashboard
            time.sleep(8)
            pages = ctx.pages
            # OAuth opens popup or same-page redirect
            for extra in pages[1:]:
                try:
                    print(f"  popup: {extra.url[:60]}")
                    extra.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception as _e: print(f"[swallow ws-oauth.py:195] {_e}")
            time.sleep(5)

            # check current page / popups for dashboard
            current = pg
            for pp in pages:
                if "webshare.io/dashboard" in (pp.url or ""):
                    current = pp; break
                if "webshare.io" in (pp.url or "") and "register" not in (pp.url or ""):
                    current = pp; break
            time.sleep(5)
            curl = current.url or ""
            print(f"  final url: {curl[:70]}")
            body = ""
            try: body = (current.inner_text("body", timeout=5000) or "").lower()
            except Exception as _e: print(f"[swallow ws-oauth.py:210] {_e}")

            if "webshare.io/dashboard" in curl or ("verify your email" not in body and "bandwidth" not in body and "/dashboard" in curl):
                # extract token from localStorage or cookies
                token = ""
                try:
                    token = current.evaluate("() => localStorage.getItem('token') || ''") or ""
                except Exception as _e: print(f"[swallow ws-oauth.py:217] {_e}")
                if not token:
                    try:
                        token = current.evaluate("() => (document.cookie.match(/token=([^;]+)/)||[])[1] || ''") or ""
                    except Exception as _e: print(f"[swallow ws-oauth.py:221] {_e}")
                # try API fetch from page context
                if not token:
                    try:
                        token = current.evaluate("""() => {
                            const m = document.cookie.match(/token=([^;]+)/);
                            return m ? m[1] : localStorage.getItem('token') || '';
                        }""") or ""
                    except Exception as _e: print(f"[swallow ws-oauth.py:229] {_e}")
                if token:
                    print(f"  ✅ TOKEN: {token[:12]}...")
                    # save account
                    with open(BASE/"ws_accounts.txt","a") as f:
                        f.write(f"{email}:oauth:{token}\n")
                    mark_used(email)
                    mark_done(email)
                    success += 1
                    ok = True
                else:
                    print("  ⚠️ on dashboard but no token found")
            else:
                print(f"  ❌ not on dashboard: {curl[:60]} body={body[:60]}")
        except Exception as e:
            print(f"  error: {str(e)[:80]}")
        finally:
            try: b.close()
            except Exception as _e: print(f"[swallow ws-oauth.py:247] {_e}")
        if ok:
            time.sleep(3)

print(f"\nDone: {success}/{COUNT} accounts via Google OAuth")
