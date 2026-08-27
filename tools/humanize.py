#!/usr/bin/env python3
"""Human-like browser interaction helpers (anti-bot-detection).

Avoids: mouse teleportation, straight/bot-like movement, 100% hit rate,
too-fast input. Adds: curved mouse paths with jitter, variable typing speed
with realistic pauses, randomized delays, imperfect targeting.
"""
import random
import time


def skewed(lo, hi, skew=1.5):
    """Skewed random value in [lo, hi] — human-like distribution, NOT flat.

    Uses a beta-ish transform: values cluster near 'lo' (fast/quick) with
    a long tail toward 'hi' (slow). skew>1 biases low, skew<1 biases high.
    """
    r = random.random()
    # beta distribution approximation: r^skew clusters near 0
    v = lo + (hi - lo) * (r ** skew)
    # clamp
    return min(hi, max(lo, v))


def human_delay(base_lo, base_hi, pause_prob=0.05, pause_extra=0.5):
    """Human-like delay: usually skewed-fast, occasional long pause (thinking).

    base_lo/base_hi in seconds. Returns nothing, sleeps.
    """
    # occasional 'thinking' pause
    if random.random() < pause_prob:
        time.sleep(skewed(base_hi, base_hi + pause_extra))
        return
    time.sleep(skewed(base_lo, base_hi))


def rand_delay(a, b):
    """Sleep a random time between a and b seconds (human-skewed)."""
    human_delay(a, b, pause_prob=0.03)


def fitts_time(distance, width=20):
    """Fitts's law: longer distance + smaller target = longer move time.

    Returns a move duration in seconds that scales with distance.
    """
    if distance <= 0:
        return skewed(0.15, 0.35)
    a, b = 0.05, 0.15  # typical Fitts constants for mouse
    mt = a + b * max(0, __import__("math").log2(distance / max(width, 1) + 1))
    # add human jitter around the Fitts prediction
    return mt * skewed(0.8, 1.4)


def human_mouse(page, locator, overshoot=True):
    """Move mouse to a locator with a curved, jittery path (not straight teleport).

    Uses CDP Input.dispatchMouseEvent for intermediate points.
    """
    try:
        box = locator.bounding_box()
        if not box:
            locator.scroll_into_view_if_needed()
            rand_delay(0.2, 0.5)
            box = locator.bounding_box()
        if not box:
            locator.click()
            return
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        # Start from a random-ish origin (current mouse pos or off-target)
        sx = tx + random.uniform(-80, 80)
        sy = ty + random.uniform(-50, 50)

        # Build a curved path with control points (Bezier-ish)
        steps = random.randint(12, 25)
        # control points for slight curve
        cx1 = sx + random.uniform(-60, 60)
        cy1 = sy + random.uniform(-40, 40)
        cx2 = tx + random.uniform(-60, 60)
        cy2 = ty + random.uniform(-40, 40)

        for i in range(1, steps + 1):
            t = i / steps
            # cubic bezier
            mt = 1 - t
            x = mt**3 * sx + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * tx
            y = mt**3 * sy + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * ty
            # jitter
            x += random.uniform(-2.5, 2.5)
            y += random.uniform(-2.5, 2.5)
            x = max(0, min(int(x), 1920))
            y = max(0, min(int(y), 1080))
            page.mouse.move(x, y)
            # Fitts-aware per-step timing: acceleration near middle, decelerate at end
            # (not uniform — humans speed up then slow down)
            speed = 1.0 - 0.6 * (1 - abs(2 * t - 1)) ** 2  # bell: fast middle, slow ends
            time.sleep(skewed(0.008, 0.045) * speed)

        # small overshoot correction (natural)
        if overshoot and random.random() < 0.4:
            page.mouse.move(tx + random.uniform(-6, 6), ty + random.uniform(-6, 6))
            time.sleep(random.uniform(0.05, 0.12))
            page.mouse.move(tx, ty)
            time.sleep(random.uniform(0.03, 0.08))
    except Exception:
        try:
            locator.click()
        except Exception:
            pass


def human_click(page, locator):
    """Click with human-like movement + small delay before click."""
    human_mouse(page, locator)
    rand_delay(0.1, 0.4)
    # occasional double-move before click (hesitation)
    box = locator.bounding_box()
    tx = ty = None
    if box:
        tx = box["x"] + box["width"] / 2
        ty = box["y"] + box["height"] / 2
    if box and tx is not None and ty is not None and random.random() < 0.3:
        x = tx + random.uniform(-4, 4)
        y = ty + random.uniform(-4, 4)
        page.mouse.move(x, y)
        rand_delay(0.05, 0.15)
    try:
        locator.click()
    except Exception:
        try:
            if tx is not None and ty is not None:
                page.mouse.click(tx, ty)
        except Exception:
            pass


def human_type(page, locator, text, mistakes=True):
    """Type with realistic speed, occasional pauses, and random typos/corrections.

    Types char-by-char with variable inter-key delay (40-180ms), sometimes
    faster/slower, occasional backspace typo.
    """
    human_click(page, locator)
    rand_delay(0.2, 0.6)
    # clear field first (select all + delete)
    try:
        locator.press("Control+A")
        rand_delay(0.1, 0.3)
        locator.press("Backspace")
    except Exception:
        try:
            locator.fill("")
        except Exception:
            pass
    rand_delay(0.2, 0.5)

    for ch in text:
        # human typing speed: skewed toward fast (60-160ms typical) with occasional slow char
        delay = skewed(0.045, 0.16, skew=2.0)  # clusters near fast
        if ch in ".,@._-":
            delay += skewed(0.04, 0.12)  # punctuation slower
        if random.random() < 0.05:
            delay += skewed(0.25, 0.6)  # random pause (thinking)
        # occasional 'double-tap' same char (natural fast repeat)
        if random.random() < 0.04:
            delay *= 0.4
        time.sleep(delay)
        try:
            locator.press(ch)  # press handles each char
        except Exception:
            try:
                locator.type(ch)
            except Exception:
                pass
        # random typo + correction
        if mistakes and random.random() < 0.06:
            time.sleep(skewed(0.1, 0.3))
            wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
            try:
                locator.press(wrong)
                time.sleep(skewed(0.15, 0.45))
                locator.press("Backspace")
                time.sleep(skewed(0.08, 0.25))
            except Exception:
                pass
    # small pause at end
    rand_delay(0.1, 0.3)
