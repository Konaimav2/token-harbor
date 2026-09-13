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


def _now():
    return int(time.time())


class KeyStore:
    def __init__(self, path=None):
        self.path = Path(path or JSON_PATH)
        self._lock_fh = None

    # ── locking ──
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self.path.with_suffix(".json.lock"), "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def _unlock(self):
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
    def load(self):
        """Return list of records (migrates legacy files on first run)."""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        recs = self._migrate()
        self.save(recs)
        return recs

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
                recs.append({
                    "email": email,
                    "password": password,
                    "api_key": api_key,
                    "status": status,
                    "imported": bool(fp and fp in imported_md5),
                    "imported_at": 0,
                    "connection_id": "",
                    "fingerprint": fp,
                    "updated_at": _now(),
                })
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
                rec = {"email": email, "password": "", "api_key": "",
                       "status": "pending", "imported": False, "imported_at": 0,
                       "connection_id": "", "fingerprint": "", "updated_at": _now()}
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
