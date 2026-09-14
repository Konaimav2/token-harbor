#!/usr/bin/env python3
"""keystore — JSON account/key store replacing keys.txt + imported.txt.

Why: pipe-delimited keys.txt corrupts on '|' in passwords, imported.txt's
md5 list can't say WHICH key or WHEN, and concurrent read-modify-writes lose
records. This store keeps one record per email:

  {email, password, api_key, status, imported, imported_at, connection_id,
   fingerprint, updated_at}

- imported flag survives Ctrl+C mid-import (set per key, right after POST).
- atomic writes (tmp + fsync + os.replace) under an fcntl lock.
- first load migrates keys.txt + imported.txt + key-checks.json states;
  originals are backed up to _backup_files/ and keys.txt is regenerated
  (read-only mirror) so legacy readers keep working during transition.
"""
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
JSON_PATH = DATA_DIR / "keys.json"
LEGACY_KEYS = BASE / "keys.txt"          # symlink -> data/keys.txt on farm hosts
LEGACY_IMPORTED = BASE / "import" / "imported.txt"
LEGACY_IMPORTED2 = BASE / "data" / "imported.txt"
LEGACY_CHECKS = BASE / "key-checks.json"
BACKUP_DIR = BASE / "_backup_files"


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _sha16(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()[:16]


def _blank(email=""):
    return {"email": email, "password": "", "api_key": "",
            "status": "pending", "imported": False, "imported_at": 0,
            "connection_id": "", "fingerprint": "",
            "health": {"state": "", "reason": "", "checked_at": 0},
            "updated_at": _now()}


def _now():
    return int(time.time())


class KeyStore:
    def __init__(self, path=None):
        self.path = Path(path or JSON_PATH)
        self._lock_fh = None
        self._depth = 0

    # ── locking ──
    def _lock(self):
        if getattr(self, "_depth", 0) > 0:
            self._depth += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self.path.with_suffix(".json.lock"), "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX)
        except Exception as e:
            try:
                self._lock_fh.close()
            except Exception:
                pass
            self._lock_fh = None
            raise RuntimeError(f"keystore lock failed: {e}")
        self._depth = 1
        return self

    def _unlock(self):
        self._depth = max(0, getattr(self, "_depth", 1) - 1)
        if self._depth > 0:
            return self
        try:
            if self._lock_fh:
                fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                self._lock_fh.close()
        except Exception:
            pass
        self._lock_fh = None
        return self

    def __enter__(self):
        return self._lock()

    def __exit__(self, *a):
        return self._unlock() is None

    # ── load / save ──
    def _quarantine(self, why):
        """Move a corrupt store aside (never overwrite in place)."""
        import time as _t
        bak = self.path.with_name(f"{self.path.stem}.corrupt-{int(_t.time())}.json")
        try:
            self.path.rename(bak)
            print(f"[keystore] quarantined {self.path.name} -> {bak.name} ({why})")
        except Exception as e:
            print(f"[keystore] quarantine failed: {e}")

    def load(self):
        """Return list of records (migrates legacy files on first run)."""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except Exception as e:
                data = None
                self._quarantine(f"unparseable: {e}")
            if isinstance(data, list):
                with self:
                    if self._backfill_health(data):
                        self.save(data)
                return data
            if data is not None:
                self._quarantine(f"unexpected {type(data).__name__}, want list")
        recs = self._migrate()
        with self:
            self.save(recs)
        return recs

    def _backfill_health(self, recs):
        """Fold key-checks.json into records missing health. Returns changed."""
        try:
            if not any(not r.get("health", {}).get("state") for r in recs):
                return False
            checks = {}
            for cand in (BASE / "key-checks.json", DATA_DIR / "key-checks.json"):
                if cand.exists():
                    checks = json.loads(cand.read_text()) or {}
                    break
            if not checks:
                return False
            changed = False
            for r in recs:
                if r.get("health", {}).get("state"):
                    continue
                ent = checks.get(r.get("email", ""), {})
                if isinstance(ent, dict) and ent.get("fingerprint") == r.get("fingerprint") \
                        and ent.get("state"):
                    r["health"] = {"state": ent.get("state", ""),
                                   "reason": str(ent.get("reason", ""))[:160],
                                   "checked_at": int(ent.get("checked_at", 0))}
                    changed = True
            return changed
        except Exception:
            return False

    def set_health_bulk(self, mapping):
        """mapping: {email: (state, reason)}. Single locked atomic save."""
        conv = {e: {"state": v[0], "reason": v[1]} if isinstance(v, tuple) else v
                for e, v in (mapping or {}).items()}
        return self.set_health_records(conv)

    def set_health_records(self, mapping):
        """mapping: {email: {state, reason, checked_at?, fingerprint?}}."""
        with self:
            recs = self.load()
            by_email = {r.get("email", "").lower(): r for r in recs}
            changed = False
            for email, ent in (mapping or {}).items():
                if not isinstance(ent, dict):
                    continue
                r = by_email.get((email or "").strip().lower())
                if r is None:
                    continue
                fp = ent.get("fingerprint", "")
                if fp and fp != r.get("fingerprint") and fp != _sha16(r.get("api_key", "")):
                    continue  # stale entry for a rotated key — never attach
                r["health"] = {"state": ent.get("state", ""),
                               "reason": str(ent.get("reason", ""))[:160],
                               "checked_at": int(ent.get("checked_at", 0) or _now())}
                if fp:
                    r["health"]["fingerprint"] = fp
                r["updated_at"] = _now()
                changed = True
            if changed:
                self.save(recs)
            return changed

    def save(self, records):
        """Atomic persist (callers should hold the lock for read-modify-write)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix="." + self.path.name + ".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(records, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass
        self._write_legacy_mirror(records)
        return True

    # ── migration ──
    def _read_lines(self, path):
        try:
            p = Path(path)
            if p.exists():
                return [l.rstrip("\n") for l in p.read_text().splitlines() if l.strip()]
        except Exception:
            pass
        return []

    def _migrate(self):
        imported_md5 = set()
        for src in (LEGACY_IMPORTED, LEGACY_IMPORTED2):
            for ln in self._read_lines(src):
                imported_md5.add(ln.strip())
        checks = {}
        try:
            if LEGACY_CHECKS.exists():
                checks = json.loads(LEGACY_CHECKS.read_text()) or {}
        except Exception:
            pass
        recs, seen = [], set()
        # keys.txt rows: email|password|apikey|status (password may contain '|':
        # split from the right so the key/status columns stay aligned)
        key_files = [LEGACY_KEYS]
        if DATA_DIR.joinpath("keys.txt") not in [LEGACY_KEYS] and (DATA_DIR / "keys.txt").exists():
            key_files.append(DATA_DIR / "keys.txt")
        for kf in key_files:
            for ln in self._read_lines(kf):
                parts = ln.split("|")
                if len(parts) < 2 or "@" not in parts[0]:
                    continue
                email = parts[0].strip().lower()
                if email in seen:
                    continue
                seen.add(email)
                if len(parts) >= 4:
                    password = "|".join(parts[1:-2])
                    api_key, status = parts[-2].strip(), parts[-1].strip() or "pending"
                elif len(parts) == 3:
                    password, api_key, status = parts[1], parts[2].strip(), "pending"
                else:
                    password, api_key, status = parts[1], "", "unused"
                fp = _md5(api_key) if api_key else ""
                rec = _blank(email)
                rec.update({"password": password, "api_key": api_key,
                            "status": status,
                            "imported": bool(fp and fp in imported_md5),
                            "fingerprint": fp})
                recs.append(rec)
        # fold in key-checks.json health cache (email+fingerprint match)
        try:
            checks = {}
            for cand in (BASE / "key-checks.json", DATA_DIR / "key-checks.json"):
                if cand.exists():
                    checks = json.loads(cand.read_text()) or {}
                    break
            for r in recs:
                ent = checks.get(r["email"], {})
                if isinstance(ent, dict) and ent.get("fingerprint") == r.get("fingerprint") \
                        and ent.get("state"):
                    r["health"] = {"state": ent.get("state", ""),
                                   "reason": str(ent.get("reason", ""))[:160],
                                   "checked_at": int(ent.get("checked_at", 0))}
        except Exception:
            pass
        # backup originals once
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            tag = time.strftime("%Y%m%d_%H%M%S")
            for src in (LEGACY_KEYS, LEGACY_IMPORTED, LEGACY_IMPORTED2):
                p = Path(src)
                if p.exists() and not p.is_symlink():
                    dst = BACKUP_DIR / f"{p.name}.pre-json-{tag}"
                    if not dst.exists():
                        dst.write_bytes(p.read_bytes())
        except Exception:
            pass
        return recs

    def _write_legacy_mirror(self, records):
        """Regenerate pipe-delimited keys.txt for legacy readers (best effort).

        Passwords containing '|' are sanitized to '_' in the mirror only —
        the JSON record keeps the real value.
        """
        try:
            targets = {DATA_DIR / "keys.txt"}
            if LEGACY_KEYS.is_symlink():
                return  # mirror IS data/keys.txt already
            targets.add(LEGACY_KEYS)
            lines = []
            for r in records:
                pw = str(r.get("password", "")).replace("|", "_").replace("\n", "")
                lines.append("|".join([r.get("email", ""), pw,
                                       r.get("api_key", ""), r.get("status", "pending")]))
            text = "\n".join(lines) + ("\n" if lines else "")
            for t in targets:
                t.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(t.parent), prefix=".keys.txt.tmp")
                with os.fdopen(fd, "w") as f:
                    f.write(text)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(tmp, Path(os.path.realpath(t)))
        except Exception:
            pass

    # ── record ops (each locks) ──
    def upsert(self, email, password=None, api_key=None, status=None, **kw):
        with self:
            recs = self.load()
            email = (email or "").strip().lower()
            rec = next((r for r in recs if r.get("email") == email), None)
            if rec is None:
                rec = _blank(email)
                recs.append(rec)
            if password is not None:
                rec["password"] = password
            if api_key is not None:
                rec["api_key"] = api_key
                rec["fingerprint"] = _md5(api_key) if api_key else rec.get("fingerprint", "")
            if status is not None:
                rec["status"] = status
            for k in ("imported", "imported_at", "connection_id"):
                if k in kw:
                    rec[k] = kw[k]
            rec["updated_at"] = _now()
            self.save(recs)
            return dict(rec)

    def mark_imported(self, api_key=None, email=None, connection_id=""):
        with self:
            recs = self.load()
            changed = False
            for r in recs:
                if (api_key and r.get("api_key") == api_key) or \
                   (email and r.get("email") == (email or "").strip().lower()):
                    r["imported"] = True
                    r["imported_at"] = _now()
                    if connection_id:
                        r["connection_id"] = connection_id
                    r["updated_at"] = _now()
                    changed = True
            if changed:
                self.save(recs)
            return changed

    def mark_unimported(self, api_key=None, email=None):
        with self:
            recs = self.load()
            changed = False
            for r in recs:
                if (api_key and r.get("api_key") == api_key) or \
                   (email and r.get("email") == (email or "").strip().lower()):
                    if r.get("imported"):
                        r["imported"] = False
                        r["imported_at"] = 0
                        r["connection_id"] = ""
                        r["updated_at"] = _now()
                        changed = True
            if changed:
                self.save(recs)
            return changed

    def pending_import(self):
        """Records with a key not yet flagged imported."""
        return [r for r in self.load()
                if r.get("api_key", "").startswith("thk_") and not r.get("imported")]

    def stats(self):
        recs = self.load()
        return {"total": len(recs),
                "imported": sum(1 for r in recs if r.get("imported")),
                "with_key": sum(1 for r in recs if r.get("api_key", "").startswith("thk_"))}


# convenience singleton-ish helpers
_store = None


def store(path=None):
    global _store
    if _store is None or (path and str(_store.path) != str(path)):
        _store = KeyStore(path)
    return _store


if __name__ == "__main__":
    import sys
    ks = store(sys.argv[1] if len(sys.argv) > 1 else None)
    st = ks.stats()
    print(f"keys.json: {st['total']} records, {st['with_key']} with keys, "
          f"{st['imported']} flagged imported")
    if "--pending" in sys.argv:
        for r in ks.pending_import():
            print(f"  PENDING {r['email']} fp={r.get('fingerprint', '')[:12]}")
