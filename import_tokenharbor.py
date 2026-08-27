#!/usr/bin/env python3
"""
import_tokenharbor.py - Import Token Harbor API keys to 9router

v3: verbose logging, --allow-unverified mode, fixed auth
"""

import argparse
import hashlib
import json
import os
import random
import string
import sqlite3
import sys
import traceback
from collections import Counter
from pathlib import Path

import requests as rq

# grok is only needed for local router auth — make it optional (remote mode doesn't need it)
_grok = None
def _get_grok():
    global _grok
    if _grok is None:
        try:
            import grok as _grok
        except ImportError:
            _grok = False
    return _grok or None

ROUTER_BASE_DEFAULT = "https://vibecode.omori.my.id"

BASE_DIR = Path(__file__).parent.resolve()
BASE_URL = "https://tokenharbor.ai/v1"
KEYS_FILE = os.environ.get("KEYS_FILE") or str(BASE_DIR / "keys.txt")
IMPORTED_FILE = BASE_DIR / "imported.txt"

DB_PATHS = [
    os.path.join(os.path.expanduser("~/.9router"), "db", "data.sqlite"),
    os.path.join(os.path.expanduser("~/.9router"), "data.sqlite"),
    os.path.join(os.environ.get("DATA_DIR", ""), "db", "data.sqlite") if os.environ.get("DATA_DIR") else None,
    "/var/lib/9router/db/data.sqlite",
]
DB_PATHS = [p for p in DB_PATHS if p]


def oklog(msg): print(f"  [+] {msg}", flush=True)
def infolog(msg): print(f"  [i] {msg}", flush=True)
def warnlog(msg): print(f"  [!] {msg}", flush=True)  # unverified / warning
def ratelog(msg): print(f"  [~] {msg}", flush=True)  # ratelimited — distinct icon
def elog(msg, detail=""):
    print(f"  [X] {msg}", flush=True)
    if detail:
        for line in str(detail).strip().split("\n"):
            print(f"      {line}", flush=True)


def _md5(s): return hashlib.md5(s.encode()).hexdigest()
def _unique_suffix(): return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def load_keys(path):
    if not os.path.exists(path):
        return []
    keys = []
    for ln in open(path):
        p = ln.strip().split("|")
        if len(p) >= 3 and "@" in p[0] and p[2].startswith("thk_"):
            keys.append((p[0].strip(), p[2].strip()))
    return keys


def load_imported():
    if not IMPORTED_FILE.exists():
        return set()
    with open(IMPORTED_FILE) as f:
        return {ln.strip() for ln in f if ln.strip()}


def mark_imported(key):
    with open(IMPORTED_FILE, "a") as f:
        f.write(_md5(key) + "\n")


def get_connected_keys(db_path=None, remote_db=None):
    """Get the set of apiKeys already connected in the 9router DB.

    Source of truth (in order):
      1. explicit --db path
      2. local DB paths (9router runs on this host)
      3. REMOTE papi DB via SSH (if --remote-db provided)
    Returns None only if every source failed (caller then falls back to local cache).
    """
    candidates = [db_path] if db_path else DB_PATHS
    for p in candidates:
        if not p or not os.path.exists(p):
            continue
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            rows = con.execute("SELECT data FROM providerConnections").fetchall()
            con.close()
            keys = set()
            for (d,) in rows:
                if not d:
                    continue
                try:
                    obj = json.loads(d)
                except Exception:
                    continue
                k = obj.get("apiKey")
                if k and isinstance(k, str):
                    keys.add(k)
            if keys or rows:
                return keys
        except Exception as e:
            warnlog(f"DB read error: {e}")
    
    # Try remote DB via SSH
    if remote_db:
        try:
            import subprocess as _sp
            from pathlib import Path as _Path
            ssh = f"ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=no {remote_db}"
            helper = str(_Path(__file__).resolve().parent.parent / "_remote_dedup.py")
            r = _sp.run(f"{ssh} python3 -", shell=True,
                        input=open(helper).read(), capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                keys = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
                infolog(f"{len(keys)} key terhubung (dari remote DB via SSH)")
                return keys
            warnlog(f"Remote DB SSH failed: {r.stderr.strip()[:120] or 'no output'}")
        except Exception as e:
            warnlog(f"Remote DB SSH error: {str(e)[:120]}")
    return None


def get_providers(router_base, token):
    try:
        resp = rq.get(f"{router_base}/api/providers",
                      cookies={"auth_token": token},
                      headers={"Accept": "application/json"}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("connections", [])
        warnlog(f"GET /api/providers -> {resp.status_code}: {resp.text[:100]}")
        return []
    except Exception as e:
        elog(f"GET /api/providers failed: {e}", traceback.format_exc()[:200])
        return []


def get_provider_nodes(router_base, token):
    try:
        resp = rq.get(f"{router_base}/api/provider-nodes",
                      cookies={"auth_token": token},
                      headers={"Accept": "application/json"}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("nodes", [])
        warnlog(f"GET /api/provider-nodes -> {resp.status_code}: {resp.text[:100]}")
        return []
    except Exception as e:
        elog(f"GET /api/provider-nodes failed: {e}", traceback.format_exc()[:200])
        return []


def unique_name(base, used_names):
    name = base
    while name in used_names:
        name = f"{base}-{_unique_suffix()}"
    return name


def create_provider_node(router_base, token, provider_nodes=None, name="TokenHarbor", prefix="tkhb", prov_type="openai"):
    """Create a new provider node on 9router. Reuses existing node if one with same baseUrl exists."""
    base_url = "https://tokenharbor.ai/v1"
    # check for existing node with matching baseUrl first (never duplicate)
    if provider_nodes:
        for n in provider_nodes:
            if n.get("baseUrl", "").rstrip("/") == base_url.rstrip("/"):
                nid = n.get("id", "")
                oklog("Provider node '" + n.get("name", "?") + "' already exists: " + nid[:40] + "...")
                return nid
    try:
        payload = {"name": name, "prefix": prefix, "apiType": "chat", "baseUrl": base_url}
        resp = rq.post(f"{router_base}/api/provider-nodes", json=payload,
                       cookies={"auth_token": token},
                       headers={"Accept": "application/json"}, timeout=20)
        if resp.status_code == 201:
            node = resp.json().get("node", {})
            node_id = node.get("id", "")
            oklog("Provider node '" + name + "' created: " + node_id[:40] + "...")
            return node_id
        elog("Create provider node failed: HTTP " + str(resp.status_code), resp.text[:200])
        return None
    except Exception as e:
        elog("Create provider node error: " + str(e), traceback.format_exc()[:200])
        return None


def find_or_create_provider(providers, api_base, router_base, token, prov_type, first_key, node_id, dry_run=False):
    """Find a provider whose connections use the TokenHarbor baseUrl; else create first connection."""
    candidates = []
    for c in providers:
        data = c.get("providerSpecificData") or {}
        url = (data.get("baseUrl") or "").strip().rstrip("/")
        if url == api_base.rstrip("/"):
            candidates.append(c)

    if candidates:
        cnt = Counter(c.get("provider") for c in candidates)
        target = cnt.most_common(1)[0][0]
        example = next((c for c in candidates if c.get("provider") == target), candidates[0])
        default_model = example.get("defaultModel") or "gpt-4o-mini"
        max_pri = max((c.get("priority") or 1) for c in candidates if c.get("provider") == target) or 0
        oklog(f"Provider TokenHarbor ditemukan: {target}")
        return target, default_model, max_pri

    # No existing connection with that baseUrl -> create a TokenHarbor node automatically
    provider_nodes = get_provider_nodes(router_base, token)
    if node_id:
        oklog(f"Menggunakan node yang ditentukan: {node_id[:40]}...")
    else:
        # auto-create a dedicated TokenHarbor node (never reuse another provider's node)
        warnlog("Belum ada koneksi TokenHarbor — membuat provider node baru 'TokenHarbor' (tkhb)...")
        if dry_run:
            print("  [dry-run] Would POST /api/provider-nodes {name:'TokenHarbor', prefix:'tkhb', baseUrl:'https://tokenharbor.ai/v1'}")
            return "openai-compatible-chat-TokenHarbor(tkhb)", "gpt-4o-mini", 0
        created = create_provider_node(router_base, token, provider_nodes=provider_nodes, name="TokenHarbor", prefix="tkhb", prov_type=prov_type)
        if not created:
            compat_nodes = [n for n in provider_nodes if (prov_type == "openai" and "openai-compatible-chat" in n.get("id","")) or (prov_type == "anthropic" and "anthropic-compatible" in n.get("id",""))]
            print("  Node yang tersedia (gunakan --provider <node_id>):")
            for n in compat_nodes:
                print(f"    - {n.get('name','?')}: {n.get('id','')}")
            return None, None, None
        node_id = created

    if not first_key:
        warnlog("Tidak ada API key untuk membuat koneksi")
        return None, None, None

    if dry_run:
        print(f"  [dry-run] Would POST to {router_base}/api/providers")
        print(f"    provider={node_id[:30]}..., apiKey={first_key[:16]}..., name=Harbor 1")
        return node_id, "gpt-4o-mini", 1

    oklog(f"Membuat koneksi pertama di node {node_id[:40]}...")
    payload = {"provider": node_id, "apiKey": first_key, "name": "Harbor 1",
               "priority": 1, "testStatus": "active"}
    try:
        resp = rq.post(f"{router_base}/api/providers", json=payload,
                       cookies={"auth_token": token},
                       headers={"Accept": "application/json"}, timeout=20)
        if resp.status_code in (200, 201):
            oklog("Koneksi pertama berhasil!")
            return node_id, "gpt-4o-mini", 1
        elog(f"BUAT KONEKSI GAGAL: HTTP {resp.status_code}", resp.text[:300])
        return None, None, None
    except Exception as e:
        elog(f"BUAT KONEKSI ERROR: {e}", traceback.format_exc()[:300])
        return None, None, None


def check_key_status(api_key):
    """Check TokenHarbor key. Returns (status, detail).
    status: 'available' (verified, usable) | 'not_verified' | 'ratelimited' | 'error'."""
    try:
        resp = rq.get("https://tokenharbor.ai/v1/models",
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_cnt = len(data.get("data", []))
            return ("available", f"{model_cnt} models")
        if resp.status_code == 403:
            body = resp.text.lower()
            if "verify" in body:
                return ("not_verified", "email not verified")
            return ("not_verified", resp.text[:120])
        if resp.status_code == 429:
            return ("ratelimited", "rate limited (429)")
        body = resp.text.lower()
        if "rate" in body or "limit" in body or "throttl" in body:
            return ("ratelimited", resp.text[:120])
        return ("error", f"HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        return ("error", str(e)[:100])


def check_keys_parallel(pending, workers=8):
    """Check all keys in parallel (much faster than sequential)."""
    import concurrent.futures
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_key_status, key): (email, key) for email, key in pending}
        for fut in concurrent.futures.as_completed(futures):
            email, key = futures[fut]
            try:
                results[(email, key)] = fut.result()
            except Exception as e:
                results[(email, key)] = ("error", str(e)[:100])
    return results


def main():
    ap = argparse.ArgumentParser(description="Import Token Harbor API keys ke 9router")
    ap.add_argument("--file", default=KEYS_FILE)
    ap.add_argument("--router-base", default=ROUTER_BASE_DEFAULT)
    ap.add_argument("--router-password", default=None)
    ap.add_argument("--provider", default=None, help="provider node id")
    ap.add_argument("--type", choices=["openai", "anthropic"], default="openai")
    ap.add_argument("--prefix", default="Harbor", help="prefix nama connection")
    ap.add_argument("--start-priority", type=int, default=None)
    ap.add_argument("--default-model", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    ap.add_argument("--remote-db", default=None, help="SSH user@host for remote 9router DB")
    ap.add_argument("--no-db-check", action="store_true")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="import meskipun key unverified")
    ap.add_argument("--skip-verify", action="store_true", default=True,
                    help="Skip re-checking keys (trust keys.txt status). Default ON. Use --check-keys to force re-check.")
    ap.add_argument("--check-keys", action="store_true", default=False,
                    help="Force re-check every key via API before import (slow).")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel workers for import (default 8)")
    args = ap.parse_args()

    keys = load_keys(args.file)
    if not keys:
        warnlog(f"Tidak ada API key di {args.file}")
        return 1
    oklog(f"Ditemukan {len(keys)} API key unik")

    # ── Auth token ──
    token = ""
    if args.router_password:
        try:
            sess = rq.Session()
            login_resp = sess.post(f"{args.router_base}/api/auth/login",
                                   json={"password": args.router_password},
                                   headers={"Content-Type": "application/json"}, timeout=15)
            if login_resp.status_code == 200 and login_resp.json().get("success"):
                token = sess.cookies.get("auth_token", "")
                if token:
                    oklog("Remote login OK (auth_token cookie)")
                else:
                    warnlog("Login OK tapi tidak ada auth_token cookie")
            else:
                elog(f"Login gagal: HTTP {login_resp.status_code}", login_resp.text[:200])
        except Exception as e:
            elog(f"Login error: {e}", traceback.format_exc()[:200])

    if not token:
        g = _get_grok()
        if g:
            token = g.generate_router_token()
            infolog("Pakai JWT lokal")
        else:
            elog("No remote auth and grok module not available (local JWT generation needs curl_cffi)")
            return

    # ── Duplicate detection ──
    # The 9router DB is the AUTHORITY for what's genuinely imported: if a key was
    # deleted from the DB, it MUST be re-importable. The local imported.txt is only
    # a fallback accelerator used when the DB can't be read.
    dup = {}
    connected = None
    db_read_ok = False
    if not args.no_db_check:
        connected = get_connected_keys(args.db, args.remote_db)
        if connected is None:
            infolog("DB 9router tidak terbaca (deteksi duplikat dilewati)")
        else:
            db_read_ok = True
            infolog(f"{len(connected)} key sudah terhubung di 9router")
            new_keys = []
            for e, k in keys:
                if k in connected:
                    dup[(e, k)] = True
                else:
                    new_keys.append((e, k))
            if dup:
                warnlog(f"{len(dup)} key duplikat (dilewati)")
            keys = new_keys
            infolog(f"Key baru: {len(keys)}")

    if not keys:
        infolog("Semua key sudah terhubung")
        return 0

    # Local imported.txt only filters when the DB wasn't consulted (offline/legacy).
    # When the DB IS readable it's authoritative — local md5s must NOT block a key
    # that no longer exists in the DB (e.g. it was deleted from 9router).
    pending = []
    if db_read_ok or args.force:
        pending = [(e, k) for e, k in keys]
    else:
        imported = load_imported()
        pending = [(e, k) for e, k in keys if _md5(k) not in imported]
        oklog(f"Belum di-import (via local cache): {len(pending)}")
    if pending:
        oklog(f"Key akan di-import: {len(pending)}")
    else:
        infolog("Semua key sudah pernah di-import (--force untuk ulang)")
        return 0

    # ── Step 1: Provider setup ──
    print(f"\n{'='*50}")
    print(f"  STEP 1: Provider setup")
    print(f"{'='*50}")

    all_providers = get_providers(args.router_base, token)
    if not all_providers:
        warnlog("Tidak bisa ambil daftar provider dari 9router")

    first_key = pending[0][1] if pending else ""
    target, default_model, max_pri = find_or_create_provider(
        all_providers, BASE_URL, args.router_base, token, args.type, first_key, args.provider, args.dry_run)
    if not target:
        warnlog("Gagal setup provider")
        return 1

    if args.default_model:
        default_model = args.default_model
    start_pri = args.start_priority if args.start_priority is not None else ((max_pri or 0) + 1)
    oklog(f"Target: {target[:40]} | model: {default_model} | priority: {start_pri}")

    # ── Step 2: Verification (parallel) ──
    available = []
    not_verified = []
    ratelimited = []
    errors = []
    
    if args.check_keys:
        # User explicitly wants to re-check every key via API
        print(f"\n{'='*50}")
        print(f"  STEP 2: Check {len(pending)} keys (parallel)")
        print(f"{'='*50}")
        status_map = check_keys_parallel(pending)
        for email, key in pending:
            status, detail = status_map.get((email, key), ("error", "no result"))
            if status == "available":
                available.append((email, key))
            elif status == "not_verified":
                not_verified.append((email, key))
                warnlog(f"{email}: NOT VERIFIED ({detail})")
            elif status == "ratelimited":
                ratelimited.append((email, key))
                ratelog(f"{email}: RATELIMITED ({detail})")
            else:
                errors.append((email, key))
                warnlog(f"{email}: {detail}")

        oklog(f"Available (will import): {len(available)}")
        if not_verified:
            warnlog(f"Skipped (not verified): {len(not_verified)}")
        if ratelimited:
            warnlog(f"Skipped (ratelimited): {len(ratelimited)}")
        if errors:
            warnlog(f"Skipped (errors): {len(errors)}")
        pending = available
        if not pending:
            warnlog("No available (verified) keys to import")
            return 0
    else:
        # DEFAULT: skip check, trust keys.txt verified status
        print(f"\n{'='*50}")
        print(f"  STEP 2: Skipping key re-check (keys already verified in keys.txt)")
        print(f"{'='*50}")
        available = pending
        oklog(f"Trusted (no re-check): {len(available)}")

    print(f"\n{'='*50}")
    print(f"  STEP 3: Import ke 9router")
    print(f"{'='*50}")

    used_names = {str(c.get("name") or "").strip() for c in all_providers or [] if c.get("name")}
    # continue numbering from the highest existing "Harbor N" instead of restarting at 1
    import re as _re
    max_n = 0
    for n in used_names:
        m = _re.match(rf"^{args.prefix}\s+(\d+)$", n.strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    start_conn = (max_n or 0) + 1
    if max_n:
        infolog(f"Melanjutkan penomoran dari {args.prefix} {max_n} -> mulai {start_conn}")
    ok = fail = 0

    if args.dry_run:
        print("\n  === DRY-RUN ===")
        _used = set(used_names)
        for i, (email, key) in enumerate(pending, 1):
            name = unique_name(f"{args.prefix} {i}", _used)
            _used.add(name)
            print(f"  POST {args.router_base}/api/providers")
            print(f"    name={name!r}")
            print(f"    key= {key[:16]}...")
        print(f"\n  Akan menambah {len(pending)} koneksi")
        return 0

    def _import_one(email, key, i):
        """POST one key to 9router. Returns (email, name, priority, status, detail)."""
        name = f"{args.prefix} {i}"
        priority = start_pri + (i - start_conn)
        payload = {"provider": target, "apiKey": key, "name": name,
                   "priority": priority, "defaultModel": default_model, "testStatus": "unknown"}
        try:
            resp = rq.post(f"{args.router_base}/api/providers", json=payload,
                           cookies={"auth_token": token},
                           headers={"Accept": "*/*", "Content-Type": "application/json"}, timeout=20)
        except Exception as e:
            return (email, name, priority, "error", str(e)[:200])
        if resp.status_code in (200, 201):
            mark_imported(key)
            cid = (resp.json().get("connection") or {}).get("id", "?")
            return (email, name, priority, "ok", cid)
        try:
            err = resp.json().get("error") or resp.text[:200]
        except Exception:
            err = resp.text[:200]
        return (email, name, priority, "fail", err)

    # Parallel import (like STEP 2). Workers configurable.
    import concurrent.futures
    import threading
    _print_lock = threading.Lock()
    ok = fail = 0
    _it = list(enumerate(pending, start_conn))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_import_one, email, key, i): (email, i) for i, (email, key) in _it}
        for fut in concurrent.futures.as_completed(futs):
            email, name, priority, status, detail = fut.result()
            used_names.add(name)
            with _print_lock:
                if status == "ok":
                    ok += 1
                    oklog(f"[{name.split()[-1]}] {name} -> connection {detail} (prio {priority})")
                elif status == "error":
                    fail += 1
                    elog(f"[{name.split()[-1]}] POST gagal: {email[:20]}", detail)
                else:
                    fail += 1
                    elog(f"[{name.split()[-1]}] DITOLAK: {email[:20]}", detail)

    print(f"\n{'='*50}")
    oklog(f"Sukses: {ok}  |  Gagal: {fail}  |  Total: {len(pending)}")
    print(f"{'='*50}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
