#!/usr/bin/env python3
"""L4 TCP proxy tester — fast check if proxies are reachable at TCP level.
Much faster than HTTP checks (no HTTP overhead, just TCP connect).
Reads proxy.txt, tests TCP connect through each proxy, reports live ones.

Usage:
  python3 th-l4.py                  # test all
  python3 th-l4.py --target 1.1.1.1:443   # custom target
  python3 th-l4.py --timeout 5             # custom timeout
"""
import socket
import time
import sys
import concurrent.futures
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROXY_FILE = BASE / "proxy.txt"


def parse_proxy_line(line):
    """Parse proxy line -> (proto, host, port, user, pass) or None."""
    import re
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # standard: proto://user:pass@host:port
    m = re.match(r'^(?P<proto>http|https|socks4|socks5)://(?:(?P<user>[^:@/]+):(?P<pass>[^@/]+)@)?(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d+)$', line)
    if m:
        g = m.groupdict()
        return g["proto"], g["host"], int(g["port"]), g.get("user"), g.get("pass")
    # niceproxy: proto://user:pass:host:port
    m2 = re.match(r'^(?P<proto>http|https|socks4|socks5)://(?P<user>[^:@/]+):(?P<pass>[^:]+):(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d+)$', line)
    if m2:
        return m2.group("proto"), m2.group("host"), int(m2.group("port")), m2.group("user"), m2.group("pass")
    # webshare: host:port:user:pass
    m3 = re.match(r'^(?P<host>[0-9a-zA-Z.\-]+):(?P<port>\d+):(?P<user>[^:\s]+):(?P<pass>\S+)$', line)
    if m3:
        return "http", m3.group("host"), int(m3.group("port")), m3.group("user"), m3.group("pass")
    return None


def tcp_connect(host, port, timeout=5):
    """Raw TCP connect to host:port. Returns latency_ms or None."""
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return int((time.time() - t0) * 1000)
    except Exception:
        return None


def socks5_connect(proxy_host, proxy_port, target_host, target_port, user=None, pw=None, timeout=5):
    """Connect to target through SOCKS5 proxy. Returns latency_ms or None."""
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((proxy_host, proxy_port))
        # SOCKS5 handshake
        s.send(b'\x05\x02\x00\x02')  # version 5, 1 auth method, no auth
        resp = s.recv(2)
        if resp[0] != 5:
            s.close()
            return None
        if resp[1] == 2:  # username/password auth required
            if not user:
                s.close()
                return None
            auth = b'\x01' + bytes([len(user)]) + user.encode() + bytes([len(pw)]) + pw.encode()
            s.send(auth)
            auth_resp = s.recv(2)
            if auth_resp[1] != 0:
                s.close()
                return None
        elif resp[1] == 0xff:
            s.close()
            return None
        # SOCKS5 connect request
        req = b'\x05\x01\x00\x03'  # VER CMD RSV ATYP domain
        req += bytes([len(target_host)]) + target_host.encode()
        req += target_port.to_bytes(2, 'big')
        s.send(req)
        resp = s.recv(10)
        s.close()
        if resp[1] == 0:  # success
            return int((time.time() - t0) * 1000)
        return None
    except Exception:
        return None


def http_connect(proxy_host, proxy_port, target_host, target_port, user=None, pw=None, timeout=5):
    """Connect to target through HTTP CONNECT proxy. Returns latency_ms or None."""
    t0 = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((proxy_host, proxy_port))
        # HTTP CONNECT
        connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n"
        if user:
            import base64
            cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
            connect_req += f"Proxy-Authorization: Basic {cred}\r\n"
        connect_req += "\r\n"
        s.send(connect_req.encode())
        resp = s.recv(4096)
        s.close()
        if b"200" in resp.split(b"\r\n")[0]:
            return int((time.time() - t0) * 1000)
        return None
    except Exception:
        return None


def test_proxy_l4(parsed, target_host="1.1.1.1", target_port=443, timeout=5):
    """L4 test a proxy. Returns (proto, host, port, user, pass, latency_ms, exit_ip) or None."""
    proto, host, port, user, pw = parsed
    if proto == "socks5":
        lat = socks5_connect(host, port, target_host, target_port, user, pw, timeout)
    elif proto in ("http", "https"):
        lat = http_connect(host, port, target_host, target_port, user, pw, timeout)
    else:
        lat = None
    if lat is None:
        return None
    # get exit IP via socks5/http
    exit_ip = None
    try:
        if proto == "socks5":
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            s.send(b'\x05\x02\x00\x02')
            resp = s.recv(2)
            if resp[1] == 2 and user:
                import base64
                auth = b'\x01' + bytes([len(user)]) + user.encode() + bytes([len(pw)]) + pw.encode()
                s.send(auth)
                s.recv(2)
            req = b'\x05\x01\x00\x03'
            req += bytes([len(target_host)]) + target_host.encode()
            req += target_port.to_bytes(2, 'big')
            s.send(req)
            resp = s.recv(10)
            if resp[1] == 0:
                # send HTTP request through SOCKS to get IP
                http_req = f"GET /ip HTTP/1.1\r\nHost: api.ipify.org\r\n\r\n"
                s.send(http_req.encode())
                data = s.recv(4096).decode(errors='ignore')
                import json, re
                m = re.search(r'(\d+\.\d+\.\d+\.\d+)', data)
                if m:
                    exit_ip = m.group(1)
            s.close()
        elif proto in ("http", "https"):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            connect_req = f"CONNECT api.ipify.org:443 HTTP/1.1\r\nHost: api.ipify.org:443\r\n"
            if user:
                import base64
                cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
                connect_req += f"Proxy-Authorization: Basic {cred}\r\n"
            connect_req += "\r\n"
            s.send(connect_req.encode())
            resp = s.recv(4096)
            if b"200" in resp.split(b"\r\n")[0]:
                # TLS to api.ipify.org
                import ssl
                ctx = ssl.create_default_context()
                ssock = ctx.wrap_socket(s, server_hostname="api.ipify.org")
                ssock.send(b"GET /ip HTTP/1.1\r\nHost: api.ipify.org\r\n\r\n")
                data = ssock.recv(4096).decode(errors='ignore')
                import re
                m = re.search(r'"ip":"(\d+\.\d+\.\d+\.\d+)"', data)
                if m:
                    exit_ip = m.group(1)
                ssock.close()
            else:
                s.close()
    except Exception:
        pass
    return proto, host, port, user, pw, lat, exit_ip


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="1.1.1.1:443", help="TCP connect target (host:port)")
    ap.add_argument("--timeout", type=float, default=5, help="Connect timeout per proxy")
    ap.add_argument("--workers", type=int, default=30, help="Concurrent workers")
    args = ap.parse_args()

    target_host, target_port = args.target.split(":")
    target_port = int(target_port)

    # load proxies
    if not PROXY_FILE.exists():
        print(f"No {PROXY_FILE} found")
        return 1
    lines = [l.strip() for l in PROXY_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]
    proxies = []
    for line in lines:
        p = parse_proxy_line(line)
        if p:
            proxies.append(p)
    print(f"L4 test: {len(proxies)} proxies → {target_host}:{target_port} (timeout {args.timeout}s, {args.workers} workers)")
    print()

    t0 = time.time()
    live = []
    dead = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(test_proxy_l4, p, target_host, target_port, args.timeout): p for p in proxies}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            result = fut.result()
            if result:
                proto, host, port, user, pw, lat, exit_ip = result
                status = f"\033[32mLIVE\033[0m"
                ip_str = f" → {exit_ip}" if exit_ip else ""
                user_str = f" ({user})" if user else ""
                print(f"  {status} {lat:5}ms {host}:{port}{user_str} {proto}{ip_str}")
                live.append(result)
            else:
                dead += 1

    elapsed = time.time() - t0
    print()
    print(f"L4 results: {len(live)} LIVE / {dead} DEAD / {len(proxies)} total ({elapsed:.1f}s)")

    if live:
        # write live proxies to l4-live.txt
        live_file = BASE / "l4-live.txt"
        with open(live_file, "w") as f:
            for proto, host, port, user, pw, lat, exit_ip in live:
                if user:
                    f.write(f"{proto}://{user}:{pw}@{host}:{port}\n")
                else:
                    f.write(f"{proto}://{host}:{port}\n")
        print(f"Live proxies saved to {live_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())