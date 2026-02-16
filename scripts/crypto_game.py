"""Crypto Tycoon — Stream Deck trading game.

Fast-paced crypto trading sim. Buy/sell fictional cryptocurrencies
with wild price swings, react to news events, and become a billionaire.
Runs on Stream Deck XL (32 keys, 4 rows x 8 cols, 96x96 buttons).

Voice pack: HD2 Helldiver

Usage:
    uv run python scripts/crypto_game.py
"""

import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import wave

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.ImageHelpers import PILHelper

import scores
import sound_engine

# -- config ----------------------------------------------------------------
ROWS = 3
COLS = 8
ROW_OFFSET = 1  # game row 0 = deck row 1
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
TICK_INTERVAL = 3.0
SAVE_FILE = os.path.expanduser("~/.streamdeck-arcade/crypto_save.json")
START_CASH = 10000.0
WIN_GOAL = 1_000_000.0
MILESTONES = [50_000, 100_000, 500_000, 1_000_000]

# -- coin definitions ------------------------------------------------------
COINS = [
    {"sym": "MOONC", "name": "Moon Coin",      "color": "#facc15", "base": 0.50,  "vol": 0.08, "max_swing": 0.50},
    {"sym": "DRGN",  "name": "Dragon Chain",    "color": "#ef4444", "base": 25.0,  "vol": 0.05, "max_swing": 0.30},
    {"sym": "NEON",  "name": "Neon Network",    "color": "#22d3ee", "base": 100.0, "vol": 0.025,"max_swing": 0.15},
    {"sym": "VOID",  "name": "Void Token",      "color": "#a855f7", "base": 10.0,  "vol": 0.06, "max_swing": 0.40},
    {"sym": "BOLT",  "name": "Bolt Protocol",   "color": "#fb923c", "base": 5.0,   "vol": 0.055,"max_swing": 0.35},
    {"sym": "GEMS",  "name": "Gem Stone",       "color": "#34d399", "base": 50.0,  "vol": 0.035,"max_swing": 0.20},
]
NUM_COINS = len(COINS)

# -- news events -----------------------------------------------------------
NEWS_EVENTS = [
    {"text": "MOONC listed on\nmajor exchange!",    "coin": 0, "mult": 0.80, "dur": 5},
    {"text": "DRGN founder\narrested!",              "coin": 1, "mult": -0.60, "dur": 3},
    {"text": "NEON partners\nwith tech giant!",      "coin": 2, "mult": 0.40, "dur": 4},
    {"text": "Market-wide\ncrash incoming!",         "coin": -1, "mult": -0.30, "dur": 4},
    {"text": "Whale buys\n1M BOLT!",                 "coin": 4, "mult": 0.50, "dur": 4},
    {"text": "Hack on VOID\nexchange!",              "coin": 3, "mult": -0.70, "dur": 3},
    {"text": "Bull market!\nEverything pumps!",      "coin": -1, "mult": 0.25, "dur": 5},
    {"text": "Celebrity tweets\nabout GEMS!",        "coin": 5, "mult": 1.00, "dur": 4},
    {"text": "MOONC dev rugs\nthe project!",         "coin": 0, "mult": -0.50, "dur": 3},
    {"text": "New BOLT update\nshipped!",             "coin": 4, "mult": 0.35, "dur": 4},
    {"text": "DRGN burns 50%\nof supply!",            "coin": 1, "mult": 0.60, "dur": 5},
    {"text": "Regulation fears\nhit markets!",       "coin": -1, "mult": -0.20, "dur": 3},
]

# -- grid helpers -----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

# -- voice pack (HD2 Helldiver) --------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "hd2_helldiver/sounds/ReadyToLiberate1.mp3",
        "hd2_helldiver/sounds/ReadyToLiberate2.mp3",
        "hd2_helldiver/sounds/ReportingForDuty1.mp3",
        "hd2_helldiver/sounds/ReportingForDuty2.mp3",
    ],
    "trade": [
        "hd2_helldiver/sounds/GetSome.mp3",
        "hd2_helldiver/sounds/SayHelloToDemocracy.mp3",
        "hd2_helldiver/sounds/ALittleShotOfLiberty.mp3",
        "hd2_helldiver/sounds/Affirmative.mp3",
    ],
    "crash": [
        "hd2_helldiver/sounds/CanistersEmpty.mp3",
        "hd2_helldiver/sounds/MagsEmpty.mp3",
        "hd2_helldiver/sounds/INeedStimms.mp3",
        "hd2_helldiver/sounds/ImSorry.mp3",
    ],
    "win": [
        "hd2_helldiver/sounds/DemocracyForAll.mp3",
        "hd2_helldiver/sounds/FreedomNeverSleeps.mp3",
        "hd2_helldiver/sounds/LibertyProsperityDemocracy.mp3",
        "hd2_helldiver/sounds/ObjectiveCompleted.mp3",
    ],
    "news": [
        "hd2_helldiver/sounds/FoundSomething.mp3",
        "hd2_helldiver/sounds/Here.mp3",
        "hd2_helldiver/sounds/PointMeToTheEnemy.mp3",
    ],
    "milestone": [
        "hd2_helldiver/sounds/ForSuperEarth.mp3",
        "hd2_helldiver/sounds/DemocracyHasLanded.mp3",
        "hd2_helldiver/sounds/FullAutoLaugh.mp3",
    ],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 4.0

def play_voice(event: str):
    global _last_voice_time
    now = time.monotonic()
    if now - _last_voice_time < VOICE_COOLDOWN:
        return
    paths = VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_voice_time = now
            sound_engine.play_voice(full)
            return

def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()

# -- 8-bit SFX -------------------------------------------------------------
SAMPLE_RATE = 22050
_sfx_cache: dict[str, str] = {}
_sfx_dir: str = ""

def _square(freq, dur, vol=1.0, duty=0.5):
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = vol if phase < duty else -vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.8)
            samples.append(val * env * tail)
    return samples

def _triangle(freq, dur, vol=1.0):
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = (4 * abs(phase - 0.5) - 1) * vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.6)
            samples.append(val * env * tail)
    return samples

def _write_wav(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in samples:
            s = max(-0.95, min(0.95, s))
            w.writeframes(struct.pack("<h", int(s * 32767)))

def _generate_sfx():
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="crypto-sfx-")
    v = SFX_VOLUME

    # buy: cash register cha-ching
    s = (_square(1200, 0.02, v * 0.4, 0.3) + _square(1600, 0.02, v * 0.45, 0.3) +
         _triangle(2000, 0.04, v * 0.5) + _square(2400, 0.06, v * 0.4, 0.4))
    _write_wav(os.path.join(_sfx_dir, "buy.wav"), s)
    _sfx_cache["buy"] = os.path.join(_sfx_dir, "buy.wav")

    # sell: coin drop
    s = (_triangle(800, 0.03, v * 0.4) + _triangle(600, 0.04, v * 0.35) +
         _triangle(400, 0.06, v * 0.3) + _triangle(300, 0.08, v * 0.2))
    _write_wav(os.path.join(_sfx_dir, "sell.wav"), s)
    _sfx_cache["sell"] = os.path.join(_sfx_dir, "sell.wav")

    # price_up: rising tone
    s = (_triangle(440, 0.04, v * 0.3) + _triangle(554, 0.04, v * 0.35) +
         _triangle(659, 0.04, v * 0.4) + _triangle(880, 0.06, v * 0.45))
    _write_wav(os.path.join(_sfx_dir, "price_up.wav"), s)
    _sfx_cache["price_up"] = os.path.join(_sfx_dir, "price_up.wav")

    # price_down: falling tone
    s = (_triangle(880, 0.04, v * 0.3) + _triangle(659, 0.04, v * 0.3) +
         _triangle(440, 0.04, v * 0.25) + _triangle(330, 0.06, v * 0.2))
    _write_wav(os.path.join(_sfx_dir, "price_down.wav"), s)
    _sfx_cache["price_down"] = os.path.join(_sfx_dir, "price_down.wav")

    # news: alert bell
    s = (_square(1000, 0.05, v * 0.5, 0.3) + _square(0, 0.03, 0) +
         _square(1000, 0.05, v * 0.5, 0.3) + _square(0, 0.03, 0) +
         _square(1200, 0.08, v * 0.55, 0.3))
    _write_wav(os.path.join(_sfx_dir, "news.wav"), s)
    _sfx_cache["news"] = os.path.join(_sfx_dir, "news.wav")

    # milestone: fanfare
    s = (_triangle(523, 0.08, v * 0.5) + _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) + _triangle(1047, 0.15, v * 0.65) +
         _triangle(1318, 0.2, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "milestone.wav"), s)
    _sfx_cache["milestone"] = os.path.join(_sfx_dir, "milestone.wav")

    # crash: dramatic drop
    s = (_square(600, 0.05, v * 0.5, 0.4) + _square(400, 0.06, v * 0.45, 0.4) +
         _square(250, 0.08, v * 0.4, 0.4) + _square(150, 0.12, v * 0.35, 0.4) +
         _square(80, 0.15, v * 0.3, 0.3))
    _write_wav(os.path.join(_sfx_dir, "crash.wav"), s)
    _sfx_cache["crash"] = os.path.join(_sfx_dir, "crash.wav")

    # unlock: upgrade sound
    s = (_triangle(660, 0.06, v * 0.4) + _triangle(880, 0.06, v * 0.45) +
         _triangle(1047, 0.06, v * 0.5) + _triangle(1320, 0.12, v * 0.55))
    _write_wav(os.path.join(_sfx_dir, "unlock.wav"), s)
    _sfx_cache["unlock"] = os.path.join(_sfx_dir, "unlock.wav")

    # select: quick click
    s = _square(800, 0.02, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "select.wav"), s)
    _sfx_cache["select"] = os.path.join(_sfx_dir, "select.wav")

    # start: game begin
    s = (_triangle(330, 0.05, v * 0.3) + _triangle(440, 0.05, v * 0.35) +
         _triangle(554, 0.05, v * 0.4) + _triangle(660, 0.08, v * 0.45))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # error
    s = _square(150, 0.1, v * 0.3, 0.3) + _square(120, 0.1, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

def play_sfx(name):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)

def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)

# -- compact number display ------------------------------------------------
def _compact(n):
    if n < 0:
        return "-" + _compact(-n)
    if n < 0.01:
        return "$0.00"
    if n < 1:
        return f"${n:.2f}"
    if n < 10:
        return f"${n:.2f}"
    if n < 1000:
        return f"${int(n)}"
    if n < 10000:
        return f"${n/1000:.1f}K"
    if n < 1_000_000:
        return f"${int(n/1000)}K"
    if n < 1_000_000_000:
        return f"${n/1_000_000:.1f}M"
    return f"${n/1_000_000_000:.1f}B"

def _compact_qty(n):
    """Compact quantity without $."""
    if n == 0:
        return "0"
    if n < 1:
        return f"{n:.2f}"
    if n < 10:
        return f"{n:.1f}"
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n/1000:.1f}K"
    return f"{n/1_000_000:.1f}M"

def _price_str(p):
    """Format price for display."""
    if p < 0.01:
        return "$0.01"
    if p < 1:
        return f"${p:.3f}"
    if p < 10:
        return f"${p:.2f}"
    if p < 100:
        return f"${p:.1f}"
    if p < 10000:
        return f"${int(p)}"
    return f"${int(p/1000)}K"

def _pct_str(pct):
    """Format percentage."""
    if pct >= 0:
        return f"+{pct:.1f}%"
    return f"{pct:.1f}%"

# -- renderers (PIL) -------------------------------------------------------

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _darken(hex_color, factor=0.3):
    r, g, b = _hex_to_rgb(hex_color)
    return (int(r * factor), int(g * factor), int(b * factor))

def render_hud_empty(size=SIZE):
    return Image.new("RGB", size, "#111827")

def render_coin_tile(coin_info, price, prev_price, history, size=SIZE):
    """Render a coin tile on the market screen."""
    up = price >= prev_price
    border_color = "#22c55e" if up else "#ef4444"
    bg = _darken(coin_info["color"], 0.15)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)

    # Border glow
    d.rectangle([1, 1, 94, 94], outline=border_color, width=2)

    # Coin symbol
    d.text((48, 12), coin_info["sym"], font=_font(13), fill=coin_info["color"], anchor="mt")

    # Price
    d.text((48, 36), _price_str(price), font=_font(16), fill="white", anchor="mm")

    # Change percentage
    if prev_price > 0:
        pct = (price - prev_price) / prev_price * 100
        arrow = "^" if up else "v"
        pct_color = "#22c55e" if up else "#ef4444"
        d.text((48, 56), f"{arrow}{abs(pct):.1f}%", font=_font(11), fill=pct_color, anchor="mm")

    # Mini trend dots (last 5 prices)
    if len(history) >= 2:
        dot_y = 76
        dot_start_x = 48 - (min(len(history), 5) - 1) * 6
        prices = history[-5:]
        for i, hp in enumerate(prices):
            x = dot_start_x + i * 12
            dot_col = "#22c55e" if i > 0 and hp >= prices[i - 1] else "#ef4444"
            if i == 0:
                dot_col = "#6b7280"
            d.ellipse([x - 3, dot_y - 3, x + 3, dot_y + 3], fill=dot_col)

    return img

def render_portfolio_tile(coin_info, qty, value, pct_change, size=SIZE):
    """Render a portfolio tile showing owned coins."""
    if qty <= 0:
        bg = _darken(coin_info["color"], 0.08)
        img = Image.new("RGB", size, bg)
        d = ImageDraw.Draw(img)
        d.text((48, 28), coin_info["sym"], font=_font(12), fill="#4b5563", anchor="mm")
        d.text((48, 52), "---", font=_font(14), fill="#374151", anchor="mm")
        d.text((48, 74), "NONE", font=_font(9), fill="#374151", anchor="mm")
        return img

    bg = _darken(coin_info["color"], 0.18)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)

    d.text((48, 10), coin_info["sym"], font=_font(11), fill=coin_info["color"], anchor="mt")
    d.text((48, 30), _compact_qty(qty), font=_font(14), fill="white", anchor="mm")
    d.text((48, 52), _compact(value), font=_font(13), fill="#fbbf24", anchor="mm")

    # P/L percentage
    pl_color = "#22c55e" if pct_change >= 0 else "#ef4444"
    d.text((48, 74), _pct_str(pct_change), font=_font(11), fill=pl_color, anchor="mm")

    return img

def render_upgrade_tile(name, desc, cost, owned, can_afford, active=False, size=SIZE):
    """Render an upgrade tile."""
    if owned:
        bg = "#1a3a1a" if not active else "#2a1a3a"
        img = Image.new("RGB", size, bg)
        d = ImageDraw.Draw(img)
        d.text((48, 14), name, font=_font(11), fill="#34d399", anchor="mt")
        label = "ON" if active else "OWNED"
        d.text((48, 48), label, font=_font(16), fill="#22c55e" if not active else "#a855f7", anchor="mm")
        d.text((48, 74), desc, font=_font(9), fill="#6b7280", anchor="mm")
        d.rectangle([1, 1, 94, 94], outline="#22c55e" if not active else "#a855f7", width=1)
        return img

    bg = "#1f2937" if can_afford else "#111827"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 12), name, font=_font(11), fill="#9ca3af" if can_afford else "#4b5563", anchor="mt")
    d.text((48, 38), desc, font=_font(9), fill="#6b7280", anchor="mm")
    cost_color = "#86efac" if can_afford else "#4b5563"
    d.text((48, 58), _compact(cost), font=_font(13), fill=cost_color, anchor="mm")
    d.text((48, 80), "BUY" if can_afford else "LOCKED", font=_font(10),
           fill="#34d399" if can_afford else "#374151", anchor="mm")
    return img

def render_hud_cash(amount, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "CASH", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), _compact(amount), font=_font(18), fill="#22c55e", anchor="mm")
    return img

def render_hud_networth(nw, pct, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 8), "NET WORTH", font=_font(9), fill="#9ca3af", anchor="mt")
    color = "#fbbf24" if pct >= 0 else "#ef4444"
    d.text((48, 38), _compact(nw), font=_font(16), fill=color, anchor="mm")
    pct_color = "#22c55e" if pct >= 0 else "#ef4444"
    d.text((48, 62), _pct_str(pct), font=_font(12), fill=pct_color, anchor="mm")
    # progress bar to $1M
    bar_w = 70
    bar_x = 48 - bar_w // 2
    prog = min(1.0, nw / WIN_GOAL)
    d.rectangle([bar_x, 78, bar_x + bar_w, 86], outline="#374151")
    fw = int(bar_w * prog)
    if fw > 0:
        d.rectangle([bar_x, 78, bar_x + fw, 86], fill=color)
    return img

def render_hud_portfolio_val(val, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "COINS", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), _compact(val), font=_font(18), fill="#a855f7", anchor="mm")
    return img

def render_hud_tick(tick, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "MARKET", font=_font(10), fill="#9ca3af", anchor="mt")
    h = tick % 24
    day = tick // 24 + 1
    d.text((48, 38), f"D{day}", font=_font(16), fill="#60a5fa", anchor="mm")
    d.text((48, 62), f"{h:02d}:00", font=_font(13), fill="#374151", anchor="mm")
    return img

def render_hud_best_coin(sym, pct, color, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "HOT", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 38), sym, font=_font(16), fill=color, anchor="mm")
    pct_col = "#22c55e" if pct >= 0 else "#ef4444"
    d.text((48, 62), _pct_str(pct), font=_font(13), fill=pct_col, anchor="mm")
    return img

def render_hud_news(text, is_new, size=SIZE):
    bg = "#78350f" if is_new else "#1c1917"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 8), "NEWS", font=_font(9), fill="#fbbf24" if is_new else "#6b7280", anchor="mt")
    if text:
        lines = text.split("\n")
        y = 30
        for line in lines:
            d.text((48, y), line, font=_font(10), fill="#fef3c7" if is_new else "#9ca3af", anchor="mt")
            y += 16
    else:
        d.text((48, 48), "---", font=_font(14), fill="#374151", anchor="mm")
    if is_new:
        d.rectangle([1, 1, 94, 94], outline="#fbbf24", width=2)
    return img

def render_hud_view_toggle(view, size=SIZE):
    img = Image.new("RGB", size, "#1e3a5f")
    d = ImageDraw.Draw(img)
    label = "MARKET" if view == "trade" else "MARKET"
    d.text((48, 24), "VIEW", font=_font(11), fill="white", anchor="mm")
    d.text((48, 48), label, font=_font(11), fill="#60a5fa", anchor="mm")
    d.text((48, 72), "TAP", font=_font(9), fill="#6b7280", anchor="mm")
    return img

def render_trade_btn(label, sub, bg_color, text_color="white", sub_color="#9ca3af",
                     can_afford=True, size=SIZE):
    bg = bg_color if can_afford else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    fill = text_color if can_afford else "#4b5563"
    sfill = sub_color if can_afford else "#374151"
    d.text((48, 28), label, font=_font(16), fill=fill, anchor="mm")
    d.text((48, 58), sub, font=_font(12), fill=sfill, anchor="mm")
    if can_afford:
        d.rectangle([1, 1, 94, 94], outline=text_color, width=1)
    return img

def render_coin_detail(coin_info, price, history, size=SIZE):
    """Large price chart for trade view."""
    bg = _darken(coin_info["color"], 0.12)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)

    d.text((48, 10), coin_info["sym"], font=_font(14), fill=coin_info["color"], anchor="mt")
    d.text((48, 32), _price_str(price), font=_font(18), fill="white", anchor="mm")

    # Mini chart
    if len(history) >= 2:
        pts = history[-16:]
        mn = min(pts)
        mx = max(pts)
        rng = mx - mn if mx > mn else 1.0
        chart_y_top = 50
        chart_y_bot = 88
        chart_x_left = 8
        chart_x_right = 88
        step = (chart_x_right - chart_x_left) / max(1, len(pts) - 1)
        points = []
        for i, p in enumerate(pts):
            x = chart_x_left + i * step
            y = chart_y_bot - (p - mn) / rng * (chart_y_bot - chart_y_top)
            points.append((x, y))
        for i in range(1, len(points)):
            col = "#22c55e" if pts[i] >= pts[i - 1] else "#ef4444"
            d.line([points[i - 1], points[i]], fill=col, width=2)

    return img

def render_back_btn(size=SIZE):
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "<", font=_font(24), fill="white", anchor="mm")
    d.text((48, 64), "BACK", font=_font(12), fill="#f87171", anchor="mm")
    return img

def render_empty_tile(size=SIZE):
    return Image.new("RGB", size, "#0f172a")

def render_title_tile(text, sub="", color="#fbbf24", size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 28), text, font=_font(16), fill=color, anchor="mm")
    if sub:
        d.text((48, 56), sub, font=_font(12), fill="#9ca3af", anchor="mm")
    return img

def render_start_btn(size=SIZE):
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "START", font=_font(18), fill="white", anchor="mm")
    d.text((48, 58), "TRADE", font=_font(14), fill="#34d399", anchor="mm")
    d.rectangle([2, 2, 93, 93], outline="#34d399", width=2)
    return img

def render_milestone_flash(text, size=SIZE):
    img = Image.new("RGB", size, "#fbbf24")
    d = ImageDraw.Draw(img)
    d.text((48, 28), text, font=_font(14), fill="#111827", anchor="mm")
    d.text((48, 56), "REACHED!", font=_font(12), fill="#92400e", anchor="mm")
    return img

def render_win_tile(size=SIZE):
    img = Image.new("RGB", size, "#fbbf24")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "$$$", font=_font(20), fill="#111827", anchor="mm")
    d.text((48, 50), "CRYPTO", font=_font(12), fill="#92400e", anchor="mm")
    d.text((48, 70), "TYCOON!", font=_font(12), fill="#92400e", anchor="mm")
    return img

def render_leverage_toggle(active, size=SIZE):
    bg = "#7c2d12" if active else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "LEVER", font=_font(11), fill="#fb923c" if active else "#6b7280", anchor="mt")
    d.text((48, 34), "AGE", font=_font(11), fill="#fb923c" if active else "#6b7280", anchor="mt")
    d.text((48, 58), "2x" if active else "1x", font=_font(20),
           fill="#fbbf24" if active else "#4b5563", anchor="mm")
    label = "ON" if active else "OFF"
    d.text((48, 82), label, font=_font(10), fill="#22c55e" if active else "#6b7280", anchor="mm")
    if active:
        d.rectangle([1, 1, 94, 94], outline="#fb923c", width=2)
    return img

# -- game ------------------------------------------------------------------

class CryptoGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | market | trade
        self.tick_timer = None
        self.timers = []

        # Player state
        self.cash = START_CASH
        self.portfolio = [0.0] * NUM_COINS  # quantities
        self.avg_buy_price = [0.0] * NUM_COINS  # average buy price per coin

        # Price state
        self.prices = [c["base"] for c in COINS]
        self.prev_prices = list(self.prices)
        self.price_history = [[] for _ in range(NUM_COINS)]
        self.momentum = [0.0] * NUM_COINS  # rolling momentum per coin
        self.tick_count = 0

        # News
        self.active_news = None  # current news event dict
        self.news_text = ""
        self.news_is_new = False
        self.news_remaining = 0  # ticks remaining for news effect
        self.next_news_tick = random.randint(10, 30)

        # Upgrades
        self.has_bot = False
        self.bot_coin = -1  # which coin the bot trades
        self.bot_avg = [0.0] * NUM_COINS  # running average prices for bot
        self.has_insider = False
        self.has_mining = False
        self.leverage_on = False

        # Milestones reached
        self.milestones_hit = set()
        self.won = False

        # Trade view
        self.selected_coin = -1  # index of coin being traded

        # Pending news (for insider: show early)
        self.pending_news = None
        self.pending_news_at = 0

        # Best net worth
        self.best_nw = scores.load_best("crypto", 0)

        # Pre-render static images
        self.img_empty = render_empty_tile()

    def set_key(self, pos, img):
        try:
            native = PILHelper.to_native_key_format(self.deck, img)
            with self.deck:
                self.deck.set_key_image(pos, native)
        except Exception:
            pass

    def _cancel_all_timers(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    # -- net worth -----------------------------------------------------------

    def _net_worth(self):
        total = self.cash
        for i in range(NUM_COINS):
            total += self.portfolio[i] * self.prices[i]
        return total

    def _portfolio_value(self):
        total = 0.0
        for i in range(NUM_COINS):
            total += self.portfolio[i] * self.prices[i]
        return total

    # -- save / load ---------------------------------------------------------

    def _save_game(self):
        nw = self._net_worth()
        if nw > self.best_nw:
            self.best_nw = nw
            scores.save_best("crypto", int(nw))
        data = {
            "cash": self.cash,
            "portfolio": self.portfolio,
            "avg_buy_price": self.avg_buy_price,
            "prices": self.prices,
            "prev_prices": self.prev_prices,
            "price_history": self.price_history,
            "momentum": self.momentum,
            "tick_count": self.tick_count,
            "has_bot": self.has_bot,
            "bot_coin": self.bot_coin,
            "has_insider": self.has_insider,
            "has_mining": self.has_mining,
            "leverage_on": self.leverage_on,
            "milestones_hit": list(self.milestones_hit),
            "won": self.won,
        }
        try:
            os.makedirs(os.path.dirname(SAVE_FILE), exist_ok=True)
            with open(SAVE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_save(self):
        try:
            with open(SAVE_FILE) as f:
                data = json.load(f)
            self.cash = data.get("cash", START_CASH)
            self.portfolio = data.get("portfolio", [0.0] * NUM_COINS)
            self.avg_buy_price = data.get("avg_buy_price", [0.0] * NUM_COINS)
            self.prices = data.get("prices", [c["base"] for c in COINS])
            self.prev_prices = data.get("prev_prices", list(self.prices))
            self.price_history = data.get("price_history", [[] for _ in range(NUM_COINS)])
            self.momentum = data.get("momentum", [0.0] * NUM_COINS)
            self.tick_count = data.get("tick_count", 0)
            self.has_bot = data.get("has_bot", False)
            self.bot_coin = data.get("bot_coin", -1)
            self.has_insider = data.get("has_insider", False)
            self.has_mining = data.get("has_mining", False)
            self.leverage_on = data.get("leverage_on", False)
            self.milestones_hit = set(data.get("milestones_hit", []))
            self.won = data.get("won", False)
            return True
        except Exception:
            return False

    def _delete_save(self):
        try:
            os.remove(SAVE_FILE)
        except FileNotFoundError:
            pass

    # -- idle screen ---------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.mode = "idle"
        self._cancel_all_timers()
        has_save = os.path.exists(SAVE_FILE)

        # HUD row
        self.set_key(1, render_title_tile("CRYPTO", "TYCOON"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())

        # Show best score
        if self.best_nw > 0:
            self.set_key(2, render_title_tile("BEST", _compact(self.best_nw), "#fbbf24"))

        # Game area - clear
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        # Coin logos as decoration
        for i, coin in enumerate(COINS):
            pos = rc_to_pos(0, i + 1)
            img = Image.new("RGB", SIZE, _darken(coin["color"], 0.15))
            d = ImageDraw.Draw(img)
            d.text((48, 48), coin["sym"], font=_font(14), fill=coin["color"], anchor="mm")
            self.set_key(pos, img)

        if has_save:
            self._load_save()
            self.set_key(3, render_title_tile("NET", _compact(self._net_worth()), "#22c55e"))
            self.set_key(rc_to_pos(1, 2), render_trade_btn("CONT", "INUE", "#1e40af", "white", "#93c5fd"))
            self.set_key(rc_to_pos(1, 5), render_start_btn())
        else:
            self.set_key(rc_to_pos(1, 3), render_start_btn())

    # -- start / continue ----------------------------------------------------

    def _start_new(self):
        self._delete_save()
        self.cash = START_CASH
        self.portfolio = [0.0] * NUM_COINS
        self.avg_buy_price = [0.0] * NUM_COINS
        self.prices = [c["base"] for c in COINS]
        self.prev_prices = list(self.prices)
        self.price_history = [[] for _ in range(NUM_COINS)]
        self.momentum = [0.0] * NUM_COINS
        self.tick_count = 0
        self.has_bot = False
        self.bot_coin = -1
        self.bot_avg = [0.0] * NUM_COINS
        self.has_insider = False
        self.has_mining = False
        self.leverage_on = False
        self.milestones_hit = set()
        self.won = False
        self.active_news = None
        self.news_text = ""
        self.news_is_new = False
        self.news_remaining = 0
        self.next_news_tick = random.randint(10, 30)
        self.pending_news = None
        self.pending_news_at = 0
        self.selected_coin = -1
        self._begin_play()

    def _continue_game(self):
        self._load_save()
        self.active_news = None
        self.news_text = ""
        self.news_is_new = False
        self.news_remaining = 0
        self.next_news_tick = self.tick_count + random.randint(10, 30)
        self.pending_news = None
        self.pending_news_at = 0
        self.selected_coin = -1
        self.bot_avg = list(self.prices)
        self._begin_play()

    def _begin_play(self):
        self.running = True
        self.mode = "market"
        play_sfx("start")
        play_voice("start")

        # Initialize bot averages
        self.bot_avg = list(self.prices)

        # Seed initial price history
        for i in range(NUM_COINS):
            if len(self.price_history[i]) == 0:
                self.price_history[i].append(self.prices[i])

        self._render_market()
        self._schedule_tick()

    def _schedule_tick(self):
        if not self.running:
            return
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    # -- price engine --------------------------------------------------------

    def _update_prices(self):
        self.prev_prices = list(self.prices)
        news_coin = -2  # no news effect
        news_mult = 0.0

        if self.news_remaining > 0 and self.active_news:
            news_coin = self.active_news["coin"]
            news_mult = self.active_news["mult"] / self.active_news["dur"]

        for i in range(NUM_COINS):
            coin = COINS[i]
            vol = coin["vol"]
            max_sw = coin["max_swing"]

            # Gaussian random factor
            rf = random.gauss(0, vol)

            # Momentum: trend continuation bias
            self.momentum[i] = 0.7 * self.momentum[i] + 0.3 * rf
            rf = 0.6 * rf + 0.4 * self.momentum[i]

            # Mean reversion: gentle pull toward base
            base = coin["base"]
            if self.prices[i] > 0:
                reversion = (base - self.prices[i]) / self.prices[i] * 0.02
                rf += reversion

            # Clamp
            rf = max(-max_sw, min(max_sw, rf))

            # News effect
            if news_coin == -1:  # affects all
                rf += news_mult
            elif news_coin == i:
                rf += news_mult

            # Apply
            self.prices[i] *= (1.0 + rf)
            self.prices[i] = max(0.01, self.prices[i])

            # Record history (keep last 50)
            self.price_history[i].append(self.prices[i])
            if len(self.price_history[i]) > 50:
                self.price_history[i] = self.price_history[i][-50:]

            # Update bot average
            self.bot_avg[i] = 0.9 * self.bot_avg[i] + 0.1 * self.prices[i]

    def _process_news(self):
        # Count down active news
        if self.news_remaining > 0:
            self.news_remaining -= 1
            if self.news_remaining <= 0:
                self.active_news = None
                self.news_is_new = False

        # Check if it is time for new news
        if self.tick_count >= self.next_news_tick:
            event = random.choice(NEWS_EVENTS)
            if self.has_insider and not self.pending_news:
                # Insider sees it 5 ticks early
                self.pending_news = event
                self.pending_news_at = self.tick_count + 5
                self.news_text = "INSIDER TIP:\n" + event["text"]
                self.news_is_new = True
                self.next_news_tick = self.tick_count + random.randint(15, 35)
            else:
                self._activate_news(event)
                self.next_news_tick = self.tick_count + random.randint(10, 30)

        # Check pending insider news
        if self.pending_news and self.tick_count >= self.pending_news_at:
            self._activate_news(self.pending_news)
            self.pending_news = None
            self.pending_news_at = 0

    def _activate_news(self, event):
        self.active_news = event
        self.news_text = event["text"]
        self.news_is_new = True
        self.news_remaining = event["dur"]
        play_sfx("news")
        play_voice("news")

        # Dramatic crash sound for negative events
        if event["mult"] < -0.3:
            def _delayed_crash():
                play_sfx("crash")
                play_voice("crash")
            t = threading.Timer(1.0, _delayed_crash)
            t.daemon = True
            t.start()
            self.timers.append(t)

    def _process_bot(self):
        """Auto-trader bot logic."""
        if not self.has_bot or self.bot_coin < 0:
            return
        i = self.bot_coin
        price = self.prices[i]
        avg = self.bot_avg[i]
        if avg <= 0:
            return

        # Buy when 20% below average
        if price < avg * 0.8 and self.cash > price:
            qty = min(self.cash * 0.1 / price, self.cash / price)  # buy 10% of cash
            if qty > 0:
                self.cash -= qty * price
                self.portfolio[i] += qty

        # Sell when 20% above average
        if price > avg * 1.2 and self.portfolio[i] > 0:
            qty = self.portfolio[i] * 0.5  # sell half
            if qty > 0:
                self.cash += qty * price
                self.portfolio[i] -= qty

    def _check_milestones(self):
        nw = self._net_worth()
        for m in MILESTONES:
            if m not in self.milestones_hit and nw >= m:
                self.milestones_hit.add(m)
                play_sfx("milestone")
                play_voice("milestone")
                if m >= WIN_GOAL:
                    self.won = True
                self._flash_milestone(m)
                return  # one at a time

    def _flash_milestone(self, amount):
        """Celebrate milestone with animation."""
        def _animate():
            label = _compact(amount)
            for _ in range(3):
                for k in range(8, 32):
                    self.set_key(k, render_milestone_flash(label))
                time.sleep(0.4)
                if self.mode == "market":
                    self._render_market_grid()
                elif self.mode == "trade":
                    self._render_trade_view()
                time.sleep(0.3)
            if amount >= WIN_GOAL:
                self._win_animation()

        t = threading.Thread(target=_animate, daemon=True)
        t.start()

    def _win_animation(self):
        """Full win celebration."""
        play_voice("win")
        for _ in range(4):
            for k in range(8, 32):
                self.set_key(k, render_win_tile())
            time.sleep(0.5)
            for k in range(8, 32):
                self.set_key(k, self.img_empty)
            time.sleep(0.3)
        if self.mode == "market":
            self._render_market_grid()

    # -- tick ----------------------------------------------------------------

    def _tick(self):
        if not self.running:
            return
        with self.lock:
            self.tick_count += 1

            # Process news before price update
            self._process_news()

            # Update prices
            self._update_prices()

            # Mining passive income
            if self.has_mining:
                self.cash += 50.0

            # Bot trading
            self._process_bot()

            # Check milestones
            self._check_milestones()

            # Save periodically
            if self.tick_count % 10 == 0:
                self._save_game()

            # Update display
            if self.mode == "market":
                self._render_hud()
                self._render_market_grid()
            elif self.mode == "trade":
                self._render_hud()
                self._render_trade_view()

            # Mark news as not-new after 2 ticks
            if self.news_is_new and self.news_remaining > 0:
                if self.active_news and self.tick_count > (self.next_news_tick - random.randint(8, 28)):
                    pass  # keep it new for a bit
                # Auto-dismiss "new" after showing
                def _clear_new():
                    self.news_is_new = False
                t = threading.Timer(2.0, _clear_new)
                t.daemon = True
                t.start()
                self.timers.append(t)

        self._schedule_tick()

    # -- rendering: market view ----------------------------------------------

    def _render_hud(self):
        nw = self._net_worth()
        pv = self._portfolio_value()
        pct = ((nw - START_CASH) / START_CASH) * 100

        self.set_key(1, render_hud_cash(self.cash))
        self.set_key(2, render_hud_networth(nw, pct))
        self.set_key(3, render_hud_portfolio_val(pv))
        self.set_key(4, render_hud_tick(self.tick_count))

        # Best performing coin
        best_i = 0
        best_pct = -999
        for i in range(NUM_COINS):
            base = COINS[i]["base"]
            cp = (self.prices[i] - base) / base * 100
            if cp > best_pct:
                best_pct = cp
                best_i = i
        self.set_key(5, render_hud_best_coin(COINS[best_i]["sym"], best_pct, COINS[best_i]["color"]))

        self.set_key(6, render_hud_news(self.news_text, self.news_is_new))
        self.set_key(7, render_hud_view_toggle(self.mode))

    def _render_market(self):
        self._render_hud()
        self._render_market_grid()

    def _render_market_grid(self):
        # Row 0 (game): 6 coin tiles + 2 info
        for i in range(NUM_COINS):
            pos = rc_to_pos(0, i)
            self.set_key(pos, render_coin_tile(
                COINS[i], self.prices[i], self.prev_prices[i], self.price_history[i]
            ))
        # Keys 14, 15: empty info tiles
        for c in range(NUM_COINS, COLS):
            self.set_key(rc_to_pos(0, c), self.img_empty)

        # Row 1 (game): Portfolio holdings
        for i in range(NUM_COINS):
            pos = rc_to_pos(1, i)
            qty = self.portfolio[i]
            val = qty * self.prices[i]
            avg = self.avg_buy_price[i]
            if avg > 0 and qty > 0:
                pct_change = (self.prices[i] - avg) / avg * 100
            else:
                pct_change = 0.0
            self.set_key(pos, render_portfolio_tile(COINS[i], qty, val, pct_change))
        for c in range(NUM_COINS, COLS):
            self.set_key(rc_to_pos(1, c), self.img_empty)

        # Row 2 (game): Upgrades
        # Key 24: BOT
        self.set_key(rc_to_pos(2, 0), render_upgrade_tile(
            "BOT", "Auto-trade", 5000, self.has_bot, self.cash >= 5000,
            active=(self.has_bot and self.bot_coin >= 0)
        ))
        # Key 25: INSIDER
        self.set_key(rc_to_pos(2, 1), render_upgrade_tile(
            "INSIDER", "Early news", 10000, self.has_insider, self.cash >= 10000
        ))
        # Key 26: MINING
        self.set_key(rc_to_pos(2, 2), render_upgrade_tile(
            "MINING", "+$50/tick", 3000, self.has_mining, self.cash >= 3000
        ))
        # Key 27: LEVERAGE toggle
        self.set_key(rc_to_pos(2, 3), render_leverage_toggle(self.leverage_on))
        # Keys 28-31: stats / empty
        nw = self._net_worth()
        self.set_key(rc_to_pos(2, 4), render_title_tile("GOAL", _compact(WIN_GOAL), "#fbbf24"))
        self.set_key(rc_to_pos(2, 5), render_title_tile("BEST", _compact(self.best_nw), "#a855f7"))
        for c in range(6, COLS):
            self.set_key(rc_to_pos(2, c), self.img_empty)

    # -- rendering: trade view (buy/sell) -----------------------------------

    def _enter_trade(self, coin_idx):
        self.selected_coin = coin_idx
        self.mode = "trade"
        play_sfx("select")
        self._render_hud()
        self._render_trade_view()

    def _exit_trade(self):
        self.selected_coin = -1
        self.mode = "market"
        play_sfx("select")
        self._render_market()

    def _render_trade_view(self):
        if self.selected_coin < 0:
            return
        i = self.selected_coin
        coin = COINS[i]
        price = self.prices[i]
        qty = self.portfolio[i]

        # Row 0: BUY buttons
        buy_amounts = [1, 10, 100, -1]  # -1 = MAX
        buy_labels = ["BUY 1", "BUY 10", "BUY\n100", "BUY\nMAX"]
        for j, (amt, label) in enumerate(zip(buy_amounts, buy_labels)):
            if amt == -1:
                cost = self.cash
                can = self.cash >= price
            else:
                cost = amt * price
                can = self.cash >= cost
            sub = _compact(cost) if amt > 0 else _compact(self.cash)
            self.set_key(rc_to_pos(0, j), render_trade_btn(
                label.split("\n")[0],
                label.split("\n")[1] if "\n" in label else sub,
                "#065f46", "#22c55e", "#86efac", can
            ))

        # Leverage indicator on row 0
        self.set_key(rc_to_pos(0, 4), render_leverage_toggle(self.leverage_on))

        # Coin detail + chart
        self.set_key(rc_to_pos(0, 5), render_coin_detail(coin, price, self.price_history[i]))

        # Holdings info
        val = qty * price
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 12), "OWNED", font=_font(10), fill="#9ca3af", anchor="mt")
        d.text((48, 38), _compact_qty(qty), font=_font(16), fill="white", anchor="mm")
        d.text((48, 62), _compact(val), font=_font(13), fill="#fbbf24", anchor="mm")
        self.set_key(rc_to_pos(0, 6), img)

        self.set_key(rc_to_pos(0, 7), self.img_empty)

        # Row 1: SELL buttons
        sell_amounts = [1, 10, 100, -1]  # -1 = ALL
        sell_labels = ["SELL 1", "SELL 10", "SELL\n100", "SELL\nALL"]
        for j, (amt, label) in enumerate(zip(sell_amounts, sell_labels)):
            if amt == -1:
                can = qty > 0
                sub = _compact_qty(qty) if qty > 0 else "---"
            else:
                can = qty >= amt
                sub = _compact(amt * price)
            self.set_key(rc_to_pos(1, j), render_trade_btn(
                label.split("\n")[0],
                label.split("\n")[1] if "\n" in label else sub,
                "#7f1d1d", "#ef4444", "#fca5a5", can
            ))

        # Fill rest of row 1
        for c in range(4, COLS):
            self.set_key(rc_to_pos(1, c), self.img_empty)

        # Row 2: BACK + other coins quick-select
        self.set_key(rc_to_pos(2, 0), render_back_btn())
        for ci in range(NUM_COINS):
            pos = rc_to_pos(2, ci + 1)
            c = COINS[ci]
            bg = _darken(c["color"], 0.25 if ci == i else 0.1)
            img = Image.new("RGB", SIZE, bg)
            d = ImageDraw.Draw(img)
            d.text((48, 28), c["sym"], font=_font(12), fill=c["color"] if ci != i else "white", anchor="mm")
            d.text((48, 56), _price_str(self.prices[ci]), font=_font(11), fill="#9ca3af", anchor="mm")
            if ci == i:
                d.rectangle([1, 1, 94, 94], outline=c["color"], width=2)
            self.set_key(pos, img)
        # Fill remaining
        for c in range(NUM_COINS + 1, COLS):
            self.set_key(rc_to_pos(2, c), self.img_empty)

    # -- trading logic -------------------------------------------------------

    def _buy_coins(self, coin_idx, amount):
        """Buy `amount` coins. -1 = max affordable."""
        price = self.prices[coin_idx]
        if price <= 0:
            return

        if amount == -1:
            amount = self.cash / price
        if amount <= 0:
            play_sfx("error")
            return

        cost = amount * price

        # Leverage: 2x quantity for same cost
        if self.leverage_on:
            amount *= 2
            self.leverage_on = False  # single use

        if cost > self.cash:
            # Buy what we can
            amount = self.cash / price
            if self.leverage_on:
                amount *= 2
            cost = self.cash

        if amount <= 0 or cost <= 0:
            play_sfx("error")
            return

        # Update average buy price
        old_qty = self.portfolio[coin_idx]
        old_avg = self.avg_buy_price[coin_idx]
        new_qty = old_qty + amount
        if new_qty > 0:
            self.avg_buy_price[coin_idx] = (old_qty * old_avg + amount * price) / new_qty
        self.portfolio[coin_idx] = new_qty
        self.cash -= min(cost, self.cash)
        self.cash = max(0, self.cash)

        play_sfx("buy")
        play_voice("trade")

        if self.mode == "trade":
            self._render_hud()
            self._render_trade_view()

    def _sell_coins(self, coin_idx, amount):
        """Sell `amount` coins. -1 = all."""
        price = self.prices[coin_idx]
        qty = self.portfolio[coin_idx]
        if qty <= 0:
            play_sfx("error")
            return

        if amount == -1:
            amount = qty
        amount = min(amount, qty)
        if amount <= 0:
            play_sfx("error")
            return

        revenue = amount * price

        # Leverage: 2x revenue
        if self.leverage_on:
            revenue *= 2
            self.leverage_on = False

        self.portfolio[coin_idx] -= amount
        if self.portfolio[coin_idx] < 0.0001:
            self.portfolio[coin_idx] = 0.0
            self.avg_buy_price[coin_idx] = 0.0
        self.cash += revenue

        play_sfx("sell")

        if self.mode == "trade":
            self._render_hud()
            self._render_trade_view()

    # -- upgrade logic -------------------------------------------------------

    def _buy_upgrade(self, upgrade_id):
        if upgrade_id == "bot":
            if self.has_bot:
                return  # already owned
            if self.cash < 5000:
                play_sfx("error")
                return
            self.cash -= 5000
            self.has_bot = True
            self.bot_coin = -1  # needs to pick a coin
            play_sfx("unlock")
            play_voice("trade")
            self._show_bot_select()
            return

        elif upgrade_id == "insider":
            if self.has_insider:
                return
            if self.cash < 10000:
                play_sfx("error")
                return
            self.cash -= 10000
            self.has_insider = True
            play_sfx("unlock")
            play_voice("trade")

        elif upgrade_id == "mining":
            if self.has_mining:
                return
            if self.cash < 3000:
                play_sfx("error")
                return
            self.cash -= 3000
            self.has_mining = True
            play_sfx("unlock")
            play_voice("trade")

        elif upgrade_id == "leverage":
            self.leverage_on = not self.leverage_on
            play_sfx("select")

        if self.mode == "market":
            self._render_hud()
            self._render_market_grid()

    def _show_bot_select(self):
        """Show coin selection for bot assignment."""
        self.mode = "bot_select"
        # Clear game area
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        # Show instructions
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 28), "PICK A", font=_font(14), fill="#fbbf24", anchor="mm")
        d.text((48, 56), "COIN", font=_font(14), fill="#fbbf24", anchor="mm")
        self.set_key(rc_to_pos(0, 7), img)

        # Show coins to pick from
        for i in range(NUM_COINS):
            pos = rc_to_pos(1, i)
            coin = COINS[i]
            img = Image.new("RGB", SIZE, _darken(coin["color"], 0.2))
            d = ImageDraw.Draw(img)
            d.text((48, 24), coin["sym"], font=_font(14), fill=coin["color"], anchor="mm")
            d.text((48, 52), _price_str(self.prices[i]), font=_font(13), fill="white", anchor="mm")
            d.text((48, 76), "SELECT", font=_font(10), fill="#22c55e", anchor="mm")
            d.rectangle([2, 2, 93, 93], outline=coin["color"], width=2)
            self.set_key(pos, img)

    def _select_bot_coin(self, coin_idx):
        self.bot_coin = coin_idx
        self.bot_avg[coin_idx] = self.prices[coin_idx]
        self.mode = "market"
        play_sfx("unlock")
        self._render_market()

    # -- key handler ---------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.mode == "idle":
                self._on_idle(key)
            elif self.mode == "market":
                self._on_market(key)
            elif self.mode == "trade":
                self._on_trade(key)
            elif self.mode == "bot_select":
                self._on_bot_select(key)

    def _on_idle(self, key):
        has_save = os.path.exists(SAVE_FILE)
        if has_save:
            r, c = pos_to_rc(key) if key >= 8 else (-1, -1)
            if r == 1 and c == 2:
                self._continue_game()
            elif r == 1 and c == 5:
                self._start_new()
        else:
            r, c = pos_to_rc(key) if key >= 8 else (-1, -1)
            if r == 1 and c == 3:
                self._start_new()

    def _on_market(self, key):
        if key < 8:
            # HUD keys
            if key == 7:
                # View toggle - currently on market, could toggle detail
                # For now this is a no-op since market is the main view
                return
            return

        r, c = pos_to_rc(key)

        # Row 0: Coin tiles (click to trade)
        if r == 0 and 0 <= c < NUM_COINS:
            self._enter_trade(c)
            return

        # Row 1: Portfolio tiles (click to trade that coin)
        if r == 1 and 0 <= c < NUM_COINS:
            self._enter_trade(c)
            return

        # Row 2: Upgrades
        if r == 2:
            if c == 0:
                self._buy_upgrade("bot")
            elif c == 1:
                self._buy_upgrade("insider")
            elif c == 2:
                self._buy_upgrade("mining")
            elif c == 3:
                self._buy_upgrade("leverage")

    def _on_trade(self, key):
        if key < 8:
            if key == 7:
                self._exit_trade()
            return

        r, c = pos_to_rc(key)
        i = self.selected_coin

        # Row 0: BUY buttons (cols 0-3)
        if r == 0:
            if c == 0:
                self._buy_coins(i, 1)
            elif c == 1:
                self._buy_coins(i, 10)
            elif c == 2:
                self._buy_coins(i, 100)
            elif c == 3:
                self._buy_coins(i, -1)  # MAX
            elif c == 4:
                # Leverage toggle in trade view
                self._buy_upgrade("leverage")
                self._render_trade_view()
            return

        # Row 1: SELL buttons (cols 0-3)
        if r == 1:
            if c == 0:
                self._sell_coins(i, 1)
            elif c == 1:
                self._sell_coins(i, 10)
            elif c == 2:
                self._sell_coins(i, 100)
            elif c == 3:
                self._sell_coins(i, -1)  # ALL
            return

        # Row 2: BACK (col 0) or quick-select coin (cols 1-6)
        if r == 2:
            if c == 0:
                self._exit_trade()
            elif 1 <= c <= NUM_COINS:
                self._enter_trade(c - 1)

    def _on_bot_select(self, key):
        if key < 8:
            return
        r, c = pos_to_rc(key)
        if r == 1 and 0 <= c < NUM_COINS:
            self._select_bot_coin(c)

# -- main ------------------------------------------------------------------

def main():
    from StreamDeck.DeviceManager import DeviceManager

    decks = DeviceManager().enumerate()
    deck = None
    for d in decks:
        if d.is_visual():
            deck = d
            break
    if not deck:
        print("No Stream Deck found!")
        sys.exit(1)

    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("CRYPTO TYCOON -- trade your way to $1M!")

    game = CryptoGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nSaving...")
        if game.running:
            game._save_game()
    finally:
        game._cancel_all_timers()
        deck.reset()
        deck.close()
        cleanup_sfx()

if __name__ == "__main__":
    main()
