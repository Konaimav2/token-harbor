#!/usr/bin/env python3
"""TH-Proxy — proxy management for th-tui.
Supports HTTP/HTTPS + SOCKS5 (with/without auth), live-checking, scraping,
smart balancing, a local HTTP bridge for socks5-auth (Chromium-compatible),
and a public tempmail fallback.
"""
import re
import json
import random
import subprocess
import urllib.request
import urllib.error
import os
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROXY_FILE = BASE / "proxy.txt"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

# ── parsing ──
PROXY_RE = re.compile(
    r"^((?P<proto>http|https|socks4|socks5|socks5h)://)?"
    r"(?:(?P<user>[^:@/\s]+):(?P<pass>[^@/\s]+)@)?"
    r"(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d{1,5})$"
)
# Webshare format: host:port:user:pass (HTTP proxy, no scheme)
PROXY_WEB_RE = re.compile(
    r"^(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d{1,5}):(?P<user>[^:\s]+):(?P<pass>\S+)$"
)


def parse_proxy(raw):
    """Parse a proxy string -> (protocol, host, port, user, pass) or None.
    Supports: http://user:pass@host:port, socks5://host:port, host:port,
    Webshare format host:port:user:pass, and socks5://user:pass:host:port."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    # Relay proxy: https://<anything>.vercel.app (or relay://https://...)
    # normalize relay:// prefix to https://
    if raw.startswith("relay://"):
        raw = raw[len("relay://"):]
    if re.match(r"^https?://[^\s]+$", raw) and "vercel.app" in raw.lower():
        return "relay", raw.rstrip("/").rstrip(":443"), 443, None, None
    # standard format: proto://user:pass@host:port
    m = PROXY_RE.match(raw)
    if m:
        g = m.groupdict()
        proto = (g["proto"] or "http").lower()
        # keep socks5h as socks5h — Chromium local-DNS leaks on plain socks5
        # socks5h = remote DNS via proxy (upstream resolves); bridge handles auth case
        return proto, g["host"], int(g["port"]), g.get("user"), g.get("pass")
    # Webshare host:port:user:pass (no scheme)
    m2 = PROXY_WEB_RE.match(raw)
    if m2:
        return "http", m2.group("host"), int(m2.group("port")), m2.group("user"), m2.group("pass")
    # niceproxy format: proto://user:pass:host:port (colon instead of @)
    m3 = re.match(r"^(?P<proto>http|https|socks4|socks5)://(?P<user>[^:@/]+):(?P<pass>[^:]+):(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d+)$", raw)
    if m3:
        return m3.group("proto"), m3.group("host"), int(m3.group("port")), m3.group("user"), m3.group("pass")
    return None


def proxy_to_playwright(parsed, ctx=None):
    """Build a playwright proxy dict. Sets ctx.proxy when given.

    NOTE: Chromium does NOT support SOCKS5 with username/password auth. For
    such proxies we spawn a local HTTP bridge and return that instead.
    """
    proto, host, port, user, pw = parsed
    # socks5 + auth -> Chromium unsupported; route through a local http bridge
    if proto == "socks5" and user:
        lp = _spawn_http_bridge(parsed)
        if lp:
            proto, host, port, user, pw = lp
    p = {"server": f"{proto}://{host}:{port}"}
    if user:
        p["username"] = user
    if pw:
        p["password"] = pw
    if ctx is not None:
        ctx.set_proxy(**p) if hasattr(ctx, "set_proxy") else None
    return p


# ── local HTTP bridge for socks5-auth / any proxy (Chromium-compatible) ──
import socket as _socket
_bridges = {}


def _spawn_http_bridge(parsed, timeout=20):
    """Spawn a local HTTP CONNECT bridge to a socks5-auth upstream.
    Returns a (http, 127.0.0.1, port, None, None) tuple, or None."""
    key = (parsed[1], parsed[2], parsed[3])
    if key in _bridges:
        return _bridges[key]
    try:
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(50)
        local_port = srv.getsockname()[1]
        t = threading.Thread(target=_bridge_loop, args=(srv, parsed, timeout), daemon=True)
        t.start()
        lp = ("http", "127.0.0.1", local_port, None, None)
        _bridges[key] = lp
        return lp
    except Exception:
        return None


def _bridge_loop(server, upstream, timeout):
    import select as _sel
    while True:
        try:
            client, _addr = server.accept()
            threading.Thread(target=_bridge_handle, args=(client, upstream, timeout), daemon=True).start()
        except Exception:
            try:
                server.close()
            except Exception:
                pass
            return


def _bridge_handle(client, upstream, timeout):
    import select as _sel
    try:
        client.settimeout(timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1", errors="replace").split("\r\n")
        method, target, _ver = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, port = target.partition(":")
            port = int(port) or 443
            up = _socks_connect(upstream, host, port, timeout)
            if up:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    up.sendall(rest)
                _relay(client, up)
            else:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        else:
            # plain HTTP forward
            try:
                import urllib.parse as _up
                pu = _up.urlsplit(target)
                host = pu.hostname
                port = pu.port or 80
                up = _socks_connect(upstream, host, port, timeout)
                if up:
                    path = _up.urlunsplit(("", "", pu.path or "/", pu.query, ""))
                    headers = [l for l in lines[1:] if not l.lower().startswith(("proxy-", "connection:", "keep-alive"))]
                    req = f"{method} {path} {_ver}\r\n" + "\r\n".join(headers) + "\r\nConnection: close\r\n\r\n"
                    up.sendall(req.encode() + rest)
                    _relay(client, up)
                else:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def _socks_connect(upstream, host, port, timeout):
    """Connect to upstream via socks5 with auth. Returns socket or None."""
    import socks
    _ensure_requests_socks()
    proto, uhost, uport, user, pw = upstream
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5, uhost, uport, username=user or None, password=pw or None)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def _relay(a, b):
    import select as _sel
    while True:
        r, _w, _e = _sel.select([a, b], [], [], 60)
        if not r:
            continue
        for s in r:
            try:
                d = s.recv(65536)
                if not d:
                    return
                (b if s is a else a).sendall(d)
            except Exception:
                return


def proxy_url(parsed, hide_creds=False, hide_password=False):
    """Format a proxy URL.
    - hide_creds=False: full user:pass@
    - hide_password=True: show username, redact password (user:***@)
    - hide_creds=True: hide both username and password
    """
    proto, host, port, user, pw = parsed
    if user and not hide_creds:
        if hide_password:
            return f"{proto}://{user}:***@{host}:{port}"
        return f"{proto}://{user}:{pw}@{host}:{port}"
    return f"{proto}://{host}:{port}"


# ── list storage ──
def load_proxies():
    """Load proxy list from file. Returns list of parsed tuples."""
    out = []
    if PROXY_FILE.exists():
        for line in PROXY_FILE.read_text().splitlines():
            p = parse_proxy(line)
            if p:
                out.append(p)
    return out


def save_proxies(proxies):
    PROXY_FILE.write_text("\n".join(proxy_url(p) for p in proxies) + "\n")


def add_proxy(raw):
    p = parse_proxy(raw)
    if not p:
        return None, "Invalid proxy format. Use host:port or http://user:pass@host:port or socks5://host:port"
    proxies = load_proxies()
    if any(x[0]==p[0] and x[1]==p[1] and x[2]==p[2] and x[3]==p[3] for x in proxies):
        return None, "Proxy already in list"
    proxies.append(p)
    save_proxies(proxies)
    return p, None


def remove_proxy(raw):
    p = parse_proxy(raw)
    if not p:
        return False
    proxies = [x for x in load_proxies() if not (x[0]==p[0] and x[1]==p[1] and x[2]==p[2] and x[3]==p[3])]
    save_proxies(proxies)
    return True


def clear_proxies():
    if PROXY_FILE.exists():
        PROXY_FILE.unlink()
    return True


def _ensure_requests_socks():
    """Ensure requests + PySocks are installed (lazy, on-use)."""
    try:
        import importlib.util, sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        spec = importlib.util.spec_from_file_location("thdeps", str(Path(__file__).resolve().parent / "th-deps.py"))
        if spec and spec.loader:
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            m.ensure("proxy_check", auto=True)
    except Exception:
        pass


# ── live checking ──
def check_proxy(parsed, timeout=8):
    """Return (latency_ms, exit_ip, region) if live, else None.
    Region fetched from ipinfo.io (country/city), fallback ip-api.com."""
    _ensure_requests_socks()
    import time
    import requests as rq
    proto, host, port, user, pw = parsed
    # relay proxy: health = relay responds to a direct ping
    if proto == "relay":
        t0 = time.time()
        try:
            r = rq.get(host, timeout=timeout, headers={"User-Agent": random.choice(USER_AGENTS)})
            if r.status_code == 400:  # relay replies 400 when x-relay-target missing — it's alive
                return int((time.time() - t0) * 1000), "relay", ""
            return None
        except Exception:
            return None
    if proto not in ("http", "https", "socks5"):
        return None
    t0 = time.time()
    try:
        if proto == "socks5":
            proxies = {"http": f"socks5h://{host}:{port}", "https": f"socks5h://{host}:{port}"}
            if user:
                proxies = {"http": f"socks5h://{user}:{pw}@{host}:{port}",
                           "https": f"socks5h://{user}:{pw}@{host}:{port}"}
        else:
            proxies = {"http": f"{proto}://{host}:{port}", "https": f"{proto}://{host}:{port}"}
            if user:
                proxies = {"http": f"{proto}://{user}:{pw}@{host}:{port}",
                           "https": f"{proto}://{user}:{pw}@{host}:{port}"}
        # try ipinfo.io for ip + region (country/city)
        try:
            r = rq.get("https://ipinfo.io/json", proxies=proxies,
                       timeout=timeout, headers={"User-Agent": random.choice(USER_AGENTS)})
            j = r.json()
            ip = j.get("ip", "")
            country = j.get("country", "")
            region = j.get("region", "")
            city = j.get("city", "")
            loc = j.get("loc", "")
            region_str = ",".join(x for x in (country, city) if x)
            # Hard checks: proxy must reach real sites + not be Google-flagged.
            # A proxy that passes ipinfo but fails google/cloudflare/recaptcha is
            # dead or flagged for real browsing — kill it.
            for _site in ["https://www.google.com", "https://www.cloudflare.com"]:
                try:
                    rq.get(_site, proxies=proxies, timeout=timeout,
                            headers={"User-Agent": random.choice(USER_AGENTS)})
                except Exception:
                    return None
            # check if proxy IP is flagged by Google (reCAPTCHA block page)
            try:
                _r = rq.get("https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php",
                            proxies=proxies, timeout=timeout,
                            headers={"User-Agent": random.choice(USER_AGENTS)})
                _b = _r.text.lower()
                if "automated queries" in _b or "unusual traffic" in _b or "blocked" in _b:
                    return None
            except Exception:
                pass
            return int((time.time() - t0) * 1000), ip, region_str
        except Exception:
            # fallback: ipify for ip
            r = rq.get("https://api.ipify.org?format=json", proxies=proxies,
                       timeout=timeout, headers={"User-Agent": random.choice(USER_AGENTS)})
            ip = r.json().get("ip", "")
            return int((time.time() - t0) * 1000), ip, ""
    except Exception:
        try:
            r = rq.get("http://ip-api.com/json", proxies=proxies,
                       timeout=timeout, headers={"User-Agent": random.choice(USER_AGENTS)})
            j = r.json()
            ip = j.get("query", "")
            cc = j.get("countryCode", "")
            city = j.get("city", "")
            region_str = ",".join(x for x in (cc, city) if x)
            return int((time.time() - t0) * 1000), ip, region_str
        except Exception:
            return None


def check_proxies(proxies=None, timeout=8):
    """Check all proxies CONCURRENTLY. Returns list of live (parsed, latency, ip, region)."""
    import concurrent.futures as cf
    proxies = proxies or load_proxies()
    if not proxies:
        return []
    results = []
    with cf.ThreadPoolExecutor(max_workers=min(30, len(proxies))) as ex:
        futs = {ex.submit(check_proxy, p, timeout): p for p in proxies}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                # r = (latency, ip, region)
                results.append((p, r[0], r[1], r[2] if len(r) > 2 else ""))
    # sort by latency
    results.sort(key=lambda x: x[1])
    return results


def load_protected_from_config():
    """Read protected proxy identities from the TUI's config.json (all formats).
    Returns a set of host:port:user keys plus v2-style opaque entries as-is."""
    prot = set()
    try:
        cfg_file = Path(__file__).parent / "config.json"
        if cfg_file.exists():
            import json as _json
            c = _json.loads(cfg_file.read_text())
            for entry in c.get("proxy", {}).get("protected", []) or []:
                if ":" in entry and "|" not in entry:
                    # legacy host:port:user -> keep as-is for matching
                    prot.add(entry)
                elif "|" in entry:
                    parts = entry.split("|")
                    if len(parts) >= 4:
                        prot.add(f"{parts[1]}:{parts[2]}:{parts[3]}")
    except Exception:
        pass
    return prot


def keep_live_only(proxies=None, timeout=8, no_delete=False, protected=None):
    if protected is None:
        protected = load_protected_from_config()
    """Filter the stored proxy list down to live ones.
    - no_delete=True: keep ALL dead proxies in the list, still return live ones.
    - protected=list: keep dead proxies that match this list (by host:port:user), delete others.
    """
    live = check_proxies(proxies, timeout)
    if not no_delete:
        live_keys = {(p[0], p[1], p[2], p[3]) for p, *_ in live}
        protected = set(protected or [])
        # keep live + protected (even if dead); drop the rest
        keep = []
        for p, *_ in live:
            keep.append(p)
        for p in (proxies or []):
            key = f"{p[1]}:{p[2]}:{p[3]}"  # host:port:user
            if key in protected or (p[0], p[1], p[2], p[3]) in live_keys:
                if p not in keep:
                    keep.append(p)
        save_proxies(keep)
    return live


def get_live_proxy():
    """Return a random live proxy, or None."""
    live = check_proxies()
    if live:
        return random.choice(live)[0]
    return None


# ── smart balancing (like mailg) ──
import json as _json
PROXY_USE_FILE = Path(__file__).resolve().parent / "proxy-usage.json"
PROXY_CHECK_CACHE_FILE = Path(__file__).resolve().parent / "proxy-check-cache.json"


def load_check_cache():
    """Load cached proxy check results -> {key: {alive: bool, ip: str, ts: float}}."""
    try:
        if PROXY_CHECK_CACHE_FILE.exists():
            return _json.loads(PROXY_CHECK_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_check_cache(cache):
    try:
        PROXY_CHECK_CACHE_FILE.write_text(_json.dumps(cache))
    except Exception:
        pass


def cached_check_proxy(parsed, timeout=8, cache_ttl=3600):
    """Like check_proxy but uses the cache (from the proxy-menu C=Check command)
    when fresh, to override the auto live-check on every use."""
    cache = load_check_cache()
    key = _proxy_key(parsed)
    import time as _t
    ent = cache.get(key)
    if ent and (_t.time() - ent.get("ts", 0)) < cache_ttl:
        if ent.get("alive"):
            return ent.get("latency", 0), ent.get("ip", ""), ent.get("region", "")
        return None
    # cache miss/expired — do a real check
    res = check_proxy(parsed, timeout=timeout)
    cache[key] = {
        "alive": res is not None,
        "ip": res[1] if res else "",
        "latency": res[0] if res else 0,
        "region": res[2] if res and len(res) > 2 else "",
        "ts": _t.time(),
    }
    save_check_cache(cache)
    return res



def _proxy_key(p):
    """Identity key for a proxy (proto, host, port, user)."""
    return f"{p[0]}|{p[1]}|{p[2]}|{p[3] or ''}"


def load_usage():
    """Load proxy usage counts -> {key: count}."""
    try:
        if PROXY_USE_FILE.exists():
            return _json.loads(PROXY_USE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_usage(usage):
    try:
        PROXY_USE_FILE.write_text(_json.dumps(usage))
    except Exception:
        pass


def mark_proxy_used(p):
    """Increment usage count for a proxy."""
    usage = load_usage()
    usage[_proxy_key(p)] = usage.get(_proxy_key(p), 0) + 1
    save_usage(usage)


def smart_pick_proxy(proxies=None, used_ips=None):
    """Pick the best proxy using smart balancing (like mailg).

    Prioritizes proxies that haven't been used, then least-used, then lowest
    latency. Skips proxies whose exit IP is already used this session.
    Returns (parsed_proxy, exit_ip) or (None, None).
    """
    proxies = proxies or load_proxies()
    if not proxies:
        return None, None
    usage = load_usage()
    used_ips = used_ips or set()
    # group by usage count
    scored = []
    for p in proxies:
        key = _proxy_key(p)
        cnt = usage.get(key, 0)
        scored.append((cnt, p))
    # sort: fresh (0 uses) first, then least-used, then random for ties
    scored.sort(key=lambda x: x[0])
    # check live among the least-used first (don't recheck all — too slow)
    import random as _r
    _r.shuffle(scored)  # randomize ties
    scored.sort(key=lambda x: x[0])
    for _cnt, p in scored[:min(12, len(scored))]:  # check top candidates
        res = check_proxy(p, timeout=10)
        if res:
            ip = res[1]
            if ip in used_ips:
                continue  # already used this IP this session
            mark_proxy_used(p)
            return p, ip
    # fallback: any live proxy (already checked ones)
    for _cnt, p in scored:
        res = check_proxy(p, timeout=10)
        if res:
            ip = res[1]
            mark_proxy_used(p)
            return p, ip
    return None, None


# ── scraping ──
SCRAPERS = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc",
]


def scrape_proxies(limit=100, timeout=15):
    """Scrape fresh proxies from public sources. Returns list of parsed tuples.

    `limit` is the target number of proxies to collect across all sources.
    """
    found = []
    for url in SCRAPERS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": random.choice(USER_AGENTS)})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="ignore")
            # geonode returns JSON
            if "geonode" in url:
                import json as _j
                data = _j.loads(text)
                for item in data.get("data", []):
                    ip = item.get("ip", "")
                    port = item.get("port", "")
                    proto = item.get("protocols", ["http"])[0].lower()
                    if ip and port:
                        found.append(parse_proxy(f"{proto}://{ip}:{port}"))
            else:
                for line in text.splitlines():
                    p = parse_proxy(line)
                    if p:
                        found.append(p)
        except Exception:
            continue
        if len(found) >= limit:
            break
    # dedupe
    seen = set()
    out = []
    for p in found:
        key = (p[0], p[1], p[2])
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out[:limit]


# ── public tempmail ──
def get_public_tempmail():
    """Get a fresh public tempmail address (mail.tm). Returns email or None."""
    try:
        import requests as rq
        r = rq.get("https://api.mail.tm/domains", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        # hydra:Collection — domain list under hydra:member
        members = data.get("hydra:member") or data.get("data") or (data if isinstance(data, list) else [])
        active = [d for d in members if isinstance(d, dict) and d.get("isActive", True) and d.get("domain")]
        if not active:
            return None
        domain = active[0]["domain"]
        addr = f"{random_8()}{random_8()}@{domain}"
        r2 = rq.post("https://api.mail.tm/accounts", json={
            "address": addr, "password": "THtmp2026!"}, timeout=10)
        if r2.status_code in (200, 201):
            return addr
    except Exception:
        pass
    return None


def read_public_tempmail(email, password="THtmp2026!", wait=60, interval=5):
    """Read inbox of a mail.tm address, return the newest message dict or None."""
    import time
    import requests as rq
    try:
        # login to get token
        r = rq.post("https://api.mail.tm/token", json={
            "address": email, "password": password}, timeout=10)
        if r.status_code != 200:
            return None
        token = r.json()["token"]
        H = {"Authorization": f"Bearer {token}"}
        deadline = time.time() + wait
        while time.time() < deadline:
            r2 = rq.get("https://api.mail.tm/messages", headers=H, timeout=10)
            if r2.status_code == 200:
                msgs = r2.json().get("hydra:member", [])
                if msgs:
                    # get the full message incl. text/html
                    mid = msgs[0]["id"]
                    r3 = rq.get(f"https://api.mail.tm/messages/{mid}", headers=H, timeout=10)
                    if r3.status_code == 200:
                        return r3.json()
            time.sleep(interval)
    except Exception:
        pass
    return None


def verify_link_from_mail(body):
    """Extract a verification URL from a message body/text."""
    if not body:
        return None
    import re as _re
    m = _re.search(r"https?://[^\s\"'<>]+(?:verify|confirm|activation)[^\s\"'<>]*", body, _re.I)
    if not m:
        m = _re.search(r"https?://[^\s\"'<>]+\.[^\s\"'<>]{3,}", body)
    return m.group(0) if m else None


def read_1secmail(email, wait=60, interval=5):
    """Read 1secmail inbox. Returns the newest message dict or None."""
    import time
    import requests as rq
    try:
        if "@" not in email:
            return None
        login, domain = email.split("@", 1)
        deadline = time.time() + wait
        while time.time() < deadline:
            r = rq.get(
                f"https://www.1secmail.com/api/v1/?action=getMessages&login={login}&domain={domain}",
                timeout=10)
            if r.status_code == 200:
                msgs = r.json()
                if msgs:
                    mid = msgs[-1]["id"]
                    r2 = rq.get(
                        f"https://www.1secmail.com/api/v1/?action=readMessage&login={login}&domain={domain}&id={mid}",
                        timeout=10)
                    if r2.status_code == 200:
                        return r2.json()
            time.sleep(interval)
    except Exception:
        pass
    return None


def random_8():
    import string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


# ── VPNGate residential mode (from proxy-controller) ──
VPNAPI = "https://www.vpngate.net/api/iphone/"


def vpngate_snapshot():
    """Harvest current VPNGate volunteer nodes (free residential exit IPs)."""
    import csv as _csv
    try:
        req = urllib.request.Request(VPNAPI, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            text = res.read().decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line and not line.startswith("*")]
        if lines and lines[0].startswith("#"):
            lines[0] = lines[0][1:]
        nodes = []
        for row in _csv.DictReader(lines):
            ip = row.get("IP")
            if not ip:
                continue
            ping = row.get("Ping", "")
            nodes.append({
                "ip": ip,
                "ping": int(ping) if ping.isdigit() else 9999,
                "country": row.get("CountryShort", "").upper(),
                "hostname": row.get("HostName", ""),
                "socks": row.get("OpenVPN_ConfigData_Base64", ""),
            })
        nodes.sort(key=lambda n: n["ping"])
        return nodes
    except Exception:
        return []


def vpngate_proxy(country=None):
    """Return a HTTP proxy (ip:18080) for the best live VPNGate residential node.

    VPNGate exposes a public proxy on port 18080/direct for each node. Returns
    a parse tuple or None. country: 'JP'/'KR'/None = any.
    """
    nodes = vpngate_snapshot()
    if not nodes:
        return None
    if country:
        nodes = [n for n in nodes if n["country"] == country]
    if not nodes:
        return None
    for n in nodes[:20]:  # try the best few
        p = ("http", n["ip"], 18080, None, None)
        r = check_proxy(p, timeout=6)
        if r:
            return p
    return None


# ── public tempmail pool (multiple addresses for batch) ──
TMPMAIL_PASSWORD = "THtmp2026!"
_tmpmail_pool = []   # list of (address, password)


def _mailtm_domains():
    import requests as rq
    r = rq.get("https://api.mail.tm/domains", timeout=10)
    if r.status_code != 200:
        return []
    data = r.json()
    members = data.get("hydra:member") or data.get("data") or (data if isinstance(data, list) else [])
    return [d["domain"] for d in members if isinstance(d, dict) and d.get("isActive", True) and d.get("domain")]


def _create_mailtm(domain=None):
    import requests as rq
    if not domain:
        doms = _mailtm_domains()
        if not doms:
            return None
        domain = doms[0]
    addr = f"{random_8()}{random_8()}@{domain}"
    r = rq.post("https://api.mail.tm/accounts", json={
        "address": addr, "password": TMPMAIL_PASSWORD}, timeout=10)
    if r.status_code in (200, 201):
        return addr
    return None


def _create_1secmail():
    """1secmail — no account creation needed; mailbox exists implicitly."""
    import requests as rq
    r = rq.get("https://www.1secmail.com/api/v1/?action=genRandomMailbox&count=1", timeout=10)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
    return None


def _create_tempail(domain=None):
    """mail.gw (tempail) — similar to mail.tm hydra API."""
    import requests as rq
    if not domain:
        r = rq.get("https://api.mail.gw/domains", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        members = data.get("hydra:member") or (data if isinstance(data, list) else [])
        active = [d for d in members if isinstance(d, dict) and d.get("domain")]
        if not active:
            return None
        domain = active[0]["domain"]
    addr = f"{random_8()}{random_8()}@{domain}"
    r = rq.post("https://api.mail.gw/accounts", json={
        "address": addr, "password": TMPMAIL_PASSWORD}, timeout=10)
    if r.status_code in (200, 201):
        return addr
    return None


# provider registry: (name, creator_fn, reader_fn)
def _all_creators():
    return [
        ("mail.tm", _create_mailtm, read_public_tempmail),
        ("mail.gw", _create_tempail, read_public_tempmail),
        ("1secmail", _create_1secmail, read_1secmail),
    ]


def create_tempmail(provider=None):
    """Create one tempmail address. provider: 'mail.tm'/'mail.gw'/'1secmail'/None(auto)."""
    for name, creator, _r in _all_creators():
        if provider and name != provider:
            continue
        try:
            addr = creator()
            if addr:
                return name, addr
        except Exception:
            continue
    return None, None


def refill_tempmail_pool(target=5):
    """Ensure at least `target` tempmail addresses exist in the pool."""
    global _tmpmail_pool
    for name, addr in list(_tmpmail_pool):
        if not addr:
            _tmpmail_pool.remove((name, addr))
    while len(_tmpmail_pool) < target:
        name, addr = create_tempmail()
        if not addr:
            break
        _tmpmail_pool.append((name, addr))
    return _tmpmail_pool


def get_tempmail_from_pool():
    """Pop and return one tempmail address from the pool; refills to 5."""
    refill_tempmail_pool(5)
    if not _tmpmail_pool:
        return None
    name, addr = _tmpmail_pool.pop(0)
    # keep pool topped up in the background (non-blocking)
    try:
        refill_tempmail_pool(5)
    except Exception:
        pass
    return addr


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for p in load_proxies():
            print(proxy_url(p, hide_password=True))
    elif cmd == "check":
        rows = check_proxies()
        # optional: sort by region if --by-region passed
        if len(sys.argv) > 2 and sys.argv[2] == "--by-region":
            rows.sort(key=lambda r: (r[3] if len(r) > 3 else ""))
        for row in rows:
            p, lat, ip = row[0], row[1], row[2]
            rg = row[3] if len(row) > 3 else ""
            rgs = f" [{rg}]" if rg else ""
            print(f"live  {lat}ms  {ip}{rgs}  {proxy_url(p, hide_password=True)}")
    elif cmd == "scrape":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        print(f"scraped {len(scrape_proxies(n))}")
    elif cmd == "add":
        if len(sys.argv) > 2:
            p, err = add_proxy(sys.argv[2])
            print(p and "added " + proxy_url(p, hide_password=True) or err)
    elif cmd == "tempmail":
        print(get_public_tempmail() or "failed")
    elif cmd == "vpngate":
        p = vpngate_proxy()
        print(p and proxy_url(p, hide_password=True) or "none")
