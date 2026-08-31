#!/usr/bin/env python3
"""TH-TUI - Token Harbor Account Creator & Manager
Every failure is logged with a real reason (no silent excepts)."""

import sys
import os
import tty
import termios
import json
import random
import string
import re
import time
import sqlite3
import subprocess
import shutil
import threading
import socket
import hashlib

TUI_BUILD = "proxyfast-v12-final-20260826"

# ── responsive terminal/layout helpers ──
MIN_TERM_COLS = 40

def term_size():
    """Return the REAL current terminal size; do not hide undersized terminals."""
    try:
        sz = shutil.get_terminal_size((60, 20))
        return max(1, sz.columns), max(1, sz.lines)
    except Exception as _e:
        print(f"[swallow th-tui.py:31] {_e}")
        return 60, 20

def term_width():
    return term_size()[0]

def term_height():
    return term_size()[1]

def _small_term_screen(min_cols, min_rows, label="TH-TUI"):
    """btop-style resize notice. Redrawn while the terminal is undersized."""
    cols, rows = term_size()
    cls()
    lines = [
        "TH-TUI",
        "",
        "Terminal not big enough",
        f"Current:  {cols} x {rows}",
        f"Required: {min_cols} x {min_rows}",
        "",
        "Resize terminal to continue",
    ]
    # Avoid wrapping the warning itself when the terminal is extremely narrow.
    usable = max(1, cols - 1)
    top_pad = max(0, (rows - len(lines)) // 2)
    if top_pad:
        sys.stdout.write("\n" * top_pad)
    for line in lines[:max(1, rows)]:
        text = line[:usable]
        left = max(0, (cols - len(text)) // 2)
        sys.stdout.write(" " * left + text + "\n")
    sys.stdout.flush()

def require_terminal(min_cols=MIN_TERM_COLS, min_rows=10, label="TH-TUI"):
    """Block like btop until the terminal is large enough; Ctrl+C still exits."""
    while True:
        cols, rows = term_size()
        if cols >= min_cols and rows >= min_rows:
            return True
        _small_term_screen(min_cols, min_rows, label)
        ch = getch(timeout=0.25)
        if ch == "\x03":
            raise KeyboardInterrupt

def box_w():
    """Box inner width for a terminal that has passed require_terminal()."""
    return max(10, min(term_width() - 4, 56))

def pad_vis(s, w, ansi_pat=r'\x1b\[[0-9;]*m'):
    """Right-pad ANSI-colored text to width w. Visible markers still count."""
    vis = re.sub(ansi_pat, '', s)
    return s + ' ' * max(0, w - len(vis))


def box_top(w):
    """Top border of a box of content width w."""
    return f"  {C}{BD}╔{'═' * w}╗{RS}"

def box_mid(w):
    """Divider line."""
    return f"  {C}{BD}╠{'═' * w}╣{RS}"

def box_bot(w):
    """Bottom border."""
    return f"  {C}{BD}╚{'═' * w}╝{RS}"

def _truncate_ansi(content, maxlen):
    """Truncate ANSI-colored text by visible columns without splitting escape codes."""
    maxlen = max(1, int(maxlen))
    vis = re.sub(r'\x1b\[[0-9;]*m', '', content)
    if len(vis) <= maxlen:
        return content
    cnt = 0
    i = 0
    target = max(0, maxlen - 1)
    while i < len(content) and cnt < target:
        if content[i] == '\x1b':
            j = content.find('m', i)
            if j == -1:
                break
            i = j + 1
        else:
            cnt += 1
            i += 1
    return content[:i] + '…' + RS

def box_row(w, content):
    """One content row inside a w-wide box. Never wraps horizontally."""
    maxlen = max(4, w - 2)
    content = _truncate_ansi(content, maxlen)
    return f"  {C}{BD}║{RS} {pad_vis(content, w - 2)} {C}{BD}║{RS}"

def box_title(w, title):
    """Title row inside a box; long filters/titles are truncated, never wrapped."""
    return box_row(w, f"{W}{BD}{title}{RS}")

def print_hint(content):
    """Print one unboxed hint line without terminal wrapping."""
    maxlen = max(1, term_width() - 2)
    print("  " + _truncate_ansi(content, maxlen))


import signal
import traceback
import urllib.request
import urllib.error
from pathlib import Path

# ── HTTP helpers for cloudmail (avoids Cloudflare 403) ──
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
_REF = None  # set after CM_BASE loads

def _cm_headers(extra=None):
    h = {"User-Agent": _UA, "Referer": (_REF or "https://cmail.arraffi.my.id/")}
    if extra:
        h.update(extra)
    return h

# Colors via simple concatenation (avoids f-string escape bugs)
_A = "\033["
R = _A + "31m"
G = _A + "32m"
Y = _A + "33m"
B = _A + "34m"
M = _A + "35m"
C = _A + "36m"
W = _A + "37m"
DI = _A + "2m"
BD = _A + "1m"
RS = _A + "0m"

BASE = Path(__file__).parent.resolve()
KEYS_FILE = BASE / "keys.txt"
USED_FILE = BASE / "used.txt"
CFG_FILE = BASE / "config.json"
KEY_CHECKS_FILE = BASE / "key-checks.json"
MAILG_TOKEN = None  # loaded from .env
MAILG_URL = None
CM_ADMIN_EMAIL = None
CM_ADMIN_PASSWORD = None
CM_BASE = None
_cm_token = None
_cm_token_lock = threading.Lock()
_cm_rate_lock = threading.Lock()
_cm_last_req = 0.0
_CM_MIN_INTERVAL = 2.0  # seconds between cloudmail API writes (KV quota safety)

# VNC settings (defaults, overridable via .env)
VNC_BIND = os.environ.get("VNC_BIND", "127.0.0.1")
VNC_PORT = os.environ.get("VNC_PORT", "6080")
VNC_PASSWORD = os.environ.get("VNC_PASSWORD", "")


def _cm_throttle():
    """Space out cloudmail API writes to avoid KV quota rate limits."""
    global _cm_last_req
    with _cm_rate_lock:
        now = time.time()
        wait = _CM_MIN_INTERVAL - (now - _cm_last_req)
        if wait > 0:
            time.sleep(wait)
        _cm_last_req = time.time()


def load_env():
    """Load live credentials from .env (0600 mode). Returns True if env file found."""
    global MAILG_TOKEN, MAILG_URL, CM_ADMIN_EMAIL, CM_ADMIN_PASSWORD, CM_BASE, _REF
    global VNC_BIND, VNC_PORT, VNC_PASSWORD
    env_path = BASE / ".env"
    if not env_path.exists():
        elog(".env not found — live credentials missing")
        return False
    try:
        for line in open(env_path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"").strip()
            if k == "TH_MAILG_TOKEN":
                MAILG_TOKEN = v
            elif k == "TH_MAILG_URL":
                MAILG_URL = v
            elif k == "CM_ADMIN_EMAIL":
                CM_ADMIN_EMAIL = v
            elif k == "CM_ADMIN_PASSWORD":
                CM_ADMIN_PASSWORD = v
            elif k == "CM_BASE_URL":
                CM_BASE = v
                _REF = v.rstrip("/") + "/"
            elif k == "VNC_BIND":
                VNC_BIND = v
            elif k == "VNC_PORT":
                VNC_PORT = v
            elif k == "VNC_PASSWORD":
                VNC_PASSWORD = v
        if not MAILG_TOKEN:
            log("TH_MAILG_TOKEN missing in .env — MailG features disabled", "warn")
        if not MAILG_URL:
            MAILG_URL = "http://127.0.0.1:8790"
        if not CM_BASE:
            CM_BASE = "https://cmail.arraffi.my.id"
        return True
    except Exception as e:
        elog("load .env: " + str(e), traceback.format_exc()[:200])
        return False


def _ts():
    return time.strftime("%H:%M:%S")


def elog(msg, detail="" ):
    """Error log with optional detail lines."""
    print(f"  {R}X{RS} [{_ts()}] {msg}", flush=True)
    if detail:
        for line in str(detail).strip().split("\n"):
            print(f"    {DI}{line}{RS}", flush=True)


def log(msg, icon="info"):
    """Concise log — shows on CLI AND writes a line to farm.log."""
    icons = {"ok": f"{G}OK{RS}", "no": f"{R}XX{RS}", "warn": f"{Y}!{RS}",
             "info": f"{B}i{RS}", "arr": f"{M}-{RS}"}
    print(f"  {icons.get(icon, '-')} [{_ts()}] {msg}", flush=True)
    # append to farm log file too (for unattended runs)
    try:
        LOG_FILE = BASE / "farm.log"
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception as _e:
        print(f"[swallow th-tui.py:262] {_e}")
        pass


def dlog(msg):
    """Detail log — file ONLY (proxy used, verification polls, inbox steps).
    Keeps the CLI clean while the log file has full detail."""
    try:
        LOG_FILE = BASE / "farm.log"
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}]   > {msg}\n")
    except Exception as _e:
        print(f"[swallow th-tui.py:273] {_e}")
        pass


# ---- interactive selection ----
_FULLSCREEN_ACTIVE = False


def enter_fullscreen():
    """CLEAR-BASED mode: clear screen each render (no alternate-screen buffer).
    Keeps the terminal in normal mode so scrollback persists and Ctrl+C/keys behave
    like a normal app, not a fullscreen takeover."""
    global _FULLSCREEN_ACTIVE
    try:
        sys.stdout.write("\x1b[H\x1b[2J")   # home + clear (NOT alternate screen)
        sys.stdout.write("\x1b[?25l")       # hide cursor
        sys.stdout.flush()
        _FULLSCREEN_ACTIVE = True
    except Exception as _e:
        print(f"[swallow th-tui.py:291] {_e}")
        pass


def exit_fullscreen():
    """Restore cursor (normal screen is always active in clear-based mode)."""
    global _FULLSCREEN_ACTIVE
    try:
        sys.stdout.write("\x1b[?25h")    # show cursor
        sys.stdout.flush()
        _FULLSCREEN_ACTIVE = False
    except Exception as _e:
        print(f"[swallow th-tui.py:302] {_e}")
        pass


def cls():
    """Clear screen without flicker (clear THEN home, avoids diagonal stagger)."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def getch(timeout=None):
    """Read one terminal character without TextIO buffering.

    IMPORTANT: select() must watch the same unbuffered fd that we read from.
    Mixing select(fd) with sys.stdin.read() can lose the remaining bytes of an
    escape sequence inside TextIOWrapper's buffer, making arrows look like ESC.
    """
    fd = sys.stdin.fileno()

    # re-assert raw mode if a subprocess/submenu restored cooked mode
    if _RAW_HELD:
        try:
            import termios as _t
            cur = _t.tcgetattr(fd)
            if (cur[3] & (_t.ICANON | _t.ECHO)) and _RAW_SAVED is not None:
                tty.setraw(fd)
                cur = _t.tcgetattr(fd)
                cur[1] |= _t.OPOST | _t.ONLCR
                _t.tcsetattr(fd, _t.TCSADRAIN, cur)
        except Exception as _e:
            print(f"[swallow th-tui.py:331] {_e}")
            pass

    try:
        if timeout is not None:
            import select
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return ''

        b0 = os.read(fd, 1)
        if not b0:
            return ''

        # Control keys / escape sequences are ASCII. For normal typed text,
        # collect a complete UTF-8 character so raw_input() still works.
        first = b0[0]
        if first < 0x80:
            return chr(first)

        if 0xC2 <= first <= 0xDF:
            need = 1
        elif 0xE0 <= first <= 0xEF:
            need = 2
        elif 0xF0 <= first <= 0xF4:
            need = 3
        else:
            return b0.decode('utf-8', 'replace')

        data = bytearray(b0)
        import select
        for _ in range(need):
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                break
            more = os.read(fd, 1)
            if not more:
                break
            data.extend(more)
        return bytes(data).decode('utf-8', 'replace')
    except Exception as _e:
        print(f"[swallow th-tui.py:376] {_e}")
        return ''


_RAW_HELD = False
_RAW_SAVED = None


def raw_start():
    """Enter raw mode once and hold it (prevents escape-byte leaks to cooked input()).
    Keeps OPOST/ONLCR so \n still returns the carriage (no diagonal stagger)."""
    global _RAW_HELD, _RAW_SAVED
    if _RAW_HELD:
        return
    fd = sys.stdin.fileno()
    try:
        _RAW_SAVED = termios.tcgetattr(fd)
        tty.setraw(fd)
        # re-enable output post-processing: \n must also \r (else lines stagger diagonally)
        import termios as _t
        attrs = _t.tcgetattr(fd)
        attrs[1] |= _t.OPOST | _t.ONLCR
        _t.tcsetattr(fd, _t.TCSADRAIN, attrs)
        _RAW_HELD = True
    except Exception as _e:
        print(f"[swallow th-tui.py:395] {_e}")
        pass


def raw_end():
    """Restore cooked mode (on exit)."""
    global _RAW_HELD
    if not _RAW_HELD:
        return
    fd = sys.stdin.fileno()
    try:
        if _RAW_SAVED is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, _RAW_SAVED)
    except Exception as _e:
        print(f"[swallow th-tui.py:408] {_e}")
        pass
    _RAW_HELD = False


def _kdump(c, tag):
    """Debug-log a raw byte to _key_debug.log (hex)."""
    try:
        with open(BASE / "_key_debug.log", "a") as _f:
            _f.write(f"{tag}: {c!r} hex={c.encode('utf-8','replace').hex() if c else 'empty'} at {time.time():.2f}\n")
    except Exception as _e:
        print(f"[swallow th-tui.py:418] {_e}")
        pass


# Global interrupt flag for Ctrl+C during batch
_BATCH_INTERRUPT = False

def _batch_sigint_handler(sig, frame):
    global _BATCH_INTERRUPT
    _BATCH_INTERRUPT = True
    raise KeyboardInterrupt

def interruptible_sleep(seconds, label=""):
    """Sleep that can be interrupted by Ctrl+C."""
    end = time.time() + seconds
    while time.time() < end:
        if _BATCH_INTERRUPT:
            raise KeyboardInterrupt
        time.sleep(min(1, end - time.time()))


def get_key():
    ch = getch()
    _kdump(ch, "first")

    if ch == '\x03':  # Ctrl+C
        return 'ctrl-c'

    if ch == '\x1b':
        # ESC alone and terminal escape sequences share the same first byte.
        # Read the rest with the same unbuffered getch() path.
        nxt = getch(timeout=0.15)
        if not nxt:
            return 'escape'  # genuine lone Esc

        _kdump(nxt, "seq")
        seq = '\x1b' + nxt

        # CSI (ESC [ ...) and SS3 (ESC O ...) sequences. Do not assume arrows
        # are exactly 3 bytes: modifiers may produce e.g. ESC [ 1 ; 2 A.
        if nxt in ('[', 'O'):
            for _ in range(16):
                c = getch(timeout=0.05)
                if not c:
                    break
                _kdump(c, "seq")
                seq += c

                # ECMA-48 final byte. This terminates CSI/SS3 sequences.
                if len(c) == 1 and '@' <= c <= '~':
                    break

            final = seq[-1] if len(seq) >= 3 else ''
            params = seq[2:-1]

            if final == 'A': return 'up'
            if final == 'B': return 'down'
            if final == 'C': return 'right'
            if final == 'D': return 'left'
            if final == 'H': return 'home'
            if final == 'F': return 'end'

            # Common CSI tilde keys: ESC [ 5 ~ / ESC [ 6 ~
            if final == '~':
                first_param = params.split(';', 1)[0]
                if first_param == '5': return 'pgup'
                if first_param == '6': return 'pgdn'
                if first_param in ('1', '7'): return 'home'
                if first_param in ('4', '8'): return 'end'

            # Unknown terminal sequence must NOT behave like Esc/back.
            return 'unknown'

        # Alt+key or another ESC-prefixed sequence: don't accidentally exit.
        return 'unknown'

    if ch in ('\r', '\n'):
        return 'enter'
    if ch == ' ':
        return 'space'
    if ch == 'q' or ch == 'Q':
        return 'escape'

    return ch


def _item_search_text(item):
    """Flatten a picker item into searchable visible text."""
    if isinstance(item, tuple):
        parts = item
    else:
        parts = (item,)
    text = " ".join(str(x) for x in parts if x is not None)
    return re.sub(r'\x1b\[[0-9;]*m', '', text).lower()


def _build_search_index(items):
    """Build normalized search text once per item (important for 1k-100k item pickers)."""
    return [(item, _item_search_text(item)) for item in items]


def _filter_search_index(index, query):
    """Return ALL matching items. Space-separated terms are ANDed, case-insensitively."""
    terms = [t for t in (query or '').strip().lower().split() if t]
    if not terms:
        return [item for item, _text in index]
    return [item for item, text in index if all(term in text for term in terms)]


def _filter_items(items, query):
    """Compatibility helper for one-off searches; indexed pickers use _filter_search_index directly."""
    return _filter_search_index(_build_search_index(items), query)


def render_list(title, items, selected=None, cursor=0, multi=False, scroll=0,
                searchable=False, query='', source_total=None):
    """Render items in a scrollable list window (dropdown-style continuous scroll)."""
    # Picker chrome consumes 8 rows (blank/top/title/divider/bottom/blank/hints).
    # Anything beyond the remaining rows is scrolled, never printed off-screen.
    require_terminal(MIN_TERM_COLS, 10, title)
    cls()
    w = box_w()
    term_rows = term_height()
    win_sz = max(1, term_rows - 8)
    total = len(items)

    print(box_top(w))
    shown_title = title
    if searchable and query:
        base_total = source_total if source_total is not None else total
        shown_title += f"  [{total}/{base_total} · {query}]"
    print(box_title(w, shown_title))
    print(box_mid(w))

    if total == 0:
        print(box_row(w, f"{DI}(no matches){RS}"))
        print(box_bot(w))
        hints = []
        if searchable:
            hints.append("F Clear" if query else "F Search")
        hints.append("Esc")
        print("\n", end="")
        print_hint(f"{DI}{'  ·  '.join(hints)}{RS}")
        return 0, 0

    # snap scroll so cursor is always visible
    if cursor < scroll:
        scroll = cursor
    if cursor >= scroll + win_sz:
        scroll = cursor - win_sz + 1
    scroll = max(0, min(scroll, max(0, total - win_sz)))
    end = min(scroll + win_sz, total)

    for idx in range(scroll, end):
        item = items[idx]
        if isinstance(item, tuple):
            key, label, sub = (item + (None, None))[:3]
        else:
            key = label = item
            sub = None
        is_cur = (idx == cursor)
        is_sel = selected and key in selected
        prefix = f"{Y}{BD}▶{RS} " if is_cur else "  "
        cb = ""
        if multi:
            cb = f"{G}●{RS} " if is_sel else f"{DI}○{RS} "
        style = f"{W}{BD}" if is_cur else W
        content = f"{prefix}{cb}{style}{label}{RS}"
        if sub:
            content += f"  {DI}· {sub}{RS}"
        print(box_row(w, content))
    print(box_bot(w))

    hints = ["↑↓", "PgUp/PgDn", "Home/End"]
    if searchable:
        hints.append("F Clear" if query else "F Search")
    if multi:
        hints += ["Space", "A All", "D None"]
    hints.append("Enter")
    hints.append("Esc")
    print("\n", end="")
    print_hint(f"{DI}{'  ·  '.join(hints)}{RS}")
    return cursor, scroll


def _flush_stdin():
    """Drop bytes already waiting in the kernel TTY input queue."""
    import select
    try:
        fd = sys.stdin.fileno()
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, 4096):
                break
    except Exception as _e:
        print(f"[swallow th-tui.py:611] {_e}")
        pass


def raw_input(prompt=""):
    """Read a line of text in raw mode (Enter=submit, Backspace=delete, Ctrl+C=abort)."""
    _flush_stdin()  # drain leftover keys before prompting
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    buf = []
    while True:
        ch = getch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(buf)
        if ch in ("\x03",):  # Ctrl+C
            raise KeyboardInterrupt
        if ch == "\x7f" or ch == "\b":  # backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch and ord(ch) >= 32:
            buf.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()


def pick_one(title, items, searchable=False, initial_query=''):
    all_items = list(items)
    search_index = _build_search_index(all_items) if searchable else None
    query = (initial_query or '').strip() if searchable else ''
    view = _filter_search_index(search_index, query) if searchable else list(all_items)
    c = 0
    scroll = 0
    _flush_stdin()
    while True:
        c, scroll = render_list(title, view, cursor=c, multi=False, scroll=scroll,
                                searchable=searchable, query=query, source_total=len(all_items))
        k = get_key()
        total = len(view)
        if searchable and (k == 'f' or k == 'F'):
            if query:
                # One-key clear: restore the complete source list immediately.
                query = ''
                view = list(all_items)
            else:
                query = raw_input("  Search: ").strip()
                view = _filter_search_index(search_index, query)
            c = 0
            scroll = 0
        elif k == 'up' and total:
            c = max(0, c - 1)
        elif k == 'down' and total:
            c = min(total - 1, c + 1)
        elif k == 'pgup' and total:
            c = max(0, c - max(3, page_sz_h()))
        elif k == 'pgdn' and total:
            c = min(total - 1, c + max(3, page_sz_h()))
        elif k == 'home' and total:
            c = 0
        elif k == 'end' and total:
            c = total - 1
        elif k == 'enter' and total:
            _flush_stdin()
            return view[c]
        elif k == 'escape' or k == 'ctrl-c':
            _flush_stdin()
            return None


def page_sz_h():
    """Visible picker item rows after reserving its fixed UI chrome."""
    return max(1, term_height() - 8)


def pick_multi(title, items, pre=None, searchable=False, initial_query=''):
    all_items = list(items)
    search_index = _build_search_index(all_items) if searchable else None
    query = (initial_query or '').strip() if searchable else ''
    view = _filter_search_index(search_index, query) if searchable else list(all_items)
    c = 0
    scroll = 0
    sel = set(pre or [])
    _flush_stdin()
    while True:
        c, scroll = render_list(title, view, selected=sel, cursor=c, multi=True,
                                scroll=scroll, searchable=searchable, query=query, source_total=len(all_items))
        k = get_key()
        total = len(view)
        if searchable and (k == 'f' or k == 'F'):
            if query:
                # One-key clear: restore the complete source list immediately.
                query = ''
                view = list(all_items)
            else:
                query = raw_input("  Search: ").strip()
                view = _filter_search_index(search_index, query)
            c = 0
            scroll = 0
        elif k == 'up' and total:
            c = max(0, c - 1)
        elif k == 'down' and total:
            c = min(total - 1, c + 1)
        elif k == 'pgup' and total:
            c = max(0, c - max(3, page_sz_h()))
        elif k == 'pgdn' and total:
            c = min(total - 1, c + max(3, page_sz_h()))
        elif k == 'home' and total:
            c = 0
        elif k == 'end' and total:
            c = total - 1
        elif k == 'space' and total:
            k_ = view[c][0]
            if k_ in sel:
                sel.discard(k_)
            else:
                sel.add(k_)
        elif k == 'a' or k == 'A':
            # When filtered, All applies only to visible matches.
            sel.update(x[0] for x in view)
        elif k == 'd' or k == 'D':
            # When filtered, None clears only visible matches.
            for x in view:
                sel.discard(x[0])
        elif k == 'enter':
            _flush_stdin()
            return set(sel)  # empty set is a valid committed selection (e.g. clear all)
        elif k == 'escape' or k == 'ctrl-c':
            _flush_stdin()
            return None      # Esc is cancel; never persist in-menu edits


# ---- config ----
def load_cfg():
    if CFG_FILE.exists():
        try:
            c = json.loads(CFG_FILE.read_text())
            # normalize mail servers (ensure create_new_mail default False)
            for s in c.get("mail_servers", []):
                if "create_new_mail" not in s:
                    s["create_new_mail"] = False
            if "proxy" not in c:
                c["proxy"] = {"enabled": False, "mode": "list",
                              "current": None, "use_public_tempmail": False}
            if "dot_trick" not in c:
                c["dot_trick"] = False
            if "protected" not in c.get("proxy", {}):
                c.setdefault("proxy", {})["protected"] = []
            if "no_delete" not in c.get("proxy", {}):
                c.setdefault("proxy", {})["no_delete"] = False
            # Repair stale active_mail pointers (default configs used to keep "mailg"
            # even when no server with that name existed).
            _servers = c.get("mail_servers", [])
            _names = [s.get("name", "") for s in _servers if s.get("name")]
            if c.get("active_mail") not in _names:
                c["active_mail"] = _names[0] if _names else ""
            return c
        except Exception as e:
            elog(f"config read: {e}")
    return {"mail_servers": [], "router": {"local": {"base_url": "http://localhost:20128"},
                                           "remote": {"base_url": "https://vibecode.omori.my.id"}},
            "active_mail": "", "router_mode": "local", "email_prefix": "",
            "account_password": "", "vnc_mode": False, "batch_count": 3,
            "proxy": {"enabled": False, "mode": "list",
                      "current": None, "use_public_tempmail": False,
                      "no_delete": False, "protected": []},
            "batch_delay": 30, "dot_trick": False}


def save_cfg(c):
    try:
        CFG_FILE.write_text(json.dumps(c, indent=2))
    except Exception as e:
        elog(f"save config: {e}", traceback.format_exc())


def get_active_mail(c):
    n = c.get("active_mail", "")
    for s in c.get("mail_servers", []):
        if s.get("name") == n:
            return s
    return None


def get_active_router(c):
    m = c.get("router_mode", "local")
    router = c.get("router", {})
    if not router:
        # Initialize with defaults
        c["router"] = {
            "local": {"name": "Local", "base_url": "http://localhost:20128"},
            "remote": {"name": "Remote", "base_url": "https://vibecode.omori.my.id"}
        }
        return c["router"].get(m, {})
    return router.get(m, {"name": m, "base_url": "http://localhost:20128"})


# ---- used addresses ----
def load_used():
    u = set()
    try:
        if KEYS_FILE.exists():
            for l in open(KEYS_FILE):
                p = l.strip().split("|")
                if p and "@" in p[0]:
                    u.add(p[0].lower())
        if USED_FILE.exists():
            for l in open(USED_FILE):
                e = l.strip().lower()
                if "@" in e:
                    u.add(e)
    except Exception as e:
        elog(f"load used: {e}")
    return u


def mark_used(e):
    try:
        with open(USED_FILE, "a") as f:
            f.write(e.lower() + "\n")
    except Exception as ex:
        elog(f"mark_used: {e}", str(ex))


# ---- mail sources ----
def get_mailg_accounts():
    try:
        db = sqlite3.connect("/root/projects/gmail-inbox/inbox.db")
        accs = [r[0] for r in db.execute("SELECT email FROM accounts ORDER BY email")]
        db.close()
        return accs
    except Exception as e:
        elog(f"mailg DB read: {e}", traceback.format_exc()[:200])
        return []


def get_cloudmail_addresses():
    """Get existing cloudmail inboxes from credentials file AND from cloudmail admin API.
    Uses /api/login (admin JWT) + /api/user/list to enumerate existing inboxes.
    Uses requests (not urllib) to avoid Cloudflare UA/TLS blocking."""
    from glob import glob
    import requests as _rq
    em = []
    # 1. from credentials file
    try:
        files = glob("/root/ReiFiles/credentials/deepseek-session-*/grok-register/mail_credentials.txt")
        for cred_file in files:
            for l in open(cred_file):
                p = l.strip().split("\t")
                if p and "@" in p[0]:
                    em.append(p[0].strip())
    except Exception as e:
        elog(f"cloudmail addresses (file): {e}")
    # 2. from cloudmail admin API (list all known inboxes via /user/list)
    if CM_BASE and CM_ADMIN_EMAIL and CM_ADMIN_PASSWORD:
        try:
            log("Fetching cloudmail inboxes...", "info")
            hdr = {"Content-Type": "application/json", "User-Agent": _UA, "Referer": CM_BASE + "/"}
            r = _rq.post(CM_BASE + "/api/login",
                         json={"email": CM_ADMIN_EMAIL, "password": CM_ADMIN_PASSWORD},
                         headers=hdr, timeout=15)
            tok = r.json().get("data", {}).get("token", "")
            if tok:
                r2 = _rq.get(CM_BASE + "/api/user/list?num=1&size=500",
                             headers={"Authorization": tok, "User-Agent": _UA, "Referer": CM_BASE + "/"},
                             timeout=15)
                data = r2.json().get("data", {})
                for u in (data.get("list") or []):
                    e = u.get("email", "")
                    if e and "@" in e:
                        em.append(e)
        except Exception as e:
            elog(f"cloudmail addresses (api): {e}")
    return sorted(set(em))


def get_cloudmail_domains():
    """Fetch the real, currently-deployed cloudmail domains from the worker API."""
    # 1. live API: /api/setting/websiteConfig returns domainList (public, no auth)
    if CM_BASE:
        try:
            import requests as _rq
            r = _rq.get(CM_BASE + "/api/setting/websiteConfig",
                        headers={"User-Agent": _UA, "Referer": CM_BASE + "/"}, timeout=10)
            dl = r.json().get("data", {}).get("domainList", [])
            if dl:
                clean = [d.lstrip("@") for d in dl if d]
                if clean:
                    return sorted(clean)
        except Exception as _e:
            print(f"[swallow th-tui.py:904] {_e}")
            pass
    # 2. config file
    try:
        c = load_cfg()
        for s in c.get("mail_servers", []):
            if s.get("type") == "cloudmail":
                dl = s.get("domains") or []
                if dl:
                    return dl
                d = s.get("domain")
                if d:
                    return [d]
    except Exception as _e:
        print(f"[swallow th-tui.py:917] {_e}")
        pass
    # 3. fallback
    return ["furries.my.id", "kona.my.id", "konaima.qzz.io", "konaima.tech", "nothingisfree.qzz.io", "onboarding.qzz.io"]


def get_server_emails(server):
    """Get available email addresses for a mail server (used in Pick existing mode)."""
    t = server.get("type", "")
    try:
        if t == "mailg":
            return get_mailg_accounts()
        elif t == "cloudmail":
            return get_cloudmail_addresses()
    except Exception as _e:
        print(f"[swallow th-tui.py:931] {_e}")
        pass
    return []


def create_cloudmail_inbox(email, password="test123"):
    """Create a cloudmail inbox. Thread-safe, throttled, with one safe token refresh."""
    import requests as _rq
    global _cm_token
    # IMPORTANT: never recursively call this function while holding _cm_token_lock;
    # threading.Lock is non-reentrant and that used to deadlock forever on a 401.
    with _cm_token_lock:
        last_err = None
        for attempt in range(2):
            try:
                _cm_throttle()
                hdr = {"Content-Type": "application/json", "User-Agent": _UA, "Referer": CM_BASE + "/"}
                if not _cm_token:
                    r = _rq.post(CM_BASE + "/api/login",
                                 json={"email": CM_ADMIN_EMAIL, "password": CM_ADMIN_PASSWORD},
                                 headers=hdr, timeout=15)
                    _cm_token = r.json().get("data", {}).get("token", "")
                    if not _cm_token:
                        raise RuntimeError("login failed")
                r2 = _rq.post(CM_BASE + "/api/account/add",
                              json={"email": email, "password": password},
                              headers={"Authorization": _cm_token, **hdr}, timeout=15)
                resp = r2.json()
                code = resp.get("code")
                # code 200 = created, code 501 = already registered (treat as success)
                if code in (200, 501):
                    return True
                return False
            except Exception as e:
                print(f"[swallow th-tui.py:976] {_e}")
                last_err = e
                auth_err = "401" in str(e) or "token" in str(e).lower() or "login failed" in str(e)
                if attempt == 0 and auth_err:
                    _cm_token = None
                    continue
                break
        elog(f"create cloudmail inbox {email}: {last_err}", traceback.format_exc()[:200])
        return False


def batch_create_cloudmail_inboxes(emails, password="test123", delay_s=3.0):
    """Create multiple inboxes sequentially with throttle between each.
    Returns (ok_count, fail_count). Much safer for KV quota than concurrent creates."""
    import urllib.request
    ok = fail = 0
    for i, email in enumerate(emails, 1):
        try:
            if create_cloudmail_inbox(email, password):
                dlog(f"Inbox created ({i}/{len(emails)}): {email}")
                ok += 1
            else:
                elog(f"Failed to create inbox ({i}/{len(emails)}): {email}")
                fail += 1
        except Exception as e:
            elog(f"Inbox creation error ({i}/{len(emails)}): {email}: {e}")
            fail += 1
        if i < len(emails):
            time.sleep(delay_s)
    return ok, fail


def _next_domain(c, domains):
    """Round-robin domain selection across the mail server's domains."""
    if not domains:
        return "furries.my.id"
    # counter in config to spread evenly across domains
    i = c.get("_domain_idx", 0)
    c["_domain_idx"] = (i + 1) % len(domains)
    return domains[i % len(domains)]


# ── realistic email prefix generator (real-looking names, not gibberish) ──
_FIRST = ["john", "jane", "mike", "sarah", "david", "emma", "chris", "lisa", "james", "anna",
          "robert", "mary", "daniel", "laura", "kevin", "jessica", "brian", "amanda", "mark",
          "rachel", "steven", "nicole", "paul", "ashley", "andrew", "melissa", "joshua", "rebecca",
          "ryan", "michelle", "jacob", "kimberly", "nathan", "amy", "brandon", "angela", "justin",
          "heather", "matthew", "stephanie", "adam", "maria", "aaron", "jennifer", "tyler", "elizabeth",
          "scott", "lauren", "kyle", "samantha", "eric", "hannah", "jason", "olivia", "philip", "diana",
          "george", "sophia", "henry", "isabella", "owen", "chloe", "liam", "grace", "noah", "lily",
          "ethan", "ava", "lucas", "mia", "benjamin", "zoe", "samuel", "ella", "jack", "ruby"]
_LAST = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez",
         "martinez", "anderson", "taylor", "thomas", "moore", "jackson", "martin", "lee", "perez",
         "thompson", "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
         "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill", "flores", "green",
         "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell", "carter", "roberts",
         "gomez", "phillips", "evans", "turner", "diaz", "parker", "cruz", "edwards", "collins",
         "reyes", "stewart", "morris", "morales", "murphy", "cook", "rogers", "gutierrez", "ortiz",
         "morgan", "cooper", "peterson", "bailey", "reed", "kelly", "howard", "ramos", "kim",
         "cox", "ward", "richardson", "watson", "brooks", "chavez", "wood", "james", "bennett",
         "gray", "mendoza", "ruiz", "hughes", "price", "alvarez", "castillo", "sanders", "patel",
         "myers", "long", "ross", "foster", "jimenez", "powell", "jenkins", "perry", "russell"]


def _real_prefix(c):
    """Generate a realistic-looking email prefix: first.last + optional number."""
    import random as _r
    f = _r.choice(_FIRST)
    l = _r.choice(_LAST)
    style = _r.random()
    if style < 0.35:
        return f"{f}.{l}"
    if style < 0.55:
        return f"{f}{_r.randint(19, 99)}"
    if style < 0.75:
        return f"{f}.{l}{_r.randint(19, 99)}"
    if style < 0.9:
        title = _r.choice(["mr", "ms", "dr", "mrs"])
        return f"{title}.{l}"
    return f"{l}{_r.randint(1, 9)}{f[:2]}"


def _dot_trick_address(base):
    """Generate a Gmail dot-trick + plus alias from a base email.
    Gmail ignores dots and everything after '+', so many unique-looking
    addresses route to one inbox. Returns e.g. 'john.doe+tok12@gmail.com'."""
    base = base.strip()
    if "@" not in base or not base.endswith("@gmail.com"):
        return None
    local = base.split("@")[0]
    # insert a random dot into the local part (if length > 1)
    if len(local) > 1:
        pos = random.randint(1, len(local) - 1)
        dotted = local[:pos] + "." + local[pos:]
    else:
        dotted = local
    tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{dotted}+{tag}@gmail.com"


def pick_address(c):
    ms = get_active_mail(c)
    used = load_used()
    # Public tempmail fallback — used when enabled and no usable mail server/address
    if not ms:
        if c.get("proxy", {}).get("use_public_tempmail"):
            pm = _load_proxy_mod()
            if pm:
                addr = pm.get_public_tempmail()
                if addr:
                    log("Public tempmail: " + addr, "info")
                    return addr
        pfx = c.get("email_prefix", "") or _real_prefix(c)
        return pfx + "@gmail.com"
    t = ms.get("type", "")
    # If this mail server is configured to create a new mail, always generate a fresh address
    if ms.get("create_new_mail"):
        pfx = c.get("email_prefix", "") or _real_prefix(c)
        domains = ms.get("domains") or []
        if not domains and ms.get("domain"):
            domains = [ms.get("domain")]
        if not domains:
            domains = ["furries.my.id"]
        # rotate across ALL domains (round-robin) to spread mail across providers
        dom = _next_domain(c, domains)
        # auto-number prefix to avoid conflicts: Reika -> Reika1, Reika2, ...
        # if prefix is user-specified (not random), find the next free number
        if c.get("email_prefix"):
            # find max existing number for this prefix across used + keys
            used_lc = {u.lower() for u in used}
            base = pfx.lower()
            max_n = 0
            for e in used_lc:
                if e.startswith(base):
                    # extract trailing number (e.g. reika12@ -> 12)
                    rest = e[len(base):].split("@")[0]
                    if rest.isdigit():
                        max_n = max(max_n, int(rest))
            # check keys.txt too
            try:
                for line in open(KEYS_FILE):
                    em = line.split("|")[0].strip().lower()
                    if em.startswith(base):
                        rest = em[len(base):].split("@")[0]
                        if rest.isdigit():
                            max_n = max(max_n, int(rest))
            except Exception as _e:
                print(f"[swallow th-tui.py:1110] {_e}")
                pass
            pfx = f"{pfx}{max_n + 1}"
        return pfx + "@" + dom
    # Pick-existing mode: prefer explicitly configured emails, else all available
    conf_emails = ms.get("emails") or []
    if conf_emails:
        avail = [a for a in conf_emails if a.lower() not in used]
        if avail:
            return random.choice(avail)
    if t == "mailg":
        accs = [a for a in get_mailg_accounts() if a.lower() not in used]
        if accs:
            chosen = random.choice(accs)
            # gmail dot-trick: one real inbox -> many unique-looking addresses
            if c.get("dot_trick"):
                aliased = _dot_trick_address(chosen)
                if aliased:
                    log(f"Dot-trick alias: {aliased}", "info")
                    return aliased
            return chosen
    elif t == "cloudmail":
        addrs = [a for a in get_cloudmail_addresses() if a.lower() not in used]
        if addrs:
            chosen = random.choice(addrs)
            # cloudmail catch-all: plus-alias works (user+tag@domain routes to user@domain)
            if c.get("dot_trick"):
                tag = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
                local, dom = chosen.rsplit("@", 1)
                aliased = f"{local}+{tag}@{dom}"
                log(f"Plus-alias: {aliased}", "info")
                return aliased
            return chosen
    if not ms.get("create_new_mail") and t in ("mailg", "cloudmail"):
        return None  # existing pool exhausted; never invent an inbox that cannot receive mail
    domain = (ms.get("domain") or "").strip()
    if not domain:
        return None
    pfx = c.get("email_prefix", "") or "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return pfx + "@" + domain


def build_pw(c):
    pw = c.get("account_password", "").strip()
    return pw or "".join(random.choices(string.ascii_letters + string.digits, k=16))


# ---- inbox reading + verify link ----
def read_mailg_inbox(email):
    import urllib.request
    try:
        req = urllib.request.Request(MAILG_URL + "/api/public/emailList",
                                     data=json.dumps({"toEmail": email}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": MAILG_TOKEN})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            msgs = d.get("data", []) if d.get("code") == 200 else []
            return sorted(msgs, key=lambda m: m.get("createTime", 0), reverse=True)
    except Exception as e:
        elog(f"mailg read {email}: {e}", traceback.format_exc()[:200])
        return []


def read_cloudmail_inbox(email):
    import requests as _rq
    try:
        hdr = {"Content-Type": "application/json", "User-Agent": _UA, "Referer": CM_BASE + "/"}
        # /public/* routes need the PUBLIC_KEY (from genToken with admin), NOT the login JWT
        r = _rq.post(CM_BASE + "/api/public/genToken",
                     json={"email": CM_ADMIN_EMAIL, "password": CM_ADMIN_PASSWORD},
                     headers=hdr, timeout=15)
        tok = r.json().get("data", {}).get("token", "")
        # emailList public endpoint returns messages for the address
        r2 = _rq.post(CM_BASE + "/api/public/emailList",
                      json={"toEmail": email, "size": 20},
                      headers={"Authorization": tok, **hdr}, timeout=15)
        msgs = r2.json().get("data", [])
        return sorted(msgs, key=lambda m: m.get("createTime", m.get("time", 0)), reverse=True)
    except Exception as e:
        elog(f"cloudmail read {email}: {e}", traceback.format_exc()[:200])
        return []


def find_verify_link(msgs):
    pat = re.compile(r'https?://[^\s\'"<>]*(?:verify(?:-email)?|confirm)[^\s\'"<>]*')
    for m in msgs:
        body = (m.get("content", "") or "") + (m.get("text", "") or "")
        m2 = pat.search(body)
        if m2:
            return m2.group(0)
        m3 = re.search(r'href=[\'"]?(https?://[^\'"\s>]*(?:verify|confirm)[^\'"\s>]*)', body)
        if m3:
            return m3.group(1)
    return None


def resend_verification(email, password):
    """Log into TokenHarbor and click 'Verify email' / resend."""
    log(f"Resending verification for {email}...", "arr")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                                  args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = b.new_context(viewport={"width": 1280, "height": 720})
            pg = ctx.new_page()
            pg.goto("https://tokenharbor.ai/login?mode=signin", wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_selector('input[name="email"]', timeout=15000)
            time.sleep(1)
            # humanize fill/click
            try:
                import importlib.util as _ihu
                _hz_spec = _ihu.spec_from_file_location("humanize", str(BASE / "humanize.py"))
                _hz = _ihu.module_from_spec(_hz_spec); _hz_spec.loader.exec_module(_hz)
                _hz.human_type(pg, pg.locator('input[name="email"]'), email)
                _hz.human_type(pg, pg.locator('input[name="password"]'), password)
                _hz.rand_delay(0.4, 1.0)
                _hz.human_click(pg, pg.locator('button[type="submit"]'))
            except Exception as _e:
                print(f"[swallow th-tui.py:1241] {_e}")
                pg.fill('input[name="email"]', email)
                pg.fill('input[name="password"]', password)
                pg.click('button[type="submit"]', timeout=30000)
            time.sleep(6)
            body = pg.inner_text("body", timeout=10000).lower()
            if "dashboard" not in body and "api-keys" not in body:
                elog(f"resend login failed: page not dashboard (shows: {body[:80]})")
                b.close()
                return False
            log("Logged into dashboard!", "ok")
            resend_found = False
            for btn_text in ["Verify email", "Resend", "Resend verification", "Send again", "verification email"]:
                try:
                    btn = pg.locator('button:has-text("' + btn_text + '")').first
                    if btn.is_visible(timeout=3000):
                        btn.click(timeout=10000)
                        log("Clicked '" + btn_text + "'", "ok")
                        resend_found = True
                        time.sleep(3)
                        break
                except Exception as e:
                    log("btn '" + btn_text + "' not clickable: " + str(e)[:40], "info")
            if resend_found:
                try:
                    ba = pg.inner_text("body", timeout=5000)
                    if "sent" in ba.lower() or "check your email" in ba.lower():
                        dlog(f"Verification email re-sent for {email}")
                except Exception as e:
                    dlog(f"post-click check: {str(e)[:40]}")
            else:
                try:
                    clicked = pg.evaluate("""() => {
                        const els = [...document.querySelectorAll('button,a,[role=button]')];
                        const t = els.find(x => /verify/i.test(x.innerText||''));
                        if (t) { t.click(); return true; }
                        return false;
                    }""")
                    if clicked:
                        log("Clicked element containing 'verify'", "ok")
                        resend_found = True
                        time.sleep(3)
                    else:
                        log("No 'verify' element found on dashboard", "warn")
                except Exception as e:
                    elog("verify fallback click: " + str(e), traceback.format_exc()[:200])
            # free models modal
            try:
                free_btn = pg.locator('button:has-text("Enable free models")').first
                if free_btn.is_visible(timeout=2000):
                    free_btn.click(timeout=10000)
                    dlog(f"Enabled free models for {email}")
                    time.sleep(3)
            except Exception as e:
                dlog(f"free models modal: {str(e)[:40]}")
            b.close()
            return resend_found
    except Exception as e:
        elog(f"resend error: {e}", traceback.format_exc()[:200])
        return False


def _open_link(link):
    """Open TH verification link in a fresh browser, then CONFIRM verification actually
    persisted (TH is an SPA: ?verify=success + 'Email Verified' text, but the real check
    is reloading WITHOUT the param and seeing no verification banner).

    Returns True only when verification is confirmed via the banner-less reload.
    Fixes the intermittent bug: link says 'verified' but status doesn't persist."""
    try:
        from playwright.sync_api import sync_playwright
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True,
                                  args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = b.new_context()
            pg = ctx.new_page()
            pg.goto(link, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 1. if the URL carries ?verify=success and page says "Email Verified", it
            #    consumed the token. But we must confirm it PERSISTED.
            url_has_success = "verify=success" in pg.url
            body = pg.inner_text("body").lower() if pg.locator("body").count() else ""
            saw_verify = "email verified" in body or "verified" in body

            # 2. reload WITHOUT the ?verify=success param (and any other success markers)
            #    to see the steady-state UI.
            pr = urlparse(pg.url)
            q = [(k, v) for k, v in parse_qsl(pr.query) if k != "verify"]
            clean = urlunparse((pr.scheme, pr.netloc, pr.path, pr.params,
                                urlencode(q), pr.fragment))
            pg.goto(clean, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            # 3. banner check — a still-unverified account shows a verification banner/
            #    prompt; verified shows none.
            body2 = pg.inner_text("body").lower() if pg.locator("body").count() else ""
            banner_texts = ["verify your email", "verify email", "confirm your email",
                            "please verify", "not verified", "verify to unlock"]
            has_banner = any(b for b in banner_texts if b in body2)

            b.close()

            if url_has_success and saw_verify and not has_banner:
                log("Verification CONFIRMED (banner-gone after clean reload)", "ok")
                return True
            if has_banner:
                dlog("verify link consumed but banner persists on reload — NOT persisted")
                return False
            # no banner at all even without ?verify=success → already verified before
            if not has_banner:
                return True
            return True
    except Exception as e:
        elog(f"open verify link: {e}", traceback.format_exc()[:200])
        return False


def verify_via_mailg(email, password="", api_key="", retries=10, delay=15):
    """Verify a TH account's email. If api_key is provided, ALSO test the key
    after banner-confirmation — only return True if the key actually works
    (email verified + key live). This catches the 'verified but reverifies' case.

    If a link was clicked but did NOT persist (?verify consumed but banner remains),
    RESEND a fresh link and keep retrying instead of giving up immediately."""
    log("Checking for existing verification email at " + email + "...", "info")
    msgs = read_mailg_inbox(email)
    link = find_verify_link(msgs)
    if link:
        log("Existing verification link found!", "ok")
        if not _open_link(link):
            # clicked but banner persists → link didn't take. Resend a fresh one.
            log("Existing link did not persist — resending a new one", "warn")
            if password:
                resend_verification(email, password)
            else:
                log("No password; waiting for email already sent during signup", "info")
        elif api_key:
            works, why = _test_key(api_key)
            if not works:
                log(f"Email verified but key FAILED ({why}) — not marked verified", "warn")
                return False
            log("Email + key verified!", "ok")
            return True
        else:
            log("Verified!", "ok")
            return True
    elif password:
        resend_verification(email, password)
    else:
        log("No password; waiting for email already sent during signup", "info")
    log("Waiting for verification at " + email + "...", "arr")
    for i in range(1, retries + 1):
        log(f"Checking inbox ({i}/{retries})...", "info")
        msgs = read_mailg_inbox(email)
        link = find_verify_link(msgs)
        if link:
            log("Link found!", "ok")
            if not _open_link(link):
                # clicked but not persisted → resend fresh and keep polling
                log(f"Link {i} consumed but not persisted — resending a new one", "warn")
                if password:
                    try:
                        resend_verification(email, password)
                    except Exception as e:
                        dlog(f"resend during retry: {e}")
                continue
            if api_key:
                works, why = _test_key(api_key)
                if not works:
                    log(f"Email verified but key FAILED ({why}) — not marked verified", "warn")
                    return False
                log("Email + key verified!", "ok")
                return True
            log("Verified!", "ok")
            return True
        log(f"No link yet ({i}/{retries}), waiting {delay}s...", "info")
        time.sleep(delay)
    log("Verification link not found after all retries", "warn")
    return False


def verify_via_cloudmail(email, password="", api_key="", retries=10, delay=15):
    if password:
        resend_verification(email, password)
    log(f"Verifying {email}...", "arr")
    for i in range(1, retries + 1):
        dlog(f"Checking inbox for {email} ({i}/{retries})...")
        msgs = read_cloudmail_inbox(email)
        link = find_verify_link(msgs)
        if link:
            dlog(f"Link found for {email}")
            if not _open_link(link):
                # clicked but not persisted → resend fresh, keep polling
                log(f"Link for {email} consumed but not persisted — resending", "warn")
                if password:
                    try:
                        resend_verification(email, password)
                    except Exception as e:
                        dlog(f"resend during retry: {e}")
                continue
            if api_key:
                works, why = _test_key(api_key)
                if not works:
                    log(f"Email verified but key FAILED ({why}) — not marked verified", "warn")
                    return False
                log(f"Verified + key OK: {email}", "ok")
                return True
            log(f"Verified: {email}", "ok")
            return True
        dlog(f"No link yet for {email} ({i}/{retries}), waiting {delay}s...")
        time.sleep(delay)
    log(f"Verification failed for {email}", "warn")
    return False


# ---- account creation ----
def _load_proxy_mod():
    """Lazy-import th-proxy so the TUI works even if it's missing."""
    # ensure generic deps (requests/socks) are present first
    _ensure_deps("proxy_check")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("thproxy", str(BASE / "th-proxy.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        elog("proxy module load: " + str(e))
        return None


def _ensure_deps(feature, auto=True):
    """Lazy-install a feature's dependencies via th-deps."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("thdeps", str(BASE / "config" / "th-deps.py"))
        if spec and spec.loader:
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            ok, missing = m.ensure(feature, auto=auto)
            if auto and not ok:
                log("Missing deps for " + feature + ": " + str(missing), "warn")
            return ok
    except Exception as e:
        elog("deps: " + str(e))
    return False


_LOCAL_PROXY_READY = False

def _ensure_local_proxy():
    """Ensure the bundled proxy-controller is running (start if not). Returns True if up.
    Caches readiness so we only wait for tunnels once, not on every proxy pick."""
    global _LOCAL_PROXY_READY
    if _LOCAL_PROXY_READY:
        return True
    start_script = BASE / "proxy-controller" / "start.sh"
    if not start_script.exists():
        return False
    import time as _t
    try:
        import subprocess as _sp
        # already running?
        st = _sp.run(["pgrep", "-f", "lite_manager.py"], capture_output=True, text=True)
        if st.returncode != 0:
            log("Starting bundled proxy-controller...", "info")
            _sp.run(["bash", str(start_script), "start"], capture_output=True, text=True, timeout=60)
        # wait for the SOCKS5 proxy to actually respond (tunnels take ~15s)
        for _ in range(12):
            try:
                r = _sp.run(["curl", "-s", "-x",
                    "socks5://proxy:wuzz%4004Store@127.0.0.1:7920",
                    "-m", "5", "http://api.ipify.org"],
                    capture_output=True, text=True, timeout=7)
                if r.returncode == 0 and r.stdout.strip() and r.stdout.strip().count('.') == 3:
                    log("Proxy-controller ready (" + r.stdout.strip() + ")", "ok")
                    _LOCAL_PROXY_READY = True
                    return True
            except Exception as _e:
                print(f"[swallow th-tui.py:1508] {_e}")
                pass
            _t.sleep(2)
        log("Proxy-controller started but not ready yet", "warn")
        return False
    except Exception as e:
        elog("ensure local proxy: " + str(e))
        return False


def _proxy_id(p):
    """Stable identity for a proxy entry (used for rate-limit cooldown)."""
    try:
        return f"{p[0]}://{p[1]}:{p[2]}" + (f":{p[3]}" if len(p) > 3 and p[3] else "")
    except Exception as _e:
        print(f"[swallow th-tui.py:1536] {_e}")
        return str(p)


def _rl_file():
    return BASE / "proxy_ratelimit.json"


def _load_ratelimited():
    """Return {proxy_id: ts} for proxies on 1h cooldown (expired purged)."""
    try:
        d = json.loads(_rl_file().read_text())
    except Exception as _e:
        print(f"[swallow th-tui.py:1548] {_e}")
        d = {}
    now = time.time()
    keep = {k: v for k, v in d.items() if now - float(v) < 3600}
    if len(keep) != len(d):
        try:
            _rl_file().write_text(json.dumps(keep, indent=2))
        except Exception as _e:
            print(f"[swallow th-tui.py:1541] {_e}")
            pass
    return keep


def _mark_proxy_ratelimited(proxy_id, ip=""):
    """Put a proxy on 1h cooldown after backend rate-limit."""
    try:
        d = _load_ratelimited()
        d[proxy_id] = time.time()
        _rl_file().write_text(json.dumps(d, indent=2))
    except Exception as e:
        elog("mark ratelimit: " + str(e))
    log(f"Proxy rate-limited -> cooldown 1h: {proxy_id}" + (f" (IP: {ip})" if ip else ""), "warn")


def _is_ratelimited(proxy_id):
    return proxy_id in _load_ratelimited()


def _next_proxy(c, last=None):
    """Get the next proxy for an account based on proxy config (smart balanced)."""
    pm = _load_proxy_mod()
    if not pm:
        return None
    cfg = c.get("proxy", {})
    mode = cfg.get("mode", "list")
    _order = cfg.get("proxy_order", "top")
    if mode in ("combo", "list"):
        # ensure local proxy-controller is up only when balancing (least) needs it
        proxies = pm.load_proxies()
        uses_local = any(p[0] in ("socks5", "http") and p[1] in ("127.0.0.1", "localhost") for p in proxies)
        if uses_local and _order == "least":
            _ensure_local_proxy()
    order = cfg.get("proxy_order", "top")
    if mode == "combo" and order == "least":
        # combo with least-used: try local proxy-controller first, fall back to list
        try:
            import subprocess
            r = subprocess.run(["curl", "-s", "-x",
                "socks5://proxy:wuzz%4004Store@127.0.0.1:7920",
                "-m", "6", "http://api.ipify.org"],
                capture_output=True, text=True, timeout=8)
            if r.returncode == 0 and r.stdout.strip() and r.stdout.strip().count('.') == 3:
                return ("socks5", "127.0.0.1", 7920, "proxy", "wuzz@04Store")
        except Exception as _e:
            print(f"[swallow th-tui.py:1586] {_e}")
            pass
        # fall through to list
    if mode == "vpngate":
        p = pm.vpngate_proxy()
        return p
    # MANUAL OVERRIDE: if proxy.current is set (user locked a proxy), use it
    # directly WITHOUT the auto liveness check — manual wins over checked status.
    manual = cfg.get("current")
    if manual:
        # manual may be a parsed tuple or a "host:port" / "scheme://user:pass@host:port" string
        p = manual
        if isinstance(manual, str):
            try:
                p = pm.parse_proxy(manual) if hasattr(pm, "parse_proxy") else _parse_manual_proxy(manual)
            except Exception as _e:
                print(f"[swallow th-tui.py:1617] {_e}")
                p = None
        if p:
            _host = p[1] if len(p) > 1 else "?"
            _port = p[2] if len(p) > 2 else "?"
            log(f"Using MANUAL proxy (override): {_host}:{_port}", "ok")
            return p
    # honor proxy_order config: top / random / least-used
    order = cfg.get("proxy_order", "top")
    proxies = pm.load_proxies()
    if not proxies:
        return None
    used_ips = set(c.get("_used_proxy_ips", []))
    if order == "least":
        p, ip = pm.smart_pick_proxy(proxies, used_ips=used_ips)
        if p:
            pass  # caller adds to _used_proxy_ips on failure
            log(f"Using proxy: {p[1] if len(p)>1 else '?'}:{p[2] if len(p)>2 else '?'} (IP: {ip}, order=least)", "info")
            return p
        return None
    # top / random: iterate candidates, skip dead/failed proxies (verify live)
    if order == "random":
        cands = list(proxies)
        random.shuffle(cands)
    else:  # top
        cands = list(proxies)
    _rl_now = _load_ratelimited()
    for p in cands[:min(30, len(cands))]:
        # relay proxies (https://*.vercel.app) can't be Playwright socket proxies — skip for browser use
        if p[0] == "relay":
            continue
        # skip proxies on 1h rate-limit cooldown
        if _is_ratelimited(_proxy_id(p)):
            dlog(f"skip ratelimited proxy: {_proxy_id(p)}")
            continue
        try:
            # use cached check result (from proxy-menu C=Check) when fresh —
            # overrides the auto live-check on every use
            res = pm.cached_check_proxy(p, timeout=8) if hasattr(pm, "cached_check_proxy") else pm.check_proxy(p, timeout=8)
        except Exception as _e:
            print(f"[swallow th-tui.py:1656] {_e}")
            res = None
        if not res:
            continue  # dead proxy, try next
        ip = res[1] if len(res) > 1 else (p[1] if len(p) > 1 else "?")
        if ip in used_ips:
            continue  # skip previously failed proxy
        c["_last_proxy_ip"] = ip  # track for failure reporting
        _host = p[1] if len(p) > 1 else "?"
        _port = p[2] if len(p) > 2 else "?"
        log(f"Using proxy: {_host}:{_port} (IP: {ip}, order={order})", "info")
        return p
    log("No live proxy found in pool", "warn")
    return None



def _parse_manual_proxy(s):
    """Parse a manual proxy string: scheme://user:pass@host:port or host:port."""
    import re as _re
    s = s.strip()
    scheme = "http"
    user = ""
    pw = ""
    host = s
    port = None
    m = _re.match(r"^(https?|socks4|socks5)://(.*)$", s, _re.I)
    if m:
        scheme = m.group(1).lower()
        rest = m.group(2)
    else:
        rest = s
    if "@" in rest:
        cred, host = rest.rsplit("@", 1)
        if ":" in cred:
            user, pw = cred.split(":", 1)
        else:
            user = cred
    if ":" in host:
        hp = host.rsplit(":", 1)
        if hp[1].isdigit():
            host = hp[0]
            port = int(hp[1])
    if port is None:
        port = 443 if scheme in ("https",) else 80
    return (scheme, host, port, user, pw)


def _solve_captcha(pg, c, timeout=180):
    """Solve Cloudflare Turnstile during signup.
    - vnc_mode: manual solve via VNC — boss watches the visible browser and
      clicks the checkbox; we poll for the turnstile token (cheap + vision-friendly).
    - headless: try grok.solve_turnstile_bycf (Camoufox-based).
    Returns the token string or None.
    """
    import urllib.parse as _up
    # detect turnstile iframe presence
    has_ts = False
    try:
        if pg.frame_locator('iframe[src*="turnstile"]').first.is_visible(timeout=1500):
            has_ts = True
    except Exception as _e:
        print(f"[swallow th-tui.py:1701] {_e}")
        pass
    if not has_ts:
        # some builds embed it differently
        try:
            body = pg.inner_text("body", timeout=3000)
            has_ts = "verify" in body.lower() and "turnstile" in pg.content().lower()
        except Exception as _e:
            print(f"[swallow th-tui.py:1708] {_e}")
            pass
    if not has_ts:
        return None

    if c.get("vnc_mode"):
        log("Turnstile detected — solve it in the VNC browser (checkbox)", "warn")
        for _ in range(int(timeout / 2)):
            try:
                tok = pg.evaluate("() => document.querySelector('[name=\"cf-turnstile-response\"]')?.value || ''")
                if tok:
                    log("Turnstile solved (token captured)", "ok")
                    return tok
            except Exception as _e:
                print(f"[swallow th-tui.py:1721] {_e}")
                pass
            time.sleep(2)
        log("Turnstile not solved manually in time", "warn")
        return None

    # headless: try BYCF/Camoufox solver
    try:
        _ensure_deps("curl_cffi")
        import importlib.util as _ilu
        sys.path.insert(0, str(BASE))
        g = _ilu.spec_from_file_location("grok", str(BASE / "grok.py"))
        gm = _ilu.module_from_spec(g)
        g.loader.exec_module(gm)
        sitekey = ""
        m = re.search(r'sitekey["\s:=]+([A-Za-z0-9_-]{20,})', pg.content())
        if m:
            sitekey = m.group(1)
        if sitekey:
            log(f"Solving Turnstile headless (sitekey {sitekey[:12]}...)", "info")
            tok = gm.solve_turnstile_bycf(sitekey=sitekey, page_url=pg.url)
            if tok:
                log("Turnstile solved (headless)", "ok")
                return tok
    except Exception as e:
        dlog(f"headless turnstile: {e}")
    return None


def _shot_fail(pg, email):
    """Save a diagnostic screenshot on signup failure."""
    try:
        _dir = BASE / "debug_shots"
        _dir.mkdir(exist_ok=True)
        _shot = _dir / f"signup_fail_{email.split('@')[0]}_{int(time.time())}.png"
        pg.screenshot(path=str(_shot), full_page=True)
        log(f"Screenshot saved: {_shot}", "info")
        return str(_shot)
    except Exception as _e:
        print(f"[swallow th-tui.py:1778] {_e}")
        return None


def _explain_signup_fail(err_text, url, body_head):
    """Map a raw signup failure to a human-readable explanation."""
    e = (err_text or "").lower()
    u = (url or "").lower()
    b = (body_head or "").lower()
    if "timeout" in e and "button" in e:
        return "Submit button clicked but no response (backend slow/blocked — proxy may be flagged)"
    if "timeout" in e and "fill" in e:
        return "Form field not rendered in time (proxy slow or page blocked)"
    if "timeout" in e:
        return "Page operation timed out (proxy slow or unresponsive)"
    if "proxy" in e:
        return f"Proxy connection error: {err_text[:80]}"
    if "too many sign-ups" in b or "from this network" in b or "in an hour" in b:
        return "TokenHarbor network rate-limit exceeded (this proxy IP used too many signups) — rotating proxy, wait before retry"
    if "couldn't create" in b or "try again in a minute" in b:
        return "TokenHarbor backend rate-limited signups from this IP — rotating proxy"
    if "couldn't create" in b or "our team has been alerted" in b:
        return "TokenHarbor backend rejected signup (IP flagged) — rotating proxy"
    if "already registered" in b or "already exists" in b:
        return "Email already registered — marking used"
    if "blacklist" in b or "blocked" in b or "suspicious" in b:
        return "Email domain/account blacklisted or flagged as suspicious"
    if "rate limit" in b or "too many requests" in b:
        return "Rate limited by TokenHarbor — rotating proxy"
    if "invalid email" in b or "temp email" in b or "disposable" in b:
        return "Email rejected as invalid/disposable"
    if "signup" in u or "signin" in u:
        return "Stayed on signup page after submit (backend did not accept — proxy IP likely flagged)"
    return f"Unexpected signup result (url={url} body={body_head[:60]})"


def _port_open(port, timeout_s=1.5):
    import socket
    s = socket.socket()
    s.settimeout(timeout_s)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _env_vnc_pw():
    """Read VNC_PASSWORD from <project>/.env (returns '' if unset)."""
    env_path = BASE / ".env"
    if not env_path.exists():
        return ""
    try:
        for line in open(env_path):
            line = line.strip()
            if line.startswith("VNC_PASSWORD=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    except Exception as _e:
        print(f"[swallow th-tui.py:1815] {_e}")
        pass
    return ""


def _start_vnc_stack():
    """Ensure Xvfb(:99) + x11vnc(5900) + websockify(6080) are running for headed mode.
    Non-blocking; each component started only if its port/process is missing."""
    import subprocess as _sp
    global VNC_PASSWORD
    vnc_pw = os.environ.get("VNC_PASSWORD", "") or _env_vnc_pw()
    if not VNC_PASSWORD:
        VNC_PASSWORD = vnc_pw  # keep global in sync
    auth_xs = vnc_pw or "Phoe9Ceixingie5ahsah7fieruNg2eijujoofoA1apu6uwevuv8ait3ieshahh3ish"
    os.environ.setdefault("DISPLAY", ":99")
    # 1. Xvfb — process + display check
    if not (_sp.call("pgrep -x Xvfb >/dev/null 2>&1", shell=True) == 0):
        _sp.Popen(["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        for _ in range(10):
            time.sleep(1)
            if _sp.call("DISPLAY=:99 xdpyinfo >/dev/null 2>&1", shell=True) == 0:
                break
        log("Started Xvfb :99", "ok" if _port_open(5900) or _sp.call("pgrep -x Xvfb >/dev/null 2>&1", shell=True) == 0 else "err")
    # 2. x11vnc — port 5900 check
    if not _port_open(5900):
        if not os.path.exists("/run/x11vnc-passwd"):
            _sp.run('x11vnc -storepasswd "%s" /run/x11vnc-passwd' % auth_xs, shell=True)
            try:
                os.chmod("/run/x11vnc-passwd", 0o600)
            except Exception as _e:
                print(f"[swallow th-tui.py:1845] {_e}")
                pass
        elif os.environ.get("VNC_PASSWORD") or _env_vnc_pw():
            try:
                _sp.run('x11vnc -storepasswd "%s" /run/x11vnc-passwd' % auth_xs, shell=True)
                os.chmod("/run/x11vnc-passwd", 0o600)
            except Exception as _e:
                print(f"[swallow th-tui.py:1851] {_e}")
                pass
        _sp.Popen(["x11vnc", "-display", ":99", "-forever", "-shared",
                   "-rfbauth", "/run/x11vnc-passwd", "-rfbport", "5900"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        for _ in range(10):
            time.sleep(1)
            if _port_open(5900):
                break
        log("Started x11vnc :99", "ok" if _port_open(5900) else "err")
    # 3. websockify noVNC — port 6080 check
    if not _port_open(6080):
        web_dir = "/opt/noVNC"
        if not os.path.isdir(os.path.join(web_dir, "vnc.html")):
            alt = _sp.getoutput("ls -d /tmp/*noVNC* 2>/dev/null; ls -d /root/*noVNC* 2>/dev/null").strip().split()
            web_dir = next((d for d in alt if os.path.isdir(os.path.join(d, "vnc.html"))), web_dir)
        _sp.Popen(["websockify", "--web", web_dir, "6080", "127.0.0.1:5900"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        for _ in range(10):
            time.sleep(1)
            if _port_open(6080):
                break
        log("Started websockify :6080", "ok" if _port_open(6080) else "err")
    try:
        out = _sp.getoutput("ss -ltn 'sport = :6080' 2>/dev/null | head -1; echo MARKER; ss -ltn 'sport = :5900' 2>/dev/null | head -1")
        log(f"VNC ports: {out.replace(chr(10), ' | ')}", "info")
    except Exception as _e:
        print(f"[swallow th-tui.py:1877] {_e}")
        pass


def create_account(c, email=None, password=None, _retry=True):
    if c.get("vnc_mode"):
        _start_vnc_stack()
    if c.get("vnc_mode") and not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = ":99"
    sys.path.insert(0, str(BASE))
    # Check/install before importing; the old order crashed before th-deps could help.
    if not _ensure_deps("playwright"):
        elog("playwright not available — install via th-deps", "")
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        elog("playwright import failed: " + str(e))
        return None
    if not email:
        email = pick_address(c)
    if not email:
        log("No usable email address available", "warn")
        return None
    password = password or build_pw(c)
    dlog(f"Creating account: {email}")
    headless = not c.get("vnc_mode", False)
    try:
        pw_timeout_ms = max(5, int(c.get("pw_timeout", 120))) * 1000
    except Exception as _e:
        print(f"[swallow th-tui.py:1929] {_e}")
        pw_timeout_ms = 120000
    _ensure_deps("curl_cffi")
    # proxy handling
    pm = _load_proxy_mod()
    pcfg = c.get("proxy", {})
    proxy_parsed = None
    if pcfg.get("enabled") and pm:
        if pcfg.get("mode") in ("list", "combo"):
            proxy_parsed = _next_proxy(c)
            if proxy_parsed:
                dlog(f"Using proxy: {pm.proxy_url(proxy_parsed, hide_password=True)} for {email}")
        elif pcfg.get("mode") == "vpngate":
            proxy_parsed = pm.vpngate_proxy()
            if proxy_parsed:
                dlog(f"Using VPNGate residential: {proxy_parsed[1]} for {email}")
    try:
        with sync_playwright() as p:
            # Host-resolver rule must EXCLUDE the proxy host, else Chromium can't
            # resolve the proxy itself over http/https (socks5 bridge is 127.0.0.1
            # IP literal — unaffected by DNS rules).
            _hrr = "MAP * ~NOTFOUND"
            if proxy_parsed and proxy_parsed[1]:
                _hrr += ", EXCLUDE " + proxy_parsed[1]
            launch_kwargs = {"executable_path": "/usr/bin/chromium-browser", "headless": headless,
                             "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                      "--disable-features=UseDnsHttpsSvcbAlpn",
                                      "--host-resolver-rules=" + _hrr,
                                      "--disable-ipv6",
                                      "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                                      "--disable-rtc-smoothness-algorithm"]}
            b = p.chromium.launch(**launch_kwargs)
            ctx_kwargs = {"viewport": {"width": 1280, "height": 720}}
            if proxy_parsed and pm:
                ctx_kwargs["proxy"] = pm.proxy_to_playwright(proxy_parsed)
            ctx = b.new_context(**ctx_kwargs)
            ctx.set_default_timeout(pw_timeout_ms)
            ctx.set_default_navigation_timeout(pw_timeout_ms)
            pg = ctx.new_page()
            _api_errors = []
            def _on_api_resp(resp):
                try:
                    if resp.request.method == "POST" and resp.status >= 400:
                        ct = (resp.headers.get("content-type") or "").lower()
                        if "json" in ct:
                            j = resp.json()
                            if isinstance(j, dict) and ("error" in j or "message" in j):
                                msg = j.get("error", {})
                                if isinstance(msg, dict):
                                    msg = msg.get("message", "")
                                text = f"{resp.status}:{str(msg)[:150]}" if msg else f"{resp.status}:{str(j)[:150]}"
                                _api_errors.append(text)
                except Exception as _e:
                    print(f"[swallow th-tui.py:1958] {_e}")
                    pass
            pg.on("response", _on_api_resp)
            pg.goto("https://tokenharbor.ai/login?mode=signup", wait_until="domcontentloaded", timeout=pw_timeout_ms)
            try:
                pg.wait_for_load_state("networkidle", timeout=min(15000, pw_timeout_ms))
            except Exception as _e:
                print(f"[swallow th-tui.py:1964] {_e}")
                pass
            # wait for form to render (proxy may be slow)
            try:
                pg.wait_for_selector('input[name="email"]', timeout=min(30000, pw_timeout_ms))
            except Exception as _e:
                print(f"[swallow th-tui.py:1969] {_e}")
                pass
            time.sleep(2)  # extra settle time for proxy
            time.sleep(1)
            # humanize: load helpers + human-like fill/click
            try:
                import importlib.util as _ihu
                _hz_spec = _ihu.spec_from_file_location("humanize", str(BASE / "humanize.py"))
                _hz = _ihu.module_from_spec(_hz_spec); _hz_spec.loader.exec_module(_hz)
            except Exception as _e:
                print(f"[swallow th-tui.py:2004] {_e}")
                _hz = None
            try:
                if _hz:
                    _hz.human_type(pg, pg.locator('input[name="email"]'), email)
                    _hz.human_type(pg, pg.locator('input[name="password"]'), password)
                    _hz.rand_delay(0.4, 1.2)
                    _hz.human_click(pg, pg.locator('button[type="submit"]'))
                else:
                    pg.fill('input[name="email"]', email)
                    pg.fill('input[name="password"]', password)
                    pg.click('button[type="submit"]', timeout=30000)
            except Exception as e:
                elog(f"form fill/submit failed: {_explain_signup_fail(str(e), '', '')}")
                _shot_fail(pg, email)
                b.close()
                return None
            # solve Turnstile if present (manual via VNC or headless solver)
            try:
                _solve_captcha(pg, c, timeout=180)
            except Exception as e:
                dlog(f"solve captcha: {e}")
            # wait for page to settle — check URL + body for dashboard/landing
            time.sleep(3)
            try:
                pg.wait_for_load_state("networkidle", timeout=15000)
            except Exception as _e:
                print(f"[swallow th-tui.py:2004] {_e}")
                pass
            url = pg.url.lower()
            body = ""
            try:
                body = pg.inner_text("body", timeout=8000).lower()
            except Exception as _e:
                print(f"[swallow th-tui.py:2010] {_e}")
                pass
            # detect dashboard vs landing page vs stall
            # SPA: URL may stay /login?mode=signup even after dashboard loads
            # Check body for dashboard-specific content
            _dash_signals = ["balance", "overview", "api key", "tokens", "subscription", "enable f"]
            on_dashboard = "dashboard" in url or "api-keys" in url or "dashboard" in body or "api-keys" in body or any(s in body for s in _dash_signals)
            # signup page has form + marketing text — NOT a landing page
            has_form = pg.locator('input[name="email"]').count() > 0
            # /login?mode=signup is the signup page (has form + marketing text)
            is_signup = "mode=signup" in url or "mode=signin" in url
            on_landing = "token harbor" in body and "one harbor" in body and "dashboard" not in body and not has_form and not is_signup
            # if page shows "loading", wait for it to finish
            if on_dashboard and ("loading" in body or len(body) < 50):
                try:
                    pg.wait_for_load_state("networkidle", timeout=15000)
                except Exception as _e:
                    print(f"[swallow th-tui.py:2026] {_e}")
                    pass
                body = pg.inner_text("body", timeout=8000).lower()
                # SPA: URL may stay /login?mode=signup even after dashboard loads
            # Check body for dashboard-specific content
            _dash_signals = ["balance", "overview", "api key", "tokens", "subscription", "enable f"]
            on_dashboard = "dashboard" in url or "api-keys" in url or "dashboard" in body or "api-keys" in body or any(s in body for s in _dash_signals)
            if on_dashboard:
                dlog(f"Account created + dashboard reached for {email}")
                # robust API key creation: retry modal, fall back to existing key on page
                api_key = ""
                for attempt in range(3):
                    try:
                        pg.goto("https://tokenharbor.ai/dashboard/api-keys", wait_until="domcontentloaded", timeout=pw_timeout_ms)
                        time.sleep(3)
                        body2 = pg.inner_text("body", timeout=10000)
                        for mm in re.finditer(r"thk_[a-zA-Z0-9_-]{20,}", body2):
                            api_key = mm.group(0)
                            break
                        if api_key:
                            break
                        # click + New key, fill label, create
                        try:
                            if _hz:
                                _hz.human_click(pg, pg.locator('button:has-text("+ New key")'))
                                time.sleep(1.5)
                                _hz.human_type(pg, pg.locator('input[placeholder*="label"], input[name*="label"], input[type="text"]'), "main")
                                time.sleep(1)
                                _hz.human_click(pg, pg.locator('button:has-text("Create key"), button:has-text("Create")'))
                            else:
                                pg.click('button:has-text("+ New key")', timeout=8000)
                                time.sleep(2)
                                pg.fill('input[placeholder*="label"], input[name*="label"], input[type="text"]', "main", timeout=8000)
                                time.sleep(1)
                                pg.click('button:has-text("Create key"), button:has-text("Create")', timeout=8000)
                            time.sleep(3)
                        except Exception as e:
                            log(f"key modal attempt {attempt+1}: {str(e)[:50]}", "warn")
                        body2 = pg.inner_text("body", timeout=10000)
                        for mm in re.finditer(r"thk_[a-zA-Z0-9_-]{20,}", body2):
                            api_key = mm.group(0)
                            break
                        if api_key:
                            break
                    except Exception as e:
                        elog(f"api key attempt {attempt+1} {email}: {str(e)[:80]}")
                        time.sleep(2)
                b.close()
                return {"email": email, "password": password, "api_key": api_key, "verified": False}
            elif any(p in body for p in [
                "has already been registered", "email already exists",
                "this email is already", "already registered with", "email is already on",
                "already on board",
            ]):
                _api = _api_errors[-1] if _api_errors else 'none'
                log(f"Email already registered: {email} | api={_api} | page={body[:120]}", "warn")
                b.close()
                mark_used(email)  # never pick this address again
                c["_email_terminal"] = True  # stop retrying this email
                return None
            elif any(p in body for p in [
                "couldn't create your account", "couldn't create your account right now",
                "can't create your account", "try again in a minute",
                "our team has been alerted", "support team has been informed",
                "rate limit", "too many requests",
                "too many sign-ups", "sign-ups from this network", "in an hour",
                "blacklist", "blocked", "suspicious", "invalid email",
                "email domain not allowed", "temp email", "disposable",
            ]):
                _is_net_rl = any(s in body for s in ["too many sign-ups", "sign-ups from this network", "in an hour"])
                if _is_net_rl and proxy_parsed:
                    _mark_proxy_ratelimited(_proxy_id(proxy_parsed), c.get("_last_proxy_ip", ""))
                    c["_last_fail_ratelimit"] = True
                else:
                    log("Backend blocked signup — rotating proxy", "warn")
                b.close()
                return None  # let run_full_flow retry with different proxy
            else:
                url_now = pg.url
                _hf = pg.locator('input[name="email"]').count()
                dlog(f"DETECT: url={url_now} on_dashboard={on_dashboard} on_landing={on_landing} has_form={_hf} body_head={body[:80]}")
                # signup page with form — try submitting (not an error, page just didn't redirect)
                if _hf > 0 and ("signup" in url_now or "signin" in url_now):
                    dlog(f"Signup page with form, retrying submit for {email}")
                    try:
                        pg.fill('input[name="email"]', email)
                        pg.fill('input[name="password"]', password)
                        pg.click('button[type="submit"]', timeout=10000)
                        # wait for page to settle after submit
                        try:
                            pg.wait_for_load_state("networkidle", timeout=20000)
                        except Exception as _e:
                            print(f"[swallow th-tui.py:2117] {_e}")
                            pass
                        time.sleep(3)
                        # re-read body + URL
                        body = pg.inner_text("body", timeout=10000).lower()
                        url_now = pg.url
                        _dash_signals = ["balance", "overview", "api key", "tokens", "subscription", "enable f"]
                        _is_dash = "dashboard" in url_now or "api-keys" in url_now or "dashboard" in body or any(s in body for s in _dash_signals)
                        if _is_dash:
                            dlog(f"Retry submit succeeded — dashboard reached for {email}")
                            on_dashboard = True
                        else:
                            _why_signup = _explain_signup_fail('', url_now, body[:120])
                            _api = _api_errors[-1] if _api_errors else 'none'
                            elog(f"signup retry failed: {_why_signup} | api={_api} | page={body[:100]}")
                            _shot_fail(pg, email)
                            b.close()
                            return None
                    except Exception as e:
                        _why_signup = _explain_signup_fail(str(e), '', '')
                        _api = _api_errors[-1] if _api_errors else 'none'
                        elog(f"signup retry error: {_why_signup} | api={_api} | raw={str(e)[:120]}")
                        _shot_fail(pg, email)
                        b.close()
                        return None
                else:
                    _why_signup = _explain_signup_fail('', url_now, body[:120])
                    _api = _api_errors[-1] if _api_errors else 'none'
                    elog(f"signup unexpected: {_why_signup} | api={_api} | page={body[:100]}")
                    _shot_fail(pg, email)
                    b.close()
                    return None
    except Exception as e:
        elog(f"create account {email}: {e}", traceback.format_exc()[:200])
        try:
            b.close()
        except Exception as _e:
            print(f"[swallow th-tui.py:2153] {_e}")
            pass
        return None


def save_key(data):
    """Persist one record without duplicate rows. An account WITH an api_key
    defaults to status=ok (not pending) unless explicitly marked unverified."""
    data = dict(data)
    if data.get("api_key") and not data.get("verified"):
        data["verified"] = True  # key present => account was created+verified at signup
    merge_key_record(data,
                     verified=bool(data.get("verified")),
                     free_ok=bool(data.get("free_ok")))


def save_unused_email(email, password="test123"):
    """Store an email that was created (inbox ready) but no TH account yet.
    Marks it as 'unused' in keys.txt so it can be reused next batch instead of wasted.
    Dedupes by email — if already present, updates password only (preserves existing status/key)."""
    email = email.lower()
    line = f"{email}|{password}||unused"
    lines = []
    if KEYS_FILE.exists():
        lines = [l.rstrip("\n") for l in KEYS_FILE.read_text().splitlines() if l.strip()]
    kept = []
    seen = False
    for l in lines:
        parts = l.split("|")
        if parts and parts[0].lower() == email:
            # if existing is pending/verified/ok, keep its progress; just update password if empty
            if len(parts) >= 2 and not parts[1]:
                parts[1] = password
                kept.append("|".join(parts))
            else:
                kept.append(l)
            seen = True
        else:
            kept.append(l)
    if not seen:
        kept.append(line)
    try:
        KEYS_FILE.write_text("\n".join(kept) + "\n")
        dlog(f"Unused email saved (reusable next batch): {email}")
        return True
    except Exception as e:
        elog(f"save unused email: {e}")
        return False


def load_reusable_emails():
    """Get emails that should be reused before generating fresh ones.
    Priority: unused (created inbox, no account) > pending (has account but unverified).
    Returns list of (email, password, status)."""
    reusable = {"unused": [], "pending": []}
    for k in load_keys():
        st = k.get("status", "pending")
        if st in ("unused", "pending"):
            reusable[st].append((k["email"], k["password"], st))
    return reusable


def pick_next_email(c):
    """Choose the next email for an account — reuse before generating fresh.
    1. unused emails (inbox created, no account yet) — just create account
    2. pending emails (account exists, needs verification) — reverify
    3. fresh generation (fallback)."""
    reusable = load_reusable_emails()
    # prefer unused first (they have ready inboxes)
    if reusable["unused"]:
        email, pw, st = reusable["unused"].pop(0)
        dlog(f"Reusing unused email: {email}")
        return email, pw, "reuse_unused"
    if reusable["pending"]:
        email, pw, st = reusable["pending"].pop(0)
        dlog(f"Reverifying pending: {email}")
        return email, pw, "reverify_pending"
    # none reusable — fresh
    return None, None, "fresh"


def load_keys():
    if not KEYS_FILE.exists():
        return []
    keys = []
    try:
        for l in open(KEYS_FILE):
            p = l.strip().split("|")
            if len(p) >= 3 and "@" in p[0]:
                keys.append({"email": p[0], "password": p[1], "api_key": p[2],
                             "status": p[3] if len(p) > 3 else "pending"})
    except Exception as e:
        elog(f"load keys: {e}")
    return keys


def _key_fingerprint(key):
    """Stable non-secret fingerprint used to invalidate stale health cache entries."""
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def load_key_checks():
    """Load cached API-key health checks. Backward/absence safe."""
    try:
        if KEY_CHECKS_FILE.exists():
            data = json.loads(KEY_CHECKS_FILE.read_text())
            return data if isinstance(data, dict) else {}
    except Exception as e:
        dlog(f"key check cache read: {e}")
    return {}


def save_key_checks(checks):
    """Atomically persist health cache so interrupted writes do not corrupt it."""
    try:
        tmp = KEY_CHECKS_FILE.with_suffix(KEY_CHECKS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(checks, indent=2, sort_keys=True))
        tmp.replace(KEY_CHECKS_FILE)
        return True
    except Exception as e:
        elog(f"key check cache write: {e}")
        return False


def _classify_key_check(works, reason):
    """Map a probe result to a stable KEY-health category.

    Account verification is deliberately separate. In particular, 401 and 403
    are not the same thing: 401 is an authentication/key rejection while 403 is
    an authenticated-but-forbidden response (which may have several causes).
    """
    r = (reason or "").lower()
    if works:
        return "live"
    if "429" in r or "rate limit" in r or "ratelimit" in r:
        return "ratelimited"
    if "402" in r or "can't serve" in r or "cannot serve" in r or "free plan" in r or "credit" in r or "insufficient" in r:
        return "planlimited"
    if "401" in r or "unauthorized" in r:
        return "invalid"
    if "403" in r or "forbidden" in r:
        return "forbidden"
    return "failed"


def _cached_key_state(rec, checks):
    """Return live/ratelimited/invalid/forbidden/failed/unchecked/no_key."""
    key = rec.get("api_key", "")
    if not key:
        return "no_key"
    ent = checks.get(rec.get("email", ""), {})
    if not isinstance(ent, dict) or ent.get("fingerprint") != _key_fingerprint(key):
        return "unchecked"
    state = ent.get("state", "unchecked")

    # v9/v10 cached 401 and 403 together as ``invalid``. Re-split legacy cache
    # entries from the persisted reason so upgrading does not require a full recheck.
    if state == "invalid":
        reason = str(ent.get("reason", "")).lower()
        if "403" in reason or "forbidden" in reason:
            return "forbidden"
    return state if state in {"live", "ratelimited", "planlimited", "invalid", "forbidden", "failed"} else "unchecked"


def _account_state(rec):
    """Persistent account state, independent of API-key health."""
    st = str(rec.get("status", "pending") or "pending").lower()
    if st in ("verified", "ok", "ok+free"):
        return "verified"
    if st == "unused":
        return "unused"
    return "unverified"


def _is_verified_record(rec):
    return _account_state(rec) == "verified"


def _token_filter_match(rec, mode, checks):
    if mode == "all":
        return True
    if mode in ("verified", "nonverified", "unused"):
        wanted = "unverified" if mode == "nonverified" else mode
        return _account_state(rec) == wanted
    return _cached_key_state(rec, checks) == mode


def _token_filter_counts(keys, checks):
    modes = (
        "live", "ratelimited", "planlimited", "invalid", "forbidden", "failed", "unchecked", "no_key",
        "verified", "nonverified", "unused",
    )
    return {m: sum(1 for rec in keys if _token_filter_match(rec, m, checks)) for m in modes}


def _test_key(key, model="deepseek-v4-flash:free"):
    """Test an API key — checks /v1/models AND tries a real :free chat completion.
    Uses relay (.vercel.app) with x-relay-target if available, else socket proxy, else direct."""
    import requests
    target = "https://tokenharbor.ai"
    relay = _get_relay_proxy()
    # build proxy kwargs for requests (socket proxies)
    proxies = None
    pm = _load_proxy_mod()
    if not relay and pm:
        pcfg = load_cfg().get("proxy", {})
        if pcfg.get("enabled"):
            try:
                pp = _next_proxy(load_cfg())
                if pp and pp[0] in ("http", "socks5"):
                    proxies = _build_requests_proxy(pp)
            except Exception as _e:
                print(f"[swallow th-tui.py:2366] {_e}")
                pass
    def _req(method, path, body=None):
        # relay: call relay ROOT, x-relay-target = full upstream URL incl path
        headers = {"Authorization": f"Bearer {key}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        kw = dict(headers=headers, proxies=proxies, timeout=20)
        if body is not None:
            kw["json"] = body
        # try relay first; fall back to direct if relay fails (POST often breaks)
        if relay:
            try:
                rh = dict(headers)
                rh["x-relay-target"] = target + path
                r = requests.request(method, relay, **{**kw, "headers": rh})
                if r.status_code >= 500 or "FUNCTION_INVOCATION_FAILED" in r.text[:100]:
                    raise RuntimeError("relay failed")
                return r
            except Exception as _e:
                print(f"[swallow th-tui.py:2417] {_e}")
                pass  # fall through to direct
        return requests.request(method, target + path, **kw)

    def _is_json_resp(r):
        ct = r.headers.get("content-type", "")
        return "json" in ct.lower()

    def _err_from(r):
        try:
            j = r.json()
            return j.get("error", {}).get("message", r.text[:60]) if isinstance(j, dict) else r.text[:60]
        except Exception as _e:
            print(f"[swallow th-tui.py:2430] {_e}")
            return r.text[:60]
    # 1. models list
    try:
        r = _req("GET", "/v1/models")
        if r.status_code != 200:
            return False, f"models={r.status_code}:{_err_from(r)[:60]}"
        if not _is_json_resp(r):
            return False, f"models=not_json:{r.text[:20]}"
    except Exception as e:
        return False, f"models_err={str(e)[:30]}"
    # 2. actual :free chat completion
    try:
        r = _req("POST", "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5})
        if r.status_code == 200 and _is_json_resp(r):
            try:
                choices = r.json().get("choices", [])
                if choices:
                    return True, "ok"
                return False, f"completions=no_choices:{r.text[:60]}"
            except Exception as _e:
                print(f"[swallow th-tui.py:2450] {_e}")
                return False, f"completions=bad_json:{r.text[:60]}"
        return False, f"completions={r.status_code}:{_err_from(r)[:80]}"
    except Exception as e:
        return False, f"completions_err={str(e)[:30]}"


def _get_relay_proxy():
    """Return a random relay proxy URL from proxy pool, else None."""
    try:
        pm = _load_proxy_mod()
        if not pm:
            return None
        relays = [p[1] for p in pm.load_proxies()
                  if p[0] == "relay" and len(p) > 1 and isinstance(p[1], str) and p[1].startswith("http")]
        if relays:
            return random.choice(relays)
    except Exception as _e:
        print(f"[swallow th-tui.py:2434] {_e}")
        pass
    return None


def _build_requests_proxy(parsed):
    """Build requests proxy dict from a parsed proxy tuple."""
    proto, host, port, user, pw = parsed
    if proto == "socks5":
        base = f"socks5h://{host}:{port}"
        if user:
            base = f"socks5h://{user}:{pw}@{host}:{port}"
    else:
        base = f"{proto}://{host}:{port}"
        if user:
            base = f"{proto}://{user}:{pw}@{host}:{port}"
    return {"http": base, "https": base}


def fetch_provider_nodes(cfg):
    """Get compatible provider nodes from 9router for manual selection."""
    router = get_active_router(cfg)
    base = router.get("base_url", "")
    auth_mode = router.get("auth", "jwt_local")
    try:
        import requests as rq2
        s = rq2.Session()
        if auth_mode == "password":
            pw = router.get("password", "")
            r = s.post(base.rstrip("/") + "/api/auth/login", json={"password": pw}, timeout=15)
            if r.status_code != 200:
                return []
        nodes = s.get(base.rstrip("/") + "/api/provider-nodes", timeout=15).json().get("nodes", [])
        return nodes
    except Exception as e:
        elog(f"fetch provider nodes: {e}")
        return []


def imp_router(api_key, cfg=None, prov_type="openai", node_id="", force=False, prefix="Harbor"):
    """Import one key to 9router. force=True passes --force so keys already in the
    local imported.txt cache get re-imported anyway (DB-authoritative dedup still
    skips keys actually present in the live 9router DB).
    prefix: custom name prefix for imported connections (anti-conflict in import script)."""
    if not cfg:
        cfg = load_cfg()
    router = get_active_router(cfg)
    base = router.get("base_url", "http://localhost:20128")
    auth_mode = router.get("auth", "jwt_local")
    log("Importing to " + base + " (" + auth_mode + ") type=" + prov_type + (" [force]" if force else "") + " prefix=" + prefix, "arr")
    extra = ["--type", prov_type, "--allow-unverified", "--prefix", prefix]
    if force:
        extra.append("--force")
    if node_id:
        extra += ["--provider", node_id]
    if auth_mode == "password":
        pw = router.get("password", "")
        if pw:
            extra += ["--router-password", pw]
        else:
            log("No password for remote router!", "warn")
    try:
        r = subprocess.run([sys.executable, str(BASE / "import" / "import_tokenharbor.py"),
                            "--router-base", base, "--file", str(KEYS_FILE)] + extra,
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        elog(f"import subprocess: {e}", traceback.format_exc()[:200])
        return False
    msg = r.stdout[-800:] if r.stdout else ""
    err = r.stderr[-500:] if r.stderr else ""
    _low = msg.lower()
    if "sudah pernah" in _low or "sudah terhubung" in _low or "already imported" in _low:
        log("Already imported", "warn")
        return True
    if r.returncode == 0 and ("sukses" in _low or "imported" in _low):
        log("Imported!", "ok")
        return True
    if "gagal" in _low or "ditolak" in _low or "error" in _low or r.returncode != 0:
        elog("Import failed:", (msg + err).strip())
    else:
        log("Import result:", "info")
        for line in (msg + err).strip().split("\n")[-12:]:
            print(f"    {DI}{line}{RS}")
    return False


def progress(pct, label=""):
    w = 22
    f = int(w * pct / 100)
    bar = G + ("#" * f) + ("-" * (w - f)) + RS
    return bar + " " + W + str(pct).rjust(3) + "%" + RS + " " + DI + label + RS


# ---- menu actions ----
def open_verify_link(link):
    """Open a verification URL — headless chrome to auto-click, else webbrowser."""
    import webbrowser
    try:
        if os.environ.get("DISPLAY"):
            subprocess.Popen(["chromium-browser", "--no-sandbox", link],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception as _e:
        print(f"[swallow th-tui.py:2536] {_e}")
        pass
    try:
        webbrowser.open(link)
    except Exception as _e:
        print(f"[swallow th-tui.py:2540] {_e}")
        pass


def _tm_msg_body(msg):
    """Combine text+html from a tempmail message into one string."""
    if not msg:
        return ""
    if isinstance(msg, dict):
        return str(msg.get("text", "")) + " " + str(msg.get("html", "") or msg.get("textBody", "") or msg.get("htmlBody", ""))
    return str(msg)


def run_full_flow(c, email=None, password=None, pm=None, provider_hint=None, skip_inbox=False):
    """Full pipeline for one account: create → verify → free model → apikey → store.

    Returns the saved record dict on success, or None. Works with any mail
    source (mailg/cloudmail/tempmail) — verification reads the configured
    inbox. When `email` is None it auto-picks one (see pick_address).
    """
    pm = pm or _load_proxy_mod()
    used_mail = get_active_mail(c)
    # AUTO-PICK an email if none provided (creates fresh for create-new mode, or reuses unused)
    if not email:
        email = pick_address(c)
        if email:
            log("Picked address: " + email, "info")
    if not email:
        log("No usable email address available (existing inbox pool may be exhausted)", "warn")
        return None
    # 0. CREATE CLOUDMAIL INBOX if in create-new mode and cloudmail — REQUIRED for verification
    #    (skip if farm pre-created all inboxes in Phase 0)
    if used_mail and used_mail.get("type") == "cloudmail" and used_mail.get("create_new_mail") and email and not skip_inbox:
        inbox_ok = False
        for attempt in range(1, 5):
            try:
                inbox_ok = create_cloudmail_inbox(email)
                if inbox_ok:
                    dlog(f"Inbox created for {email}")
                    break
                dlog(f"Inbox creation attempt {attempt}/4 failed for {email}")
            except Exception as e:
                dlog(f"create inbox {email}: {e}")
            time.sleep(3)
        if not inbox_ok:
            log(f"SKIP {email}: no inbox", "no")
            return None
    # 1. CREATE (with proxy rotation: fail 3x → rotate proxy → retry same account)
    log(f"Registering {email}...", "arr")
    r = None
    max_attempts = 30  # up to 10 proxy rotations × 3 tries each — NEVER give up on the account
    proxy_fail_count = 0
    for attempt in range(1, max_attempts + 1):
        c.pop("_last_fail_ratelimit", None)
        r = create_account(c, email=email, password=password)
        if r:
            dlog(f"Account created for {email}")
            break
        if c.get("_email_terminal"):
            c.pop("_email_terminal", None)
            log(f"{email} terminal (already registered) — moving to next email", "warn")
            return None
        if c.get("_last_fail_ratelimit"):
            # rate-limited: rotate IMMEDIATELY — retrying same proxy is suspicious
            failed_ip = c.get("_last_proxy_ip")
            if failed_ip:
                c.setdefault("_used_proxy_ips", []).append(failed_ip)
            log(f"Proxy rate-limited, rotating immediately -> retry {email} with new proxy", "warn")
            proxy_fail_count = 0
            interruptible_sleep(2)
            continue
        proxy_fail_count += 1
        if proxy_fail_count >= 3:
            # rotate to next proxy after 3 failures — SAME account kept
            failed_ip = c.get("_last_proxy_ip")
            if failed_ip:
                c.setdefault("_used_proxy_ips", []).append(failed_ip)
                log(f"Proxy failed 3x ({failed_ip}), rotating -> retry {email} with new proxy", "warn")
            proxy_fail_count = 0
        else:
            dlog(f"Create attempt {attempt}/{max_attempts} failed for {email}, retrying same proxy...")
        interruptible_sleep(2)
    if not r:
        log("Failed to create after all attempts: " + (email or "(no email)"), "no")
        return None
    log("Created: " + email, "ok")
    pw = r.get("password", password or "")
    key = r.get("api_key", "")
    # 2. STORE (provisional) so we don't lose it if later steps fail
    save_key({"email": email, "password": pw, "api_key": key})

    # 3. VERIFY — poll the inbox for the verification link
    verified = False
    if key:
        log("Verifying " + email + "...", "arr")
        if used_mail and used_mail.get("type") == "mailg":
            verified = verify_via_mailg(email, password=pw, api_key=key)
        elif used_mail and used_mail.get("type") == "cloudmail":
            verified = verify_via_cloudmail(email, password=pw, api_key=key)
        else:
            # tempmail path — poll with provider reader
            verified = _verify_tempmail(pm, email, provider_hint)
        if verified:
            log("Verified: " + email, "ok")
        else:
            log("Verification not confirmed: " + email, "warn")
    else:
        log("No API key generated — cannot verify", "warn")

    # 4. FREE MODELS — enable if verified
    free_ok = False
    if key:
        if not verified:
            # still try free-model check directly (it may not need email verify)
            pass
        free_ok = enable_free_models_for(email, pw, key)
        if free_ok:
            log("Free models enabled: " + email, "ok")

    # 5. STORE final (merge/update the record)
    merge_key_record({"email": email, "password": pw, "api_key": key},
                     verified=verified, free_ok=free_ok)
    return {"email": email, "password": pw, "api_key": key, "verified": verified, "free_ok": free_ok}


def reverify_flow(c, email, password):
    """Re-verify a pending account that already exists.
    Resends verification → poll inbox → click link → test API key.
    Returns dict with verified status + api_key if successful."""
    dlog(f"Re-verifying existing account: {email}")
    # 1. VERIFY VIA CLOUDMAIL/MailG (the provider verifier resends once if needed)
    ms = get_active_mail(c)
    verified = False
    if ms and ms.get("type") == "cloudmail":
        verified = verify_via_cloudmail(email, password=password, retries=10, delay=15)
    elif ms and ms.get("type") == "mailg":
        verified = verify_via_mailg(email, password=password, retries=10, delay=15)
    else:
        log("Verification mode not configured", "warn")
    
    if not verified:
        log(f"Re-verification failed for {email}", "no")
        return None

    # 3. FETCH + TEST API KEY (login, get key, verify it works)
    try:
        import requests
        s = requests.Session()
        r = s.post("https://tokenharbor.ai/api/auth/login", json={"email": email, "password": password}, timeout=30)
        if r.status_code != 200:
            log("Login failed during re-verify", "warn")
            return None
        r2 = s.get("https://tokenharbor.ai/api/v1/account/api-keys", timeout=10)
        key = ""
        if r2.status_code == 200:
            try:
                keys = r2.json().get("data", []) or r2.json().get("keys", [])
                if keys:
                    key = keys[0].get("key", "") or keys[0].get("accessToken", "")
                    log(f"API key fetched: {key[:40]}...", "ok")
            except Exception as _e:
                print(f"[swallow th-tui.py:2700] {_e}")
                pass
        if key:
            works, why = _test_key(key)
            merged = {"email": email, "password": password, "api_key": key, "verified": True, "free_ok": works}
            merge_key_record(merged, verified=True, free_ok=works)
            log("Verified + KEY WORKS" if works else f"Verified but key fail: {why}", "ok" if works else "warn")
            return merged
        log("No API key returned", "warn")
        return None
    except Exception as e:
        elog(f"Key fetch during re-verify: {e}")
        return None


def _verify_tempmail(pm, email, provider_hint=None):
    """Poll tempmail inbox for verification link and open it."""
    if not pm:
        return False
    log("Waiting for verification mail...", "arr")
    # find the right reader for the provider
    msg = None
    if "@" in email and provider_hint == "1secmail":
        msg = pm.read_1secmail(email, wait=90)
    else:
        msg = pm.read_public_tempmail(email, wait=90)
    link = pm.verify_link_from_mail(_tm_msg_body(msg))
    if link:
        log("Verification link: " + link[:80] + "...", "info")
        open_verify_link(link)
        log("Opened in browser. Verify and press Enter.", "ok")
        raw_input("  " + DI + "Press Enter when verified" + RS)
        return True
    log("No verification link found in mail", "warn")
    return False


def _efm_python():
    """Find a python interpreter that has camoufox (needed by enable_free_models.py).
    Prefer the current interpreter if it has it, else scan common paths."""
    import shutil as _sh
    try:
        import camoufox  # noqa
        return sys.executable
    except Exception as _e:
        print(f"[swallow th-tui.py:2744] {_e}")
        pass
    for py in ["/usr/local/lib/hermes-agent/venv/bin/python3", "/usr/bin/python3", "python3"]:
        try:
            p = _sh.which(py) or py
            import subprocess as _sp
            r = _sp.run([p, "-c", "import camoufox"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return p
        except Exception as _e:
            print(f"[swallow th-tui.py:2753] {_e}")
            continue
    return sys.executable


def enable_free_models_for(email, password, api_key):
    """Enable free models for one account via the free-model script."""
    _EFM = BASE / "tools" / "enable_free_models.py"
    _MOD = str(BASE / "tools")
    _GROK = str(BASE / "config")
    # 1. quick API check — already free?
    try:
        import sys as _sys
        _sys.path.insert(0, _MOD)
        _sys.path.insert(0, _GROK)
        import enable_free_models as efm
        ok, _det = efm.free_model_ok(api_key)
        if ok:
            return True
    except Exception as _e:
        print(f"[swallow th-tui.py:2772] {_e}")
        pass
    # 2. run the script for this one account (handles Camoufox login + consent)
    try:
        import os as _os
        _py = _efm_python()
        _env = dict(_os.environ)
        _env["PYTHONPATH"] = _GROK + os.pathsep + _MOD + os.pathsep + _env.get("PYTHONPATH", "")
        r = subprocess.run(
            [_py, str(_EFM), "--email", email, "--file", str(KEYS_FILE)],
            capture_output=True, text=True, timeout=240, env=_env)
        out = (r.stdout or "") + (r.stderr or "")
        # success if the account's free model now reports OK
        try:
            import sys as _sys
            _sys.path.insert(0, _MOD)
            _sys.path.insert(0, _GROK)
            import enable_free_models as efm
            ok2, _ = efm.free_model_ok(api_key)
            return ok2
        except Exception as _e:
            print(f"[swallow th-tui.py:2832] {_e}")
            return "consent" in out or "enabled" in out.lower()
    except Exception as _e:
        print(f"[swallow th-tui.py:2834] {_e}")
        return False


def merge_key_record(rec, verified=False, free_ok=False):
    """Update/append one KEYS_FILE record and collapse duplicate rows by email.
    Never regresses: an existing ok/ok+free stays unless the new state is better."""
    email = rec["email"]
    verified = bool(verified or rec.get("verified"))
    free_ok = bool(free_ok or rec.get("free_ok"))
    new_status = "ok" if (rec.get("api_key") and verified) else ("verified" if verified else "pending")
    if free_ok:
        new_status = "ok+free"
    # rank existing statuses so reverify can't downgrade ok -> pending
    _rank = {"pending": 0, "unused": 0, "verified": 1, "ok": 2, "ok+free": 3}
    old_status = None
    if KEYS_FILE.exists():
        for l in KEYS_FILE.read_text().splitlines():
            parts = l.split("|")
            if len(parts) >= 4 and parts[0].lower() == email.lower():
                old_status = parts[3]
                break
    status = new_status
    if old_status and _rank.get(old_status, 0) > _rank.get(new_status, 0) and rec.get("api_key"):
        status = old_status  # keep the better existing state
    new_line = f"{email}|{rec['password']}|{rec.get('api_key','')}|{status}"
    lines = []
    if KEYS_FILE.exists():
        lines = [l.rstrip("\n") for l in KEYS_FILE.read_text().splitlines() if l.strip()]
    kept = []
    inserted = False
    for l in lines:
        parts = l.split("|")
        if parts and parts[0].lower() == email.lower():
            if not inserted:
                kept.append(new_line)
                inserted = True
            # skip any additional duplicate rows for this email
        else:
            kept.append(l)
    if not inserted:
        kept.append(new_line)
    try:
        KEYS_FILE.write_text("\n".join(kept) + "\n")
    except Exception as e:
        elog("merge key record: " + str(e))
        return False
    mark_used(email)
    return True


def menu_create():
    c = load_cfg()
    ms = get_active_mail(c)
    # Public tempmail path — no mail server configured but tempmail option on
    if (not ms) and c.get("proxy", {}).get("use_public_tempmail"):
        pm = _load_proxy_mod()
        if not pm:
            log("th-proxy.py missing", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
            return
        addr = pm.get_tempmail_from_pool() or pm.get_public_tempmail()
        if not addr:
            log("Public tempmail creation failed", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
            return
        log("Public tempmail: " + addr, "ok")
        run_full_flow(c, addr, pm=pm)
        raw_input("  " + DI + "Press Enter" + RS)
        return
    if not ms:
        log("No mail server configured! Go to Settings first.", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    t = ms.get("type", "")
    mname = ms.get("name", "?")
    print(f"  Mail: {W}{mname}{RS}\n")

    if t == "mailg":
        accs = get_mailg_accounts()
        used = load_used()
        items = []
        for a in accs:
            st = "USED" if a.lower() in used else "FRESH"
            sub = f"{G}Fresh{RS}" if st == "FRESH" else f"{R}Used{RS}"
            items.append((a, a.split("@")[0], sub))
        if not items:
            log("No mailg accounts found in DB!", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
            return
        sel = pick_multi("Select Gmail Accounts", items, searchable=True)
        if not sel:
            log("No accounts selected!", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
            return
        for email in sel:
            run_full_flow(c, email)
    elif t == "cloudmail":
        choice = pick_one("Cloud Mail - Select Option", [
            ("existing", "Use existing inbox", "From credentials file"),
            ("new", "Create new inbox", "Pick domain & generate"),
        ])
        if not choice:
            raw_input("  " + DI + "Press Enter" + RS)
            return
        if choice[0] == "existing":
            addrs = get_cloudmail_addresses()
            items = [(a, a.split("@")[0], a.split("@")[1] if "@" in a else "") for a in addrs]
            if not items:
                log("No cloudmail addresses found!", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
                return
            sel = pick_multi("Select Cloudmail Inbox", items, searchable=True)
            if not sel:
                raw_input("  " + DI + "Press Enter" + RS)
                return
            for email in sel:
                # inbox already exists (picked from list) — skip inbox creation
                run_full_flow(c, email, skip_inbox=True)
        elif choice[0] == "new":
            domains = get_cloudmail_domains()
            items = [(d, d, "") for d in domains]
            dom = pick_one("Select Domain", items)
            if not dom:
                raw_input("  " + DI + "Press Enter" + RS)
                return
            prefix = raw_input("  Email prefix (empty=random): ").strip()
            if not prefix:
                prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            email = prefix + "@" + dom[0]
            if create_cloudmail_inbox(email):
                log("Inbox created: " + email, "ok")
                run_full_flow(c, email)
            else:
                log("Failed to create cloudmail inbox: " + email, "no")
    raw_input("  " + DI + "Press Enter to continue..." + RS)


def menu_batch():
    c = load_cfg()
    n = c.get("batch_count", 3)
    ms = get_active_mail(c)
    using_tempmail = (not ms) and c.get("proxy", {}).get("use_public_tempmail")
    if not ms and not using_tempmail:
        log("No mail server configured!", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    t = ms.get("type", "") if ms else "tempmail"
    pm = _load_proxy_mod()
    ok = []
    delay = c.get("batch_delay", 30)
    # warn if batch count exceeds available gmails
    if t == "mailg":
        all_mg = get_mailg_accounts()
        used = load_used()
        avail = [a for a in all_mg if a.lower() not in used]
        if n > len(avail):
            log(f"Warning: batch {n} but only {len(avail)} unregistered gmails available ({len(all_mg)} total, {len(used)} used)", "warn")
            if len(avail) == 0:
                log("No unregistered gmails left!", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
                return
            log(f"Capping batch to {len(avail)}", "info")
            n = len(avail)
    global _BATCH_INTERRUPT
    _BATCH_INTERRUPT = False
    old_handler = signal.signal(signal.SIGINT, _batch_sigint_handler)
    try:
        for i in range(n):
            if _BATCH_INTERRUPT:
                raise KeyboardInterrupt
            pct = (i + 1) * 100 // n
            # separate line for progress so it doesn't overwrite account logs
            print(f"  {progress(pct, f'({i+1}/{n})')}")
            # resolve an address for this round (auto-pick inside run_full_flow)
            email = None
            if using_tempmail and pm:
                email = pm.get_tempmail_from_pool() or pm.get_public_tempmail()
                if not email:
                    log("Tempmail pool exhausted", "warn")
                    break
            elif t == "mailg":
                # check if pool still has available gmails
                avail_check = [a for a in get_mailg_accounts() if a.lower() not in load_used()]
                if not avail_check:
                    log("All gmails registered — pool exhausted", "warn")
                    break
            r = run_full_flow(c, email) if email else run_full_flow(c)
            if _BATCH_INTERRUPT:
                raise KeyboardInterrupt
            if r:
                ok.append(r)
                print(f"  ✓ {r.get('email','') if isinstance(r, dict) else (email or '')} OK")
            else:
                print(f"  ✗ {(email or '')} FAIL")
            # ratelimit delay between accounts (from tokenharbour-farm: 60/30/15s)
            if i < n - 1 and delay > 0:
                log(f"Waiting {delay}s to avoid rate-limit...", "info")
                interruptible_sleep(delay)
    except KeyboardInterrupt:
        print(f"\nBatch interrupted: {len(ok)}/{n} OK")
    else:
        if ok and (t in ("mailg", "cloudmail")):
            # verification already done inside run_full_flow
            if raw_input("  Import to 9router? (y/N): ").strip().lower() == "y":
                pt = raw_input("  Provider type (openai/anthropic) [openai]: ").strip() or "openai"
                for r in ok:
                    if r.get("api_key"):
                        imp_router(r["api_key"], c, pt)
        elif ok and using_tempmail:
            if raw_input("  Import to 9router? (y/N): ").strip().lower() == "y":
                pt = raw_input("  Provider type (openai/anthropic) [openai]: ").strip() or "openai"
                for r in ok:
                    if r.get("api_key"):
                        imp_router(r["api_key"], c, pt)
    raw_input("  " + DI + "Press Enter to continue..." + RS)


def menu_reverify():
    """Re-verify accounts marked UNVER (403)."""
    keys = load_keys()
    if not keys:
        log("No tokens to reverify!", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    checks = load_key_checks()
    unver = [k for k in keys if checks.get(k["email"], {}).get("state") == "forbidden"]
    if not unver:
        log("No unverified (403) accounts found", "info")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    log(f"Found {len(unver)} unverified accounts", "info")
    ok = 0
    for rec in unver:
        email = rec["email"]
        pw = rec.get("password", "")
        api_key = rec.get("api_key", "")
        log(f"Re-verifying {email}...", "arr")
        try:
            ms = get_active_mail(load_cfg())
            mt = ms.get("type", "") if ms else ""
            # use stored proxy from registration if available
            stored_proxy = rec.get("proxy", "")
            if stored_proxy:
                log(f"Using stored proxy: {stored_proxy}", "info")
            if mt == "mailg":
                r = verify_via_mailg(email, pw, api_key, retries=8, delay=12)
            elif mt == "cloudmail":
                r = verify_via_cloudmail(email, pw, api_key, retries=8, delay=12)
            else:
                log(f"Unknown mail type: {mt}", "warn")
                continue
            if r:
                ok += 1
                log(f"Verified: {email}", "ok")
            else:
                # re-check key status — might be verified via different path
                try:
                    works, why = _test_key(api_key)
                    if works:
                        ok += 1
                        log(f"Verified (re-check): {email} ({why})", "ok")
                    else:
                        log(f"Still unverified: {email} ({why})", "warn")
                except Exception:
                    log(f"Still unverified: {email}", "warn")
        except KeyboardInterrupt:
            log(f"Reverify interrupted: {ok}/{len(unver)} verified", "warn")
            break
        except Exception as e:
            log(f"Error: {str(e)[:60]}", "warn")
        interruptible_sleep(5)
    log(f"Done: {ok}/{len(unver)} verified", "arr")
    raw_input("  " + DI + "Press Enter" + RS)



def menu_tokens():
    keys = load_keys()
    if not keys:
        log("No tokens yet!", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return

    checks = load_key_checks()
    filter_mode = "all"
    scroll = 0
    filter_labels = {
        "all": "All",
        "verified": "Verified accounts",
        "nonverified": "Unverified accounts",
        "unused": "Unused emails",
        "live": "Live keys",
        "ratelimited": "429 rate-limited",
        "planlimited": "402 plan-limited",
        "invalid": "401 invalid key",
        "forbidden": "403 forbidden",
        "failed": "Failed/network",
        "unchecked": "Unchecked keys",
        "no_key": "No API key",
    }

    while True:
        # With an overflow marker, token chrome consumes 11 rows. Keep >=1 data row.
        require_terminal(MIN_TERM_COLS, 12, "Tokens")
        cls()
        w = box_w()
        source_total = len(keys)
        view = [rec for rec in keys if _token_filter_match(rec, filter_mode, checks)]
        total = len(view)

        term_rows = term_height()
        win_sz = max(1, term_rows - 11)
        if scroll > max(0, total - win_sz):
            scroll = max(0, total - win_sz)

        title = "TOKENS (" + str(source_total) + ")"
        if filter_mode != "all":
            title = f"TOKENS [{total}/{source_total} · {filter_labels[filter_mode]}]"
        print(box_top(w))
        print(box_title(w, title))
        print(box_mid(w))

        if not view:
            print(box_row(w, f"{DI}(no records in this filter){RS}"))
        else:
            end = min(scroll + win_sz, total)
            for i, rec in enumerate(view[scroll:end], scroll + 1):
                # Two explicit dimensions: ACCOUNT state | KEY health.
                # Never render a misleading combination such as "OK AUTH" again.
                ast = _account_state(rec)
                account_plain, account_color = {
                    "verified": ("VER", G),
                    "unverified": ("UNVER", Y),
                    "unused": ("UNUSED", DI),
                }.get(ast, ("?", DI))
                account = f"{account_color}{account_plain:<6}{RS}"

                hs = _cached_key_state(rec, checks)
                health_plain, health_color = {
                    "live": ("LIVE", G),
                    "ratelimited": ("429", Y),
                    "planlimited": ("402", C),
                    "invalid": ("401", R),
                    "forbidden": ("403", M),
                    "failed": ("FAIL", R),
                    "unchecked": ("?", DI),
                    "no_key": ("NOKEY", DI),
                }.get(hs, ("?", DI))
                health = f"{health_color}{health_plain:<5}{RS}"

                e = rec["email"][:24]
                k_ = rec.get("api_key", "") or ""
                # show a hint of the key in list
                if k_:
                    kdisp = f"{DI}{k_[:10]}...{k_[-6:]}{RS}"
                else:
                    kdisp = f"{DI}(no key){RS}"
                print(box_row(w, f"{B}{i:02}.{RS} {account}{DI}|{RS} {health}{DI}|{RS} {W}{e}{RS} {kdisp}"))

        if total > win_sz:
            print(box_row(w, f"  {DI}▼ Page {scroll+1}:{win_sz}|{total}{RS} ▼"))
        counts = _token_filter_counts(keys, checks)
        checkable_n = source_total - counts["no_key"]
        print(box_mid(w))
        print(box_row(w, f"{Y}F.{RS} {W}Filter keys{RS}      {DI}{filter_labels[filter_mode]} · {total}/{source_total}{RS}"))
        print(box_row(w, f"{Y}K.{RS} {W}Check API keys{RS}   {DI}{checkable_n}/{source_total} checkable{RS}"))
        print(box_row(w, f"{Y}B.{RS} {W}Back{RS}"))
        print(box_bot(w))
        print_hint(f"{DI}↑↓ · PgUp/PgDn · F=Filter · K=Check All · C=Check One · V=View Key · D=Delete · B=Back{RS}")

        k = get_key()
        if k in ('b', 'B', 'escape', 'ctrl-c'):
            break
        elif k == 'u' or k == 'U':
            pcfg = c.get("proxy", {})
            pcfg["use_public_tempmail"] = not pcfg.get("use_public_tempmail", False)
            c["proxy"] = pcfg
            save_cfg(c)
            log("Public tempmail " + ("ON" if pcfg["use_public_tempmail"] else "OFF"), "ok")
        elif k == 'up':
            scroll = max(0, scroll - 1)
        elif k == 'down':
            scroll = min(max(0, total - win_sz), scroll + 1)
        elif k == 'pgup':
            scroll = max(0, scroll - max(3, win_sz // 2))
        elif k == 'pgdn':
            scroll = min(max(0, total - win_sz), scroll + max(3, win_sz // 2))
        elif k == 'home':
            scroll = 0
        elif k == 'end':
            scroll = max(0, total - win_sz)
        elif k in ('f', 'F'):
            counts = _token_filter_counts(keys, checks)
            choices = [
                ("all", "All records", str(source_total)),
                ("verified", "Account: Verified", str(counts["verified"])),
                ("nonverified", "Account: Unverified", str(counts["nonverified"])),
                ("unused", "Account: Unused email", str(counts["unused"])),
                ("live", "Key: Live", str(counts["live"])),
                ("ratelimited", "Key: 429 rate-limited", str(counts["ratelimited"])),
                ("planlimited", "Key: 402 plan-limited", str(counts["planlimited"])),
                ("invalid", "Key: 401 invalid", str(counts["invalid"])),
                ("forbidden", "Key: 403 forbidden", str(counts["forbidden"])),
                ("failed", "Key: Failed/network", str(counts["failed"])),
                ("unchecked", "Key: Unchecked", str(counts["unchecked"])),
                ("no_key", "Key: No API key", str(counts["no_key"])),
            ]
            chosen = pick_one("Filter tokens", choices)
            if chosen:
                filter_mode = chosen[0]
                scroll = 0
        elif k in ('k', 'K'):
            checkable = [x for x in keys if x.get("api_key")]
            if not checkable:
                log("No API keys to check (only pending/unused records)", "warn")
                raw_input("  " + DI + "Press Enter to continue..." + RS)
                continue

            log("Checking " + str(len(checkable)) + " API keys (parallel)...", "arr")
            import concurrent.futures as _cf
            results = {}
            with _cf.ThreadPoolExecutor(max_workers=min(8, len(checkable))) as pool:
                fut_map = {pool.submit(_test_key, rec["api_key"]): rec for rec in checkable}
                for fut in _cf.as_completed(fut_map):
                    rec = fut_map[fut]
                    try:
                        works, why = fut.result()
                    except Exception as e:
                        print(f"[swallow th-tui.py:3262] {_e}")
                        works, why = False, str(e)[:80]
                    results[rec["email"]] = (works, why)
                    checks[rec["email"]] = {
                        "fingerprint": _key_fingerprint(rec["api_key"]),
                        "state": _classify_key_check(works, why),
                        "reason": str(why)[:160],
                        "checked_at": int(time.time()),
                    }

            save_key_checks(checks)
            state_counts = {m: 0 for m in ("live", "ratelimited", "planlimited", "invalid", "forbidden", "failed")}
            for rec in checkable:
                ent = checks.get(rec["email"], {})
                state = ent.get("state", "failed")
                if state in state_counts:
                    state_counts[state] += 1
            log(
                f"Check complete: {state_counts['live']} live | "
                f"{state_counts['ratelimited']} 429 | {state_counts['planlimited']} 402 | {state_counts['invalid']} 401 | "
                f"{state_counts['forbidden']} 403 | {state_counts['failed']} failed",
                "ok" if state_counts["live"] else "warn"
            )
            raw_input("  " + DI + "Press Enter to continue..." + RS)
        elif k in ('d', 'D'):
            # Delete a specific key/account
            if not view:
                log("No records to delete", "warn")
                continue
            choices = [(rec["email"], rec["email"], rec.get("api_key", "")[:20]) for rec in view]
            chosen = pick_multi("Select account(s) to delete (Space to multi)", choices, searchable=True)
            if chosen:
                emails = list(chosen)
                # sanity: never proceed if we somehow selected 0 or ALL records (guard against wipe)
                if not emails:
                    log("No account selected — nothing deleted", "warn")
                    continue
                if raw_input(f"  Delete {len(emails)} account(s)? (y/N): ").strip().lower() == "y":
                    # backup first
                    import shutil, time as _t
                    _bk = BASE / "_backup_files" / f"keys.txt.pre-delete-{int(_t.time())}"
                    _bk.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(KEYS_FILE, _bk)
                    lines = KEYS_FILE.read_text().splitlines()
                    # exact-match on the email field only (column 0), never substring the whole line
                    emset = set(emails)
                    kept = []
                    removed = 0
                    for l in lines:
                        field = l.split("|")[0].strip() if l else ""
                        if field in emset:
                            removed += 1
                        else:
                            kept.append(l)
                    KEYS_FILE.write_text("\n".join(kept) + ("\n" if kept else ""))
                    for email in emails:
                        if email in checks:
                            del checks[email]
                    save_key_checks(checks)
                    keys = load_keys()
                    log(f"Deleted {removed} account(s) (backup: {_bk.name})", "ok")
                    scroll = 0
        elif k in ('v', 'V'):
            # View FULL key for an account (copyable)
            if not view:
                log("No records", "warn")
                continue
            choices = [(rec["email"], rec["email"], rec.get("api_key", "")[:20]) for rec in view]
            chosen = pick_multi("Select account(s) to view keys (Space to multi)", choices)
            if chosen:
                for email in list(chosen):
                    rec = next((x for x in keys if x.get("email") == email), None)
                    if rec and rec.get("api_key"):
                        print("\n  " + W + BD + "API KEY (" + email + "):" + RS)
                        print("  " + Y + rec["api_key"] + RS)
                raw_input("  " + DI + "Press Enter to continue..." + RS)
        elif k in ('c', 'C'):
            # Check a specific account's key
            if not view:
                log("No records to check", "warn")
                continue
            choices = [(rec["email"], rec["email"], rec.get("api_key", "")[:20]) for rec in view]
            chosen = pick_one("Select account to check", choices)
            if chosen:
                email = chosen[0]
                rec = next((x for x in keys if x.get("email") == email), None)
                if rec and rec.get("api_key"):
                    log(f"Checking {email}...", "arr")
                    works, why = _test_key(rec["api_key"])
                    checks[email] = {
                        "fingerprint": _key_fingerprint(rec["api_key"]),
                        "state": _classify_key_check(works, why),
                        "reason": str(why)[:160],
                        "checked_at": int(time.time()),
                    }
                    save_key_checks(checks)
                    log(f"{email}: {'LIVE' if works else 'FAIL'} ({why})", "ok" if works else "warn")
                else:
                    log(f"No API key for {email}", "warn")
                raw_input("  " + DI + "Press Enter to continue..." + RS)



def menu_import():
    c = load_cfg()
    router = get_active_router(c)
    rname = router.get("name", c.get("router_mode", "local"))
    ptype = pick_one("Select Provider Type", [
        ("openai", "OpenAI Compatible", "gpt-4o, deepseek, etc."),
        ("anthropic", "Anthropic Compatible", "claude-3-haiku, etc."),
    ])
    if not ptype:
        raw_input("  " + DI + "Press Enter" + RS)
        return
    keys = load_keys()
    valid = [k for k in keys if k["api_key"]]
    if not valid:
        log("No keys to import!", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    # --force toggle: re-import even keys flagged by the local imported.txt cache.
    # NOT the default — the SSH-DB dedup still skips keys actually in 9router.
    force = False
    ans = raw_input("  Force re-import (bypass local cache)? [y/N]: ").strip().lower()
    if ans in ("y", "yes"):
        force = True
        log("Force mode ON — re-importing cache-flagged keys", "info")
    # Auto-detect TokenHarbor node — no manual selection. The importer finds the
    # existing TokenHarbor connection, or auto-creates a 'TokenHarbor' node.
    log("Auto-detecting TokenHarbor provider node...", "info")
    log("Importing " + str(len(valid)) + " keys to " + rname + " (" + ptype[0] + ") prefix='" + c.get("import_prefix", "Harbor") + "'...", "arr")
    # Batch import all at once (single subprocess — no duplicate connections)
    keys_list = [f"{k['email']}|{k['password']}|{k['api_key']}|ok" for k in valid]
    imp_router(None, c, ptype[0], force=force, prefix=c.get("import_prefix", "Harbor"))
    raw_input("  " + DI + "Press Enter to continue..." + RS)


def menu_mail_servers(c):
    """Manage mail servers in config: add, edit, delete, set active, toggle create_new_mail."""
    scroll = 0
    while True:
        # Mail menu has 12 fixed rows around its server list. Scroll the list only.
        require_terminal(MIN_TERM_COLS, 13, "Mail Servers")
        cls()
        servers = c.get("mail_servers", [])
        active_name = c.get("active_mail", "none")
        w = box_w()
        term_rows = term_height()
        win_sz = max(1, term_rows - 12)
        total = len(servers)
        if scroll > max(0, total - win_sz):
            scroll = max(0, total - win_sz)
        print(box_top(w))
        print(box_title(w, "MAIL SERVERS (" + str(total) + ") · F=SEARCH"))
        print(box_mid(w))
        _use_tmp = c.get("proxy", {}).get("use_public_tempmail", False)
        _tmp_row = 1 if _use_tmp else 0
        _total = total + _tmp_row
        if _use_tmp:
            # virtual "Public Tempmail" entry
            print(box_row(w, f"{' ' if _total else ''} {DI}Public Tempmail{RS} {M}T{RS} {DI}free mail.tm{RS}"))
        if servers:
            end = min(scroll + win_sz, total)
            for i, s in enumerate(servers[scroll:end], scroll + 1):
                nm = s.get("name", "?")
                tp = s.get("type", "?")
                mode = "C" if s.get("create_new_mail") else "E"
                mode_color = G if s.get("create_new_mail") else DI
                act = f"{G}●{RS}" if nm == active_name else " "
                extra = ""
                if s.get("create_new_mail"):
                    doms = s.get("domains") or []
                    extra = f"  {DI}dom:{','.join(doms)}{RS}" if doms else ""
                else:
                    ems = s.get("emails") or []
                    extra = f"  {DI}{len(ems)} emails{RS}" if ems else f"  {DI}all avail{RS}"
                label = "Email" if tp == "mailg" else ("Domain" if tp == "cloudmail" else tp)
                print(box_row(w, f"{act} {Y}{i:2}.{RS} {W}{nm}{RS} {mode_color}{mode}{RS} {DI}{label}{RS}{extra}"))
        else:
            print(box_row(w, f"{DI}(no mail servers configured){RS}"))
        print(box_mid(w))
        print(box_row(w, f"{Y}A.{RS} {W}Add new mail server{RS}"))
        print(box_row(w, f"{Y}D.{RS} {W}Delete mail server{RS}"))
        print(box_row(w, f"{Y}T.{RS} {W}Toggle C/P mode{RS}"))
        tmp = "ON" if c.get("proxy",{}).get("use_public_tempmail", False) else "OFF"
        print(box_row(w, f"{Y}U.{RS} {W}Public Tempmail{RS} {DI}{tmp}{RS}  (free emails when no server){RS}"))
        print(box_row(w, f"{Y}B.{RS} {W}Back{RS}"))
        print(box_bot(w))
        print_hint(f"{DI}C=Create E=Pick #=Active A=Add D=Del T=Toggle U=Temp F=Search B=Back{RS}")
        k = get_key()
        if k in ('b', 'B', 'escape', 'ctrl-c'):
            break
        elif k == 'up':
            scroll = max(0, scroll - 1)
        elif k == 'down':
            scroll = min(max(0, total - win_sz), scroll + 1)
        elif k == 'pgup':
            scroll = max(0, scroll - max(3, win_sz // 2))
        elif k == 'pgdn':
            scroll = min(max(0, total - win_sz), scroll + max(3, win_sz // 2))
        elif k == 'f' or k == 'F':
            if not servers:
                log("No mail servers to search", "warn")
                continue
            q = raw_input("  Search mail (server/email/domain/type): ").strip()
            if not q:
                continue

            # One visible result per entity. Do NOT collapse thousands of matching
            # email addresses into a single server row.
            search_items = []
            for si, s_ in enumerate(servers):
                name = s_.get("name", "?")
                stype = s_.get("type", "")
                base = s_.get("base_url", "")
                mode = "Create" if s_.get("create_new_mail") else "Pick"
                search_items.append((("server", si, ""), name,
                                     f"server · {stype} · {mode} · {base}"))

                # Configured/selected addresses: each address is its own search result.
                for email in s_.get("emails") or []:
                    search_items.append((("email", si, email), email,
                                         f"{name} · {stype} · configured email"))

                # Create-mode domains: each domain is independently searchable.
                domains = list(s_.get("domains") or [])
                if s_.get("domain") and s_.get("domain") not in domains:
                    domains.append(s_.get("domain"))
                for domain in domains:
                    search_items.append((("domain", si, domain), domain,
                                         f"{name} · {stype} · domain"))

                # For pick-existing sources, include live available addresses too.
                # Deduplicate configured addresses; this is done only when F is pressed.
                if not s_.get("create_new_mail"):
                    try:
                        configured = set(s_.get("emails") or [])
                        for email in get_server_emails(s_):
                            if email not in configured:
                                search_items.append((("email", si, email), email,
                                                     f"{name} · {stype} · available email"))
                    except Exception as e:
                        dlog(f"mail search addresses for {name}: {e}")

            # Keep the complete source list inside the picker so F can clear the
            # initial query and instantly restore every mail result.
            found = pick_one("Mail search", search_items, searchable=True, initial_query=q)
            if found:
                result_key = found[0]
                try:
                    _kind, si, _value = result_key
                    chosen_server = servers[si].get("name", "?")
                    c["active_mail"] = chosen_server
                    save_cfg(c)
                    if _kind == "email":
                        log(f"Found email: {_value} · server: {chosen_server} (now active)", "ok")
                    elif _kind == "domain":
                        log(f"Found domain: {_value} · server: {chosen_server} (now active)", "ok")
                    else:
                        log("Active mail: " + chosen_server, "ok")
                except Exception as e:
                    elog("mail search selection: " + str(e))
        elif k == 'a' or k == 'A':
            # Add new mail server — interactive
            try:
                name = raw_input("  Server name: ").strip()
            except Exception as _e:
                print(f"[swallow th-tui.py:3528] {_e}")
                name = ""
            if not name:
                log("Server name required", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
                continue
            if any(x.get("name", "").lower() == name.lower() for x in servers):
                log("A mail server with that name already exists", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
                continue
            srv_type = pick_one("Select Mail Type", [
                ("mailg", "MailG / Gmail Inbox", "multi-account via local API"),
                ("cloudmail", "Cloud Mail Worker", "self-hosted cloudmail"),
            ])
            if not srv_type:
                continue
            srv_type = srv_type[0]
            # default endpoint per type
            default_url = "http://127.0.0.1:8790" if srv_type == "mailg" else ("https://cmail.arraffi.my.id" if srv_type == "cloudmail" else "")
            try:
                base_url = raw_input(f"  API endpoint [Enter={default_url}]: ").strip() or default_url
            except Exception as _e:
                print(f"[swallow th-tui.py:3549] {_e}")
                base_url = default_url
            domain = ""
            domains = []
            if srv_type in ("mailg", "cloudmail"):
                # domain selection — offer available domains, multi-select, allow custom
                available = []
                if srv_type == "mailg":
                    available = ["gmail.com"]
                else:
                    available = get_cloudmail_domains()
                if available:
                    dsel = pick_multi("Select Domains (Space to toggle, Enter=confirm)", [(d, d, "") for d in available])
                    if dsel:
                        domains = sorted(dsel)
                        domain = domains[0]
                add_custom = pick_one("Add custom domain?", [("no", "No", ""), ("yes", "Yes", "type it manually")])
                if add_custom and add_custom[0] == "yes":
                    try:
                        cd = raw_input("  Domain (e.g. example.com): ").strip()
                    except Exception as _e:
                        print(f"[swallow th-tui.py:3569] {_e}")
                        cd = ""
                    if cd:
                        if cd not in domains:
                            domains.append(cd)
                        domain = domain or cd
            create_new = pick_one("Mail mode", [
                (False, "Pick from existing", "select an available inbox"),
                (True, "Create new mail", "generate a fresh address"),
            ])
            if not create_new:
                continue
            create_new = create_new[0]
            server = {
                "name": name,
                "type": srv_type,
                "base_url": base_url,
                "domain": domain,
                "domains": domains,
                "create_new_mail": create_new,
            }
            # If Pick existing mode, let user select specific emails
            if not create_new:
                s_emails = get_server_emails(server)
                if s_emails:
                    esel = pick_multi("Select emails to use (Space toggle, Enter=confirm)", [(e, e.split("@")[0], e.split("@")[1] if "@" in e else "") for e in s_emails], searchable=True)
                    if esel:
                        server["emails"] = sorted(esel)
            servers.append(server)
            save_cfg(c)
            # make the new server active if the current active pointer is empty/stale
            if not any(x.get("name") == c.get("active_mail") for x in servers):
                c["active_mail"] = server["name"]
                save_cfg(c)
            log("Added mail server '" + name + "'", "ok")
        elif k == 'd' or k == 'D':
            if not servers:
                log("No mail servers to delete", "warn")
                continue
            to_del = pick_one("Delete which mail server?", [(i, x.get("name", "?"), x.get("type","")) for i, x in enumerate(servers)], searchable=True)
            if to_del:
                di = to_del[0]
                if 0 <= di < len(servers):
                    deleted_name = servers[di].get("name", "?")
                    c["mail_servers"] = [x for i, x in enumerate(servers) if i != di]
                    if c.get("active_mail") == deleted_name:
                        c["active_mail"] = c["mail_servers"][0]["name"] if c["mail_servers"] else ""
                    save_cfg(c)
                    log("Deleted '" + deleted_name + "'", "ok")
        elif k == 't' or k == 'T':
            # Toggle mode for the ACTIVE mail server — choose which mode
            if not servers:
                log("No mail servers to toggle", "warn")
                continue
            active_name = c.get("active_mail", "")
            target = next((s for s in servers if s.get("name") == active_name), servers[0])
            cur = "Create new mail" if target.get("create_new_mail") else "Pick existing"
            new_choice = pick_one(f"Mode for '{target['name']}' (now {cur})?", [
                ("create", "Create new mail", "generate a fresh address"),
                ("pick", "Pick from existing", "select available inboxes"),
            ])
            if not new_choice:
                continue
            s = target
            want_create = (new_choice[0] == "create")
            if want_create:
                s["create_new_mail"] = True
                log("'" + s["name"] + "' -> Create new mail", "ok")
                # configure domains
                available = get_cloudmail_domains() if s.get("type") == "cloudmail" else (["gmail.com"] if s.get("type") == "mailg" else [])
                if available:
                    dsel = pick_multi("Select domains for new mail", [(d, d, "") for d in available], pre=s.get("domains"))
                    if dsel:
                        s["domains"] = sorted(dsel)
                        s["domain"] = s["domains"][0]
                add_custom = pick_one("Add custom domain?", [("no", "No", ""), ("yes", "Yes", "type it")])
                if add_custom and add_custom[0] == "yes":
                    try:
                        cd = raw_input("  Domain: ").strip()
                    except Exception as _e:
                        print(f"[swallow th-tui.py:3648] {_e}")
                        cd = ""
                    if cd and cd not in s.get("domains", []):
                        s.setdefault("domains", []).append(cd)
                        s["domain"] = s["domains"][0]
            else:
                s["create_new_mail"] = False
                log("'" + s["name"] + "' -> Pick existing", "ok")
                # configure emails
                s_emails = get_server_emails(s)
                if s_emails:
                    esel = pick_multi("Select emails to use (Space toggle, Enter=confirm)", [(e, e.split("@")[0], e.split("@")[1] if "@" in e else "") for e in s_emails], pre=s.get("emails"), searchable=True)
                    if esel is None:
                        pass  # Esc: cancel without changing the saved selection
                    elif esel:
                        s["emails"] = sorted(esel)
                    else:
                        s.pop("emails", None)  # committed empty set = use all available
                else:
                    log("No existing emails found for this server — it will use all available on create", "warn")
                    s.pop("emails", None)
            save_cfg(c)
        elif k == 'u' or k == 'U':
            pcfg = c.get("proxy", {})
            pcfg["use_public_tempmail"] = not pcfg.get("use_public_tempmail", False)
            c["proxy"] = pcfg
            save_cfg(c)
            log("Public tempmail " + ("ON" if pcfg["use_public_tempmail"] else "OFF"), "ok")
            raw_input("  " + DI + "Press Enter" + RS)
        else:
            # number key → set active
            try:
                idx = int(k) - 1
                if 0 <= idx < len(servers):
                    c["active_mail"] = servers[idx]["name"]
                    save_cfg(c)
                    log(f"Active mail: {servers[idx]['name']}", "ok")
            except (ValueError, IndexError):
                pass


def menu_options(c):
    while True:
        require_terminal(MIN_TERM_COLS, 13, "Options")
        cls()
        # Only show Email Prefix if active mail server is in "Create new mail" mode
        ms = get_active_mail(c)
        show_prefix = ms and ms.get("create_new_mail")
        w = box_w()
        print(box_top(w))
        print(box_title(w, "OPTIONS"))
        print(box_mid(w))
        items = []
        if show_prefix:
            items.append(("prefix", "Email Prefix", c.get("email_prefix", "") or "(random)"))
        items += [
            ("password", "Account Password", c.get("account_password", "") or "(random)"),
            ("batch", "Batch Count", str(c.get("batch_count", 3))),
            ("delay", "Batch Delay (rate-limit)", str(c.get("batch_delay", 30)) + "s"),
            ("timeout", "Playwright Timeout", str(c.get("pw_timeout", 120)) + "s"),
            ("dot_trick", "Gmail Dot-Trick", "ON" if c.get("dot_trick") else "OFF"),
        ]
        for i, (k, l, v) in enumerate(items, 1):
            val = f"{W}{v}{RS}" if v else f"{DI}empty{RS}"
            print(box_row(w, f"{Y}{i}.{RS} {W}{l}{RS} {val}"))
        print(box_row(w, f"{Y}{len(items)+1}.{RS} {W}Back{RS}"))
        print(box_bot(w))
        if not show_prefix:
            print_hint(f"{DI}(Email prefix hidden — active server is in 'Pick existing' mode){RS}")
        print_hint(f"{DI}Number keys to edit{RS}")
        k = get_key()
        idx = None
        try:
            idx = int(k)
        except (ValueError, TypeError):
            idx = None
        if k in ('b', 'B', 'escape', 'ctrl-c') or (idx is not None and idx == len(items) + 1):
            break
        elif idx is not None and show_prefix and idx == 1:
            val = raw_input("  Prefix (empty=random): ").strip()
            c["email_prefix"] = val
            save_cfg(c)
        elif idx is not None:
            # shift to the actual item key
            item_idx = idx - 1
            item_keys = [x[0] for x in items]
            if 0 <= item_idx < len(item_keys):
                ik = item_keys[item_idx]
                if ik == "password":
                    val = raw_input("  Password (empty=random): ").strip()
                    if "|" in val:
                        log("Password cannot contain '|' because keys.txt is pipe-delimited", "warn")
                    else:
                        c["account_password"] = val
                        save_cfg(c)
                elif ik == "batch":
                    try:
                        c["batch_count"] = int(raw_input("  Count: ").strip() or c["batch_count"])
                        save_cfg(c)
                    except ValueError as e:
                        elog("batch count input: " + str(e))
                elif ik == "delay":
                    try:
                        c["batch_delay"] = int(raw_input("  Delay between accounts in batch (seconds, 0=none): ").strip() or c["batch_delay"])
                        save_cfg(c)
                        log("Batch delay set to " + str(c["batch_delay"]) + "s", "ok")
                    except ValueError as e:
                        elog("batch delay input: " + str(e))
                elif ik == "timeout":
                    try:
                        c["pw_timeout"] = int(raw_input("  Timeout: ").strip() or c["pw_timeout"])
                        save_cfg(c)
                    except ValueError as e:
                        elog("timeout input: " + str(e))
                elif ik == "dot_trick":
                    c["dot_trick"] = not c.get("dot_trick")
                    save_cfg(c)
                    log("Gmail dot-trick: " + ("ON" if c["dot_trick"] else "OFF"), "ok")


def _proxy_tuple(p):
    """Canonical full proxy identity; auth/session fields are part of identity."""
    return tuple(p)


def _proxy_display_id(p, max_user=38):
    """Human-readable proxy identity that keeps residential username/session info."""
    proto = str(p[0]) if len(p) > 0 and p[0] is not None else "proxy"
    host = str(p[1]) if len(p) > 1 and p[1] is not None else "?"
    port = str(p[2]) if len(p) > 2 and p[2] is not None else "?"
    user = str(p[3]) if len(p) > 3 and p[3] not in (None, "") else ""
    if user:
        # Residential usernames commonly encode country/session near the tail.
        # Preserve both the prefix and tail instead of hiding the useful session ID.
        if len(user) > max_user:
            left = max(6, max_user // 3)
            right = max(8, max_user - left - 1)
            user = user[:left] + "…" + user[-right:]
        return f"{proto}://{user}@{host}:{port}"
    return f"{proto}://{host}:{port}"


def _proxy_check_progress(phase, done, total, live_n, failed_n):
    """Update one terminal line in-place; avoids printing 1000+ result lines."""
    msg = f"  {phase}: {done}/{total}  live:{live_n}  failed:{failed_n}"
    width = max(10, term_width() - 1)
    # ANSI clear-line + carriage return keeps output O(1) rows while checks run.
    sys.stdout.write("\r\x1b[2K" + msg[:width])
    sys.stdout.flush()


def _proxy_check_progress_end():
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.flush()


def _proxy_protect_key(p):
    """Stable, password-safe protection key for the full proxy tuple."""
    import hashlib
    raw = json.dumps(list(_proxy_tuple(p)), ensure_ascii=False, separators=(",", ":"), default=str)
    return "v2:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _legacy_proxy_protect_key(p):
    """Protection key used by v6 and earlier, for one-time migration."""
    host = p[1] if len(p) > 1 else ""
    port = p[2] if len(p) > 2 else ""
    user = p[3] if len(p) > 3 else ""
    return f"{host}:{port}:{user}"


def _pipe_proxy_protect_key(p):
    """Protection key used by the merged-v11 build: proto|host|port|user|pass."""
    try:
        return "|".join(str(x) for x in p)
    except Exception as _e:
        print(f"[swallow th-tui.py:3823] {_e}")
        return ""


def _normalize_protected_keys(pcfg, proxies):
    """Migrate ALL historical key formats to full-identity v2 hashes.

    Recognized inputs per proxy p:
      - v2 hash (current)
      - legacy host:port:user (v6 and earlier)
      - pipe proto|host|port|user|pass (merged-v11 build)
    Unrecognized/obsolete entries are pruned; recognized ones are migrated so
    switching builds NEVER silently drops protections."""
    old = set(pcfg.get("protected", []))
    new = set()
    for p in proxies:
        v2 = _proxy_protect_key(p)
        if v2 in old or _legacy_proxy_protect_key(p) in old or _pipe_proxy_protect_key(p) in old:
            new.add(v2)
    changed = new != old
    pcfg["protected"] = sorted(new)
    return changed


def menu_proxy(c):
    """Proxy management: list, add, enable/disable, check, scrape, vpngate, tempmail."""
    pm = _load_proxy_mod()
    if not pm:
        log("th-proxy.py missing — cannot open proxy menu", "warn")
        raw_input("  " + DI + "Press Enter" + RS)
        return
    while True:
        # Proxy Settings is intentionally fixed-layout; below this size, show the
        # resize screen rather than clipping actions off the bottom.
        require_terminal(MIN_TERM_COLS, 22, "Proxy Settings")
        cls()
        w = box_w()
        pcfg = c.get("proxy", {})
        status = f"{G}● ON{RS}" if pcfg.get("enabled") else f"{DI}○ OFF{RS}"
        mode = pcfg.get("mode", "list")
        mode_lbl = {"list": "List", "vpngate": "VPNGate", "combo": "Combo (local+list)"}.get(mode, mode)
        order_lbl = {"top": "Top", "random": "Random", "least": "Least Used"}.get(pcfg.get("proxy_order", "top"), "Top")
        tmp = "yes" if pcfg.get("use_public_tempmail") else "no"
        no_del = "ON" if pcfg.get("no_delete") else "OFF"
        prox = pm.load_proxies() if pm else []
        # Migrate old host:port:user protection keys and prune entries whose
        # exact proxy no longer exists. v2 keys include protocol+auth/session identity.
        _before_prot = set(pcfg.get("protected", []))
        if _normalize_protected_keys(pcfg, prox):
            c["proxy"] = pcfg
            save_cfg(c)
            _removed = max(0, len(_before_prot) - len(pcfg.get("protected", [])))
            if _removed:
                log(f"Removed {_removed} stale protected entries", "info")
        # live/dead counts from LAST check (cached in config) — instant, no blocking
        _live_n = pcfg.get("last_live")
        _dead_n = pcfg.get("last_dead")
        _last_check = (f"{G}{_live_n} live{RS} / {C}{_dead_n} failed{RS}"
                       if _live_n is not None and _dead_n is not None else f"{DI}not checked{RS}")
        print(box_top(w))
        print(box_title(w, "PROXY SETTINGS · F=SEARCH"))
        print(box_mid(w))
        print(box_row(w, f"{Y}1.{RS} {W}Status{RS}         {status}"))
        print(box_row(w, f"{Y}2.{RS} {W}Mode{RS}           {mode_lbl}"))
        print(box_row(w, f"{Y}3.{RS} {W}Proxy Order{RS}    {order_lbl}"))
        print(box_row(w, f"{Y}4.{RS} {W}No Delete{RS}      {W}{no_del}{RS} {DI}(keep ALL failed proxies){RS}"))
        print(box_row(w, f"{Y}5.{RS} {W}Proxies{RS}         {len(prox)} total  {DI}prot:{len(pcfg.get('protected',[]))}{RS}"))
        print(box_row(w, f"   {DI}Last check:{RS}     {_last_check}"))
        print(box_row(w, f"{Y}P.{RS} {W}Protect proxies{RS}  {G}{len(pcfg.get('protected', []))}{RS}/{len(prox)} {DI}protected/total{RS}"))
        print(box_mid(w))
        print(box_row(w, f"{Y}A.{RS} {W}Add proxy{RS}      {DI}http(s)/socks5 w/wo auth{RS}"))
        print(box_row(w, f"{Y}F.{RS} {W}Search proxies{RS}"))
        print(box_row(w, f"{Y}D.{RS} {W}Delete proxy{RS}"))
        print(box_row(w, f"{Y}C.{RS} {W}Check live{RS}     {DI}2-pass; asks before purge{RS}"))
        print(box_row(w, f"{Y}S.{RS} {W}Scrape fresh{RS}   {DI}set count, pull from lists{RS}"))
        print(box_row(w, f"{Y}L.{RS} {W}Add local proxy{RS} {DI}proxy-controller :7920/:8118{RS}"))
        print(box_row(w, f"{Y}R.{RS} {W}Run proxy-ctrl{RS}  {DI}start/stop bundled :7920/:8118{RS}"))
        _mcur = pcfg.get("current")
        _mdisp = (_mcur if isinstance(_mcur, str) else (f"{_mcur[1]}:{_mcur[2]}" if _mcur else "none"))
        print(box_row(w, f"{Y}M.{RS} {W}Manual proxy{RS}    {DI}{_mdisp} (override auto-check){RS}"))
        print(box_row(w, f"{Y}B.{RS} {W}Back{RS}"))
        print(box_bot(w))
        print_hint(f"{DI}1-4 toggle · A=Add F=Search D=Del C=Check S=Scrape L=Local R=Run M=Manual B=Back{RS}")
        k = get_key()
        if k in ('b', 'B', 'escape', 'ctrl-c'):
            break
        elif k == '1':
            pcfg["enabled"] = not pcfg.get("enabled", False)
            c["proxy"] = pcfg
            save_cfg(c)
            log("Proxy " + ("ENABLED" if pcfg["enabled"] else "DISABLED"), "ok")
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == '2':
            sel = pick_one("Proxy mode", [
                ("list", "Proxy list", "use proxies from proxy.txt"),
                ("combo", "Combo", "local proxy-controller, else list"),
                ("vpngate", "VPNGate", "free residential IPs via vpngate.net"),
            ])
            if sel:
                pcfg["mode"] = sel[0]
                c["proxy"] = pcfg
                save_cfg(c)
        elif k == '3':
            sel = pick_one("Proxy Order", [
                ("top", "Top", "use first proxies in list"),
                ("random", "Random", "random selection"),
                ("least", "Least Used", "least recently used"),
            ])
            if sel:
                pcfg["proxy_order"] = sel[0]
                c["proxy"] = pcfg
                save_cfg(c)
                log("Proxy order: " + sel[0], "ok")
        elif k == '4':
            pcfg["no_delete"] = not pcfg.get("no_delete", False)
            c["proxy"] = pcfg
            save_cfg(c)
            log("No Delete " + ("ON — failed proxies will always be kept" if pcfg["no_delete"] else "OFF — failed-twice proxies require confirmation before purge"), "ok")
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'f' or k == 'F':
            prox = pm.load_proxies()
            if not prox:
                log("No proxies to search", "warn")
                continue
            q = raw_input("  Search proxy (protocol/host/IP/port/user): ").strip()
            if not q:
                continue
            search_items = []
            for pi, p_ in enumerate(prox):
                url = pm.proxy_url(p_, hide_password=True)
                details = " ".join(str(x) for x in p_ if x is not None)
                # Numeric index gives every result a stable unique key even if URLs duplicate.
                search_items.append((pi, url, details))
            # Keep all proxies available so F clears the initial search without
            # leaving/reopening this picker.
            found = pick_one("Proxy search", search_items, searchable=True, initial_query=q)
            if found:
                log("Found: " + found[1], "ok")
                raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'p' or k == 'P':
            prox = pm.load_proxies()
            if not prox:
                log("No proxies to protect", "warn")
                continue
            items = []
            existing_prot = set(pcfg.get("protected", []))
            for p in prox:
                url = pm.proxy_url(p, hide_password=True)
                key = _proxy_protect_key(p)
                mark = "●" if key in existing_prot else " "
                items.append((key, f"{mark} {url}", url))
            # pre-select the already-protected ones (persists across reopens)
            sel = pick_multi("Protect proxies (never delete)", items, pre=list(existing_prot), searchable=True)
            if sel is not None:
                # pick_multi returns a set of item KEYS.  Each key is already the
                # full host:port:user protection identifier; taking x[0] here would
                # save only the first character and make protection disappear on reopen.
                pcfg["protected"] = sorted(sel)
                c["proxy"] = pcfg
                save_cfg(c)
                log("Protected " + str(len(sel)) + " proxies", "ok")
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'a' or k == 'A':
            try:
                raw = raw_input("  Proxy (e.g. 1.2.3.4:8080, socks5://u:p@host:1080): ").strip()
            except Exception as _e:
                print(f"[swallow th-tui.py:3988] {_e}")
                raw = ""
            if raw:
                p, err = pm.add_proxy(raw)
                if p:
                    log("Added: " + pm.proxy_url(p, hide_password=True), "ok")
                else:
                    log(err or "Invalid", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'd' or k == 'D':
            prox = pm.load_proxies()
            if not prox:
                log("No proxies to delete", "warn")
                continue
            items = [(i, pm.proxy_url(p, hide_password=True), p[0]) for i, p in enumerate(prox)]
            sel = pick_one("Delete proxy", items, searchable=True)
            if sel:
                idx = sel[0]
                if 0 <= idx < len(prox):
                    removed = prox[idx]
                    pm.save_proxies([p for i, p in enumerate(prox) if i != idx])
                    log("Deleted: " + pm.proxy_url(removed, hide_password=True), "ok")
        elif k == 'c' or k == 'C':
            prox = pm.load_proxies()
            if not prox:
                log("No proxies to check", "warn")
                continue

            import concurrent.futures as _cf
            # 40/20 workers made 1k+ lists unnecessarily slow. Network checks are I/O-bound,
            # so use a higher but bounded pool. Can be overridden in config.json if needed.
            try:
                first_workers = max(1, min(128, int(pcfg.get("check_workers", 96)), len(prox)))
            except Exception as _e:
                print(f"[swallow th-tui.py:4021] {_e}")
                first_workers = min(96, len(prox))
            retry_cap = max(1, first_workers // 2)
            try:
                retry_workers_cfg = int(pcfg.get("retry_workers", retry_cap))
            except Exception as _e:
                print(f"[swallow th-tui.py:4026] {_e}")
                retry_workers_cfg = retry_cap

            log(f"Checking {len(prox)} proxies ({first_workers} workers, compact output)...", "arr")
            log("Residential session/user is preserved in failure/slow-proxy output; password stays hidden.", "info")

            def _chk(_p, timeout=15):
                try:
                    _r = pm.check_proxy(_p, timeout=timeout)
                    _h = _p[1] if len(_p) > 1 else "?"
                    _pt = _p[2] if len(_p) > 2 else "?"
                    # populate the check cache so _next_proxy can reuse (override auto-check)
                    if hasattr(pm, "load_check_cache"):
                        import time as _tt
                        _cache = pm.load_check_cache()
                        _key = f"{_p[0]}|{_p[1]}|{_p[2]}|{_p[3] or ''}"
                        _cache[_key] = {
                            "alive": _r is not None,
                            "ip": _r[1] if _r else "",
                            "latency": _r[0] if _r else 0,
                            "region": _r[2] if _r and len(_r) > 2 else "",
                            "ts": _tt.time(),
                        }
                        pm.save_check_cache(_cache)
                    if _r:
                        _rg = _r[2] if len(_r) > 2 else ""
                        return (_p, _r[0], _r[1] or _h, _h, _pt, _rg)
                    return (_p, None, None, _h, _pt, "")
                except Exception as e:
                    _h = _p[1] if len(_p) > 1 else "?"
                    _pt = _p[2] if len(_p) > 2 else "?"
                    return (_p, None, None, _h, _pt, "", str(e)[:80])

            first = []
            first_live = 0
            first_fail = 0
            done = 0
            with _cf.ThreadPoolExecutor(max_workers=first_workers) as _ex:
                _futs = {_ex.submit(_chk, _p, 15): _p for _p in prox}
                for _f in _cf.as_completed(_futs):
                    try:
                        r = _f.result()
                    except Exception as e:
                        print(f"[swallow th-tui.py:4068] {_e}")
                        _p = _futs[_f]
                        _h = _p[1] if len(_p) > 1 else "?"
                        _pt = _p[2] if len(_p) > 2 else "?"
                        r = (_p, None, None, _h, _pt, "", str(e)[:80])
                    first.append(r)
                    done += 1
                    if r[1] is not None:
                        first_live += 1
                    else:
                        first_fail += 1
                    _proxy_check_progress("Pass 1", done, len(prox), first_live, first_fail)
            _proxy_check_progress_end()

            live = [r[:6] for r in first if r[1] is not None]
            failed_once = [r[0] for r in first if r[1] is None]

            # Residential/rotating proxies are often temporarily slow. Never delete based
            # on one timeout; retry failures with a longer timeout.
            recovered = []
            dead_proxies = []
            if failed_once:
                retry_workers = max(1, min(64, retry_workers_cfg, len(failed_once)))
                log(f"Retrying {len(failed_once)} failures ({retry_workers} workers, 25s timeout)...", "info")
                done = rec_n = dead_n = 0
                with _cf.ThreadPoolExecutor(max_workers=retry_workers) as _ex:
                    _futs = {_ex.submit(_chk, _p, 25): _p for _p in failed_once}
                    for _f in _cf.as_completed(_futs):
                        try:
                            r = _f.result()
                        except Exception as e:
                            print(f"[swallow th-tui.py:4098] {_e}")
                            _p = _futs[_f]
                            _h = _p[1] if len(_p) > 1 else "?"
                            _pt = _p[2] if len(_p) > 2 else "?"
                            r = (_p, None, None, _h, _pt, "", str(e)[:80])
                        done += 1
                        if r[1] is not None:
                            recovered.append(r[:6])
                            rec_n += 1
                        else:
                            dead_proxies.append(r[0])
                            dead_n += 1
                        _proxy_check_progress("Retry", done, len(failed_once), rec_n, dead_n)
                _proxy_check_progress_end()
                live.extend(recovered)

            # Do not dump 1000+ lines to the terminal. Show actionable identities only:
            # final failures + slowest live proxies. Full passwords are never printed.
            if dead_proxies:
                log(f"Failed twice ({len(dead_proxies)}):", "warn")
                max_show = 25
                for _p in dead_proxies[:max_show]:
                    log("  ✗ " + _proxy_display_id(_p), "warn")
                if len(dead_proxies) > max_show:
                    log(f"  … and {len(dead_proxies) - max_show} more", "info")

            if live:
                slow = sorted(live, key=lambda r: (r[1] if isinstance(r[1], (int, float)) else -1), reverse=True)[:10]
                if slow and slow[0][1] is not None:
                    log("Slowest live proxies:", "info")
                    for _p, _lat, _ip, _h, _pt, _rg in slow:
                        _rgs = f" [{_rg}]" if _rg else ""
                        log(f"  {_proxy_display_id(_p)} -> {_ip}{_rgs}, {_lat}ms", "info")

            # One bulk detail block in farm.log, useful when distinguishing residential sessions.
            try:
                with open(BASE / "farm.log", "a") as _lf:
                    _lf.write(f"[{time.strftime('%H:%M:%S')}] PROXY CHECK DETAIL ({len(prox)} total)\n")
                    for _p, _lat, _ip, _h, _pt, _rg in sorted(live, key=lambda r: str(r[0])):
                        _lf.write(f"  LIVE {_proxy_display_id(_p, max_user=120)} -> {_ip} {_rg} {_lat}ms\n")
                    for _p in dead_proxies:
                        _lf.write(f"  DEAD {_proxy_display_id(_p, max_user=120)}\n")
            except Exception as e:
                dlog(f"proxy detail log: {e}")

            _prot = set(pcfg.get("protected", []))
            dead = len(dead_proxies)
            removed_n = 0

            if not pcfg.get("no_delete", False) and dead_proxies:
                answer = raw_input(f"  Remove {dead} proxies that failed twice? (y/N): ").strip().lower()
                if answer == "y":
                    dead_ids = {_proxy_tuple(p) for p in dead_proxies}
                    _keep = []
                    for _p in prox:
                        ident = _proxy_tuple(_p)
                        if ident not in dead_ids or _proxy_protect_key(_p) in _prot:
                            _keep.append(_p)
                    removed_n = len(prox) - len(_keep)
                    pm.save_proxies(_keep)
                else:
                    log("Dead candidates kept (no destructive write)", "info")
            elif pcfg.get("no_delete", False) and dead_proxies:
                log("No Delete is ON — failed proxies were kept", "info")

            pcfg["last_live"] = len(live)
            pcfg["last_dead"] = dead
            _after = pm.load_proxies() if pm else []
            _normalize_protected_keys(pcfg, _after)
            c["proxy"] = pcfg
            save_cfg(c)
            log(
                f"LIVE: {len(live)} | FAILED TWICE: {dead} | recovered: {len(recovered)} | "
                f"removed: {removed_n} | protected: {len(pcfg.get('protected', []))}",
                "ok" if live else "warn"
            )
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 's' or k == 'S':
            try:
                amt = raw_input("  How many proxies to scrape? [100]: ").strip()
                amt = int(amt) if amt else 100
                amt = max(1, min(amt, 2000))
            except Exception as _e:
                print(f"[swallow th-tui.py:4180] {_e}")
                amt = 100
            log("Scraping " + str(amt) + " fresh proxies...", "arr")
            fresh = pm.scrape_proxies(amt)
            if fresh:
                existing = pm.load_proxies()

                # IMPORTANT: residential/rotating proxy providers often reuse the same
                # gateway host:port and encode the account/session in username/password.
                # Deduping only (protocol, host, port) destroys those distinct proxies.
                # Treat the full parsed tuple as the proxy identity so only truly exact
                # duplicates are collapsed. Existing entries are kept first.
                existing_ids = {_proxy_tuple(p) for p in existing}
                seen = set()
                out = []
                for p in existing + fresh:
                    key = _proxy_tuple(p)
                    if key not in seen:
                        seen.add(key)
                        out.append(p)

                # Report real additions rather than calling every fetched candidate "new".
                final_ids = {_proxy_tuple(p) for p in out}
                actually_added = len(final_ids - existing_ids)
                exact_dupes = len(fresh) - actually_added

                pm.save_proxies(out)
                log(
                    "Fetched " + str(len(fresh)) +
                    " | added " + str(actually_added) +
                    " | exact duplicates " + str(exact_dupes) +
                    " | total " + str(len(out)),
                    "ok"
                )
            else:
                log("Scrape failed (no sources reachable)", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'l' or k == 'L':
            # Add the local proxy-controller endpoints + switch to combo mode
            added = []
            for proxy_str in ("socks5://proxy:wuzz@04Store@127.0.0.1:7920",
                              "http://proxy:wuzz@04Store@127.0.0.1:8118"):
                p, err = pm.add_proxy(proxy_str)
                if p:
                    added.append(proxy_str.split("@")[-1])
            if added:
                log("Added local proxy-controller: " + ", ".join(added), "ok")
                pcfg["mode"] = "combo"
                pcfg["enabled"] = True
                c["proxy"] = pcfg
                save_cfg(c)
                log("Switched to Combo mode (local+list)", "ok")
            else:
                log("Local proxy already present or add failed", "warn")
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'r' or k == 'R':
            # start/stop the bundled proxy-controller
            start_script = BASE / "proxy-controller" / "start.sh"
            if not start_script.exists():
                log("proxy-controller/start.sh not found", "warn")
                raw_input("  " + DI + "Press Enter" + RS)
                continue
            try:
                import subprocess as _sp
                st = _sp.run(["pgrep", "-f", "lite_manager.py"], capture_output=True, text=True)
                running = st.returncode == 0
                action = "stop" if running else "start"
                r = _sp.run(["bash", str(start_script), action], capture_output=True, text=True, timeout=60)
                log(("Stopped proxy-controller" if action == "stop" else "Started proxy-controller"), "ok")
                print("  " + (r.stdout or "").replace("\n", "\n  "))
            except Exception as e:
                elog("run proxy-ctrl: " + str(e))
            raw_input("  " + DI + "Press Enter" + RS)
        elif k == 'm' or k == 'M':
            # MANUAL OVERRIDE: lock a specific proxy (skips auto liveness check)
            pcfg = c.get("proxy", {})
            cur = pcfg.get("current")
            cur_disp = cur if isinstance(cur, str) else (f"{cur[1]}:{cur[2]}" if cur else "none")
            ans = raw_input(f"  Manual proxy override (current: {cur_disp}, empty=clear, host:port or scheme://user:pass@host:port): ").strip()
            if not ans:
                pcfg["current"] = None
                log("Manual proxy override cleared", "ok")
            else:
                pcfg["current"] = ans
                log("Manual proxy override set: " + ans, "ok")
            c["proxy"] = pcfg
            save_cfg(c)
            raw_input("  " + DI + "Press Enter" + RS)


def menu_9router(c):
    """Full 9router configuration: mode, base URL, auth, name."""
    while True:
        require_terminal(MIN_TERM_COLS, 10, "9router Settings")
        cls()
        m = c.get("router_mode", "local")
        router = get_active_router(c)
        w = box_w()
        print(box_top(w))
        print(box_title(w, "9ROUTER SETTINGS"))
        print(box_mid(w))
        
        # Current settings display
        name = router.get("name", m)
        url = router.get("base_url", "http://localhost:20128")
        auth = router.get("auth", "jwt_local")
        rdb = router.get("remote_db", "")
        prefix = c.get("import_prefix", "Harbor")
        
        print(box_row(w, f"{Y}1.{RS} {W}Mode{RS}           {G}{m}{RS}"))
        print(box_row(w, f"{Y}2.{RS} {W}Base URL{RS}        {url[:40]}"))
        print(box_row(w, f"{Y}3.{RS} {W}Auth mode{RS}       {auth}"))
        print(box_row(w, f"{Y}4.{RS} {W}Name tag{RS}        {DI}{prefix}{RS} (import prefix)"))
        print(box_row(w, f"{Y}5.{RS} {W}Test connection{RS}"))
        print(box_row(w, f"{Y}6.{RS} {W}Remote DB{RS}       {rdb or 'not set'}"))
        print(box_mid(w))
        print(box_row(w, f"{Y}E.{RS} {W}Back{RS}"))
        print(box_bot(w))
        
        k = get_key()
        if k in ('e', 'E', 'escape', 'ctrl-c'):
            break
        elif k == '1':
            items = [("local", "Local", "http://localhost:20128"), ("remote", "Remote", "https://vibecode.omori.my.id")]
            sel = pick_one("Select Router Mode", items)
            if sel:
                c["router_mode"] = sel[0]
                save_cfg(c)
                log("Router mode: " + sel[0], "ok")
        elif k == '2':
            url = raw_input(f"  Base URL [{url}]: ").strip()
            if url:
                c["router"][m]["base_url"] = url
                save_cfg(c)
                log("Base URL updated", "ok")
        elif k == '6':
            rdb = router.get("remote_db", "")
            new_rdb = raw_input(f"  Remote DB (user@host) [{rdb}]: ").strip()
            if new_rdb or new_rdb == "":
                c["router"][m]["remote_db"] = new_rdb
                save_cfg(c)
                log("Remote DB updated", "ok")
        elif k == '3':
            items = [("jwt_local", "JWT Local", "Generate JWT from ~/.9router/jwt-secret"), ("password", "Password", "Login with password (remote)")]
            sel = pick_one("Select Auth Mode", items)
            if sel:
                c["router"][m]["auth"] = sel[0]
                if sel[0] == "password":
                    pw = raw_input("  Password: ").strip()
                    if pw:
                        c["router"][m]["password"] = pw
                save_cfg(c)
                log("Auth mode: " + sel[0], "ok")
        elif k == '4':
            prefix = raw_input(f"  Name tag/prefix [{prefix}]: ").strip()
            if prefix:
                c["import_prefix"] = prefix
                save_cfg(c)
                log("Import prefix: " + prefix, "ok")
        elif k == '5':
            log("Testing connection to " + url + "...", "info")
            try:
                import subprocess
                _extra = ["--dry-run", "--no-db-check", "--file", str(BASE / "data" / "keys.txt")]
                _pw = router.get("password")
                if _pw:
                    _extra += ["--router-password", _pw]
                r = subprocess.run([sys.executable, str(BASE / "import" / "import_tokenharbor.py"), "--router-base", url] + _extra, capture_output=True, text=True, timeout=15)
                if r.returncode == 0:
                    log("Connection OK!", "ok")
                    for line in (r.stdout or "").strip().split("\n")[-5:]:
                        print(f"    {DI}{line}{RS}")
                else:
                    log("Connection failed: " + (r.stderr or r.stdout or "unknown")[:200], "warn")
            except Exception as e:
                log("Error: " + str(e)[:100], "warn")
        elif k in ('r', 'R'):
            pass
        raw_input("  " + DI + "Press Enter" + RS)



def menu_settings():
    c = load_cfg()
    while True:
        require_terminal(MIN_TERM_COLS, 10, "Settings")
        cls()
        ms = get_active_mail(c)
        mname = ms.get("name", "none") if ms else "none"
        rmode = c.get("router_mode", "local")
        router = get_active_router(c)
        rname = router.get("name", rmode)
        prefix = c.get("import_prefix", "Harbor")
        w = box_w()
        print(box_top(w))
        print(box_title(w, "SETTINGS"))
        print(box_mid(w))
        vnc = f"{G}● ON{RS}" if c.get("vnc_mode") else f"{DI}○ OFF{RS}"
        print(box_row(w, f"{Y}A.{RS} {W}Mail Server{RS}   {mname}"))
        print(box_row(w, f"{Y}B.{RS} {W}9router{RS}       {rname} ({rmode})"))
        print(box_row(w, f"{Y}C.{RS} {W}VNC{RS}           {vnc}"))
        print(box_row(w, f"{Y}D.{RS} {W}Options{RS}"))
        print(box_row(w, f"{Y}E.{RS} {W}Back{RS}"))
        print(box_bot(w))
        k = get_key()
        if k in ('e', 'E', 'escape', 'ctrl-c'):
            break
        elif k == 'a' or k == 'A':
            menu_mail_servers(c)
        elif k == 'b' or k == 'B':
            menu_9router(c)
        elif k == 'c' or k == 'C':
            c["vnc_mode"] = not c.get("vnc_mode", False)
            save_cfg(c)
        elif k == 'd' or k == 'D':
            menu_options(c)


def main():
    if not load_env():
        elog("Cannot start: live credentials (.env) missing. See .env.example")
        sys.exit(1)
    load_cfg()
    raw_start()          # hold raw mode for the whole session (no echo, arrows don't leak)
    enter_fullscreen()   # tmux/vim-style full control until exit
    try:
        while True:
            require_terminal(MIN_TERM_COLS, 15, "Main Menu")
            cls()
            w = box_w()
            banner = "TH-TUI  Token Harbor Account Creator · v12"
            if w < 42:
                banner = "TH-TUI"
            elif w < len(banner) + 4:
                banner = "TH-TUI  TH Acc. Creator · v12"
            print(box_top(w))
            print(box_title(w, banner))
            print(box_mid(w))
            print(box_row(w, f"{G}{BD}MAIN MENU{RS}"))
            print(box_mid(w))
            items = [
                ("1", "Create Account", "mailg/cloudmail + auto-verify"),
                ("2", "Batch Create", "create N + auto-verify"),
                ("3", "View Tokens", "list saved API keys"),
                ("4", "Import to 9router", "push keys to router"),
                ("5", "Settings", "mail server, router, proxy, options"),
                ("6", "Proxy", "list/check/scrape/vpngate"),
                ("7", "Reverify", "re-verify unconfirmed accounts"),
                ("E", "Exit", ""),
            ]
            for n, l, s in items:
                line = f"{Y}{n}.{RS} {W}{BD}{l}{RS}"
                if s:
                    line += f" {DI}{s}{RS}"
                print(box_row(w, line))
            print(box_bot(w))
            print_hint(f"{DI}Number keys · Esc/E for quit{RS}")
            k = get_key()
            if k == '1':
                menu_create()
            elif k == '2':
                menu_batch()
            elif k == '3':
                menu_tokens()
            elif k == '4':
                menu_import()
            elif k == '5':
                menu_settings()
            elif k == '6':
                menu_proxy(load_cfg())
            elif k == '7':
                menu_reverify()
            elif k in ('e', 'E', 'escape', 'ctrl-c'):
                cls()
                print(f"\n  {C}╔{'═' * 20}╗{RS}")
                print(f"  {C}║{RS}  {G}{BD}Goodbye!{RS}   {C}║{RS}")
                print(f"  {C}╚{'═' * 20}╝{RS}")
                break
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
        except Exception as _e:
            print(f"[swallow th-tui.py:4421] {_e}")
            pass
        exit_fullscreen()  # restore terminal on exit (even Ctrl+C)
        raw_end()          # restore cooked mode


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_fullscreen()
        print("\n\n  " + G + "Bye!" + RS)
        sys.exit(0)



