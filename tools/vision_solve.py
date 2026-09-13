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
  VISION_FALLBACK_MODELS  default deepseek-v4.1-flash,gpt-4o-mini
                          (tried in order on 400/401/404; 429/402 still
                          rotate across keys on the same model)
  VISION_MAX_WAIT  default 300 (cap, in seconds, on cumulative VLM waits
                   per solve call so a dead backend can't stall a batch)
  VISION_API_KEY  default: first working key from data/keys.txt (429s skipped)

Coordinate parsing is STRICT: only a JSON object with numeric "x"/"y"
(0-1000) counts. Explanatory text without JSON yields None — never
fabricated numbers — and callers must check for None before clicking
(click_at refuses None rather than clicking (0,0)).

Usage from a farm script:
  import vision_solve as vs
  pt = vs.locate_on_page(pg, "the 'I am not a robot' checkbox")
  if pt is None:
      ...  # no click: backend down or no coords in reply
  else:
      vs.click_at(pg, *pt)
  vs.solve_slider(pg, "the slider knob", "the right end of the slider track")
"""
import base64
import json
import os
import re
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
KEYS_FILE = BASE / "data" / "keys.txt"
ROOT_KEYS = BASE / "keys.txt"

VISION_BASE = os.environ.get("VISION_BASE", "https://tokenharbor.ai/v1")
VISION_MODEL = os.environ.get("VISION_MODEL", "deepseek-v4.1-flash:free")
VISION_FALLBACK_MODELS = os.environ.get(
    "VISION_FALLBACK_MODELS", "deepseek-v4.1-flash,gpt-4o-mini")
VISION_MAX_WAIT = float(os.environ.get("VISION_MAX_WAIT", "300"))

_key_cache = ""


def _log(msg):
    print(f"[vision] {msg}", flush=True)


def _model_chain(model=None, models=None):
    """Primary model + vision-capable fallbacks, deduped, in try order."""
    primary = model or os.environ.get("VISION_MODEL", VISION_MODEL)
    if models is None:
        raw = os.environ.get("VISION_FALLBACK_MODELS", VISION_FALLBACK_MODELS)
        models = (raw or "").split(",")
    chain = [primary]
    for m in models or []:
        m = (m or "").strip()
        if m and m not in chain:
            chain.append(m)
    return chain


def _cap(max_total_wait):
    """Effective total-wait cap for one solve call (<=0/None-invalid = default)."""
    if max_total_wait is None:
        try:
            return float(os.environ.get("VISION_MAX_WAIT", VISION_MAX_WAIT))
        except Exception:
            return 300.0
    try:
        cap = float(max_total_wait)
        return cap if cap > 0 else 300.0
    except Exception:
        return 300.0


def _time_left(t0, timeout, max_total_wait):
    """Per-request timeout left under the cumulative cap, or None if capped."""
    left = _cap(max_total_wait) - (time.monotonic() - t0)
    if left <= 0:
        return None
    try:
        return min(float(timeout), left)
    except Exception:
        return left


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


def _ask(png: bytes, prompt: str, timeout=120, model=None, models=None):
    """POST screenshot + prompt, return raw model text.

    Model fallback chain: the configured model first, then vision-capable
    alternates on 400/401/404 (unknown model / no vision support for the
    key). 429/402 keep rotating across keys on the SAME model. A cached key
    that starts failing is dropped from the cache so it can't dominate.
    """
    import requests
    global _key_cache
    img = base64.b64encode(png).decode()
    last = ""
    for mdl in _model_chain(model, models):
        ordered = []
        if _key_cache:
            ordered.append(_key_cache)
        ordered += [k for k in _iter_keys() if k != _key_cache]
        next_model = False
        for key in ordered:
            try:
                r = requests.post(
                    VISION_BASE + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": mdl, "messages": [{
                        "role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/png;base64," + img}}]}],
                        "max_tokens": 120},
                    timeout=timeout)
                if r.status_code == 200:
                    _key_cache = key
                    return r.json()["choices"][0]["message"]["content"]
                last = f"{mdl} {r.status_code}:{r.text[:80]}"
                if key == _key_cache:
                    _key_cache = ""  # sticky cached key failed — drop, rotate
                if r.status_code in (429, 402):
                    continue  # quota/billing — next key, same model
                if r.status_code in (400, 401, 404):
                    next_model = True  # capability error — next fallback model
                break
            except Exception as e:
                last = f"{mdl} err:{str(e)[:80]}"
                if key == _key_cache:
                    _key_cache = ""  # sticky cached key failed — drop, rotate
        if next_model:
            continue
        break
    raise RuntimeError(f"vision backend failed: {last}")


def _parse_xy(text):
    """Strict 0-1000 coord parse. Returns (x, y) or None.

    Only a JSON object with numeric "x" and "y" counts (single or double
    quotes, fenced or bare). The old bare-number fallback is gone on
    purpose: scraping arbitrary integers out of explanatory prose ("there
    are 2 boxes...") fabricates clicks. No JSON -> None, never (0,0).
    """
    if not text:
        return None
    m = re.search(r"\{[^{}]*['\"]x['\"]\s*:\s*\"?(\d{1,4})\"?"
                  r"[^{}]*['\"]y['\"]\s*:\s*\"?(\d{1,4})\"?[^{}]*\}", text)
    if m:
        return max(0, min(1000, int(m.group(1)))), max(0, min(1000, int(m.group(2))))
    return None


def locate(png: bytes, target: str, timeout=120, model=None, models=None,
           max_total_wait=None):
    """Return (x1000, y1000) for the center of `target`, or None.

    None means the model reply held no valid JSON coords (strict parse).
    Backend errors still raise RuntimeError; a capped cumulative wait logs
    and returns None.
    """
    t0 = time.monotonic()
    prompt = (
        "Return ONLY JSON like {\"x\": 500, \"y\": 500} with integer 0-1000 "
        "coordinates for the center of this target. No other text.\n"
        f"Target: {target}")
    budget = _time_left(t0, timeout, max_total_wait)
    if budget is None:
        _log(f"solve capped: cumulative VLM wait > {_cap(max_total_wait):.0f}s "
             f"for target {target[:40]!r} — giving up")
        return None
    try:
        return _parse_xy(_ask(png, prompt, timeout=budget,
                              model=model, models=models))
    except RuntimeError:
        raise
    except Exception as e:
        _log(f"locate ask failed: {str(e)[:80]}")
        return None


def _vp_size(pg):
    try:
        vp = pg.viewport_size or {}
        return vp.get("width", 1280), vp.get("height", 800)
    except Exception:
        return 1280, 800


def _device_scale(pg):
    """Page deviceScaleFactor (window.devicePixelRatio), default 1."""
    try:
        dsf = float(pg.evaluate("() => window.devicePixelRatio || 1"))
        return dsf if dsf > 0 else 1.0
    except Exception:
        return 1.0


def _png_size(png):
    """Physical (w, h) of a PNG screenshot via IHDR (stdlib only)."""
    try:
        import struct
        if png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) >= 24:
            w, h = struct.unpack(">II", png[16:24])
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None, None


def _to_page(x1000, y1000, png, pg):
    """0-1000 coords -> page (CSS) pixels, honoring deviceScaleFactor.

    Screenshots are physical pixels (CSS * DSF) while mouse input wants CSS
    pixels, so divide the physical offset by the DSF. Falls back to the
    viewport size when the PNG header is unreadable.
    """
    dsf = _device_scale(pg)
    pw, ph = _png_size(png)
    if pw and ph:
        return int(x1000 / 1000 * pw / dsf), int(y1000 / 1000 * ph / dsf)
    w, h = _vp_size(pg)
    return int(x1000 / 1000 * w), int(y1000 / 1000 * h)


def locate_on_page(pg, target: str, timeout=120, model=None, models=None,
                   max_total_wait=None):
    """Screenshot pg, return target center in page pixels, or None.

    None = screenshot failed, VLM wait cap hit, backend error, or no valid
    JSON coords. Never (0,0): check before clicking.
    """
    t0 = time.monotonic()
    try:
        png = pg.screenshot()
    except Exception as e:
        _log(f"screenshot failed: {str(e)[:60]}")
        return None
    budget = _time_left(t0, timeout, max_total_wait)
    if budget is None:
        _log(f"solve capped: cumulative VLM wait > {_cap(max_total_wait):.0f}s "
             f"for target {target[:40]!r} — giving up")
        return None
    try:
        pt = locate(png, target, timeout=budget, model=model, models=models,
                    max_total_wait=max_total_wait)
    except RuntimeError as e:
        _log(f"backend failed: {str(e)[:100]}")
        return None
    if pt is None:
        return None
    return _to_page(pt[0], pt[1], png, pg)


def click_at(pg, x, y):
    if x is None or y is None:
        raise ValueError("click_at: refusing to click unknown coordinates (None)")
    pg.mouse.click(x, y)


def click_target(pg, target: str, timeout=120, model=None, models=None,
                 max_total_wait=None):
    """Locate + click. Returns (x, y), or None when nothing was clicked."""
    pt = locate_on_page(pg, target, timeout=timeout, model=model,
                        models=models, max_total_wait=max_total_wait)
    if pt is None:
        return None
    click_at(pg, pt[0], pt[1])
    return pt[0], pt[1]


def solve_slider(pg, knob_desc="the slider knob or puzzle piece",
                 end_desc="the right end of the slider track", timeout=120,
                 model=None, models=None, max_total_wait=None):
    """Drag-style challenge: locate knob + track end, drag across.

    Returns True on drag, False when coords/backend/cap failed (no blind
    drag to (0,0)). Both VLM asks share one screenshot and one wait budget.
    """
    t0 = time.monotonic()
    try:
        png = pg.screenshot()
    except Exception as e:
        _log(f"screenshot failed: {str(e)[:60]}")
        return False
    budget = _time_left(t0, timeout, max_total_wait)
    if budget is None:
        _log(f"solve capped: cumulative VLM wait > {_cap(max_total_wait):.0f}s "
             "for slider — giving up")
        return False
    try:
        knob = locate(png, knob_desc, timeout=budget, model=model,
                      models=models, max_total_wait=max_total_wait)
        if knob is None:
            return False
        budget = _time_left(t0, timeout, max_total_wait)
        if budget is None:
            _log(f"solve capped: cumulative VLM wait > {_cap(max_total_wait):.0f}s "
                 "for slider end — giving up")
            return False
        end = _parse_xy(_ask(png, (
            "Return ONLY JSON like {\"x\": 500, \"y\": 500} with integer 0-1000 "
            f"coordinates for: {end_desc}. No other text."), timeout=budget,
            model=model, models=models))
        if end is None:
            return False
    except RuntimeError as e:
        _log(f"slider backend failed: {str(e)[:100]}")
        return False
    sx, sy = _to_page(knob[0], knob[1], png, pg)
    ex, ey = _to_page(end[0], end[1], png, pg)
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


def confirm(png: bytes, expectation: str, timeout=120, model=None,
            models=None, max_total_wait=None):
    """Vision gate: does the screenshot satisfy `expectation`?
    Returns (True/False, one-line reason). Use after every automation step."""
    t0 = time.monotonic()
    budget = _time_left(t0, timeout, max_total_wait)
    if budget is None:
        return False, (f"vision wait capped (>{_cap(max_total_wait):.0f}s) — "
                       "backend too slow, treating as not confirmed")
    prompt = (
        "Look at this UI screenshot. Expectation: " + expectation + "\n"
        "Reply with exactly one line: YES or NO, then a hyphen, then a short "
        "reason. Example: YES - dashboard with 2GB balance visible")
    try:
        text = _ask(png, prompt, timeout=budget,
                    model=model, models=models).strip().split("\n")[0]
    except Exception as e:
        return False, f"vision backend error: {str(e)[:80]}"
    verdict = text[:3].upper() == "YES"
    return verdict, text[:160]


def confirm_page(pg, expectation: str, timeout=120, model=None, models=None,
                 max_total_wait=None):
    """Screenshot pg and vision-confirm `expectation`. Returns (bool, reason)."""
    try:
        png = pg.screenshot()
    except Exception as e:
        return False, f"screenshot failed: {str(e)[:60]}"
    return confirm(png, expectation, timeout=timeout, model=model,
                   models=models, max_total_wait=max_total_wait)


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
