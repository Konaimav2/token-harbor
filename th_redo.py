#!/usr/bin/env python3
"""th_redo.py — redo failed TH Google OAuth accounts to completion.

For each account in --accounts (default: the 2 oauth ones):
  1. Signin TH with config.json password (reset already set it)
  2. If signin fails → fresh forgot-password → read FRESH reset link from gmail mailbox (browser, run-batch style) → set password
  3. Dashboard check → verify email if banner (fresh verify link from mailbox)
  4. Enable free models
  5. Create/capture API key
  6. Save email|password|apikey to data/keys.txt
  7. Test key via relay (x-relay-target full URL) AND direct fallback

Usage:
  python3 th_redo.py                # both default accounts
  python3 th_redo.py --vnc          # headed on :99
  python3 th_redo.py --accounts a@gmail.com,b@gmail.com
"""
import json, time, re, os, sys
from pathlib import Path
os.environ.setdefault("DISPLAY", ":99")
BASE = Path(__file__).resolve().parent
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
SECRETS_FILE = _gmail_path("GMAIL_2FA_SECRETS", "data/.2fa-secrets")
RELAY = "https://vercel-relay-1j23u4mq1-konaimav2s-projects.vercel.app"
MAILG_API = "http://127.0.0.1:8790"
VNC = "--vnc" in sys.argv
PW = json.loads((BASE / "config.json").read_text()).get("account_password", "")

def parse_accounts():
    for a in sys.argv[1:]:
        if a.startswith("--accounts"):
            idx = sys.argv.index(a)
            if idx + 1 < len(sys.argv):
                return [x.strip() for x in sys.argv[idx+1].split(",") if "@" in x]
    # default: oauth accounts that DON'T have a key yet in keys.txt (done ones skipped)
    done = BASE / "th_oauth_done.txt"
    kf = BASE / "data" / "keys.txt"
    have_key = set()
    if kf.exists():
        for l in kf.read_text().splitlines():
            parts = l.split("|")
            if len(parts) >= 3 and parts[2].strip():
                have_key.add(parts[0].lower())
    todo = []
    if done.exists():
        for l in done.read_text().splitlines():
            if "@" in l and l.strip().lower() not in have_key:
                todo.append(l.strip())
    if not todo:
        print("nothing to redo — all oauth accounts already have keys (pass --accounts e1@gmail.com,e2@gmail.com to force)")
    return todo

def cookie_file_for(email):
    if not COOKIE_DIR.exists():
        print(f"[warn] cookie dir missing: {COOKIE_DIR} (set GMAIL_COOKIES or GMAIL_INBOX_DIR)")
    try:
        import sqlite3
        db = sqlite3.connect(INBOX_DB)
        row = db.execute("SELECT cookie_file FROM accounts WHERE email=?", (email,)).fetchone()
        if row: return COOKIE_DIR / row[0]
    except Exception as _e: print(f"[swallow th_redo.py:47] {_e}")
    return COOKIE_DIR / (email.split("@")[0].replace(".", "_") + "_gmail_com.json")

def _mailg_key():
    import sqlite3
    if not Path(INBOX_DB).exists():
        raise FileNotFoundError(f"mailg DB missing: {INBOX_DB} (set GMAIL_INBOX_DB or GMAIL_INBOX_DIR)")
    db = sqlite3.connect(INBOX_DB)
    return db.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]

def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)

# ---- FULL challenge handling (ported from run-batch + th_oauth_temp_v2) ----
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
            except Exception as _e: print(f"[swallow {p.name if False else 'th_redo.py'}] {_e}"); return ""
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
        try:
            os.chmod(secf, 0o600)  # TOTP secrets: owner-only
        except Exception: pass
        return True
    except Exception as _e: print(f"[swallow th_redo.py] {_e}"); return False

def th_challenge_loop(pg, email, max_s=300):
    """Full Google challenge handling. Returns True when lands on tokenharbor.ai."""
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
        except Exception as _e: print(f"[swallow th_redo.py:110] {_e}")
        return False
    while time.time() - t0 < max_s:
        time.sleep(2.5)
        try: T = (pg.inner_text("body", timeout=5000) or "")[:4000]
        except Exception as _e: print(f"[swallow th_redo.py:115] {_e}"); T = ""
        st_url = pg.url or ""
        # OUTCOME: reached tokenharbor
        if "tokenharbor.ai" in st_url and "accounts.google.com" not in st_url:
            _log("challenge loop → landed on tokenharbor ✅")
            return True
        # Verify it's you → phone tap code
        if re.search(r"Verify it's you|Check your", T):
            if not findingLogged:
                _log('challenge: "Verify it\'s you" — getting phone code'); findingLogged = True
            m = re.search(r"(?:Click|Tap)[^0-9]{0,30}(\d{2})\b", T)
            if m and not codeLogged:
                _log(f"Code found! Click {m.group(1)} on your phone. Waiting...."); codeLogged = True
            elif not m and not codeLogged:
                _log("no code text yet, continuing")
            continue
        # manual phone/QR → human in VNC
        if re.search(r"Verifikasi info|verify your info|phone verification|QR code|scan the QR", T):
            _log("manual phone/QR verification — waiting 30s in VNC (human must scan)")
            time.sleep(30); continue
        # reCAPTCHA → click checkbox inside anchor iframe (top document can't see cross-origin iframe)
        if re.search(r"reCAPTCHA|I'?m not a robot|Verify you are human|not a robot", T, re.I):
            _log("reCAPTCHA detected — auto-click checkbox")
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
                if clicked: _log("clicked reCAPTCHA"); time.sleep(5); continue
            except Exception as _e: print(f"[swallow th_redo.py:141] {_e}")
            time.sleep(4); continue
        # authenticator SETUP screen (has key)
        if re.search(r"Open your authenticator app|and this key|authenticator app", T, re.I) and "verification code" not in T.lower():
            m = re.search(r"([a-z0-9]{4}(?:[ -][a-z0-9]{4})+)", T, re.I)
            if m:
                _log(f"authenticator setup key: {m.group(1)[:12]}...")
                if _save_2fa_secret(email, m.group(1)): _log("saved 2FA secret to .2fa-secrets")
                code = _totp_for(email)
                try:
                    inp = pg.locator("input[type='tel'], input[autocomplete='one-time-code']").first
                    if code and inp.count():
                        inp.fill(code); time.sleep(0.3)
                        nxt = pg.locator("button:has-text('Next'), button:has-text('Verify')").first
                        if nxt.count(): nxt.click()
                        _log("auto-filled TOTP (6-digit)"); time.sleep(1.5); continue
                except Exception as _e: print(f"[swallow th_redo.py:157] {_e}")
            _log("waiting for manual 2FA in VNC..."); time.sleep(3); continue
        # 2SV chooser → Google Authenticator
        if "/challenge/selection" in st_url and re.search(r"Google Authenticator|verification code from the Google Authenticator", T):
            if acted(st_url): continue
            _log("2SV chooser → clicking Google Authenticator")
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
                            _log(f"navigated to TOTP (attempt {attempt+1})"); break
                except Exception as _e: print(f"[swallow th_redo.py:179] {_e}"); break
            continue
        # recovery phone/email → cancel
        if re.search(r"Enter phone|Add a recovery phone|recovery email|Make sure you can always sign in", T):
            _log('recovery phone/email prompt — clicking Cancel')
            if acted(st_url): continue
            click_text("Cancel") or click_text("not now")
            continue
        # selfie → not now
        if re.search(r"Selfie", T):
            _log("selfie screen — not now / skip")
            if acted(st_url): time.sleep(25)
            else: click_text("not now") or click_text("Skip") or click_text("Done") or click_text("No thanks")
            continue
        # phone number prompt → cancel
        if re.search(r"Enter your phone number|Phone number", T) and "Phone number:" not in T:
            _log("phone number prompt — cancel")
            click_text("cancel") or click_text("Skip") or click_text("Not now"); time.sleep(1.5)
            continue
        # Home / wizard → skip
        if re.search(r"^Home|Home\b", (pg.title() or "")) or re.search(r"Save your password|Welcome", T):
            cn = click_text("not now") or click_text("Skip") or click_text("Done") or click_text("No thanks")
            if cn: _log("Home/wizard — skip clicked")
            time.sleep(1.5); continue
        # post-verification onboarding wizard
        if re.search(r"recovery|protect your account|google one|set up|profile|personalize|recovery phone|recovery email|add.*phone|add.*email", T) and re.search(r"Skip|Done|Not now|Later|No thanks", T):
            if acted(st_url): continue
            _log("onboarding wizard — skip")
            click_text("Skip") or click_text("Not now") or click_text("Done") or click_text("No thanks") or click_text("I'll do this later")
            time.sleep(1.5); continue
        # code entry screens
        if re.search(r"Enter the code|Enter code|one-time-code|verification code|Enter security code|Get a code to sign in|g\.co/sc", T):
            if re.search(r"Get a code to sign in|g\.co/sc", T):
                _log("g.co/sc → switch to authenticator method")
                if click_text("Try another way"): time.sleep(2.5)
                try:
                    picked = pg.evaluate("""(() => {
                        const opts=[...document.querySelectorAll('li, [role="option"]')].filter(x=>/authenticator app|Enter code from your authenticator|Google Authenticator/i.test((x.innerText||'').trim()) && x.offsetParent!==null && (x.innerText||'').trim().length < 120);
                        if(!opts.length) return null;
                        const li=opts[0];
                        const a=li.querySelector('a,[role="link"],[jsaction],button') || li;
                        a.click(); return true;
                    })()""")
                    if picked: _log("selected authenticator app"); time.sleep(3)
                    else: click_text("Try another way"); time.sleep(2.5)
                except Exception as _e: print(f"[swallow th_redo.py:224] {_e}")
                continue
            code = _totp_for(email)
            if code:
                try:
                    inp = pg.locator("input[type='tel'], input[autocomplete='one-time-code'], input[name*='code']").first
                    if inp.count():
                        inp.fill(code); time.sleep(0.3)
                        nxt = pg.locator("button:has-text('Next'), button:has-text('Verify'), button:has-text('Continue')").first
                        if nxt.count(): nxt.click()
                        _log("auto-filled TOTP (6-digit)"); time.sleep(2); continue
                except Exception as _e: print(f"[swallow th_redo.py:235] {_e}")
            _log("code screen but no TOTP secret — waiting in VNC"); time.sleep(5); continue
        # OAuth consent (app wants Google profile/email) → Allow/Izinkan.
        # Without this the loop times out ON the consent page (post-loop consent
        # code is unreachable). Next pass lands on tokenharbor.ai via OUTCOME.
        if "accounts.google.com" in st_url and ("consent" in st_url or re.search(r"ingin mengakses|wants to access|would like to (access|see)|access your|lihat.*email|mengaitkan", T, re.I)):
            if click_text("Izinkan") or click_text("Allow") or click_text("Continue") or click_text("Lanjutkan"):
                _log("oauth consent → allowed"); time.sleep(6); continue
            _log("oauth consent screen — allow button not found, retrying"); time.sleep(3); continue
        # wrong creds
        if re.search(r"password was incorrect|couldn't sign you in|couldn't find your google account|Wrong password", T):
            _log("wrong password / bad creds → fail"); return False
        # security flag → human
        if re.search(r"This browser or app may not be secure|Sign in blocked|Access blocked|Account disabled", T):
            _log("security flag / blocked — waiting 30s (check VNC)")
            time.sleep(30); continue
        # email already exists on TH (signup page says already)
        if "already on board" in T.lower() or "already registered" in T.lower():
            _log("email already registered on TH"); return "already"
    _log("challenge loop TIMEOUT 5min"); return False


def mailg_links(email, kind, since_ts=0, poll_s=90):
    """mailg API: newest TH links of kind (verify|reset) from body_html. Polls until fresh link arrives. FULL LOGS."""
    import requests, urllib.parse
    try:
        key = _mailg_key()
    except Exception as e:
        _log(f"mailg poll skipped: {e}")
        return "", 0
    H = {"X-API-Key": key}
    em = urllib.parse.quote(email)
    deadline = time.time() + poll_s
    _log(f"mailg poll start: kind={kind} since_ts={since_ts} poll_s={poll_s}")
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r1 = requests.post(f"{MAILG_API}/api/accounts/{em}/messages/refresh", headers=H, timeout=10)
            _log(f"refresh: {r1.status_code}")
            msgs = requests.get(f"{MAILG_API}/api/accounts/{em}/messages?limit=8", headers=H, timeout=10).json()
            _log(f"inbox: {len(msgs)} threads")
            for t in msgs:
                subj = (t.get("subject") or "")
                tsl = subj.lower()
                ts = t.get("ts", 0)
                age = round((time.time()*1000 - ts) / 1000)
                fresh = ts > since_ts
                _log(f"  thread: '{subj[:50]}' ts={ts} age={age}s fresh={fresh}")
                if not fresh: continue
                want = ("verify" in tsl) if kind == "verify" else ("reset" in tsl or "password" in tsl)
                th_match = ("token harbor" in tsl) or ("tokenharbor" in (t.get("sender") or "").lower())
                _log(f"  match: want={want} th={th_match}")
                if not want or not th_match: continue
                detail = requests.get(f"{MAILG_API}/api/accounts/{em}/messages/{t['thread_id']}", headers=H, timeout=10).json()
                html = detail[0].get("body_html", "") if isinstance(detail, list) and detail else ""
                links = [l.replace("&amp;", "&") for l in re.findall(r"https?://[^\"'<>\s]+", html) if "tokenharbor.ai" in l and "google.com/url" not in l]
                _log(f"  detail: html={len(html)} links={len(links)}")
                for l in links[:3]: _log(f"    link: {l[:100]}")
                out = []
                for l in links:
                    if kind == "reset" and ("type=recovery" in l or "/reset-password" in l or "reset" in l.lower()): out.append(l)
                    if kind == "verify" and ("verify-email" in l or "type=signup" in l): out.append(l)
                if not out: out = links
                if out:
                    _log(f"  ✅ FOUND {kind} link (attempt {attempt})")
                    return out[0], ts
        except Exception as e:
            _log(f"  err: {str(e)[:80]}")
        time.sleep(6)
    _log(f"⛔ mailg poll timeout after {attempt} attempts ({poll_s}s)")
    return "", 0


def mailbox_open(pg):
    pg.goto("https://mail.google.com/mail/u/0/h/?v=l", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    # category tabs (Utama/Promosi/Sosial) — MUST switch to Utama (Primary) or rows are tabs, not messages
    try:
        tab = pg.locator("text=Utama").first
        if tab.count(): tab.click(); time.sleep(3)
    except Exception as _e: print(f"[swallow th_redo.py:305] {_e}")

def find_th_links(pg, kind):
    """Open TH message rows NEWEST-first, return links matching kind: verify|reset|any."""
    global _tried_rows
    _tried_rows = set()
    out = []
    tried = 0
    for attempt in range(10):
        if tried >= 4 or out: break
        rows = pg.locator("tr")
        # collect candidate indices fresh each pass
        cand = []
        for i in range(rows.count()):
            try:
                tl = rows.nth(i).inner_text(timeout=2000).lower()
            except Exception as _e:
                print(f"[swallow th_redo.py:321] {_e}")
                continue
            if "token harbor" not in tl: continue
            if kind == "verify" and "verify" not in tl: continue
            if kind == "reset" and "reset" not in tl and "password" not in tl: continue
            cand.append(i)
        # newest first = smallest index not yet tried
        target = None
        for i in cand:
            if i not in _tried_rows: target = i; break
        if target is None: break
        _tried_rows.add(target)
        tried += 1
        try:
            rows.nth(target).click(); time.sleep(4)
            # extract links from HREF attributes (verify email uses buttons, not plain text)
            hrefs = pg.evaluate("(() => [...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => h.includes('tokenharbor')))()")
            links = [l.replace("&amp;", "&") for l in hrefs]
            # plus plain-text links (reset email pastes URL as text)
            mb = pg.inner_text("body", timeout=8000)
            links += [l.replace("&amp;", "&") for l in re.findall(r"https?://[^\s\"'<>]+", mb) if "tokenharbor.ai" in l.lower()]
            links = list(dict.fromkeys(links))
            if kind == "verify":
                out += [l for l in links if "type=signup" in l or ("auth/v1/verify" in l and "type=recovery" not in l)]
            elif kind == "reset":
                out += [l for l in links if "type=recovery" in l]
            else:
                out += links
            pg.go_back(wait_until="domcontentloaded"); time.sleep(3)
        except Exception:
            try: pg.go_back(wait_until="domcontentloaded"); time.sleep(2)
            except Exception as _e: print(f"[swallow th_redo.py:351] {_e}")
    return out

_tried_rows = set()

def is_logged(pg):
    try: body = pg.inner_text("body", timeout=6000)[:500].lower()
    except Exception as _e: print(f"[swallow th_redo.py] {_e}"); return False
    return any(k in body for k in ["balance", "overview", "api key", "tokens"]) or "dashboard" in (pg.url or "")

def process_account(ctx, pg_mail, pg_th, email):
    print(f"\n=== {email} ===")
    # 1. OAuth via Google (cookies injected) — password later
    cf = cookie_file_for(email)
    if not cf.exists():
        print(f"  ⛔ cookie file missing: {cf} (set GMAIL_COOKIES or GMAIL_INBOX_DIR) — skip account"); return None
    raw = json.loads(cf.read_text())
    g = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/"),
          "secure": bool(c.get("secure", True)), "httpOnly": bool(c.get("httpOnly", False)),
          "sameSite": c.get("sameSite", "Lax")}
         for c in raw if "google.com" in c.get("domain", "") or "youtube.com" in c.get("domain", "")]
    pg_th.context.add_cookies(g)
    pg_th.goto("https://tokenharbor.ai/login?mode=signup", wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    gbtn = pg_th.locator("button[aria-label='Continue with Google']").first
    if not gbtn.count():
        print("  ⛔ no Google button"); return None
    gbtn.click(); time.sleep(7)
    if "accountchooser" in (pg_th.url or ""):
        clicked = False
        for _ in range(3):
            try:
                tile = pg_th.locator(f'div[data-email="{email}"]').first
                if tile.count():
                    tile.click(timeout=8000); clicked = True; break
            except Exception: pass
            try:
                txt = pg_th.get_by_text(email, exact=False).first
                if txt.count():
                    txt.click(timeout=8000); clicked = True; break
            except Exception: pass
            time.sleep(2)
        _log(f"chooser clicked={clicked}")
        time.sleep(6)
    # FULL challenge loop (phone tap / passkey / selfie / wizard / TOTP / recaptcha / security)
    ok = th_challenge_loop(pg_th, email)
    if ok == "already":
        _log("email already registered — continue to dashboard")
    elif not ok:
        _log("⛔ challenge unresolved — skip account"); return None
    # consent if still on google (rare after loop)
    if "accounts.google.com" in (pg_th.url or ""):
        for sel in ["button:has-text('Continue')", "button:has-text('Allow')", "button:has-text('Izinkan')"]:
            loc = pg_th.locator(sel).first
            if loc.count(): loc.click(); _log(f"consent {sel}"); time.sleep(7); break
    fin = pg_th.locator("button:has-text('Finish and start chatting')").first
    if fin.count(): fin.click(); _log("finished setup"); time.sleep(8)
    # 2. dashboard
    try: pg_th.goto("https://tokenharbor.ai/dashboard", wait_until="domcontentloaded", timeout=25000)
    except Exception as _e: print(f"[swallow th_redo.py:395] {_e}")
    time.sleep(3)
    if not is_logged(pg_th):
        print("  ⛔ not logged in"); return None
    print("  ✅ logged in")
    # 3. verify email if banner
    body = pg_th.inner_text("body", timeout=6000)[:500].lower()
    if "verify" in body and "email" in body:
        _log("verify banner — clicking Verify/Resend")
        for sel in ["button:has-text('Verify')", "button:has-text('Resend')", "a:has-text('Verify')"]:
            loc = pg_th.locator(sel).first
            if loc.count(): loc.click(); _log(f"clicked {sel} — link sent"); time.sleep(3); break
        since = int(time.time() * 1000) - 60000
        vlink, _ = mailg_links(email, "verify", since_ts=since, poll_s=90)
        if vlink:
            print(f"  fresh verify link: {vlink[:80]}")
            pg_th.goto(vlink, wait_until="domcontentloaded", timeout=25000)
            time.sleep(4)
            try: pg_th.goto("https://tokenharbor.ai/dashboard", wait_until="domcontentloaded", timeout=25000)
            except Exception as _e: print(f"[swallow th_redo.py:414] {_e}")
            time.sleep(3)
            print("  ✅ verified")
        else:
            print("  ⚠️ no verify link found (mailg)")
    else:
        print("  no verify banner — already verified")
    # 4. free models
    fb = pg_th.locator("button:has-text('Enable free models'), button:has-text('Enable')").first
    if fb.count(): fb.click(); print("  ✅ free models enabled"); time.sleep(4)
    # 5. api key
    try: pg_th.goto("https://tokenharbor.ai/dashboard/api-keys", wait_until="domcontentloaded", timeout=25000)
    except Exception as _e: print(f"[swallow th_redo.py:426] {_e}")
    time.sleep(3)
    bk = pg_th.inner_text("body", timeout=6000)
    m = re.search(r"thk_[a-zA-Z0-9_-]{20,}", bk)
    api_key = m.group(0) if m else ""
    if not api_key:
        nb = pg_th.locator("button:has-text('New key'), button:has-text('Create key')").first
        if nb.count():
            nb.click(); time.sleep(2)
            li = pg_th.locator("input[type='text']").first
            if li.count(): li.fill("main"); time.sleep(0.5)
            cb = pg_th.locator("button:has-text('Create')").last
            if cb.count(): cb.click(); time.sleep(4)
            bk = pg_th.inner_text("body", timeout=6000)
            m = re.search(r"thk_[a-zA-Z0-9_-]{20,}", bk)
            if m: api_key = m.group(0)
    print(f"  key: {api_key[:12]}..." if api_key else "  key: NONE")
    # 5b. set a known password on the account via Supabase (OAuth session ->
    # access token -> updateUser). Makes keys.txt password REAL, not placeholder.
    if api_key:
        try:
            tok_json = pg_th.evaluate("""(() => {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k && k.startsWith('sb-') && k.endsWith('-auth-token')) return localStorage.getItem(k);
                }
                return null;
            })()""")
            access = (json.loads(tok_json).get("access_token", "") if tok_json else "")
            if access:
                import requests as _rq2
                anon = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlzYm56bXdqbXRpdWlwZXNnbW1nIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY3NjU1MzYsImV4cCI6MjA5MjM0MTUzNn0.CodUcchio6jNW_k68vaAb--LshBQXK51tZ6VTxNSz_A"
                u = _rq2.put("https://auth.tokenharbor.ai/auth/v1/user", json={"password": PW},
                             headers={"apikey": anon, "Authorization": f"Bearer {access}"}, timeout=20)
                print(f"  password set: {u.status_code}")
            else:
                print("  password set: skipped (no sb token in localStorage)")
        except Exception as e:
            print(f"  password set err: {str(e)[:100]}")
    # 6. dual-write: keystore PRIMARY (JSON source of truth), txt mirror (exact 3-col format)
    if api_key:
        try:
            import keystore as _ksmod
            _ksmod.store().upsert(email, password=PW, api_key=api_key)
        except Exception as e:
            print(f"  keystore save fail: {str(e)[:60]}")
        try:
            kf = BASE / "data" / "keys.txt"
            kf.parent.mkdir(parents=True, exist_ok=True)
            existing = set(l.split("|")[0].lower() for l in kf.read_text().splitlines() if "|" in l) if kf.exists() else set()
            if email.lower() not in existing:
                kf.open("a").write(f"{email}|{PW}|{api_key}\n")
                try:
                    os.chmod(kf, 0o600)  # password+api_key: owner-only
                except Exception: pass
                print(f"  ✅ saved to keys.txt: {email}")
            else:
                print(f"  already in keys.txt: {email}")
        except Exception as e:
            print(f"  keys.txt save fail: {str(e)[:60]}")
    return api_key

def test_key(api_key):
    """Relay first (9router format: x-relay-target base + x-relay-path), fallback direct. Model :free."""
    import requests
    body = {"model": "deepseek-v4-flash:free", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    # relay (9router correct format)
    try:
        r = requests.post(RELAY,
            headers={"x-relay-target": "https://tokenharbor.ai",  # base only, no path
                     "x-relay-path": "/v1/chat/completions",       # path+query
                     "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body, timeout=25)
        if r.status_code == 200 and "choices" in r.text:
            return f"relay:{r.status_code}:LIVE"
        if r.status_code >= 500 or "FUNCTION_INVOCATION_FAILED" in r.text[:100]:
            print(f"  relay failed ({r.status_code}), falling back direct")
        else:
            return f"relay:{r.status_code}:{r.text[:80]}"
    except Exception as e:
        print(f"  relay err: {e}")
    # direct
    try:
        r = requests.post("https://tokenharbor.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body, timeout=25)
        return f"direct:{r.status_code}:{r.text[:80]}"
    except Exception as e:
        return f"direct_err:{e}"


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
    except Exception as _e: print(f"[swallow th_redo.py:bundled] {_e}")
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


if __name__ == "__main__":
    accounts = parse_accounts()
    print(f"accounts: {accounts}")
    from playwright.sync_api import sync_playwright
    results = {}
    with sync_playwright() as pw:
        b = _launch_browser(pw, headless=not VNC,
                            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        try:
            for email in accounts:
                ctx = None
                try:
                    ctx = b.new_context(viewport={"width": 1280, "height": 900})
                    pg_th = ctx.new_page()
                    # gmail locked check via mailg API (fast, no browser)
                    import requests as _rq, urllib.parse as _up
                    try:
                        _em = _up.quote(email)
                        _mk = _mailg_key()
                        _msgs = _rq.get(f"{MAILG_API}/api/accounts/{_em}/messages?limit=1",
                                        headers={"X-API-Key": _mk}, timeout=8)
                        if _msgs.status_code != 200:
                            print(f"⛔ {email}: mailg API auth fail ({_msgs.status_code})")
                            results[email] = "mailg_auth_fail"; continue
                    except Exception as e:
                        print(f"⛔ {email}: mailg API down: {e}")
                        results[email] = "mailg_down"; continue
                    key = process_account(ctx, None, pg_th, email)
                    if key:
                        status = test_key(key)
                        print(f"  relay test: {status}")
                        results[email] = {"key": key[:18] + "...", "status": status}
                    else:
                        results[email] = "failed"
                finally:
                    # closes ctx on ALL exits incl. SystemExit/KeyboardInterrupt
                    try:
                        if ctx is not None: ctx.close()
                    except Exception as _e: print(f"[swallow th_redo.py:ctx] {_e}")
        finally:
            # closes browser on ALL exits incl. SystemExit/KeyboardInterrupt
            try: b.close()
            except Exception as _e: print(f"[swallow th_redo.py:browser] {_e}")
    print("\n=== SUMMARY ===")
    for k, v in results.items(): print(k, "->", v)


