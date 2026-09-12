#!/usr/bin/env python3
"""vision_solve — coordinate-based visual captcha solver.

Takes a page screenshot, asks a vision model for the 0-1000 normalized
coordinates of a described target, then clicks or drags there with Playwright.
Handles what DOM selectors cannot: reCAPTCHA/checkbox widgets inside
cross-origin iframes, slider-drag challenges, "click the X" targets.

Backend: any OpenAI-compatible chat-completions endpoint with image input.
Default is the farm's own TokenHarbor keys (deepseek-v4.1-flash:free does
vision; deepseek-v4-flash does NOT). Override via env:
  VISION_BASE   default https://tokenharbor.ai/v1
  VISION_MODEL  default deepseek-v4.1-flash:free
  VISION_API_KEY  default: first working key from data/keys.txt (429s skipped)

Usage from a farm script:
  import vision_solve as vs
  x, y = vs.locate_on_page(pg, "the 'I am not a robot' checkbox")
  vs.click_at(pg, x, y)
  vs.solve_slider(pg, "the slider knob", "the right end of the slider track")
"""
import base64
import json
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
KEYS_FILE = BASE / "data" / "keys.txt"
ROOT_KEYS = BASE / "keys.txt"

VISION_BASE = os.environ.get("VISION_BASE", "https://tokenharbor.ai/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "deepseek-v4.1-flash:free")

_key_cache = ""


def _iter_keys():
    if os.environ.get("VISION_API_KEY"):
        yield os.environ["VISION_API_KEY"]
    seen = set()
    for f in (KEYS_FILE, ROOT_KEYS):
        try:
            if not f.exists():
                continue
            for ln in f.read_text().splitlines():
                p = ln.strip().split("|")
                if len(p) >= 3 and p[2].startswith("thk_") and p[2] not in seen:
                    seen.add(p[2])
                    yield p[2]
        except Exception:
            pass


def _ask(png: bytes, prompt: str, timeout=120):
    """POST screenshot + prompt, return raw model text (rotates keys on 429)."""
    import requests
    global _key_cache
    img = base64.b64encode(png).decode()
    ordered = []
    if _key_cache:
        ordered.append(_key_cache)
    ordered += [k for k in _iter_keys() if k != _key_cache]
    last = ""
    for key in ordered:
        try:
            r = requests.post(
                VISION_BASE + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": VISION_MODEL, "messages": [{
                    "role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64," + img}}]}],
                    "max_tokens": 120},
                timeout=timeout)
            if r.status_code == 200:
                _key_cache = key
                return r.json()["choices"][0]["message"]["content"]
            last = f"{r.status_code}:{r.text[:80]}"
            if r.status_code not in (429, 402):
                break  # capability/config error — rotating keys won't help
        except Exception as e:
            last = str(e)[:80]
    raise RuntimeError(f"vision backend failed: {last}")


def _parse_xy(text):
    m = re.search(r"\{[^{}]*\"x\"\s*:\s*(\d+)[^{}]*\"y\"\s*:\s*(\d+)[^{}]*\}", text)
    if m:
        return max(0, min(1000, int(m.group(1)))), max(0, min(1000, int(m.group(2))))
    nums = re.findall(r"\b(\d{1,4})\b", text)
    if len(nums) >= 2:
        return max(0, min(1000, int(nums[0]))), max(0, min(1000, int(nums[1])))
    raise ValueError(f"no coordinates in model reply: {text[:120]}")


def locate(png: bytes, target: str, timeout=120):
    """Return (x1000, y1000) for the center of `target` in a PNG screenshot."""
    prompt = (
        "Return ONLY JSON like {\"x\": 500, \"y\": 500} with integer 0-1000 "
        "coordinates for the center of this target. No other text.\n"
        f"Target: {target}")
    return _parse_xy(_ask(png, prompt, timeout=timeout))


def _vp_size(pg):
    try:
        vp = pg.viewport_size or {}
        return vp.get("width", 1280), vp.get("height", 800)
    except Exception:
        return 1280, 800


def locate_on_page(pg, target: str, timeout=120):
    """Screenshot pg, return target center in page pixels."""
    png = pg.screenshot()
    x1000, y1000 = locate(png, target, timeout=timeout)
    w, h = _vp_size(pg)
    return int(x1000 / 1000 * w), int(y1000 / 1000 * h)


def click_at(pg, x, y):
    pg.mouse.click(x, y)


def click_target(pg, target: str, timeout=120):
    """Locate + click. Returns (x, y)."""
    x, y = locate_on_page(pg, target, timeout=timeout)
    click_at(pg, x, y)
    return x, y


def solve_slider(pg, knob_desc="the slider knob or puzzle piece",
                 end_desc="the right end of the slider track", timeout=120):
    """Drag-style challenge: locate knob + track end, drag across. Returns True."""
    import time
    png = pg.screenshot()
    x1, y1 = locate(png, knob_desc, timeout=timeout)
    w, h = _vp_size(pg)
    # second ask reuses the same screenshot (cheap, no re-capture skew)
    x2, y2 = _parse_xy(_ask(png, (
        "Return ONLY JSON like {\"x\": 500, \"y\": 500} with integer 0-1000 "
        f"coordinates for: {end_desc}. No other text."), timeout=timeout))
    sx, sy, ex, ey = (int(x1 / 1000 * w), int(y1 / 1000 * h),
                      int(x2 / 1000 * w), int(y2 / 1000 * h))
    pg.mouse.move(sx, sy)
    pg.mouse.down()
    steps = 24
    for i in range(1, steps + 1):
        pg.mouse.move(sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps)
        time.sleep(0.03)
    pg.mouse.up()
    return True


def _visible_iframe(pg, src_part):
    """True only if a matching iframe renders a real interactive challenge.

    reCAPTCHA v3 also mounts an api2/anchor iframe (256x60, technically
    visible) that is NOT clickable — so for recaptcha we additionally require
    the frame to contain checkbox text ("not a robot"). v3 badge anchors
    have empty/minimal bodies.
    """
    try:
        for fr in pg.frames:
            if src_part not in (fr.url or ""):
                continue
            if "recaptcha" in src_part:
                try:
                    txt = (fr.locator("body").inner_text(timeout=2000) or "").lower()
                    if "robot" in txt:
                        return True
                    continue
                except Exception:
                    continue
            try:
                el = pg.locator(f'iframe[src*="{src_part}"]').first
                if not el.is_visible(timeout=2000):
                    continue
                bb = el.bounding_box(timeout=2000)
                if bb and bb.get("width", 0) > 50 and bb.get("height", 0) > 30:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def confirm(png: bytes, expectation: str, timeout=120):
    """Vision gate: does the screenshot satisfy `expectation`?
    Returns (True/False, one-line reason). Use after every automation step."""
    prompt = (
        "Look at this UI screenshot. Expectation: " + expectation + "\n"
        "Reply with exactly one line: YES or NO, then a hyphen, then a short "
        "reason. Example: YES - dashboard with 2GB balance visible")
    try:
        text = _ask(png, prompt, timeout=timeout).strip().split("\n")[0]
    except Exception as e:
        return False, f"vision backend error: {str(e)[:80]}"
    verdict = text[:3].upper() == "YES"
    return verdict, text[:160]


def confirm_page(pg, expectation: str, timeout=120):
    """Screenshot pg and vision-confirm `expectation`. Returns (bool, reason)."""
    try:
        png = pg.screenshot()
    except Exception as e:
        return False, f"screenshot failed: {str(e)[:60]}"
    return confirm(png, expectation, timeout=timeout)


def visible_challenge_kind(pg):
    """Classify a visible bot challenge, or '' if none. Cheap DOM probes only."""
    try:
        if _visible_iframe(pg, "recaptcha/api2/anchor"):
            return "recaptcha"
        if _visible_iframe(pg, "turnstile") or _visible_iframe(pg, "challenge"):
            body = (pg.inner_text("body", timeout=3000) or "").lower()
            if "slide" in body or "drag" in body:
                return "slider"
            return "checkbox"
        body = (pg.inner_text("body", timeout=3000) or "").lower()
        if "slide to" in body or "drag the" in body:
            return "slider"
    except Exception:
        pass
    return ""
