#!/usr/bin/env python3
"""Cloudmail email manager — shows all emails, marks registered status, allows manual add/remove."""
import sys
from pathlib import Path
import json
from glob import glob

BASE = Path(__file__).resolve().parent

def load_env():
    """Load credentials from .env."""
    env_path = BASE / ".env"
    CM_BASE_URL = None
    CM_ADMIN_EMAIL = None
    CM_ADMIN_PASSWORD = None
    try:
        for line in open(env_path):
            line = line.strip()
            if not line or "#" in line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"").strip()
            if k == "CM_BASE_URL":
                CM_BASE_URL = v
            elif k == "CM_ADMIN_EMAIL":
                CM_ADMIN_EMAIL = v
            elif k == "CM_ADMIN_PASSWORD":
                CM_ADMIN_PASSWORD = v
    except Exception as e:
        print(f"Load .env err: {e}")
        return None, None, None
    return CM_BASE_URL, CM_ADMIN_EMAIL, CM_ADMIN_PASSWORD


def load_th_keys():
    """Load TokenHarbor keys from keys.txt."""
    keys_file = BASE / "keys.txt"
    if not keys_file.exists():
        return []
    keys = []
    for ln in keys_file.read_text().splitlines():
        p = ln.strip().split("|")
        if len(p) >= 4 and "@" in p[0]:
            keys.append({
                "email": p[0].lower(),
                "password": p[1],
                "api_key": p[2] if len(p) > 2 and p[2].startswith("thk_") else "",
                "status": p[3] if len(p) > 3 else "pending",
                "registered": True
            })
    return keys


def generate_available_emails(domains, skip_emails=None):
    """Generate available real-name emails based on cloudmail domains."""
    from random import choice, randint
    if skip_emails is None:
        skip_emails = set()
    
    FIRST = ["john", "jane", "mike", "sarah", "david", "emma", "chris", "lisa", "james", "anna",
             "robert", "mary", "daniel", "laura", "kevin", "jessica", "brian", "amanda", "mark"]
    LAST = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez",
            "martinez", "anderson", "taylor", "thomas", "moore", "jackson", "martin", "lee"]
    
    avail = []
    for domain in domains:
        for i in range(30):  # generate 30 candidates per domain
            f = choice(FIRST)
            l = choice(LAST)
            style = randint(0, 5)
            if style == 0:
                prefix = f"{f}.{l}"
            elif style == 1:
                prefix = f"{f}{randint(19, 99)}"
            elif style == 2:
                prefix = f"{f}.{l}{randint(19, 99)}"
            elif style == 3:
                title = choice(["mr", "ms", "dr"])
                prefix = f"{title}.{l}"
            else:
                prefix = f"{l}{randint(1, 9)}{f[:2]}"
            
            email = f"{prefix}@{domain}".lower()
            if email not in skip_emails:
                avail.append(email)
                skip_emails.add(email)
                if len(avail) >= 100:
                    break
        if len(avail) >= 100:
            break
    return avail


def main():
    cm_url, admin_email, admin_pass = load_env()
    if not cm_url:
        print("No credentials — cannot connect to cloudmail")
        return 1
    
    print("="*60)
    print("CLOUDMAIL EMAIL MANAGER")
    print("="*60)
    print(f"Server: {cm_url}")
    
    # Load TH keys
    th_keys = load_th_keys()
    th_registered = {k["email"]: k for k in th_keys}
    
    # Get cloudmail domains from config
    try:
        spec = __import__('importlib.util').util.spec_from_file_location("tt", BASE / "th-tui.py")
        m = __import__('importlib.util').util.module_from_spec(spec)
        spec.loader.exec_module(m)
        c = m.load_cfg()
        ms = m.get_active_mail(c)
        domains = ms.get("domains") or [ms.get("domain")] if ms else ["furries.my.id"]
    except Exception as e:
        print(f"Config err: {e}")
        domains = ["furries.my.id"]
    
    print(f"\nDomains: {', '.join(domains)}")
    print(f"TH registered: {len(th_registered)}")
    
    # Generate available emails
    skip = set(th_registered.keys())
    avail = generate_available_emails(domains, skip)
    
    print(f"Available slots: ~{len(avail)} generated")
    
    # Interactive menu
    while True:
        print("\n" + "-"*60)
        print("1. View TH registered emails")
        print("2. View available emails")
        print("3. Check specific email")
        print("4. Register manually via VNC (opens URL)")
        print("5. Exit")
        
        k = input("Choose (1-5): ").strip()
        
        if k == '1':
            print(f"\nTH REGISTERED ({len(th_registered)}):")
            for email in sorted(th_registered.keys()):
                info = th_registered[email]
                status_icon = "[OK]" if info["status"] in ("ok", "ok+free") else "[! ]"
                key_preview = info["api_key"][:20]+"..." if info["api_key"] else "[none]"
                print(f"  {status_icon} {email:40} {key_preview}")
                
        elif k == '2':
            n = int(input("How many? [10]: ") or 10)
            print(f"\nAVAILABLE ({min(n,len(avail))}):")
            for email in avail[:n]:
                reg_info = th_registered.get(email)
                if reg_info:
                    icon = "[REG]"
                else:
                    icon = "[FREE]"
                print(f"  {icon} {email}")
                
        elif k == '3':
            email = input("Email to check: ").strip().lower()
            if email in th_registered:
                info = th_registered[email]
                print(f"\nREGISTERED: {email}")
                print(f"  Status: {info['status']}")
                print(f"  Key: {info['api_key'][:30]}...")
            elif email in set(a.lower() for a in avail):
                print(f"\nAVAILABLE: {email}")
            else:
                print(f"\nNOT FOUND (or unavailable): {email}")
                
        elif k == '4':
            email = input("Enter email to register: ").strip()
            if not email:
                continue
            password = input("Password (leave empty for random): ").strip()
            if not password:
                password = "".join([__import__('random').choice("abcdefghijklmnopqrstuvwxyz"), 
                                   __import__('random').choice("0123456789"),
                                   __import__('random').choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), "x"])
            print(f"\nOpening browser at: https://tokenharbor.ai/login?mode=signup")
            print(f"Login: {email}")
            print(f"Pass: {password}")
            print("Use VNC to solve reCAPTCHA manually!")
            import webbrowser
            webbrowser.open("https://tokenharbor.ai/login?mode=signup")
            # Don't auto-register—let you do it manually then press Enter
            input("Press Enter after registration completes...")
            # Reload keys
            th_keys = load_th_keys()
            th_registered = {k["email"]: k for k in th_keys}
            
        elif k == '5':
            break
            
        else:
            print("Invalid option")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
