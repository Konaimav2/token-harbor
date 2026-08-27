#!/usr/bin/env python3
"""th-deps — lazy dependency manager for th-tui.

Only installs the exact package needed when a feature is used, instead of
bundling everything at install time (keeps the tool lightweight).
"""
import importlib.util
import subprocess
import sys
import os

# feature -> (module_name, pip_package)
FEATURES = {
    "proxy_check": (("requests", "socks"), ("requests", "PySocks")),   # proxy live check
    "socks":       (("socks",), ("PySocks",)),                          # socks5 proxy support
    "playwright":  (("playwright",), ("playwright",)),                  # browser automation
    "curl_cffi":   (("curl_cffi",), ("curl_cffi",)),                    # TH signup / turnstile
    "camoufox":    (("camoufox",), ("camoufox",)),                      # free-model browser
    "faker":       (("faker",), ("Faker",)),                            # fake name gen
}

_installing = set()


def have(mod):
    return importlib.util.find_spec(mod) is not None


def check(feature):
    """Return True if all modules for `feature` are importable."""
    mods, _pkgs = FEATURES.get(feature, ((), ()))
    return all(have(m) for m in mods)


def ensure(feature, auto=True):
    """Ensure a feature's deps are installed. Returns (ok, missing_modules)."""
    mods, pkgs = FEATURES.get(feature, ((), ()))
    missing = [m for m in mods if not have(m)]
    if not missing:
        return True, []
    if not auto:
        return False, missing
    return install(pkgs[0] if isinstance(pkgs, tuple) and pkgs else pkgs), missing


def _pip():
    """Return the right pip command for this python."""
    if sys.prefix != sys.base_prefix or getattr(sys, "base_prefix", "") != sys.prefix:
        return [sys.executable, "-m", "pip"]
    return [sys.executable, "-m", "pip"]


def install(package):
    """Install one pip package (with --break-system-packages fallback). Returns bool."""
    if package in _installing:
        return True
    _installing.add(package)
    cmds = [[*_pip(), "install", "-q", package]]
    # PEP 668: some distros need --break-system-packages
    cmds.append([*_pip(), "install", "-q", "--break-system-packages", package])
    last = None
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                return True
            last = (r.stderr or r.stdout or "")[-400:]
        except Exception as e:
            last = str(e)
    print(f"  [deps] install failed for {package}: {last}", flush=True)
    return False


def ensure_soft(feature):
    """Like ensure() but never errors; used for optional features."""
    try:
        return ensure(feature)
    except Exception:
        return False, []


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "proxy_check"
    ok, missing = ensure(target)
    print(f"{target}: {'ready' if ok else 'missing ' + str(missing)}")