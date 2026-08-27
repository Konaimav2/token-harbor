"""xAI console signup via API:
DARI AI UNTUK AI
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import re
import string
import sys
import time
from urllib.parse import quote

from curl_cffi import requests as cffi_requests
import requests as std_requests

BASE = "https://console.x.ai"
SEND_URL = f"{BASE}/api/auth/send-verification-code"
VERIFY_URL = f"{BASE}/api/auth/sign-up/verify-email"
CREATE_URL = f"{BASE}/api/auth/sign-up/create-account"

DEFAULT_PASSWORD = os.environ.get("DEFAULT_PASSWORD", "")

# ── tempmail ─────────────────────────────────────────────────────────
MAIL_TM_API = "https://api.mail.tm"
GENERATOR_BASE = "https://generator.email"
# domain yang tersedia di layanan tempmail (fallback via generator.email)
GENERATOR_DOMAINS = [
    "tools-capcut.com", "save4now.com", "cuscuscuspen.life", "vectorbrasil.app",
    "sentra-premium.com", "youtube-com-watch-jtpdc8khnpi.cyou",
]

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
TURNSTILE_PAGE_URL = f"{BASE}/login?mode=sign-up"
TURNSTILE_SOLVER_BASE = ""  # empty = pakai BYCF by default; isi URL solver eksternal bila perlu

# ── BYCF turnstile solver (docs: https://bycf.nett.to/docs) ───────────
BYCF_API = "https://shannz.zone.id/api/solve-turnstile-min"
BYCF_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-bycf-version": "1.0.5",
    "x-bycf-secret": os.environ.get("BYCF_SECRET", ""),
}

ROUTER_BASE = "http://localhost:20128"
ROUTER_AUTH_TOKEN = os.environ.get("ROUTER_AUTH_TOKEN", "")
OTP_FALSE_POSITIVE = {
    "FAFAFA", "ABCDEF", "123456", "000000", "111111", "989898",
    "FFFFFF", "AAAAAA", "QQQQQQ", "XXXXXX", "SCRIPT", "STYLE",
    "BUTTON", "OBJECT", "WINDOW", "DOCUMENT", "NUMBER", "STRING",
    "RETURN", "IMPORT", "EXPORT", "LENGTH", "SOURCE", "TARGET",
}

API_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": BASE,
    "referer": f"{BASE}/login",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

FIRST_NAMES = [
    "James", "Oliver", "Liam", "Noah", "Ethan", "Mason", "Logan", "Lucas",
    "Aiden", "Jackson", "Sebastian", "Mateo", "Jack", "Owen", "Theodore",
    "Emma", "Olivia", "Ava", "Sophia", "Isabella", "Mia", "Charlotte",
    "Amelia", "Harper", "Evelyn", "Abigail", "Emily", "Luna", "Sofia", "Ella",
    "Ahmad", "Rafi", "Dimas", "Budi", "Andi", "Sari", "Putri", "Dewi",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
    "White", "Harris", "Martin", "Thompson", "Robinson", "Clark", "Lewis",
    "Young", "Walker", "Hall", "Allen", "King", "Wright", "Scott", "Green",
    "Udin", "Pratama", "Saputra", "Wijaya", "Nugraha", "Santoso",
]


def random_email(domains: list[str] | None = None, length: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    username = "".join(random.choices(chars, k=length))
    domain = random.choice(domains or GENERATOR_DOMAINS)
    return f"{username}@{domain}"


def random_name() -> tuple[str, str]:
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


def parse_email(email: str) -> tuple[str, str]:
    if "@" not in email:
        raise ValueError(f"invalid email: {email}")
    user, domain = email.rsplit("@", 1)
    return user, domain


_CODE_PATTERNS = [
    r"SpaceXAI\s+confirmation code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
    r"confirmation code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
    r"verification code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
    r"SpaceXAI\s+confirmation code[:\s]+([A-Z0-9]{6})",
    r"confirmation code[:\s]+([A-Z0-9]{6})",
    r"verification code[:\s]+([A-Z0-9]{6})",
]


def normalize_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")


def is_plausible_code(code: str) -> bool:
    """xAI email codes are always 6 alnum after removing dash, e.g. CMF-EAX -> CMFEAX."""
    c = normalize_code(code)
    if len(c) != 6 or not c.isalnum():
        return False
    if c in OTP_FALSE_POSITIVE:
        return False
    if c.startswith("20"):
        return False
    if c.isalpha() and c in OTP_FALSE_POSITIVE:
        return False
    if len(set(c)) <= 2:
        return False
    return True


def match_code(text: str) -> str | None:
    """Cari kode konfirmasi xAI pada teks (subject email)."""
    if not text:
        return None
    for pat in _CODE_PATTERNS:
        for m in reversed(re.findall(pat, text, re.IGNORECASE)):
            if is_plausible_code(m):
                return normalize_code(m)
    return None


class TempEmail:
    def __init__(self, email: str | None = None, proxy: str | None = None):
        email = email or random_email()
        self.username, self.domain = parse_email(email)
        self.email = email
        self.session = std_requests.Session()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def _headers(self) -> dict:
        return {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        }

    def warm(self) -> None:
        inbox_url = f"{GENERATOR_BASE}/{self.domain}/{self.username}"
        self.session.get(GENERATOR_BASE, headers=self._headers(), timeout=20, proxies=self.proxies)
        self.session.get(inbox_url, headers=self._headers(), timeout=20, proxies=self.proxies)

    def get_inbox_page(self) -> str:
        inbox_url = f"{GENERATOR_BASE}/{self.domain}/{self.username}"
        resp = self.session.get(
            inbox_url,
            headers=self._headers(),
            timeout=20,
            proxies=self.proxies,
        )
        return resp.text if resp.status_code == 200 else ""

    @staticmethod
    def _normalize_code(code: str) -> str:
        return normalize_code(code)

    @staticmethod
    def _is_plausible_code(code: str) -> bool:
        return is_plausible_code(code)

    @staticmethod
    def _strip_noise(html: str) -> str:
        html = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", html)
        html = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", html)
        html = re.sub(r"(?is)<!--.*?-->", " ", html)
        return html

    def extract_code(self, text: str) -> str | None:
        """Only accept codes from the actual xAI confirmation email subject.

        Real subject example:
          SpaceXAI confirmation code: CMF-EAX
        Never scan random page tokens (PER100, SCRIPT, CSS crumbs, ads).
        """
        if not text:
            return None

        text = self._strip_noise(text)

        # 1) exact confirmation/verification subject patterns (dashed preferred)
        code = match_code(text)
        if code:
            return code

        # 2) generator.email subject div only
        for block in re.findall(
            r'class="[^"]*subj_div[^"]*"[^>]*>(.*?)</div>',
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            m = re.search(
                r"(?:confirmation|verification)\s+code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3}|[A-Z0-9]{6})",
                block,
                re.I,
            )
            if m and self._is_plausible_code(m.group(1)):
                return self._normalize_code(m.group(1))
            # subject is literally just the code form
            m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", block, re.I)
            if m and self._is_plausible_code(m.group(1)):
                return self._normalize_code(m.group(1))

        # Do NOT scan whole HTML for bare 6-char tokens (caused PER100/SCRIPT)
        return None

    def wait_for_code(self, max_retries: int = 25, delay: int = 4) -> str | None:
        for attempt in range(1, max_retries + 1):
            try:
                code = self.extract_code(self.get_inbox_page())
            except Exception as e:
                print(f"  ... inbox error ({attempt}/{max_retries}): {e}")
                code = None
            if code:
                print()  # finish the waiting line cleanly
                return code
            if attempt < max_retries:
                print(f"  ... waiting OTP ({attempt}/{max_retries})   ", end="\r", flush=True)
                time.sleep(delay)
        print()
        return None


class MailTmBox:
    """Tempmail via API mail.tm — email & domain random dari layanan."""

    def __init__(self, proxy: str | None = None):
        self.session = std_requests.Session()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.email: str | None = None
        self._token: str | None = None
        self.account_id: str | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/ld+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _get(self, path: str, **kw):
        return self.session.get(
            f"{MAIL_TM_API}{path}", headers=self._headers(), timeout=20,
            proxies=self.proxies, **kw,
        )

    def _post(self, path: str, payload=None, **kw):
        return self.session.post(
            f"{MAIL_TM_API}{path}", headers=self._headers(),
            json=payload, timeout=20, proxies=self.proxies, **kw,
        )

    def create(self, max_retries: int = 5) -> str:
        resp = self._get("/domains")
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):
            domains = [d["domain"] for d in body.get("hydra:member", []) if d.get("isActive")]
        else:
            domains = [d["domain"] for d in body if d.get("isActive")]
        if not domains:
            raise RuntimeError("mail.tm: tidak ada domain aktif")
        chars = string.ascii_lowercase + string.digits
        last_err = "?"
        for _ in range(max_retries):
            domain = random.choice(domains)
            address = f"{''.join(random.choices(chars, k=8))}@{domain}"
            cr = self._post("/accounts", {"address": address, "password": DEFAULT_PASSWORD})
            if cr.status_code not in (200, 201):
                last_err = f"{cr.status_code} {cr.text[:120]}"
                continue
            try:
                self.account_id = cr.json().get("id")
            except Exception:
                pass
            tr = self._post("/token", {"address": address, "password": DEFAULT_PASSWORD})
            if tr.status_code == 200:
                try:
                    self._token = tr.json().get("token")
                except Exception:
                    pass
            self.email = address
            return address
        raise RuntimeError(f"mail.tm: gagal buat akun ({last_err})")

    def warm(self) -> None:
        if not self.email:
            self.create()
        self._get("/messages")

    def get_inbox_page(self) -> str:
        resp = self._get("/messages")
        if resp.status_code != 200:
            return ""
        try:
            body = resp.json()
        except Exception:
            return ""
        if isinstance(body, dict):
            member = body.get("hydra:member", [])
        else:
            member = body
        if not isinstance(member, list):
            member = []
        return "\n".join(
            f"Subject: {m.get('subject', '')}\n{m.get('intro') or ''}" for m in member
        )

    def extract_code(self, text: str) -> str | None:
        return match_code(text)

    def wait_for_code(self, max_retries: int = 25, delay: int = 4) -> str | None:
        for attempt in range(1, max_retries + 1):
            try:
                code = self.extract_code(self.get_inbox_page())
            except Exception as e:
                print(f"  ... inbox error ({attempt}/{max_retries}): {e}")
                code = None
            if code:
                print()  # finish the waiting line cleanly
                return code
            if attempt < max_retries:
                print(f"  ... waiting OTP ({attempt}/{max_retries})   ", end="\r", flush=True)
                time.sleep(delay)
        print()
        return None


class CloudMailBox:
    """Cloud Mail Worker inbox using pre-existing accounts from mail_credentials.txt."""
    
    def __init__(self, proxy: str | None = None):
        import os
        self.session = std_requests.Session()
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.email: str | None = None
        self._token: str | None = os.environ.get("CLOUDMAIL_TOKEN", "13b8e754-2447-48e9-9539-61e418077f5b")
        self.cmail_base = os.environ.get("CLOUDMAIL_BASE", "https://cmail.arraffi.my.id/api")
        
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": self._token or "",
        }
    
    def _post(self, path: str, payload=None, **kw):
        url = f"{self.cmail_base}{path}"
        return self.session.post(url, headers=self._headers(), json=payload or {}, timeout=20, proxies=self.proxies, **kw)
    
    def create(self, max_retries: int = 5) -> str:
        """Preload known emails from mail_credentials.txt and pick one randomly."""
        import glob
        
        # Try multiple credential file locations
        creds_path = "/root/ReiFiles/credentials/deepseek-session-*/grok-register/mail_credentials.txt"
        files = glob.glob(creds_path)
        
        if not files:
            raise RuntimeError("CloudMailBox: tidak menemukan file mail_credentials.txt")
        
        lines = open(files[0]).read().strip().split("\n")
        accounts = []
        for line in lines:
            parts = line.strip().split("\t")
            if len(parts) >= 1:
                email = parts[0].strip()
                if email and "@" in email:
                    accounts.append(email)
        
        if not accounts:
            raise RuntimeError(f"CloudMailBox: tidak ada akun di mail_credentials.txt")
        
        # Pick random account
        self.email = random.choice(accounts)
        print(f"  [CLOUDMAIL] Selected: {self.email}")
        
        # Verify mailbox exists
        resp = self._post("/public/emailList", {"toEmail": self.email})
        if resp.status_code != 200:
            raise RuntimeError(f"CloudMailBox: verifikasi gagal ({resp.status_code})")
        
        return self.email
    
    def warm(self) -> None:
        if not self.email:
            self.create()
        self._post("/public/emailList", {"toEmail": self.email})
    
    def get_inbox_page(self) -> str:
        """Get all messages from Cloud Mail inbox."""
        try:
            resp = self._post("/public/emailList", {"toEmail": self.email})
            if resp.status_code != 200:
                try:
                    detail = resp.text[:150]
                except Exception:
                    detail = str(resp.status_code)
                raise RuntimeError(f"CloudMail inbox HTTP {resp.status_code}: {detail}")
            data = resp.json().get("data", [])
            if not isinstance(data, list):
                return ""
            return "\n".join(f"Subject: {m.get('subject','')}\n{m.get('content','')[:200]}" for m in data)
        except Exception as e:
            return f"Error: {e}"
    
    def extract_code(self, text: str) -> str | None:
        return match_code(text)
    
    def wait_for_code(self, max_retries: int = 25, delay: int = 4) -> str | None:
        """Poll Cloud Mail inbox for verification code."""
        for attempt in range(1, max_retries + 1):
            try:
                code = self.extract_code(self.get_inbox_page())
            except Exception as e:
                print(f"  ... CloudMail error ({attempt}/{max_retries}): {e}")
                code = None
            
            if code:
                print()
                return code
            
            if attempt < max_retries:
                print(f"  ... waiting OTP ({attempt}/{max_retries})   ", end="\r", flush=True)
                time.sleep(delay)
        print()
        return None


def create_mailbox(proxy: str | None = None):
    """Buat inbox tempmail: cloudmail → mail.tm → generator.email."""
    for provider in (CloudMailBox, MailTmBox, TempEmail):
        try:
            box = provider(proxy=proxy)
            if isinstance(box, CloudMailBox):
                box.create()
            if getattr(box, "email", None):
                return box
        except Exception:
            continue
    return TempEmail(proxy=proxy)


def make_session(impersonate: str, proxy: str | None) -> cffi_requests.Session:
    session = cffi_requests.Session(impersonate=impersonate)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def warmup(session: cffi_requests.Session) -> None:
    session.get(
        f"{BASE}/login",
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
        },
        timeout=30,
        allow_redirects=True,
    )


def send_code(session: cffi_requests.Session, email: str) -> cffi_requests.Response:
    return session.post(SEND_URL, headers=API_HEADERS, json={"email": email}, timeout=30)


def verify_email(session: cffi_requests.Session, email: str, code: str) -> cffi_requests.Response:
    return session.post(
        VERIFY_URL,
        headers=API_HEADERS,
        json={"email": email, "code": code},
        timeout=30,
    )


def create_account(
    session: cffi_requests.Session,
    email: str,
    password: str,
    given_name: str,
    family_name: str,
    email_code: str,
    turnstile_token: str,
) -> cffi_requests.Response:
    payload = {
        "email": email,
        "password": password,
        "givenName": given_name,
        "familyName": family_name,
        "emailValidationCode": email_code,
        "turnstileToken": turnstile_token,
    }
    return session.post(CREATE_URL, headers=API_HEADERS, json=payload, timeout=45)


def solve_turnstile_camoufox(
    sitekey: str = TURNSTILE_SITEKEY,
    page_url: str = TURNSTILE_PAGE_URL,
    proxy: str | None = None,
    timeout_ms: int = 60_000,
) -> str:
    """Solve Turnstile using camoufox headless browser (no external API needed)."""
    from camoufox.sync_api import Camoufox

    launch_kw: dict = {"headless": True}
    if proxy:
        from urllib.parse import urlparse
        p = urlparse(proxy)
        pcfg: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            pcfg["username"] = p.username
        if p.password:
            pcfg["password"] = p.password
        launch_kw["proxy"] = pcfg
        launch_kw["geoip"] = True

    token = None
    with Camoufox(**launch_kw) as browser:
        page = browser.new_page()
        page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_function(
            "typeof window.turnstile !== 'undefined'",
            timeout=20_000,
        )
        js = f"""() => new Promise((resolve, reject) => {{
            const div = document.createElement('div');
            div.style.display = 'none';
            document.body.appendChild(div);
            window.turnstile.render(div, {{
                sitekey: '{sitekey}',
                callback: (t) => resolve(t),
                'error-callback': () => reject(new Error('turnstile error-callback')),
                'expired-callback': () => reject(new Error('turnstile expired')),
            }});
            setTimeout(() => reject(new Error('turnstile timeout')), {timeout_ms});
        }})"""
        token = page.evaluate(js)

    if not token or len(token) < 40:
        raise RuntimeError(f"camoufox turnstile bad token: {token!r}")
    return token


def solve_turnstile_bycf(
    sitekey: str = TURNSTILE_SITEKEY,
    page_url: str = TURNSTILE_PAGE_URL,
    proxy: str | None = None,
    api: str = BYCF_API,
    timeout: int = 180,
) -> str:
    """Solve Turnstile via BYCF (bycf.nett.to). Sinkron: 1 request = 1 token."""
    payload = {"url": page_url, "siteKey": sitekey, "proxy": proxy}
    resp = std_requests.post(api, json=payload, headers=BYCF_HEADERS, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"bycf http {resp.status_code}: {resp.text[:120]}")
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"bycf bukan json: {e}") from e
    if not data.get("success"):
        raise RuntimeError(f"bycf gagal: {data.get('error', data)}")
    token = data.get("data") or data.get("token")
    if not token or not isinstance(token, str) or len(token) < 40:
        raise RuntimeError(f"bycf token tidak valid: {str(token)[:60]!r}")
    return token


def solve_turnstile(
    sitekey: str = TURNSTILE_SITEKEY,
    page_url: str = TURNSTILE_PAGE_URL,
    solver_base: str = TURNSTILE_SOLVER_BASE,
    max_wait: int = 180,
    poll_every: float = 2.0,
    proxy: str | None = None,
) -> str:
    """BYCF (default) → external solver → camoufox fallback."""
    # ── BYCF (https://bycf.nett.to) ────────────────────────────────────
    if not solver_base:
        try:
            return solve_turnstile_bycf(sitekey=sitekey, page_url=page_url, proxy=proxy, timeout=max_wait)
        except Exception as e:
            print(f"  ... bycf gagal, fallback ke camoufox: {e}")
        return solve_turnstile_camoufox(sitekey=sitekey, page_url=page_url, proxy=proxy)

    # ── external solver ───────────────────────────────────────────────
    create_url = (
        f"{solver_base.rstrip('/')}/turnstile"
        f"?url={quote(page_url, safe='')}"
        f"&sitekey={quote(sitekey, safe='')}"
    )
    try:
        create_resp = std_requests.get(create_url, timeout=15)
        create_resp.raise_for_status()
        create_data = create_resp.json()
    except Exception:
        return solve_turnstile_camoufox(sitekey=sitekey, page_url=page_url, proxy=proxy)

    task_id = create_data.get("task_id") or create_data.get("id")
    if not task_id:
        return solve_turnstile_camoufox(sitekey=sitekey, page_url=page_url, proxy=proxy)

    result_url = f"{solver_base.rstrip('/')}/result?id={quote(str(task_id), safe='')}"
    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            resp = std_requests.get(result_url, timeout=15)
        except Exception:
            time.sleep(poll_every)
            continue
        text = resp.text.strip()
        try:
            data = resp.json()
        except Exception:
            data = None

        token = None
        status = None
        if isinstance(data, dict):
            status = str(data.get("status", "")).lower()
            nested = data.get("data")
            nested_token = None
            if isinstance(nested, dict):
                nested_token = nested.get("token") or nested.get("value") or nested.get("result")
            elif isinstance(nested, str):
                nested_token = nested
            token = (
                data.get("token")
                or data.get("turnstileToken")
                or data.get("value")
                or data.get("result")
                or nested_token
            )
            if isinstance(token, dict):
                token = token.get("token") or token.get("value")
        elif isinstance(data, str) and len(data) > 40:
            token = data
        elif text and "status" not in text.lower() and len(text) > 40 and " " not in text:
            token = text.strip('"')

        if token and isinstance(token, str) and len(token) > 40:
            return token

        if status in {"failed", "error", "fail"}:
            return solve_turnstile_camoufox(sitekey=sitekey, page_url=page_url, proxy=proxy)

        time.sleep(poll_every)

    return solve_turnstile_camoufox(sitekey=sitekey, page_url=page_url, proxy=proxy)


def print_resp(label: str, resp: cffi_requests.Response, verbose: bool = False) -> None:
    body = resp.text
    ok = resp.status_code < 400
    icon = "✓" if ok else "✗"
    if "cloudflare" in body.lower() and len(body) > 400:
        tag = "BLOCKED" if "Sorry, you have been blocked" in body else "CF-HTML"
        print(f"  {icon} [{label}] {resp.status_code} {tag}")
        return
    if verbose or not ok:
        try:
            j = resp.json()
            print(f"  {icon} [{label}] {resp.status_code} {json.dumps(j)}")
        except Exception:
            print(f"  {icon} [{label}] {resp.status_code} {body[:300]}")
    else:
        print(f"  {icon} [{label}] {resp.status_code} ok")


def load_router_secret() -> str | None:
    """Baca JWT secret 9router: env JWT_SECRET dulu, lalu file DATA_DIR/jwt-secret."""
    import os

    env_val = os.environ.get("JWT_SECRET")
    if env_val and env_val.strip():
        return env_val.strip()

    cand_paths: list[str] = []
    for base in (
        os.environ.get("DATA_DIR"),
        os.path.expanduser("~/.9router"),
        os.path.expanduser("~/.config/9router"),
        "/var/lib/9router",
    ):
        if base:
            cand_paths.append(os.path.join(base, "jwt-secret"))
    for p in cand_paths:
        try:
            with open(p, encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
        except Exception:
            continue
    return None


def generate_router_token(secret: str | None = None, ttl: int = 86400) -> str:
    """Buat JWT auth_token 9router (HS256, authenticated=true) seperti dashboard."""
    secret = secret or load_router_secret()
    if not secret:
        raise RuntimeError(
            "JWT secret 9router tidak ditemukan. Set env JWT_SECRET atau pastikan "
            "file ~/.9router/jwt-secret ada."
        )
    _b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    header = _b64(json.dumps({"alg": "HS256"}).encode())
    now = int(time.time())
    payload = _b64(json.dumps({"authenticated": True, "iat": now, "exp": now + ttl}).encode())
    sig = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def refresh_router_token() -> str:
    """Generate token baru & simpan ke global ROUTER_AUTH_TOKEN (auto-update runtime)."""
    global ROUTER_AUTH_TOKEN
    ROUTER_AUTH_TOKEN = generate_router_token()
    return ROUTER_AUTH_TOKEN


def console_login(
    session: cffi_requests.Session,
    email: str,
    password: str,
    proxy: str | None = None,
) -> cffi_requests.Response | None:
    """Login ke console.x.ai via API. Butuh token Turnstile (BYCF/camoufox).
    Cookie sso/sso-rw hasil login tersimpan di session."""
    tok = solve_turnstile(
        sitekey=TURNSTILE_SITEKEY,
        page_url=f"{BASE}/login?mode=sign-in",
        proxy=proxy,
    )
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": BASE,
        "referer": f"{BASE}/login",
    }
    return session.post(
        f"{BASE}/api/auth/sign-in",
        json={"email": email, "password": password, "turnstileToken": tok},
        headers=headers,
        timeout=30,
    )


def session_cookies(session: cffi_requests.Session) -> dict:
    """Ambil cookie sso/sso-rw dari sesi curl_cffi untuk dipakai di browser."""
    return {k: v for k, v in session.cookies.items() if k in ("sso", "sso-rw")}


def create_xai_api_key(
    cookies: dict,
    key_name: str = "9router",
    proxy: str | None = None,
) -> str | None:
    """Buat API key xai-... via browser camoufox. Bila akun belum selesai
    onboarding (belum ada team), alur "Create your team" + "Continue for free"
    diisi otomatis. Return string key atau None bila gagal."""
    from camoufox.sync_api import Camoufox
    import random as _random

    launch_kw: dict = {"headless": True}
    if proxy:
        from urllib.parse import urlparse

        p = urlparse(proxy)
        pcfg: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            pcfg["username"] = p.username
        if p.password:
            pcfg["password"] = p.password
        launch_kw["proxy"] = pcfg
        launch_kw["geoip"] = True

    key_re = re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}\b")
    with Camoufox(**launch_kw) as browser:
        ctx = browser.new_context()
        for k, v in cookies.items():
            ctx.add_cookies([{"name": k, "value": v, "domain": "console.x.ai", "path": "/"}])
        page = ctx.new_page()

        # 1) pastikan onboarding selesai (bila belum)
        page.goto(f"{BASE}/welcome", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5000)
        for _ in range(8):
            body = page.evaluate("document.body.innerText")
            if "Create your team" in body:
                try:
                    page.wait_for_selector('input[name="name"]', timeout=15_000)
                    page.fill('input[name="name"]', f"dev-team-{_random.randint(1000, 9999)}")
                except Exception:
                    pass
                for label in ("Engineer", "Hobbyist", "Student", "Business", "Other"):
                    try:
                        page.click(f'button:has-text("{label}")', timeout=2500)
                        break
                    except Exception:
                        continue
                try:
                    page.click('button[type="submit"]:has-text("Continue")', timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                continue
            if "Continue for free" in body:
                try:
                    page.click('button:has-text("Continue for free")', timeout=6000)
                    page.wait_for_timeout(4000)
                    continue
                except Exception:
                    pass
            break
        page.wait_for_timeout(3000)

        # 2) dapatkan teamId (mis. /team/<uuid> atau param created=)
        m = re.search(
            r"/team/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            page.url,
        )
        if not m:
            m = re.search(r"created=([0-9a-fA-F-]{36})", page.url)
        if not m:
            return None
        team_id = m.group(1)

        # 3) buka halaman API keys + modal create
        page.goto(f"{BASE}/team/{team_id}/api-keys?create=api-key", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)
        if "Create API key" in page.evaluate("document.body.innerText"):
            try:
                page.wait_for_selector('input[name="name"]', timeout=10_000)
                page.fill('input[name="name"]', key_name)
                page.click('button[type="submit"]:has-text("Create API key")', timeout=8000)
            except Exception:
                pass

        # 4) ekstrak key dari teks halaman (modal menampilkannya sekali saja)
        deadline = time.time() + 20
        while time.time() < deadline:
            body = page.evaluate("document.body.innerText")
            m = key_re.search(body)
            if m:
                return m.group(0)
            page.wait_for_timeout(1500)
        return None


def import_to_router(
    api_key: str,
    display_name: str,
    router_base: str,
    router_token: str,
    auto_refresh: bool = True,
    max_refresh: int = 3,
) -> tuple[bool, object]:
    """Import koneksi xai (authType=apikey) ke 9router via POST /api/providers."""
    for attempt in range(1 + max_refresh):
        if attempt:
            router_token = refresh_router_token()
            print(f"  ↻ Regenerate token 9router (percobaan {attempt}/{max_refresh})")
        try:
            resp = std_requests.post(
                f"{router_base}/api/providers",
                json={
                    "provider": "xai",
                    "apiKey": api_key,
                    "name": display_name,
                    "priority": 1,
                    "testStatus": "unknown",
                },
                cookies={"auth_token": router_token},
                headers={"Accept": "*/*", "Content-Type": "application/json"},
                timeout=15,
            )
        except Exception as e:
            print(f"  ✗ 9router tidak dapat dijangkau: {e}")
            return False, None
        if resp.status_code in (200, 201):
            try:
                return True, resp.json()
            except Exception:
                return True, resp.text
        print(f"  ✗ 9router menolak token (kode {resp.status_code}): {resp.text[:120]}")
        if auto_refresh and resp.status_code in (401, 403):
            continue
        return False, resp.text
    return False, "token ditolak"


def connect_to_router(
    session: cffi_requests.Session,
    email: str,
    password: str,
    display_name: str,
    router_base: str = ROUTER_BASE,
    router_token: str = ROUTER_AUTH_TOKEN,
    proxy: str | None = None,
    auto_refresh: bool = True,
    max_refresh: int = 3,
) -> bool:
    """Hubungkan akun xAI ke 9router via jalur API-key:
    1) login console.x.ai (BYCF turnstile)
    2) buat API key via browser camoufox (isi onboarding bila perlu)
    3) import ke 9router sebagai provider xai (authType=apikey)

    auto_refresh=True → jika token 9router ditolak, regenerate & retry."""
    print("\n[+] Menghubungkan akun ke 9router (jalur API key)...")

    # 1) login console
    try:
        login_resp = console_login(session, email, password, proxy)
    except Exception as e:
        print(f"  ✗ Login console gagal: {e}")
        return False
    if login_resp is None or login_resp.status_code != 200:
        detail = login_resp.text[:160] if login_resp is not None else "-"
        print(f"  ✗ Login console gagal ({getattr(login_resp, 'status_code', '?')}): {detail}")
        return False
    print("  ✓ Login console OK")

    # 2) buat API key (camoufox)
    try:
        key = create_xai_api_key(session_cookies(session), key_name=display_name, proxy=proxy)
    except Exception as e:
        print(f"  ✗ Gagal membuat API key: {e}")
        key = None
    if not key:
        print("  ✗ Tidak mendapat API key (browser gagal / onboarding terblokir)")
        return False
    print(f"  ✓ API key dibuat: {key[:14]}...")

    # 3) import ke 9router
    ok, payload = import_to_router(key, display_name, router_base, router_token, auto_refresh, max_refresh)
    if not ok:
        print("  ✗ Gagal import ke 9router")
        return False
    try:
        conn_id = payload.get("connection", {}).get("id", "?")
    except AttributeError:
        conn_id = "?"
    print(f"  ✓ Terhubung ke 9router (connection {conn_id})")

    # 4) cek status kredit (informasional — akun baru umumnya 403 tanpa top-up)
    try:
        test = std_requests.get(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        if test.status_code == 200:
            print("  ✓ API key aktif (kredit tersedia)")
        elif test.status_code == 403:
            print("  ⚠ API key aktif di 9router, TAPI akun belum punya kredit → request 403 sampai top-up")
        else:
            print(f"  ⚠ Uji API key: HTTP {test.status_code}")
    except Exception:
        pass
    return True


import threading as _threading
import datetime as _dt


def _print_lock_fn():
    """Return a module-level print lock (created once)."""
    if not hasattr(_print_lock_fn, "_lock"):
        _print_lock_fn._lock = _threading.Lock()
    return _print_lock_fn._lock


def _safe_print(*a, **kw):
    with _print_lock_fn():
        print(*a, **kw)


def register_one(args, worker_id: int = 0) -> bool:
    """Buat satu akun. Return True jika berhasil."""
    prefix = f"[#{worker_id}] " if worker_id else ""

    given_name = random.choice(FIRST_NAMES)
    family_name = random.choice(LAST_NAMES)

    # inbox tempmail — email & domain random dari layanan
    mail = create_mailbox(args.proxy)
    email = mail.email

    _safe_print(f"\n{prefix}📧 {email}  👤 {given_name} {family_name}")

    try:
        mail.warm()
    except Exception:
        pass

    # session
    session = make_session(args.impersonate, args.proxy)
    if not args.no_warmup:
        try:
            warmup(session)
        except Exception:
            pass

    # 1) send code
    try:
        send_resp = send_code(session, email)
    except Exception as e:
        _safe_print(f"{prefix}  ✗ Gagal mengirim kode: {e}")
        return False
    if send_resp.status_code >= 400:
        try:
            err = send_resp.json().get("error") or send_resp.text[:80]
        except Exception:
            err = send_resp.text[:80]
        _safe_print(f"{prefix}  ✗ Email ditolak: {err}")
        return False
    _safe_print(f"{prefix}  [1/4] Kode verifikasi terkirim!")

    # 2) OTP
    code = mail.wait_for_code(max_retries=args.otp_retries, delay=args.otp_delay)
    if not code:
        _safe_print(f"{prefix}  ✗ Kode OTP tidak diterima")
        return False
    _safe_print(f"{prefix}  [2/4] Kode OTP diterima: {code}")

    # 3) verify email
    try:
        verify_resp = verify_email(session, email, code)
    except Exception as e:
        _safe_print(f"{prefix}  ✗ Gagal verifikasi: {e}")
        return False
    if verify_resp.status_code >= 400:
        _safe_print(f"{prefix}  ✗ Kode tidak valid")
        return False
    _safe_print(f"{prefix}  [3/4] Email terverifikasi, melewati keamanan...")

    # 4) turnstile
    try:
        token = solve_turnstile(
            sitekey=args.sitekey,
            page_url=TURNSTILE_PAGE_URL,
            solver_base=args.solver,
            max_wait=args.turnstile_wait,
            proxy=args.proxy,
        )
    except Exception as e:
        _safe_print(f"{prefix}  ✗ Verifikasi keamanan gagal: {e}")
        return False

    # 5) create account
    try:
        create_resp = create_account(
            session=session,
            email=email,
            password=args.password,
            given_name=given_name,
            family_name=family_name,
            email_code=code,
            turnstile_token=token,
        )
    except Exception as e:
        _safe_print(f"{prefix}  ✗ Gagal membuat akun: {e}")
        return False

    if create_resp.status_code >= 400:
        try:
            err = create_resp.json().get("error") or create_resp.text[:80]
        except Exception:
            err = create_resp.text[:80]
        _safe_print(f"{prefix}  ✗ Gagal membuat akun: {err}")
        return False

    try:
        uid = create_resp.json().get("session", {}).get("userId", "-")
    except Exception:
        uid = "-"
    _safe_print(f"{prefix}  [4/4] ✅ Akun berhasil dibuat!")

    # 6) 9router
    router_status = "-"
    if args.router:
        ok = connect_to_router(
            session=session,
            email=email,
            password=args.password,
            display_name=f"{given_name} {family_name}",
            router_base=args.router_base,
            router_token=args.router_token,
            proxy=args.proxy,
        )
        router_status = "connected" if ok else "failed"

    # simpan ke accounts.txt
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{email}|{args.password}|{given_name} {family_name}|{uid}|router={router_status}|{ts}\n"
    with _print_lock_fn():
        with open("accounts.txt", "a", encoding="utf-8") as f:
            f.write(line)
    _safe_print(f"{prefix}  💾 Tersimpan → accounts.txt")
    return True


def main() -> int:
    args = argparse.Namespace(
        email=None,
        proxy=None,
        impersonate="chrome136",
        password=DEFAULT_PASSWORD,
        given_name=None,
        family_name=None,
        code=None,
        turnstile_token=None,
        solver=TURNSTILE_SOLVER_BASE,
        sitekey=TURNSTILE_SITEKEY,
        no_warmup=False,
        send_only=False,
        verify_only=False,
        otp_retries=25,
        otp_delay=4,
        turnstile_wait=180,
        router=True,
        router_token=ROUTER_AUTH_TOKEN,
        router_base=ROUTER_BASE,
        verbose=False,
        count=None,
        threads=None,
    )

    # Tanya interaktif jika tidak di-pass via argumen
    if args.count is None:
        try:
            args.count = int(input("Berapa banyak akun yang ingin dibuat? "))
        except (ValueError, EOFError):
            args.count = 1
    if args.threads is None:
        try:
            args.threads = int(input("Berapa thread (proses paralel)? [1 = satu-satu] "))
        except (ValueError, EOFError):
            args.threads = 1
    if args.proxy is None:
        try:
            pakai = input("Mau pakai proxy? (y/n) ").strip().lower()
            if pakai == "y":
                args.proxy = input("Masukkan proxy (http://user:pass@host:port): ").strip() or None
        except EOFError:
            pass

    args.count = max(1, args.count)
    args.threads = max(1, min(args.threads, args.count))

    print(f"\n🚀 Membuat {args.count} akun dengan {args.threads} thread...")
    print("─" * 46)

    results = {"ok": 0, "fail": 0}
    lock = _threading.Lock()
    start_time = _dt.datetime.now()

    def worker(wid: int):
        ok = register_one(args, worker_id=wid)
        with lock:
            if ok:
                results["ok"] += 1
            else:
                results["fail"] += 1

    # Jalankan dengan thread pool
    pending = list(range(1, args.count + 1))
    while pending:
        batch = pending[: args.threads]
        pending = pending[args.threads :]
        threads = [_threading.Thread(target=worker, args=(wid,), daemon=True) for wid in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    elapsed = _dt.datetime.now() - start_time
    total_sec = int(elapsed.total_seconds())
    menit, detik = divmod(total_sec, 60)

    print("\n" + "─" * 46)
    print(f"✅ Berhasil : {results['ok']}")
    if results["fail"]:
        print(f"❌ Gagal    : {results['fail']}")
    print(f"⏱ Waktu    : {menit} menit {detik} detik")
    print(f"📄 Lihat akun di: accounts.txt")
    return 0 if results["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
