#!/usr/bin/env python3
"""egress — expose each machine IPv4 as a local forward proxy.

This host has 2 public IPv4s but all traffic leaves via .16 by default.
For each extra IP we run a tiny localhost-only HTTP CONNECT proxy whose
OUTBOUND sockets bind to that source IP. The farm then treats
http://127.0.0.1:18091, :18092, ... as ordinary pool entries, so rotation,
cooldowns and failure counting work unchanged — while TokenHarbor sees
distinct egress IPs (separate rate-limit budgets, zero proxy cost).

Security: listeners bind 127.0.0.1 ONLY. No auth (loopback). Never expose.
"""
import asyncio
import ipaddress
import json
import os
import socket
import subprocess
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "proxy" / "egress.json"
BASE_PORT = 18091
PIDFILE = BASE / "proxy" / "egress.pid"
STATE_MAX_AGE = 600  # 10 minutes


def detect_egress_ips():
    """Public (non-private, non-loopback) IPv4s on this host, sorted."""
    out = []
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ip = parts[3].split("/")[0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_private or addr.is_multicast:
                continue
            if ip not in out:
                out.append(ip)
    except Exception:
        pass
    return sorted(out)


def _port_open(port):
    s = socket.socket()
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _load_state_validated():
    """Return STATE_FILE mapping, or [] if stale (old or ports dead)."""
    import time
    try:
        st = STATE_FILE.stat()
        if time.time() - st.st_mtime > STATE_MAX_AGE:
            return []
        state = json.loads(STATE_FILE.read_text())
    except Exception:
        return []
    try:
        ports = [int(e["proxy"].rsplit(":", 1)[1]) for e in state]
    except Exception:
        return []
    if not ports:
        return []
    if not all(_port_open(p) for p in ports):
        return []  # stale: regenerate on next ensure call
    return state


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_r, client_w, src_ip):
    peer = None
    try:
        head = await client_r.readuntil(b"\r\n\r\n")
    except Exception:
        try:
            client_w.close()
        except Exception:
            pass
        return
    try:
        request_line = head.split(b"\r\n", 1)[0].decode("latin1")
        method, target = request_line.split()[:2]
    except Exception:
        try:
            client_w.close()
        except Exception:
            pass
        return
    try:
        if method.upper() == "CONNECT":
            host, _, port = target.partition(":")
            port = int(port or 443)
            remote_r, remote_w = await asyncio.open_connection(
                host, port, local_addr=(src_ip, 0))
            client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_w.drain()
            await asyncio.gather(_pipe(client_r, remote_w), _pipe(remote_r, client_w))
        else:
            # plain-HTTP absolute URI
            from urllib.parse import urlsplit
            u = urlsplit(target)
            host, port = u.hostname or "", u.port or 80
            if not host:
                client_w.close()
                return
            remote_r, remote_w = await asyncio.open_connection(
                host, port, local_addr=(src_ip, 0))
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            out_head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
            # forward remaining headers except proxy-hop ones
            for line in head.decode("latin1").split("\r\n")[1:]:
                if not line or ":" not in line:
                    continue
                k = line.split(":", 1)[0].strip().lower()
                if k in ("host", "connection", "proxy-connection",
                         "proxy-authenticate", "proxy-authorization"):
                    continue
                out_head += line + "\r\n"
            out_head += "\r\n"
            remote_w.write(out_head.encode("latin1"))
            await remote_w.drain()
            await asyncio.gather(_pipe(client_r, remote_w), _pipe(remote_r, client_w))
    except Exception:
        try:
            client_w.close()
        except Exception:
            pass
    finally:
        try:
            peer = client_w.get_extra_info("peername")
        except Exception:
            pass


def _serve_forever(port, src_ip):
    async def _main():
        server = await asyncio.start_server(
            lambda r, w: _handle(r, w, src_ip), "127.0.0.1", port)
        async with server:
            await server.serve_forever()
    asyncio.run(_main())


_threads = {}
_lock = threading.Lock()


def ensure_egress_proxies():
    """Start (or reuse) one localhost proxy per egress IP.

    Returns [{"ip": src, "proxy": "http://127.0.0.1:PORT"}, ...].
    Idempotent across processes: a port already listening is reused.
    """
    out = []
    with _lock:
        for i, ip in enumerate(detect_egress_ips()):
            port = BASE_PORT + i
            if port not in _threads or not _threads[port].is_alive():
                if not _port_open(port):
                    t = threading.Thread(target=_serve_forever, args=(port, ip),
                                         daemon=True)
                    t.start()
                    _threads[port] = t
            out.append({"ip": ip, "proxy": f"http://127.0.0.1:{port}"})
    # wait briefly for fresh listeners
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        if all(_port_open(BASE_PORT + i) for i in range(len(out))):
            break
        time.sleep(0.3)
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass
    return out


def egress_ip_for_proxy(parsed):
    """Map a pool entry back to its egress IP (or '' if not an egress entry)."""
    try:
        if parsed and parsed[0] == "http" and parsed[1] in ("127.0.0.1", "localhost"):
            state = _load_state_validated()
            if not _port_open(int(parsed[2])):
                return ""  # stale STATE_FILE (daemon crashed): fail closed
            for ent in state:
                try:
                    if int(ent["proxy"].rsplit(":", 1)[1]) == int(parsed[2]):
                        return ent["ip"]
                except Exception:
                    pass
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    import sys
    info = ensure_egress_proxies()
    print(json.dumps(info, indent=2))
    if "--serve" in sys.argv:
        import time
        try:
            PIDFILE.parent.mkdir(parents=True, exist_ok=True)
            if PIDFILE.exists():
                try:
                    old = int(PIDFILE.read_text().strip().split()[0])
                except Exception:
                    old = 0
                if old and (_pid_alive(old) or _port_open(BASE_PORT)):
                    print(f"already running (pid {old})", file=sys.stderr)
                    sys.exit(1)
            PIDFILE.write_text(str(os.getpid()))
        except SystemExit:
            raise
        except Exception:
            pass
        print("serving (Ctrl+C to stop)...")
        while True:
            time.sleep(3600)
