"""Batch verify all 403 accounts: login -> click verify -> read inbox -> open link."""
import sys, os, time, re, json, importlib.util
from pathlib import Path
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "tools"))
sys.path.insert(0, str(BASE / "config"))
import enable_free_models as efm
from camoufox.addons import DefaultAddons
from camoufox.sync_api import Camoufox

spec = importlib.util.spec_from_file_location("tui", str(BASE / "th-tui.py"))
tui = importlib.util.module_from_spec(spec); sys.modules["tui"]=tui; spec.loader.exec_module(tui); tui.load_env()

# all 403 accounts (from the earlier check)
ACCOUNTS = [
    "kpmvcc69@furries.my.id", "qo2skybo@furries.my.id", "fymygf2g@furries.my.id",
    "9i28j1op@furries.my.id", "oz05e8fn@furries.my.id", "usfofujr@furries.my.id",
    "jjsomqmb@furries.my.id", "ifjuft4k@furries.my.id", "w6sn6nbc@furries.my.id",
    "814cbsom@furries.my.id", "9u9ik180@furries.my.id", "1uzgxrdo@furries.my.id",
    "lflkl1nr@furries.my.id", "zuy0nofd@furries.my.id", "me9j6kl0@furries.my.id",
    "v5aqmakz@furries.my.id", "collins1ma@arqonara.web.id",
    "kona_1@arqonara.web.id", "kona_2@arqonara.web.id",
    "testers_@modrinth.my.id", "something_@modrinth.my.id",
]

keylines = {}
_keys_file = BASE / "data" / "keys.txt"
if not _keys_file.exists():
    print(f"⚠️ keys file missing: {_keys_file} — all accounts will skip (no creds)")
for line in _keys_file.read_text().splitlines() if _keys_file.exists() else []:
    parts = line.strip().split("|")
    if len(parts) >= 3:
        keylines[parts[0]] = (parts[1], parts[2])

def open_verify_link(link):
    try:
        with Camoufox(headless=True, addons=[], exclude_addons=[DefaultAddons.UBO]) as browser:
            page = browser.new_page()
            page.goto(link, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
    except Exception as _e:
        print(f"[swallow batch_verify_403.py:36] {_e}")
        pass

def check_key(key):
    import requests
    try:
        r = requests.post("https://tokenharbor.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model":"deepseek-v4-flash:free","messages":[{"role":"user","content":"hi"}],"max_tokens":5}, timeout=60)
        return r.status_code
    except Exception as _e:
        print(f"[swallow batch_verify_403.py:47] {_e}")
        return -1

results = {}
for EMAIL in ACCOUNTS:
    if EMAIL not in keylines:
        print(f"{EMAIL}: NO creds, skip")
        continue
    pw, key = keylines[EMAIL]
    print(f"=== {EMAIL} ===", flush=True)
    try:
        # login + click verify
        with Camoufox(headless=True, addons=[], exclude_addons=[DefaultAddons.UBO]) as browser:
            ctx = browser.new_context(viewport={"width":1280,"height":900})
            ctx.add_init_script(efm.INIT)
            page = ctx.new_page()
            page.goto(f"{efm.BASE}/login?mode=signin", wait_until="domcontentloaded", timeout=60000)
            tok = efm.grok.solve_turnstile_bycf(sitekey=efm.SITEKEY, page_url=f"{efm.BASE}/login?mode=signin")
            if tok and page.evaluate("typeof window.__thCb === 'function'"):
                page.evaluate("(t) => { window.__thCb(t); }", tok)
            page.wait_for_selector('input[name="email"]', timeout=30000)
            page.fill('input[name="email"]', EMAIL)
            page.fill('input[name="password"]', pw)
            try:
                page.evaluate("""(t) => {
                    const form = document.querySelector('form');
                    if (!form) return;
                    let h = form.querySelector('input[name="cf-turnstile-response"]');
                    if (!h) { h = document.createElement('input'); h.type='hidden'; h.name='cf-turnstile-response'; h.value=t; form.appendChild(h); }
                    else h.value = t;
                }""", tok)
            except Exception as _e:
                print(f"[swallow batch_verify_403.py:77] {_e}")
                pass
            try:
                page.click('button[type="submit"]', timeout=5000)
            except Exception as _e:
                print(f"[swallow batch_verify_403.py:81] {_e}")
                pass
            try:
                page.wait_for_url(re.compile(r"/dashboard"), timeout=30000)
            except Exception as _e:
                print(f"[swallow batch_verify_403.py:85] {_e}")
                pass
            time.sleep(2)
            try:
                if page.locator('button:has-text("Verify email")').count():
                    page.locator('button:has-text("Verify email")').first.click(timeout=6000)
                    page.wait_for_timeout(2000)
                    print("  clicked Verify email", flush=True)
            except Exception as _e:
                print(f"[swallow batch_verify_403.py:93] {_e}")
                pass
        # poll inbox for link
        link = None
        for i in range(10):
            time.sleep(8)
            msgs = tui.read_cloudmail_inbox(EMAIL)
            for m in msgs:
                body = (m.get("content","") or "") + (m.get("text","") or "")
                mm = re.search(r'https?://[^\s\'"<>]*(?:verify|confirm)[^\s\'"<>]*', body)
                if mm:
                    link = mm.group(0)
                    break
            if link:
                break
        if link:
            print("  link found, opening...", flush=True)
            open_verify_link(link)
        code = check_key(key)
        results[EMAIL] = code
        print(f"  -> final status: {code}", flush=True)
    except Exception as e:
        print(f"  ERROR: {str(e)[:80]}", flush=True)
        results[EMAIL] = "ERR"
    time.sleep(2)

print("\n=== SUMMARY ===")
for e, c in results.items():
    print(f"  {e}: {c}")
