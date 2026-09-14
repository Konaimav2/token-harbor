#!/usr/bin/env python3
"""th_oauth_temp.py — TH Google OAuth via site click (not direct oauth URL).
FIX: run with WS_TH_NO_PROXY removed so we can match cookie ID location.
     Proxy location flag removed per your report — run without proxy so
     google session matches machine location.
Usage: python3 th_oauth_temp.py 1
       python3 th_oauth_temp.py 3 --vnc
"""
import json, time, glob, re, os
from pathlib import Path
BASE = Path(__file__).parent

# P8 portable paths: env override (GMAIL_INBOX_DIR legacy root, or per-file
# vars) → BASE-relative default. Missing → warn + skip, never crash.
def _gmail_path(env_key, default_rel):
    v = os.environ.get(env_key)
    if v:
        return Path(v)
    home = os.environ.get("GMAIL_INBOX_DIR")
    if home:
        return Path(home) / Path(default_rel).name
    return BASE / default_rel
COOKIE_DIR = _gmail_path("GMAIL_COOKIES", "cookies")
INBOX_DB = str(_gmail_path("GMAIL_INBOX_DB", "data/inbox.db"))
LOGGEDMAIL = _gmail_path("GMAIL_LOGGEDMAIL", "data/loggedmail.txt")
SECRETS_FILE = _gmail_path("GMAIL_2FA_SECRETS", "data/.2fa-secrets")

def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def _mailg_link(email, kind, since_ts=0, poll_s=120):
    """mailg API: newest TH link of kind (reset|verify) from body_html. FULL LOGS + delay."""
    import requests, sqlite3, urllib.parse
    MAILG_API = "http://127.0.0.1:8790"
    _log(f"mailg poll: kind={kind} up to {poll_s}s (fresh after ts={since_ts})")
    try:
        if not Path(INBOX_DB).exists():
            raise FileNotFoundError(f"mailg DB missing: {INBOX_DB} (set GMAIL_INBOX_DB or GMAIL_INBOX_DIR)")
        key = sqlite3.connect(INBOX_DB).execute(
            "SELECT value FROM settings WHERE key='api_key'").fetchone()[0]
    except Exception as e:
        _log(f"mailg api key fail: {e}"); return "", 0
    H = {"X-API-Key": key}
    em = urllib.parse.quote(email)
    deadline = time.time() + poll_s
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            requests.post(f"{MAILG_API}/api/accounts/{em}/messages/refresh", headers=H, timeout=10)
            msgs = requests.get(f"{MAILG_API}/api/accounts/{em}/messages?limit=8", headers=H, timeout=10).json()
            _log(f"mailg inbox: {len(msgs)} threads (attempt {attempt})")
            for t in msgs:
                subj = (t.get("subject") or "")
                tsl = subj.lower()
                ts = t.get("ts", 0)
                age = round((time.time()*1000 - ts)/1000)
                fresh = ts > since_ts
                _log(f"  thread '{subj[:45]}' age={age}s fresh={fresh}")
                if not fresh: continue
                th = ("token harbor" in tsl) or ("tokenharbor" in (t.get("sender") or "").lower())
                if not th: continue
                if kind == "reset" and not ("reset" in tsl or "password" in tsl): continue
                if kind == "verify" and "verify" not in tsl: continue
                detail = requests.get(f"{MAILG_API}/api/accounts/{em}/messages/{t['thread_id']}", headers=H, timeout=10).json()
                html = detail[0].get("body_html", "") if isinstance(detail, list) and detail else ""
                links = [l.replace("&amp;", "&") for l in re.findall(r"https?://[^\"'<>\s]+", html)
                         if "tokenharbor.ai" in l and "google.com/url" not in l]
                _log(f"  detail: html={len(html)} links={len(links)}")
                for l in links[:2]: _log(f"    link: {l[:95]}")
                out = []
                for l in links:
                    if kind == "reset" and ("type=recovery" in l or "/reset-password" in l): out.append(l)
                    if kind == "verify" and ("verify-email" in l or "type=signup" in l): out.append(l)
                if not out: out = links
                if out:
                    _log(f"  ✅ {kind} link found (attempt {attempt})")
                    return out[0], ts
        except Exception as e:
            _log(f"  mailg poll err: {e}")
        time.sleep(6)
    _log(f"⛔ mailg poll timeout ({poll_s}s) — no {kind} link")
    return "", 0

VNC = "--vnc" in __import__("sys").argv
COUNT = 1
for a in __import__("sys").argv[1:]:
    if a.isdigit(): COUNT = int(a)

# use mailg cookies + loggedlist matched counts
import sqlite3
def _warn_missing(p, env_hint):
    if not Path(p).exists():
        _log(f"⚠️ missing: {p} (set {env_hint}) — related steps will skip")
_warn_missing(COOKIE_DIR, "GMAIL_COOKIES or GMAIL_INBOX_DIR")
_warn_missing(INBOX_DB, "GMAIL_INBOX_DB or GMAIL_INBOX_DIR")

def cookie_email(name):
    try:
        db = sqlite3.connect(INBOX_DB)
        cur = db.execute("SELECT email FROM accounts WHERE cookie_file=?", (name,))
        row = cur.fetchone()
        if row: return row[0]
    except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:28] {_e}")
    return name.replace("_gmail_com.json","").replace(".json","") + "@gmail.com"

# count email list used — match mailg cookies with loggedlist
logged = set()
lf = LOGGEDMAIL
if lf.exists():
    for ln in lf.read_text().splitlines():
        if "|" in ln: logged.add(ln.split("|")[0].strip())

cookies = sorted(glob.glob(str(COOKIE_DIR/"*.json")))
done_file = BASE/"th_oauth_done.txt"
already_done = set(done_file.read_text().splitlines()) if done_file.exists() else set()

print(f"cookies {len(cookies)}  loggedmail {len(logged)}  already oauth {len(already_done)}")

from playwright.sync_api import sync_playwright
import subprocess as _sp, os, time

_VNC_OWNED = []  # Popen handles WE started (kill by PID, never bare pkill)


def _vnc_track(proc):
    try:
        _VNC_OWNED.append(proc)
    except Exception:
        pass
    return proc


def _vnc_cleanup():
    for p in list(_VNC_OWNED):
        try:
            p.terminate()
        except Exception:
            pass
    _VNC_OWNED.clear()


try:
    import atexit as _atexit
    _atexit.register(_vnc_cleanup)
except Exception:
    pass


def _default_vnc_pw():
    """Shared generated VNC fallback (env VNC_PASSWORD always wins).

    Same file/contract as th-tui: BASE/.vnc-default-pw, 0600, never logged."""
    try:
        pf = BASE / ".vnc-default-pw"
        if pf.exists():
            pw = pf.read_text().strip()
            if len(pw) >= 16:
                return pw
        import secrets as _sec
        pw = _sec.token_urlsafe(24)
        pf.write_text(pw)
        try:
            os.chmod(pf, 0o600)
        except Exception:
            pass
        return pw
    except Exception:
        return ""


def _ensure_vnc():
    # always set DISPLAY when VNC requested, regardless of WS_TH_NO_PROXY or other env
    if not VNC:
        return
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"]=":99"
    if _sp.call("pgrep -x Xvfb >/dev/null 2>&1", shell=True)!=0:
        _vnc_track(_sp.Popen(["Xvfb",":99","-screen","0","1280x900x24","-nolisten","tcp"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        for _ in range(8):
            time.sleep(1)
            if _sp.call("DISPLAY=:99 xdpyinfo >/dev/null 2>&1", shell=True)==0: break
    if VNC and _sp.call("ss -ltn 2>/dev/null | grep -q :5900", shell=True)!=0:
        # start x11vnc+websockify if missing (same as th-tui stack)
        pwf = os.environ.get("VNC_PASSWORD", "")
        if not pwf and (BASE / ".env").exists():
            try:
                for _ln in (BASE / ".env").read_text().splitlines():
                    _ln = _ln.strip()
                    if _ln.startswith("VNC_PASSWORD=") and not _ln.startswith("#"):
                        pwf = _ln.split("=", 1)[1].strip().strip("'\"")
                        break
            except Exception:
                pass
        pwf = pwf or _default_vnc_pw()
        if not pwf:
            _log("No VNC password available (set VNC_PASSWORD) — refusing empty auth")
            return
        if "\n" in pwf or "\x00" in pwf:
            _log("VNC password contains newline/NUL — refusing to store")
            return
        if not Path("/run/x11vnc-passwd").exists():
            _sp.run(["x11vnc", "-storepasswd", pwf, "/run/x11vnc-passwd"])
        _vnc_track(_sp.Popen(["x11vnc","-display",":99","-forever","-shared","-rfbauth","/run/x11vnc-passwd","-rfbport","5900"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        import shutil as _shutil
        _wsock = _shutil.which("websockify") or "/usr/local/lib/hermes-agent/venv/bin/websockify"
        _vnc_track(_sp.Popen([_wsock,"--web","/opt/noVNC","6080","127.0.0.1:5900"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        time.sleep(2)

# count protection + minus already-used TH keys (keys.txt + th_oauth_done)
import sqlite3 as _sql
_used_th=set()
for fp in [BASE/"th_oauth_done.txt"]:
    if fp.exists():
        for ln in fp.read_text().splitlines():
            if "|" in ln: _used_th.add(ln.split("|")[0].lower().strip())
            elif "@" in ln: _used_th.add(ln.split()[0].lower().strip())
# also from loggedmail already above, and skip used
available=[p for p in cookies if cookie_email(Path(p).name).lower() not in _used_th]
if len(available) < COUNT:
    print(f"[!] capping {COUNT} -> {len(available)} (minus {_used_th.__len__()} used TH keys)")
    COUNT=len(available)
    cookies=available
else:
    cookies=available
if not cookies:
    print("[!] no eligible cookies after minus used — abort (set GMAIL_COOKIES or GMAIL_INBOX_DIR if mailbox moved)")
    raise SystemExit(0)

# ---- ported from gmail-inbox run-batch.mjs: full challenge loop (phone tap / passkey /
# selfie / wizard / authenticator / TOTP / recaptcha / security-flag) — 5 min poll ----
import base64 as _b64c, hmac as _hmc, hashlib as _hsc, struct as _stc

def _totp_for(email):
    secf = SECRETS_FILE
    if not secf.exists(): return ""
    for ln in secf.read_text().splitlines():
        if ln.lower().startswith(email.lower()+"|"):
            sec = ln.split("|",1)[1].strip() if "|" in ln else ""
            if not sec: continue
            try:
                s2 = sec.strip().upper().replace(" ","")
                s2 += "=" * (-len(s2)%8) if len(s2)%8 else ""
                key = _b64c.b32decode(s2)
                msg = _stc.pack(">Q", int(time.time()//30))
                h = _hmc.new(key, msg, _hsc.sha1).digest()
                o = h[19] & 0x0f
                return f"{(_stc.unpack('>I', h[o:o+4])[0] & 0x7fffffff) % 1000000:06d}"
            except Exception as _e: print(f"[swallow {p.name if False else 'th_oauth_temp_v2.py'}] {_e}"); return ""
    return ""

def _save_2fa_secret(email, key_group):
    try:
        secf = SECRETS_FILE
        secf.parent.mkdir(parents=True, exist_ok=True)
        secret = key_group.replace(" ","").replace("-","").upper()
        lines = secf.read_text().splitlines() if secf.exists() else []
        out, seen = [], False
        for l in lines:
            if l.lower().startswith(email.lower()+"|"):
                out.append(f"{email}|{secret}"); seen = True
            else: out.append(l)
        if not seen: out.append(f"{email}|{secret}")
        secf.write_text("\n".join(out)+"\n")
        return True
    except Exception as _e: print(f"[swallow th_oauth_temp_v2.py] {_e}"); return False

def th_challenge_loop(pg, email, max_s=300):
    """Port of run-batch outcome poll: phone-tap code, passkey, selfie/phone skips,
    wizard skip, authenticator chooser, TOTP auto-fill, recaptcha, security flags.
    Returns True when page lands back on tokenharbor.ai (OAuth consent/finish)."""
    t0 = time.time()
    findingLogged = codeLogged = False
    actedOn = None; actedAt = 0
    def acted(url):
        nonlocal actedOn, actedAt
        if actedOn == url and time.time()-actedAt < 25: return True
        actedOn = url; actedAt = time.time(); time.sleep(2); return False
    def click_text(txt):
        try:
            for sel in [f"button:has-text('{txt}')", f"a:has-text('{txt}')",
                        f"span:has-text('{txt}')", f"div[role='button']:has-text('{txt}')"]:
                loc = pg.locator(sel).first
                if loc.count(): loc.click(); return True
        except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:141] {_e}")
        return False
    while time.time() - t0 < max_s:
        time.sleep(2.5)
        try: T = (pg.inner_text("body", timeout=5000) or "")[:4000]
        except Exception as _e: print(f"[swallow th_oauth_temp_v2.py body-read] {_e}"); T = ""
        st_url = pg.url or ""
        # reached Token Harbor → OAuth done on google side
        if "tokenharbor.ai" in st_url and "accounts.google.com" not in st_url:
            return True
        # Verify it's you / Check your ... → phone tap code
        if re.search(r"Verify it's you|Check your", T):
            if not findingLogged:
                print("  -> Finding text \"Verify it's you\" and \"Passkey\" and getting the code"); findingLogged = True
            m = re.search(r"(?:Click|Tap)[^0-9]{0,30}(\d{2})\b", T)
            if m and not codeLogged:
                print(f"  -> Code found! Click {m.group(1)} on your phone. Waiting...."); codeLogged = True
            elif not m and not codeLogged:
                print("  -> No text match, continuing....")
            continue
        # manual phone/QR verification → wait for human in VNC (30s, cap 3)
        if re.search(r"Verifikasi info|verify your info|phone verification|QR code|scan the QR", T):
            print("  -> Manual phone/QR verification detected — waiting 30s in VNC...")
            time.sleep(30); continue
        # reCAPTCHA → click checkbox inside anchor iframe (top document can't see cross-origin iframe)
        if re.search(r"reCAPTCHA|I'?m not a robot|Verify you are human|not a robot", T, re.I):
            print("  -> reCAPTCHA detected; attempting auto-solve...")
            try:
                clicked = False
                for _fr in pg.frames:
                    if "/anchor" in (_fr.url or "") and "recaptcha" in (_fr.url or ""):
                        if not _fr.evaluate("() => !!document.querySelector('.recaptcha-checkbox-checked')"):
                            _cb = _fr.locator(".recaptcha-checkbox-border").first
                            if _cb.count():
                                _cb.click(timeout=6000)
                                clicked = True
                        break
                if clicked: print("  -> Clicked reCAPTCHA, waiting 5s..."); time.sleep(5); continue
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:171] {_e}")
            time.sleep(4); continue
        # 2FA authenticator setup screen with key
        if re.search(r"Open your authenticator app|and this key|authenticator app", T, re.I) and "verification code" not in T.lower():
            m = re.search(r"([a-z0-9]{4}(?:[ -][a-z0-9]{4})+)", T, re.I)
            if m:
                print(f"  -> Authenticator setup key found: {m.group(1)[:12]}...")
                if _save_2fa_secret(email, m.group(1)):
                    print("  -> Saved 2FA secret; future logins auto-fill")
                code = _totp_for(email)
                try:
                    inp = pg.locator("input[type='tel'], input[autocomplete='one-time-code']").first
                    if code and inp.count():
                        inp.fill(code); time.sleep(0.3)
                        nxt = pg.locator("button:has-text('Next'), button:has-text('Verify')").first
                        if nxt.count(): nxt.click()
                        time.sleep(1.5); continue
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:188] {_e}")
            print("  -> Waiting for manual 2FA entry in VNC..."); time.sleep(3); continue
        # 2SV chooser → Google Authenticator
        if "/challenge/selection" in st_url and re.search(r"Google Authenticator|verification code from the Google Authenticator", T):
            if acted(st_url): continue
            print("  -> 2-Step Verification chooser; clicking Google Authenticator app")
            for attempt in range(5):
                time.sleep(1.5)
                try:
                    target = pg.evaluate("""(() => {
                        const opts=[...document.querySelectorAll('li, [role="option"]')].filter(x=>/Google Authenticator|verification code from the Google Authenticator/i.test((x.innerText||'').trim()) && x.offsetParent!==null && (x.innerText||'').trim().length < 120);
                        if(!opts.length) return null;
                        const li=opts[0];
                        const a=li.querySelector('a,[role="link"],[jsaction],button') || li;
                        const b=(a||li).getBoundingClientRect();
                        return {x:b.x+b.width/2, y:b.y+b.height/2};
                    })()""")
                    if target and target.get("x") is not None:
                        pg.mouse.click(target["x"], target["y"])
                        time.sleep(2.5)
                        if "/challenge/totp" in (pg.url or ""):
                            print(f"  -> Navigated to TOTP challenge (attempt {attempt+1})"); break
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py loop] {_e}"); break
            continue
        # recovery phone/email prompt → cancel
        if re.search(r"Enter phone|Add a recovery phone|recovery email|Make sure you can always sign in", T):
            print('  -> Checking for "Take selfie", "Home", or "Phone number"')
            if acted(st_url): continue
            cn = click_text("Cancel") or click_text("not now")
            if cn: print("  -> Found! Clicking cancel")
            continue
        # selfie → not now
        if re.search(r"Selfie", T):
            print('  -> Checking for "Take selfie", "Home", or "Phone number"')
            if acted(st_url):
                print("  -> Selfie screen: clicked once, waiting 25s…"); time.sleep(25)
            else:
                print("  -> Found! Clicking not now (manual ok in VNC if video selfie)")
                click_text("not now") or click_text("Skip") or click_text("Done") or click_text("No thanks")
            continue
        # phone number prompt → cancel
        if re.search(r"Enter your phone number|Phone number", T) and "Phone number:" not in T:
            print('  -> Checking for "Take selfie", "Home", or "Phone number"')
            print("  -> Found! Clicking cancel")
            click_text("cancel") or click_text("Skip") or click_text("Not now"); time.sleep(1.5)
            continue
        # Home / wizard screens → skip
        if re.search(r"^Home|Home\b", (pg.title() or "")) or re.search(r"Save your password|Welcome", T):
            cn = click_text("not now") or click_text("Skip") or click_text("Done") or click_text("No thanks")
            if cn: print("  -> Found! Clicking skip/not now")
            time.sleep(1.5); continue
        # post-verification onboarding wizard catch-all
        if re.search(r"recovery|protect your account|google one|set up|profile|personalize|recovery phone|recovery email|add.*phone|add.*email", T) and re.search(r"Skip|Done|Not now|Later|No thanks", T):
            if acted(st_url): continue
            print("  -> Post-verification onboarding wizard; clicking skip...")
            click_text("Skip") or click_text("Not now") or click_text("Done") or click_text("No thanks") or click_text("I'll do this later")
            time.sleep(1.5); continue
        # code entry screens
        if re.search(r"Enter the code|Enter code|one-time-code|verification code|Enter security code|Get a code to sign in|g\.co/sc", T):
            if re.search(r"Get a code to sign in|g\.co/sc", T):
                print("  -> g.co/sc screen detected; switching to authenticator method")
                if click_text("Try another way"): time.sleep(2.5)
                try:
                    picked = pg.evaluate("""(() => {
                        const opts=[...document.querySelectorAll('li, [role="option"]')].filter(x=>/authenticator app|Enter code from your authenticator|Google Authenticator/i.test((x.innerText||'').trim()) && x.offsetParent!==null && (x.innerText||'').trim().length < 120);
                        if(!opts.length) return null;
                        const li=opts[0];
                        const a=li.querySelector('a,[role="link"],[jsaction],button') || li;
                        a.click(); return true;
                    })()""")
                    if picked: print("  -> Selected authenticator app; waiting for TOTP screen"); time.sleep(3)
                    else: click_text("Try another way"); time.sleep(2.5)
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:260] {_e}")
                continue
            code = _totp_for(email)
            if code:
                try:
                    inp = pg.locator("input[type='tel'], input[autocomplete='one-time-code'], input[name*='code']").first
                    if inp.count():
                        inp.fill(code); time.sleep(0.3)
                        nxt = pg.locator("button:has-text('Next'), button:has-text('Verify'), button:has-text('Continue')").first
                        if nxt.count(): nxt.click()
                        print("  -> Auto-filled code (6-digit)")
                        time.sleep(2); continue
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:272] {_e}")
            print("  -> No text match, continuing...."); continue
        # wrong password / bad creds
        if re.search(r"password was incorrect|couldn't sign you in|couldn't find your google account|Wrong password", T):
            print("  -> Wrong password / bad creds detected, skipping."); return False
        # security flag / blocked → wait for human (VNC)
        if re.search(r"This browser or app may not be secure|Sign in blocked|Access blocked|Account disabled", T):
            print("  -> Security flag / access blocked — waiting 30s for human in VNC")
            time.sleep(30); continue
    print("  -> challenge loop timeout (5min)"); return False

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
                _log(f"bundled chromium launch fail: {str(e)[:80]} — trying channel=chrome")
    except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:bundled] {_e}")
    # 2. channel="chrome"
    try:
        return pw.chromium.launch(channel="chrome", headless=headless, args=args)
    except Exception as e:
        _log(f"channel=chrome launch fail: {str(e)[:80]} — trying system chrome")
    # 3. absolute system path via shutil.which
    for _cand in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        _p = shutil.which(_cand)
        if _p:
            try:
                return pw.chromium.launch(executable_path=_p, headless=headless, args=args)
            except Exception as e:
                _log(f"system chrome {_p} launch fail: {str(e)[:80]}")
                continue
    raise SystemExit("no usable chromium found (bundled chromium missing, channel=chrome unavailable, no google-chrome/chromium in PATH)")

_ensure_vnc()
success = 0
with sync_playwright() as pw:
    b = _launch_browser(pw, headless=not VNC,
                        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
    for path in cookies:
        if success >= COUNT: break
        cf = Path(path)
        email = cookie_for = cookie_email(cf.name)
        if email in already_done:  # only skip TH oauth done, not all mailg logged
            continue
        raw = json.loads(cf.read_text())
        g_cookies = []
        for c in raw:
            dom = c.get("domain","")
            if "google.com" not in dom and "youtube.com" not in dom: continue
            g_cookies.append({"name":c["name"],"value":c["value"],"domain":dom,"path":c.get("path","/"),"secure":bool(c.get("secure",True)),"httpOnly":bool(c.get("httpOnly",False)),"sameSite":c.get("sameSite","Lax")})
        ctx = None
        try:
            ctx = b.new_context(viewport={"width":1280,"height":800})
            pg = ctx.new_page()
            ctx.add_cookies(g_cookies)
            print(f"\n--- [{success+1}/{COUNT}] {email} ---")
            pg.goto("https://tokenharbor.ai/login?mode=signup", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            # verify google session alive — else skip (count mismatch cause)
            body = (pg.inner_text("body", timeout=5000) or "")[:200]
            # click Google via site button (real oauth params, cookie-anchored II
            gbtn = pg.locator("button[aria-label='Continue with Google']").first
            gbtn.wait_for(state="visible", timeout=10000)
            gbtn.click()
            time.sleep(7)
            url = pg.url or ""
            print(f"  after click: {url[:120]}")
            # challenge after Google button — full run-batch ported loop
            if "accounts.google.com" in url:
                # handle account chooser first
                if "accountchooser" in url:
                    clicked = pg.evaluate("() => { const el=document.querySelector('div[data-email]'); if(el){el.click(); return el.getAttribute('data-email');} return null; }")
                    print(f"  chooser {clicked}"); time.sleep(6)
                # then run full challenge loop (phone tap / passkey / selfie / wizard / TOTP / recaptcha)
                ok = th_challenge_loop(pg, email)
                if not ok:
                    print("  ⛔ challenge not resolved — skip this account")
                    ctx.close(); continue
            body = (pg.inner_text("body", timeout=5000) or "")[:400]
            # screenshot on challenge/fallback
            def _shot(suf):
                try: pg.screenshot(path=f"/tmp/th_fail_{email.split('@')[0]}_{suf}.png", full_page=True)
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:331] {_e}")
            # CONSENT — unconditional, Google uses "will allow ... to access this info"
            for sel in ["button:has-text('Allow')","button:has-text('Continue')","button:has-text('Izinkan')","button:has-text('Lanjutkan')","input[type='submit']"]:
                try:
                    loc = pg.locator(sel).first
                    if loc.count():
                        loc.click(); print(f"  consent {sel}"); time.sleep(7); break
                except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:338] {_e}")
            # after OAuth the site redirects to /auth/callback → sets session
            _shot("consent")
            time.sleep(4)
            # finish page — humanizer click + verification + free model + keys
            try:
                import importlib.util as _iu2
                _hz = _iu2.module_from_spec(_iu2.spec_from_file_location("hz2", str(BASE/"humanize.py")))
                _iu2.spec_from_file_location("hz2", str(BASE/"humanize.py")).loader.exec_module(_hz)
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py humanize] {_e}"); _hz = None
            # handle "Finish and start chatting" / "One more step"
            try:
                finish_btn = pg.locator("button:has-text('Finish and start chatting')").first
                if finish_btn.count():
                    print("  finishing setup...")
                    (_hz.human_click(pg, finish_btn) if _hz else finish_btn.click())
                    time.sleep(8)
                    print(f"  after finish: {(pg.url or '')[:100]}")
            except Exception as e: print(f"  finish click fail: {str(e)[:60]}")
            # CONFIRM actually logged into TH (dashboard) before clearing browser
            try:
                pg.goto("https://tokenharbor.ai/dashboard", wait_until="domcontentloaded", timeout=25000)
                time.sleep(3)
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:361] {_e}")
            dash_body = (pg.inner_text("body", timeout=5000) or "")[:400]
            dash_ok = ("balance" in dash_body.lower() or "overview" in dash_body.lower()
                       or "api key" in dash_body.lower() or "tokens" in dash_body.lower()
                       or "dashboard" in (pg.url or "").lower())
            if not dash_ok:
                print("  ⛔ not logged into TH after OAuth — skip account")
                pg.screenshot(path=f"/tmp/th_not_logged_{email.split('@')[0]}.png", full_page=True)
                ctx.close(); continue
            print(f"  ✅ TH dashboard confirmed — logged in")
            # CLEAR BROWSER — logout/clear cookies before forgot-password flow
            try:
                ctx.clear_cookies()
                pg.goto("https://tokenharbor.ai/forgot-password", wait_until="domcontentloaded", timeout=25000)
                print("  cleared browser → forgot-password")
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:376] {_e}")
            time.sleep(3)
            # need password from config.json
            import json as _js_cfg
            password = ""
            try:
                _cfg = json.loads((BASE/"config.json").read_text()) if (BASE/"config.json").exists() else {}
                password = _cfg.get("account_password","") or ""
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:384] {_e}")
            if not password:
                import secrets as _sec, string as _str
                password = ''.join(_sec.choice(_str.ascii_letters+_str.digits) for _ in range(12)) + "A1!"
            print(f"  config password: {'***' if password else 'none'}")
            # forgot-password → reset link from mailg → set password
            try:
                email_input = pg.locator("input[type='email'], input[name='email']").first
                if email_input.count():
                    email_input.fill(email)
                    time.sleep(1)
                    submit = pg.locator("button[type='submit'], button:has-text('Reset'), button:has-text('Send')").first
                    has_ratelimit = lambda txt: any(p in txt.lower() for p in ["rate limit","too many","try again later","429","wait a moment","exceeded"])
                    if submit.count():
                        (_hz.human_click(pg, submit) if _hz else submit.click())
                        print("    forgot-password submitted")
                        time.sleep(3)
                        # check ratelimit / banner after submit
                        post_body = (pg.inner_text("body", timeout=3000) or "")[:400]
                        if has_ratelimit(post_body):
                            print("    ⛔ ratelimit after forgot-password — skip")
                            pg.screenshot(path=f"/tmp/th_ratelimit_{email.split('@')[0]}.png", full_page=True)
                            raise SystemExit("ratelimit")
                        # "Check your email — If an account exists..." is success
                        if "check your email" in post_body.lower() or "we just sent" in post_body.lower():
                            print("    ✅ Check your email — reset link sent")
                        time.sleep(1)
                # poll mailg for reset link (via mailg API, full logs + delay)
                since = int(time.time() * 1000)
                reset_url, _ts = _mailg_link(email, "reset", since_ts=since, poll_s=120)
                if reset_url:
                    print(f"    ✅ reset link: {reset_url[:80]}...")
                    pg.goto(reset_url, wait_until="domcontentloaded", timeout=25000)
                    time.sleep(4)
                    pw_inputs = pg.locator("input[type='password'], input[name*='password'], input[name*='new']")
                    for i in range(min(2, pw_inputs.count())):
                        pw_inputs.nth(i).fill(password)
                        time.sleep(0.5)
                    save_btn = pg.locator("button:has-text('Reset'), button:has-text('Save'), button:has-text('Update'), button:has-text('Confirm')").first
                    if save_btn.count():
                        (_hz.human_click(pg, save_btn) if _hz else save_btn.click())
                        time.sleep(5)
                        print(f"    ✅ password set — already logged in from reset")
                    else:
                        print("    ⚠️ no save button on reset page")
                else:
                    print("    ⚠️ no reset email within 120s")
            except Exception as e: print(f"  forgot-password fail: {str(e)[:80]}")
            # already logged in from reset → enable free model
            try:
                pg.goto("https://tokenharbor.ai/dashboard", wait_until="domcontentloaded", timeout=25000)
                time.sleep(4)
                free_btn = pg.locator("button:has-text('Enable free models'), button:has-text('Enable')").first
                if free_btn.count():
                    print("  enabling free models...")
                    (_hz.human_click(pg, free_btn) if _hz else free_btn.click())
                    time.sleep(5)
                    print(f"    free model: {(pg.url or '')[:80]} — ✅ enabled")
            except Exception as e: print(f"  free model fail: {str(e)[:60]}")
            # verify via mailg (via mailg API, full logs + delay)
            try:
                body_dash = (pg.inner_text("body", timeout=5000) or "")[:800]
                if "verify" in body_dash.lower() and "email" in body_dash.lower():
                    print("  verify banner — resend + poll mailg")
                    for sel in ["button:has-text('Verify')","button:has-text('Resend')","a:has-text('Verify')","a:has-text('Resend')"]:
                        loc = pg.locator(sel).first
                        if loc.count():
                            (_hz.human_click(pg, loc) if _hz else loc.click())
                            print(f"    clicked {sel}"); time.sleep(3); break
                    since = int(time.time() * 1000)
                    vlink, _ts = _mailg_link(email, "verify", since_ts=since, poll_s=120)
                    if vlink:
                        print(f"    ✅ verify link: {vlink[:80]}...")
                        pg.goto(vlink, wait_until="domcontentloaded", timeout=25000)
                        time.sleep(4)
                        try: pg.goto("https://tokenharbor.ai/dashboard", wait_until="domcontentloaded", timeout=25000)
                        except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:post-verify] {_e}")
                        time.sleep(3)
                        print("    ✅ verified")
                    else:
                        print("    ⚠️ no verify link within 120s")
                else:
                    print("  no verify banner — already verified")
            except Exception as e: print(f"  verify fail: {str(e)[:60]}")
            # get apikey
            api_key = ""
            try:
                pg.goto("https://tokenharbor.ai/dashboard/api-keys", wait_until="domcontentloaded", timeout=25000)
                time.sleep(4)
                body_keys = (pg.inner_text("body", timeout=5000) or "")
                import re as _re3
                m = _re3.search(r"thk_[a-zA-Z0-9_-]{20,}", body_keys)
                if m:
                    api_key = m.group(0)
                    print(f"  existing key: {api_key[:14]}...")
                else:
                    new_btn = pg.locator("button:has-text('+ New key'), button:has-text('New key'), button:has-text('Create key')").first
                    if new_btn.count():
                        (_hz.human_click(pg, new_btn) if _hz else new_btn.click())
                        time.sleep(2)
                        label_input = pg.locator("input[placeholder*='label'], input[name*='label'], input[type='text']").first
                        if label_input.count():
                            label_input.fill("main")
                            time.sleep(1)
                        create_btn = pg.locator("button:has-text('Create key'), button:has-text('Create')").last
                        if create_btn.count():
                            (_hz.human_click(pg, create_btn) if _hz else create_btn.click())
                            time.sleep(4)
                        body_keys2 = (pg.inner_text("body", timeout=5000) or "")
                        m2 = _re3.search(r"thk_[a-zA-Z0-9_-]{20,}", body_keys2)
                        if m2: api_key = m2.group(0)
                        print(f"  new key: {api_key[:14] if api_key else 'none'}...")
            except Exception as e: print(f"  key creation fail: {str(e)[:80]}")
            final = pg.url or ""
            m = re.search(r"[?&]code=([^&]+)", final)
            if m or "dashboard" in final.lower():
                # keystore PRIMARY (JSON source of truth); txt below stays as mirror
                try:
                    import keystore as _ksmod
                    _ksmod.store().upsert(email, password=password, api_key=api_key)
                except Exception as e:
                    print(f"  keystore save fail: {str(e)[:60]}")
                # save to keys.txt: email|password|api_key
                try:
                    kf = BASE / "data" / "keys.txt"
                    kf.parent.mkdir(parents=True, exist_ok=True)
                    # dedupe
                    existing = set(l.split("|")[0].lower() for l in kf.read_text().splitlines() if "|" in l) if kf.exists() else set()
                    if email.lower() not in existing:
                        kf.open("a").write(f"{email}|{password}|{api_key}\n")
                        print(f"  ✅ saved to keys.txt: {email} | *** | {api_key[:14] if api_key else 'no-key'}...")
                    else:
                        print(f"  already in keys.txt: {email}")
                    # always check key via relay proxy
                    if api_key:
                        try:
                            import requests as _rq3
                            _relay = "https://vercel-relay-1j23u4mq1-konaimav2s-projects.vercel.app"
                            # find any relays from proxy.txt
                            _relays = [l.strip() for l in (BASE/"proxy/proxy.txt").read_text().splitlines() if l.strip().startswith("relay://")]
                            _relay_url = _relays[0].replace("relay://","").rstrip(":443") if _relays else _relay
                            _r = _rq3.post(_relay_url, headers={"x-relay-target": "https://tokenharbor.ai", "x-relay-path": "/v1/chat/completions", "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json={"model": "deepseek-v4-flash:free", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}, timeout=20)
                            print(f"  relay check: {_r.status_code} {'✅ LIVE' if _r.ok else _r.text[:80]}")
                        except Exception as e: print(f"  relay check fail: {str(e)[:60]}")
                except Exception as e: print(f"  keys.txt save fail: {str(e)[:60]}")
                if api_key:
                    success += 1
                    already_done.add(email)
                    done_file.open("a").write(email + "\n")
                else:
                    print("  ⚠️ no key — not marking done (will retry)")
                code = m.group(1) if m else "dashboard"
                print(f"  ✅ code {code[:20]}...")
        except (KeyboardInterrupt, SystemExit):
            # SystemExit (ratelimit) / KeyboardInterrupt must still release the browser
            try: b.close()
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:578] {_e}")
            raise
        except Exception as e:
            print(f"  err {str(e)[:80]}")
        finally:
            try:
                if ctx is not None: ctx.close()
            except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:578] {_e}")
    try: b.close()
    except Exception as _e: print(f"[swallow th_oauth_temp_v2.py:578] {_e}")
print(f"\nDone: {success}/{COUNT}")
