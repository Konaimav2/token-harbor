#!/usr/bin/env python3
"""TH-SELECT - Interactive selection UI (like Hermes skill selector)"""

import sys, os, tty, termios

C_RESET = "\033[0m"
C_BRIGHT = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_WHITE = "\033[37m"
C_BG_GREEN = "\033[42m"
C_BG_BLUE = "\033[44m"
C_BG_WHITE = "\033[47m"
C_BLACK = "\033[30m"

def getch():
    """Read single character without echo."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch

def render_list(title, items, selected=None, cursor=0, multi=False):
    """Render interactive list with cursor and selection state."""
    os.system('clear')
    print(f"\n{C_CYAN}{C_BRIGHT}{'─' * 60}{C_RESET}")
    print(f"{C_CYAN}{C_BRIGHT}  {title}{C_RESET}")
    print(f"{C_CYAN}{C_BRIGHT}{'─' * 60}{C_RESET}\n")
    
    for i, (key, label, sublabel) in enumerate(items):
        is_cursor = (i == cursor)
        is_selected = selected and key in selected
        
        prefix = "  "
        if is_cursor:
            prefix = f"{C_YELLOW}{C_BRIGHT}►{C_RESET} "
        else:
            prefix = "  "
        
        checkbox = ""
        if multi:
            if is_selected:
                checkbox = f"{C_GREEN}[●]{C_RESET} "
            else:
                checkbox = f"{C_DIM}[○]{C_RESET} "
        
        label_style = f"{C_WHITE}{C_BRIGHT}" if is_cursor else C_WHITE
        sub_style = C_DIM
        
        line = f"{prefix}{checkbox}{label_style}{label}{C_RESET}"
        if sublabel:
            line += f"  {sub_style}{sublabel}{C_RESET}"
        
        print(line)
    
    print(f"\n{C_DIM}  [↑/↓] Navigate  {'[Space] Select  ' if multi else ''}[Enter] Confirm  [Esc] Cancel{C_RESET}")
    
    return cursor

def select_single(title, items, key=None):
    """
    Interactive single-select list.
    items: list of (key, label, sublabel) tuples
    Returns selected key or None.
    """
    cursor = 0
    
    while True:
        render_list(title, items, cursor=cursor, multi=False)
        ch = getch()
        
        if ch == '\x1b':  # ESC
            return None
        elif ch == '\r' or ch == '\n':  # Enter
            return items[cursor][0]
        elif ch == '\x1b[A':  # Up arrow
            cursor = max(0, cursor - 1)
        elif ch == '\x1b[B':  # Down arrow
            cursor = min(len(items) - 1, cursor + 1)
        elif ch == 'q':
            return None

def select_multi(title, items, preselected=None):
    """
    Interactive multi-select list.
    items: list of (key, label, sublabel) tuples
    Returns set of selected keys.
    """
    cursor = 0
    selected = set(preselected or [])
    
    while True:
        render_list(title, items, selected=selected, cursor=cursor, multi=True)
        ch = getch()
        
        if ch == '\x1b':  # ESC
            return selected if selected else None
        elif ch == '\r' or ch == '\n':  # Enter
            return selected if selected else None
        elif ch == '\x1b[A':  # Up arrow
            cursor = max(0, cursor - 1)
        elif ch == '\x1b[B':  # Down arrow
            cursor = min(len(items) - 1, cursor + 1)
        elif ch == ' ':  # Space - toggle selection
            key = items[cursor][0]
            if key in selected:
                selected.discard(key)
            else:
                selected.add(key)
        elif ch == 'a':  # Select all
            selected = {item[0] for item in items}
        elif ch == 'd':  # Deselect all
            selected = set()
        elif ch == 'q':
            return selected if selected else None


# ── Test ──
if __name__ == "__main__":
    # Test single select
    items = [
        ("mailg", "mailg", "25 Gmail accounts"),
        ("cloudmail", "cloudmail", "3 cloudmail addresses"),
        ("mail.tm", "mail.tm", "Temporary email"),
    ]
    result = select_single("Select Mail Server", items)
    print(f"\nSelected: {result}")
    
    # Test multi select
    gmail_items = [
        ("acc1@gmail.com", "user1@gmail.com", "FRESH"),
        ("acc2@gmail.com", "user2@gmail.com", "FRESH"),
        ("acc3@gmail.com", "user3@gmail.com", "USED"),
    ]
    result2 = select_multi("Select Gmail Accounts", gmail_items, preselected=["acc1@gmail.com"])
    print(f"Selected accounts: {result2}")
