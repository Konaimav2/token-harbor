#!/usr/bin/env python3
"""
TokenHarbor signup using system Chromium via Playwright (HEADLESS - NO VNC).
Uses cloudmail for email verification.

Run: python3 th-signup-system.py --count 1
"""
import sys, os, time, random, string, json, re, glob, argparse, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright

BASE = "https://tokenharbor.ai"
# System chromium: $CHROME_PATH / $TH_CHROME_PATH → PATH lookup; None = Playwright bundled (warn, never crash).
CHROME_PATH = next(
    (p for p in (os.environ.get("CHROME_PATH"), os.environ.get("TH_CHROME_PATH"),
                 shutil.which("chromium-browser"), shutil.which("chromium"),
                 shutil.which("google-chrome")) if p), None)
if CHROME_PATH is None:
    print("[!] No system chromium found (set CHROME_PATH); using Playwright bundled")

def get_cloudmail_addresses():
    """Get list of cloudmail addresses from credentials."""
    creds_glob = os.environ.get("TH_CLOUDMAIL_CREDS_GLOB") or os.path.join(
        os.path.expanduser("~"), "ReiFiles", "credentials",
        "deepseek-session-*", "grok-register", "mail_credentials.txt")
    files = glob.glob(creds_glob)
    if not files:
        print(f"[!] No mail_credentials.txt at {creds_glob} (set TH_CLOUDMAIL_CREDS_GLOB)")
        return []
    accounts = []
    with open(files[0]) as f:
        for line in f:
            parts = line.strip().split('\t')
            if len(parts) >= 1 and parts[0].strip() and '@' in parts[0]:
                accounts.append(parts[0].strip())
    return accounts

class CloudReader:
    """Read cloudmail inbox via API."""
    def __init__(self, email):
        self.email = email
    
    def read_inbox(self):
        try:
            import requests
            payload = {"toEmail": self.email}
            headers = {
                "Content-Type": "application/json",
                "Authorization": "13b8e754-2447-48e9-9539-61e418077f5b"
            }
            r = requests.post("https://cmail.arraffi.my.id/api/public/emailList", 
                            headers=headers, json=payload, timeout=15)
            if r.status_code == 200:
                data = r.json()
                emails = data.get('data', [])
                if emails:
                    latest = emails[-1]
                    subject = latest.get('subject', '')
                    text = (latest.get('content', '') or '') + (latest.get('text', '') or '')
                    return [(subject, text)]
        except Exception as e:
            print(f"[!] Read error: {str(e)[:100]}")
        return []
    
    def wait_for_verify_link(self, timeout=120, poll=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            inbox_list = self.read_inbox()
            for subj, body in inbox_list:
                # Verification link
                link_match = re.search(r'https?://[^\s"\'<>]+(?:verify|confirm|activate)[^\s"\'<>]*', body, re.I)
                if link_match:
                    url = link_match.group(0).rstrip('.,)')
                    print(f"✓ Found verification URL: {url[:120]}")
                    return None, url
                # Also try tokenharbor-style links
                link2 = re.search(r'href=["\']([^"\']+)["\']', body)
                if link2:
                    url = link2.group(1).replace('&amp;', '&')
                    if '/verify' in url.lower() or 'verify' in url.lower():
                        print(f"✓ Found verify link: {url[:120]}")
                        return None, url
            remaining = int(deadline - time.time())
            if remaining > 0:
                print(f"  ...waiting for email ({remaining}s left)    ", end='\r', flush=True)
            time.sleep(poll)
        print("\n✗ No verification email received")
        return None, None

def create_account(email=None, password=None):
    """Create TokenHarbor account using system Chromium headless."""
    all_emails = get_cloudmail_addresses()
    if not email and all_emails:
        email = random.choice(all_emails)
        print(f"[+] Using cloudmail: {email}")
    elif not email:
        print("✗ No cloudmail addresses available")
        return None
    
    password = password or ''.join(random.choices(string.ascii_letters + string.digits, k=18))
    print(f"[+] Password: {password}")
    
    with sync_playwright() as p:
        launch_kw = dict(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        if CHROME_PATH:
            launch_kw["executable_path"] = CHROME_PATH
        browser = p.chromium.launch(**launch_kw)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        page = context.new_page()
        
        try:
            print("[+] Navigating to signup...")
            page.goto(f"{BASE}/login?mode=signup", wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(3000)
            
            # Try to find and fill email/password fields
            email_filled = False
            password_filled = False
            
            # Various selectors for email
            email_selectors = ['input[name="email"]', 'input[type="email"]', '#email']
            for sel in email_selectors:
                try:
                    page.fill(sel, email)
                    email_filled = True
                    break
                except Exception:
                    continue
            
            if not email_filled:
                print("[!] Could not find email field")
                return None
            
            # Password
            password_selectors = ['input[name="password"]', 'input[type="password"]']
            for sel in password_selectors:
                try:
                    page.fill(sel, password)
                    password_filled = True
                    break
                except Exception:
                    continue
            
            if not password_filled:
                print("[!] Could not find password field")
                return None
            
            print("[+] Form filled, attempting submit...")
            
            # Try to find and click submit
            submit_clicked = False
            for sel in ['button[type="submit"]', 'button:has-text("Sign up")', 'button:has-text("Create")', 'button:has-text("Register")']:
                try:
                    page.click(sel, timeout=8000)
                    submit_clicked = True
                    break
                except Exception:
                    continue
            
            if not submit_clicked:
                print("[!] Could not find submit button")
                return None
            
            print("[+] Submitted, waiting for result...")
            page.wait_for_timeout(8000)
            
            # Check result
            try:
                body = page.inner_text('body', timeout=10000)
            except Exception:
                body = ""
            
            if "dashboard" in body.lower() or "api-keys" in body.lower():
                print("[✓] Signup successful! (dashboard visible)")
            else:
                print(f"[!] Signup result unclear. Body snippet: {body[:200]}")
            
            # Verify email
            print("[+] Waiting for verification email...")
            reader = CloudReader(email)
            code, link = reader.wait_for_verify_link(timeout=120)
            
            if link:
                print("[+] Opening verification link...")
                page.goto(link, timeout=30000)
                page.wait_for_timeout(5000)
            else:
                print("[!] No verification link received yet")
                browser.close()
                return {"email": email, "password": password, "verified": False, "api_key": ""}
            
            # After verification, go to API keys
            try:
                page.goto(f"{BASE}/dashboard/api-keys", timeout=30000)
                page.wait_for_timeout(5000)
                body = page.inner_text('body', timeout=10000)
                api_key = re.search(r'\bthk_[a-zA-Z0-9_-]{20,}\b', body)
                api_key = api_key.group(0) if api_key else ""
            except Exception:
                api_key = ""
            
            browser.close()
            return {
                "email": email,
                "password": password,
                "verified": True,
                "api_key": api_key,
                "status": "ok"
            }
            
        except Exception as e:
            print(f"[!] Error: {type(e).__name__}: {str(e)[:300]}")
            try:
                browser.close()
            except Exception:
                pass
            return {"email": email, "password": password, "verified": False, "status": f"error_{type(e).__name__}"}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TokenHarbor signup (headless chromium)")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--email", type=str, default=None)
    args = parser.parse_args()
    
    print("="*60)
    print("TokenHarbor Account Creation (Headless System Chromium)")
    print("="*60)
    
    for i in range(args.count):
        print(f"\n--- Account {i+1} ---")
        result = create_account(email=args.email)
        
        if result:
            print(f"\n✓ Email: {result['email']}")
            print(f"✓ Password: {result['password']}")
            print(f"✓ Verified: {result.get('verified', False)}")
            if result.get('api_key'):
                print(f"✓ API Key: {result['api_key']}")
