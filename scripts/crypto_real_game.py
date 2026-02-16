"""Crypto Real — Stream Deck paper trading with real Binance data.

Paper trading (simulated wallet) with live prices from Binance.
Two modes: Spot and Futures. Market, Limit, and Stop-Loss orders.
Runs on Stream Deck XL (32 keys, 4 rows x 8 cols, 96x96 buttons).

Voice pack: HD2 Helldiver

Usage:
    uv run python scripts/crypto_real_game.py
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
TICK_INTERVAL = 5.0
SAVE_FILE = os.path.expanduser("~/.streamdeck-arcade/crypto_real_save.json")
START_CASH = 10000.0
MAX_OPEN_ORDERS = 5
LEVERAGE_OPTIONS = [1, 2, 5, 10]

# -- coin definitions (real Binance pairs) ---------------------------------
COINS = [
    {"sym": "BTC",  "pair": "BTC/USDT",  "color": "#f7931a"},
    {"sym": "ETH",  "pair": "ETH/USDT",  "color": "#627eea"},
    {"sym": "SOL",  "pair": "SOL/USDT",  "color": "#9945ff"},
    {"sym": "DOGE", "pair": "DOGE/USDT", "color": "#c2a633"},
    {"sym": "XRP",  "pair": "XRP/USDT",  "color": "#00aae4"},
    {"sym": "PEPE", "pair": "PEPE/USDT", "color": "#4ca843"},
]
NUM_COINS = len(COINS)
SYMBOLS = [c["pair"] for c in COINS]

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
    ],
    "win": [
        "hd2_helldiver/sounds/DemocracyForAll.mp3",
        "hd2_helldiver/sounds/FreedomNeverSleeps.mp3",
        "hd2_helldiver/sounds/LibertyProsperityDemocracy.mp3",
    ],
    "news": [
        "hd2_helldiver/sounds/FoundSomething.mp3",
        "hd2_helldiver/sounds/Here.mp3",
    ],
    "milestone": [
        "hd2_helldiver/sounds/ForSuperEarth.mp3",
        "hd2_helldiver/sounds/DemocracyHasLanded.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="crypto-real-sfx-")
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

    # order_placed: ascending beep (limit order set)
    s = (_triangle(600, 0.04, v * 0.35) + _triangle(800, 0.04, v * 0.4) +
         _triangle(1000, 0.04, v * 0.45) + _triangle(1200, 0.06, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "order_placed.wav"), s)
    _sfx_cache["order_placed"] = os.path.join(_sfx_dir, "order_placed.wav")

    # order_filled: cha-ching (limit order executed)
    s = (_square(1400, 0.03, v * 0.5, 0.3) + _square(1800, 0.03, v * 0.55, 0.3) +
         _triangle(2200, 0.05, v * 0.6) + _triangle(2600, 0.08, v * 0.55))
    _write_wav(os.path.join(_sfx_dir, "order_filled.wav"), s)
    _sfx_cache["order_filled"] = os.path.join(_sfx_dir, "order_filled.wav")

    # liquidated: dramatic crash
    s = (_square(800, 0.06, v * 0.6, 0.4) + _square(500, 0.08, v * 0.55, 0.4) +
         _square(300, 0.10, v * 0.5, 0.4) + _square(150, 0.15, v * 0.45, 0.3) +
         _square(80, 0.20, v * 0.3, 0.3))
    _write_wav(os.path.join(_sfx_dir, "liquidated.wav"), s)
    _sfx_cache["liquidated"] = os.path.join(_sfx_dir, "liquidated.wav")

    # crash: price crash sound
    s = (_square(600, 0.05, v * 0.5, 0.4) + _square(400, 0.06, v * 0.45, 0.4) +
         _square(250, 0.08, v * 0.4, 0.4) + _square(150, 0.12, v * 0.35, 0.4))
    _write_wav(os.path.join(_sfx_dir, "crash.wav"), s)
    _sfx_cache["crash"] = os.path.join(_sfx_dir, "crash.wav")

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
        return f"${n:.3f}"
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
    if n == 0:
        return "0"
    if abs(n) < 0.0001:
        return f"{n:.6f}"
    if abs(n) < 0.01:
        return f"{n:.4f}"
    if abs(n) < 1:
        return f"{n:.3f}"
    if abs(n) < 10:
        return f"{n:.2f}"
    if abs(n) < 1000:
        return f"{n:.1f}"
    if abs(n) < 1_000_000:
        return f"{n/1000:.1f}K"
    return f"{n/1_000_000:.1f}M"

def _price_str(p):
    if p < 0.0001:
        return f"${p:.7f}"
    if p < 0.01:
        return f"${p:.5f}"
    if p < 1:
        return f"${p:.3f}"
    if p < 10:
        return f"${p:.2f}"
    if p < 100:
        return f"${p:.1f}"
    if p < 100000:
        return f"${int(p)}"
    return f"${p/1000:.1f}K"

def _pct_str(pct):
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

def _tint_bg(base_rgb, pct, strength=1.0):
    """Tint a background green (up) or red (down) based on percentage move.
    Stronger moves = more saturated tint. Returns RGB tuple."""
    r, g, b = base_rgb
    # Clamp intensity: 0 at ±0%, full at ±10%
    intensity = min(1.0, abs(pct) / 10.0) * strength
    if pct > 0:
        # Green tint
        r = int(r * (1 - intensity * 0.5))
        g = min(255, int(g + (60 * intensity)))
        b = int(b * (1 - intensity * 0.3))
    elif pct < 0:
        # Red tint
        r = min(255, int(r + (60 * intensity)))
        g = int(g * (1 - intensity * 0.5))
        b = int(b * (1 - intensity * 0.3))
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

def render_hud_empty(size=SIZE):
    return Image.new("RGB", size, "#111827")

def render_coin_tile(coin_info, price, pct_24h, trend_dots, size=SIZE):
    up = pct_24h >= 0
    border_color = "#22c55e" if up else "#ef4444"
    base_bg = _darken(coin_info["color"], 0.15)
    bg = _tint_bg(base_bg, pct_24h, strength=0.8)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    # Gradient glow at top edge for strong moves
    intensity = min(1.0, abs(pct_24h) / 8.0)
    if intensity > 0.15:
        glow_color = (0, int(80 * intensity), 0) if up else (int(80 * intensity), 0, 0)
        for row in range(int(20 * intensity)):
            alpha = 1.0 - row / (20 * intensity)
            gc = tuple(int(c * alpha) for c in glow_color)
            blended = tuple(min(255, bg_c + gc_c) for bg_c, gc_c in zip(bg, gc))
            d.line([(0, row), (95, row)], fill=blended)
    d.rectangle([1, 1, 94, 94], outline=border_color, width=2)
    d.text((48, 12), coin_info["sym"], font=_font(13), fill=coin_info["color"], anchor="mt")
    ptxt = _price_str(price)
    pfsz = 14 if len(ptxt) <= 7 else 11 if len(ptxt) <= 9 else 9
    d.text((48, 36), ptxt, font=_font(pfsz), fill="white", anchor="mm")

    arrow = "^" if up else "v"
    pct_color = "#22c55e" if up else "#ef4444"
    d.text((48, 56), f"{arrow}{abs(pct_24h):.1f}%", font=_font(11), fill=pct_color, anchor="mm")

    # Mini trend dots
    if len(trend_dots) >= 2:
        dot_y = 78
        pts = trend_dots[-5:]
        dot_start_x = 48 - (len(pts) - 1) * 6
        for j, hp in enumerate(pts):
            x = dot_start_x + j * 12
            dot_col = "#22c55e" if j > 0 and hp >= pts[j - 1] else "#ef4444"
            if j == 0:
                dot_col = "#6b7280"
            d.ellipse([x - 3, dot_y - 3, x + 3, dot_y + 3], fill=dot_col)

    return img

def render_position_tile(coin_info, qty, value, pct_change, mode="spot", size=SIZE):
    if qty == 0:
        bg = _darken(coin_info["color"], 0.08)
        img = Image.new("RGB", size, bg)
        d = ImageDraw.Draw(img)
        d.text((48, 28), coin_info["sym"], font=_font(12), fill="#4b5563", anchor="mm")
        d.text((48, 52), "---", font=_font(14), fill="#374151", anchor="mm")
        d.text((48, 74), "NONE", font=_font(9), fill="#374151", anchor="mm")
        return img

    base_bg = _darken(coin_info["color"], 0.18)
    bg = _tint_bg(base_bg, pct_change, strength=0.9)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 10), coin_info["sym"], font=_font(11), fill=coin_info["color"], anchor="mt")
    d.text((48, 30), _compact_qty(qty), font=_font(13), fill="white", anchor="mm")
    d.text((48, 50), _compact(value), font=_font(12), fill="#fbbf24", anchor="mm")

    pl_color = "#22c55e" if pct_change >= 0 else "#ef4444"
    d.text((48, 70), _pct_str(pct_change), font=_font(11), fill=pl_color, anchor="mm")

    if mode == "futures":
        d.text((48, 86), "FUT", font=_font(8), fill="#60a5fa", anchor="mm")

    return img

def render_hud_cash(amount, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "USDT", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), _compact(amount), font=_font(18), fill="#22c55e", anchor="mm")
    return img

def render_hud_portfolio(val, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "TOTAL", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), _compact(val), font=_font(16), fill="#fbbf24", anchor="mm")
    return img

def render_hud_pnl(pct, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "P/L", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#22c55e" if pct >= 0 else "#ef4444"
    d.text((48, 48), _pct_str(pct), font=_font(18), fill=color, anchor="mm")
    return img

def render_hud_mode(mode, size=SIZE):
    bg = "#1e3a5f" if mode == "spot" else "#4c1d28"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "MODE", font=_font(10), fill="#9ca3af", anchor="mt")
    label = "SPOT" if mode == "spot" else "FUTURES"
    color = "#60a5fa" if mode == "spot" else "#f87171"
    d.text((48, 42), label, font=_font(16), fill=color, anchor="mm")
    d.text((48, 68), "TAP", font=_font(9), fill="#6b7280", anchor="mm")
    return img

def render_hud_orders(count, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "ORDERS", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#fbbf24" if count > 0 else "#374151"
    d.text((48, 48), str(count), font=_font(22), fill=color, anchor="mm")
    return img

def render_hud_status(online, last_update, tick_count=0, size=SIZE):
    bg = "#111827"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if online:
        # Pulse dot: alternates bright/dim each tick
        dot_color = "#22c55e" if tick_count % 2 == 0 else "#15803d"
        d.text((48, 10), "LIVE", font=_font(12), fill="#22c55e", anchor="mt")
        d.ellipse([40, 30, 56, 46], fill=dot_color)
        if last_update:
            elapsed = int(time.time() - last_update)
            d.text((48, 56), f"{elapsed}s ago", font=_font(9), fill="#6b7280", anchor="mm")
        d.text((48, 74), f"#{tick_count}", font=_font(10), fill="#374151", anchor="mm")
    else:
        d.text((48, 14), "OFFLINE", font=_font(11), fill="#ef4444", anchor="mt")
        d.ellipse([40, 38, 56, 54], fill="#ef4444")
        d.text((48, 68), "RETRY...", font=_font(9), fill="#6b7280", anchor="mm")
    return img

def render_order_tile(order, coins, prices, size=SIZE):
    coin = coins[order["symbol_idx"]]
    bg = _darken(coin["color"], 0.15)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)

    side_color = "#22c55e" if order["side"] == "buy" else "#ef4444"
    type_label = "LMT" if order["type"] == "limit" else "STP"
    d.text((48, 8), f"{type_label} {order['side'].upper()}", font=_font(10), fill=side_color, anchor="mt")
    d.text((48, 26), coin["sym"], font=_font(12), fill=coin["color"], anchor="mm")
    d.text((48, 44), f"@{_price_str(order['price'])}", font=_font(10), fill="white", anchor="mm")
    d.text((48, 60), _compact(order["amount_usdt"]), font=_font(10), fill="#fbbf24", anchor="mm")

    # Distance from current price
    current = prices.get(coin["pair"], 0)
    if current > 0:
        dist = (order["price"] - current) / current * 100
        d.text((48, 78), f"{dist:+.1f}%", font=_font(9), fill="#9ca3af", anchor="mm")

    d.text((48, 90), "CANCEL", font=_font(7), fill="#f87171", anchor="mm")
    d.rectangle([1, 1, 94, 94], outline=side_color, width=1)
    return img

def render_stats_tile(label, value, color="#fbbf24", size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 18), label, font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(value), font=_font(16), fill=color, anchor="mm")
    return img

def render_orders_btn(size=SIZE):
    img = Image.new("RGB", size, "#1e3a5f")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "VIEW", font=_font(12), fill="white", anchor="mm")
    d.text((48, 52), "ORDERS", font=_font(11), fill="#60a5fa", anchor="mm")
    return img

def render_trade_btn(label, sub, bg_color, text_color="white", sub_color="#9ca3af",
                     can_afford=True, size=SIZE):
    bg = bg_color if can_afford else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    fill = text_color if can_afford else "#4b5563"
    sfill = sub_color if can_afford else "#374151"
    d.text((48, 28), label, font=_font(14), fill=fill, anchor="mm")
    d.text((48, 56), sub, font=_font(11), fill=sfill, anchor="mm")
    if can_afford:
        d.rectangle([1, 1, 94, 94], outline=text_color, width=1)
    return img

def render_coin_detail(coin_info, price, ohlcv_data, size=SIZE):
    bg = _darken(coin_info["color"], 0.12)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 10), coin_info["sym"], font=_font(14), fill=coin_info["color"], anchor="mt")
    d.text((48, 32), _price_str(price), font=_font(16), fill="white", anchor="mm")

    # Mini chart from OHLCV closes
    if ohlcv_data and len(ohlcv_data) >= 2:
        closes = [c[4] for c in ohlcv_data if c and len(c) > 4]
        if len(closes) >= 2:
            mn = min(closes)
            mx = max(closes)
            rng = mx - mn if mx > mn else 1.0
            chart_y_top = 50
            chart_y_bot = 88
            chart_x_left = 8
            chart_x_right = 88
            step = (chart_x_right - chart_x_left) / max(1, len(closes) - 1)
            points = []
            for idx, c in enumerate(closes):
                x = chart_x_left + idx * step
                y = chart_y_bot - (c - mn) / rng * (chart_y_bot - chart_y_top)
                points.append((x, y))
            for idx in range(1, len(points)):
                col = "#22c55e" if closes[idx] >= closes[idx - 1] else "#ef4444"
                d.line([points[idx - 1], points[idx]], fill=col, width=2)
    return img

def render_order_type_btn(order_type, size=SIZE):
    labels = {"market": "MKT", "limit": "LMT", "stop": "STOP"}
    colors = {"market": "#22c55e", "limit": "#60a5fa", "stop": "#f87171"}
    img = Image.new("RGB", size, "#1f2937")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "ORDER", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 42), labels.get(order_type, "MKT"), font=_font(18), fill=colors.get(order_type, "white"), anchor="mm")
    d.text((48, 68), "TAP", font=_font(9), fill="#6b7280", anchor="mm")
    d.rectangle([1, 1, 94, 94], outline=colors.get(order_type, "white"), width=1)
    return img

def render_price_info(bid, ask, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "BID", font=_font(9), fill="#22c55e", anchor="mt")
    d.text((48, 26), _price_str(bid), font=_font(11), fill="#22c55e", anchor="mm")
    d.text((48, 42), "ASK", font=_font(9), fill="#ef4444", anchor="mt")
    d.text((48, 58), _price_str(ask), font=_font(11), fill="#ef4444", anchor="mm")
    spread = abs(ask - bid)
    d.text((48, 78), f"SPR {_price_str(spread)}", font=_font(8), fill="#6b7280", anchor="mm")
    return img

def render_position_info(coin_info, qty, entry, value, pnl_pct, mode="spot", size=SIZE):
    bg = _darken(coin_info["color"], 0.12)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if qty == 0:
        d.text((48, 28), "NO", font=_font(14), fill="#4b5563", anchor="mm")
        d.text((48, 52), "POS", font=_font(14), fill="#4b5563", anchor="mm")
        return img
    d.text((48, 8), "POS", font=_font(9), fill="#9ca3af", anchor="mt")
    d.text((48, 24), _compact_qty(qty), font=_font(12), fill="white", anchor="mm")
    d.text((48, 40), f"@{_price_str(entry)}", font=_font(9), fill="#9ca3af", anchor="mm")
    d.text((48, 56), _compact(value), font=_font(11), fill="#fbbf24", anchor="mm")
    pl_color = "#22c55e" if pnl_pct >= 0 else "#ef4444"
    d.text((48, 72), _pct_str(pnl_pct), font=_font(11), fill=pl_color, anchor="mm")
    return img

def render_leverage_btn(leverage, size=SIZE):
    bg = "#7c2d12" if leverage > 1 else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "LEVER", font=_font(10), fill="#fb923c" if leverage > 1 else "#6b7280", anchor="mt")
    d.text((48, 42), f"{leverage}x", font=_font(20), fill="#fbbf24" if leverage > 1 else "#4b5563", anchor="mm")
    d.text((48, 68), "TAP", font=_font(9), fill="#6b7280", anchor="mm")
    if leverage > 1:
        d.rectangle([1, 1, 94, 94], outline="#fb923c", width=2)
    return img

def render_liq_price(liq_price, current_price, size=SIZE):
    img = Image.new("RGB", size, "#1f2937")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "LIQ", font=_font(10), fill="#f87171", anchor="mt")
    if liq_price > 0:
        d.text((48, 36), _price_str(liq_price), font=_font(12), fill="#f87171", anchor="mm")
        if current_price > 0:
            dist = abs(current_price - liq_price) / current_price * 100
            d.text((48, 58), f"{dist:.1f}%", font=_font(10), fill="#fbbf24", anchor="mm")
            d.text((48, 76), "away", font=_font(9), fill="#6b7280", anchor="mm")
    else:
        d.text((48, 48), "N/A", font=_font(14), fill="#374151", anchor="mm")
    return img

def render_long_short_btn(side, size=SIZE):
    bg = "#065f46" if side == "long" else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    label = "LONG" if side == "long" else "SHORT"
    color = "#22c55e" if side == "long" else "#ef4444"
    d.text((48, 28), label, font=_font(16), fill=color, anchor="mm")
    d.text((48, 56), "TAP", font=_font(10), fill="#6b7280", anchor="mm")
    d.rectangle([1, 1, 94, 94], outline=color, width=2)
    return img

def render_limit_price_btn(label, sub, bg_color, text_color="white", size=SIZE):
    img = Image.new("RGB", size, bg_color)
    d = ImageDraw.Draw(img)
    d.text((48, 28), label, font=_font(14), fill=text_color, anchor="mm")
    d.text((48, 56), sub, font=_font(10), fill="#9ca3af", anchor="mm")
    d.rectangle([1, 1, 94, 94], outline=text_color, width=1)
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


# -- Binance data layer ----------------------------------------------------

class BinanceData:
    """Fetches real market data from Binance via ccxt (sync, no auth)."""

    def __init__(self, symbols):
        self.symbols = symbols
        self.exchange = None
        self.prices = {}         # {symbol: last_price}
        self.pct_24h = {}        # {symbol: 24h_change_pct}
        self.bids = {}           # {symbol: best_bid}
        self.asks = {}           # {symbol: best_ask}
        self.price_history = {s: [] for s in symbols}  # last N prices per tick
        self.ohlcv_cache = {}    # {symbol: [[t,o,h,l,c,v], ...]}
        self.online = False
        self.last_update = None
        self.last_retry = 0
        self._init_exchange()

    def _init_exchange(self):
        try:
            import ccxt
            self.exchange = ccxt.binance({
                "enableRateLimit": True,
                "timeout": 5000,
            })
            self.online = True
        except Exception:
            self.exchange = None
            self.online = False

    def fetch_tickers(self):
        """Fetch all 6 coin prices in one API call. Returns True on success."""
        if not self.exchange:
            self._init_exchange()
            if not self.exchange:
                self.online = False
                return False

        try:
            tickers = self.exchange.fetch_tickers(self.symbols)
            for sym in self.symbols:
                t = tickers.get(sym)
                if t:
                    self.prices[sym] = t.get("last", 0) or 0
                    self.pct_24h[sym] = t.get("percentage", 0) or 0
                    self.bids[sym] = t.get("bid", 0) or 0
                    self.asks[sym] = t.get("ask", 0) or 0
                    self.price_history[sym].append(self.prices[sym])
                    if len(self.price_history[sym]) > 50:
                        self.price_history[sym] = self.price_history[sym][-50:]
            self.online = True
            self.last_update = time.time()
            return True
        except Exception:
            self.online = False
            return False

    def fetch_ohlcv(self, symbol):
        """Fetch 24h of hourly candles for a coin. Cached."""
        if not self.exchange:
            return self.ohlcv_cache.get(symbol, [])
        try:
            data = self.exchange.fetch_ohlcv(symbol, "1h", limit=24)
            self.ohlcv_cache[symbol] = data
            return data
        except Exception:
            return self.ohlcv_cache.get(symbol, [])

    def get_price(self, symbol):
        return self.prices.get(symbol, 0)

    def get_pct_24h(self, symbol):
        return self.pct_24h.get(symbol, 0)


# -- game ------------------------------------------------------------------

class CryptoRealGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.view = "idle"  # idle | market | trade | limit_set
        self.tick_timer = None
        self.timers = []
        self.tick_count = 0

        # Data layer
        self.data = BinanceData(SYMBOLS)

        # Trading mode
        self.trade_mode = "spot"  # spot | futures

        # Spot positions: {symbol: {"qty": float, "avg_price": float}}
        self.cash = START_CASH
        self.positions = {}
        for sym in SYMBOLS:
            self.positions[sym] = {"qty": 0.0, "avg_price": 0.0}

        # Futures positions: {symbol: {"side": "long"/"short", "qty": float, "entry": float, "leverage": int}}
        self.futures_positions = {}

        # Orders
        self.open_orders = []
        self._next_order_id = 1

        # Trade view state
        self.selected_coin = -1
        self.order_type = "market"  # market | limit | stop
        self.futures_side = "long"  # long | short
        self.leverage = 1
        self.limit_price = 0.0  # for limit/stop order setting

        # Stats
        self.total_trades = 0
        self.best_trade_pct = 0.0

        # Pre-render
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

    # -- portfolio value -----------------------------------------------------

    def _portfolio_value(self):
        """Total value: cash + spot positions + futures unrealized P/L."""
        total = self.cash
        for sym in SYMBOLS:
            pos = self.positions.get(sym, {})
            qty = pos.get("qty", 0)
            price = self.data.get_price(sym)
            total += qty * price

        # Futures unrealized P/L
        for sym, fpos in self.futures_positions.items():
            price = self.data.get_price(sym)
            if price > 0 and fpos.get("qty", 0) > 0:
                entry = fpos["entry"]
                lev = fpos.get("leverage", 1)
                notional = fpos["qty"] * entry
                if fpos["side"] == "long":
                    pnl = (price - entry) / entry * notional * lev
                else:
                    pnl = (entry - price) / entry * notional * lev
                total += pnl
        return total

    def _spot_value(self):
        total = 0.0
        for sym in SYMBOLS:
            pos = self.positions.get(sym, {})
            qty = pos.get("qty", 0)
            price = self.data.get_price(sym)
            total += qty * price
        return total

    # -- save / load ---------------------------------------------------------

    def _save_game(self):
        data = {
            "mode": self.trade_mode,
            "cash": self.cash,
            "positions": self.positions,
            "futures_positions": self.futures_positions,
            "open_orders": self.open_orders,
            "total_trades": self.total_trades,
            "best_trade_pct": self.best_trade_pct,
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
            self.trade_mode = data.get("mode", "spot")
            self.cash = data.get("cash", START_CASH)
            self.positions = data.get("positions", {})
            # Ensure all symbols exist
            for sym in SYMBOLS:
                if sym not in self.positions:
                    self.positions[sym] = {"qty": 0.0, "avg_price": 0.0}
            self.futures_positions = data.get("futures_positions", {})
            self.open_orders = data.get("open_orders", [])
            self.total_trades = data.get("total_trades", 0)
            self.best_trade_pct = data.get("best_trade_pct", 0.0)
            if self.open_orders:
                self._next_order_id = max(o.get("id", 0) for o in self.open_orders) + 1
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
        self.view = "idle"
        self._cancel_all_timers()
        has_save = os.path.exists(SAVE_FILE)

        # HUD row
        self.set_key(1, render_title_tile("CRYPTO", "REAL"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())

        if has_save:
            self._load_save()
            self.set_key(2, render_title_tile("WALLET", _compact(self.cash), "#22c55e"))

        # Game area - clear
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        # Coin logos
        for i, coin in enumerate(COINS):
            pos = rc_to_pos(0, i + 1)
            img = Image.new("RGB", SIZE, _darken(coin["color"], 0.15))
            d = ImageDraw.Draw(img)
            d.text((48, 28), coin["sym"], font=_font(14), fill=coin["color"], anchor="mm")
            d.text((48, 56), "LIVE", font=_font(10), fill="#22c55e", anchor="mm")
            self.set_key(pos, img)

        if has_save:
            self.set_key(rc_to_pos(1, 2), render_trade_btn("CONT", "INUE", "#1e40af", "white", "#93c5fd"))
            self.set_key(rc_to_pos(1, 5), render_start_btn())
        else:
            self.set_key(rc_to_pos(1, 3), render_start_btn())

        # Binance branding
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 28), "BINANCE", font=_font(11), fill="#f0b90b", anchor="mm")
        d.text((48, 52), "LIVE", font=_font(11), fill="#22c55e", anchor="mm")
        self.set_key(rc_to_pos(2, 3), img)

    # -- start / continue ----------------------------------------------------

    def _start_new(self):
        self._delete_save()
        self.cash = START_CASH
        self.trade_mode = "spot"
        self.positions = {}
        for sym in SYMBOLS:
            self.positions[sym] = {"qty": 0.0, "avg_price": 0.0}
        self.futures_positions = {}
        self.open_orders = []
        self._next_order_id = 1
        self.total_trades = 0
        self.best_trade_pct = 0.0
        self.selected_coin = -1
        self.order_type = "market"
        self.futures_side = "long"
        self.leverage = 1
        self.tick_count = 0
        self._begin_play()

    def _continue_game(self):
        self._load_save()
        self.selected_coin = -1
        self.order_type = "market"
        self.futures_side = "long"
        self.leverage = 1
        self.tick_count = 0
        self._begin_play()

    def _begin_play(self):
        self.running = True
        self.view = "market"
        play_sfx("start")
        play_voice("start")

        # Initial data fetch
        self.data.fetch_tickers()

        self._render_market()
        self._schedule_tick()

    def _schedule_tick(self):
        if not self.running:
            return
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    # -- tick ----------------------------------------------------------------

    def _tick(self):
        if not self.running:
            return

        # Network fetch OUTSIDE lock — key presses stay responsive
        success = self.data.fetch_tickers()

        if not success and time.time() - (self.data.last_retry or 0) > 30:
            self.data._init_exchange()
            self.data.last_retry = time.time()

        if not self.running:
            return

        with self.lock:
            self.tick_count += 1

            # Check open orders
            self._check_orders()

            # Check futures liquidations
            self._check_liquidations()

            # Auto-save every 10 ticks
            if self.tick_count % 10 == 0:
                self._save_game()

            # Update display
            if self.view == "market":
                self._render_hud()
                self._render_market_grid()
            elif self.view == "trade":
                self._render_hud()
                self._render_trade_view()
            elif self.view == "limit_set":
                self._render_hud()
                self._render_limit_set_view()

        self._schedule_tick()

    # -- order checking ------------------------------------------------------

    def _check_orders(self):
        filled = []
        for order in self.open_orders:
            sym = SYMBOLS[order["symbol_idx"]]
            price = self.data.get_price(sym)
            if price <= 0:
                continue

            if order["type"] == "limit":
                if order["side"] == "buy" and price <= order["price"]:
                    self._execute_limit_order(order, price)
                    filled.append(order)
                elif order["side"] == "sell" and price >= order["price"]:
                    self._execute_limit_order(order, price)
                    filled.append(order)
            elif order["type"] == "stop":
                if order["side"] == "sell" and price <= order["price"]:
                    self._execute_limit_order(order, price)
                    filled.append(order)
                elif order["side"] == "buy" and price >= order["price"]:
                    self._execute_limit_order(order, price)
                    filled.append(order)

        for order in filled:
            if order in self.open_orders:
                self.open_orders.remove(order)

    def _execute_limit_order(self, order, exec_price):
        sym = SYMBOLS[order["symbol_idx"]]
        amount_usdt = order["amount_usdt"]

        if order["side"] == "buy":
            qty = amount_usdt / exec_price
            pos = self.positions[sym]
            old_qty = pos["qty"]
            old_avg = pos["avg_price"]
            new_qty = old_qty + qty
            if new_qty > 0:
                pos["avg_price"] = (old_qty * old_avg + qty * exec_price) / new_qty
            pos["qty"] = new_qty
        else:
            qty = amount_usdt / exec_price
            pos = self.positions[sym]
            sell_qty = min(qty, pos["qty"])
            if sell_qty > 0:
                revenue = sell_qty * exec_price
                self.cash += revenue
                pos["qty"] -= sell_qty
                if pos["qty"] < 0.0000001:
                    pos["qty"] = 0.0
                    pos["avg_price"] = 0.0

        self.total_trades += 1
        play_sfx("order_filled")
        play_voice("trade")
        self._save_game()

    def _check_liquidations(self):
        to_liquidate = []
        for sym, fpos in self.futures_positions.items():
            if fpos.get("qty", 0) <= 0:
                continue
            price = self.data.get_price(sym)
            if price <= 0:
                continue
            liq = self._calc_liq_price(fpos)
            if liq <= 0:
                continue
            if fpos["side"] == "long" and price <= liq:
                to_liquidate.append(sym)
            elif fpos["side"] == "short" and price >= liq:
                to_liquidate.append(sym)

        for sym in to_liquidate:
            fpos = self.futures_positions[sym]
            # Liquidation: lose the margin
            margin = fpos["qty"] * fpos["entry"]
            # Margin is already deducted from cash at open, so just remove position
            self.futures_positions[sym] = {"side": "long", "qty": 0, "entry": 0, "leverage": 1}
            play_sfx("liquidated")
            play_voice("crash")

    def _calc_liq_price(self, fpos):
        if fpos.get("qty", 0) <= 0 or fpos.get("leverage", 1) <= 1:
            return 0
        entry = fpos["entry"]
        lev = fpos["leverage"]
        # Simplified liquidation: lose when move = 1/leverage
        if fpos["side"] == "long":
            return entry * (1 - 1.0 / lev)
        else:
            return entry * (1 + 1.0 / lev)

    # -- rendering: HUD (keys 1-7) ------------------------------------------

    def _render_hud(self):
        self.set_key(1, render_hud_cash(self.cash))
        self.set_key(2, render_hud_portfolio(self._portfolio_value()))

        pnl_pct = (self._portfolio_value() - START_CASH) / START_CASH * 100
        self.set_key(3, render_hud_pnl(pnl_pct))
        self.set_key(4, render_hud_mode(self.trade_mode))
        self.set_key(5, render_hud_orders(len(self.open_orders)))
        self.set_key(6, render_hud_status(self.data.online, self.data.last_update, self.tick_count))
        self.set_key(7, render_hud_empty())

    # -- rendering: market view (keys 8-31) ----------------------------------

    def _render_market(self):
        self._render_hud()
        self._render_market_grid()

    def _render_market_grid(self):
        # Row 0 (keys 8-13): 6 coin tiles
        for i in range(NUM_COINS):
            sym = SYMBOLS[i]
            price = self.data.get_price(sym)
            pct = self.data.get_pct_24h(sym)
            history = self.data.price_history.get(sym, [])
            self.set_key(rc_to_pos(0, i), render_coin_tile(COINS[i], price, pct, history))

        # Keys 14-15: empty
        for c in range(NUM_COINS, COLS):
            self.set_key(rc_to_pos(0, c), self.img_empty)

        # Row 1 (keys 16-21): position tiles
        for i in range(NUM_COINS):
            sym = SYMBOLS[i]
            price = self.data.get_price(sym)

            if self.trade_mode == "spot":
                pos = self.positions.get(sym, {})
                qty = pos.get("qty", 0)
                val = qty * price
                avg = pos.get("avg_price", 0)
                pct = (price - avg) / avg * 100 if avg > 0 and qty > 0 else 0.0
                self.set_key(rc_to_pos(1, i), render_position_tile(COINS[i], qty, val, pct, "spot"))
            else:
                fpos = self.futures_positions.get(sym, {})
                qty = fpos.get("qty", 0)
                entry = fpos.get("entry", 0)
                lev = fpos.get("leverage", 1)
                side = fpos.get("side", "long")
                if qty > 0 and entry > 0 and price > 0:
                    notional = qty * entry
                    if side == "long":
                        pnl_pct = (price - entry) / entry * 100 * lev
                    else:
                        pnl_pct = (entry - price) / entry * 100 * lev
                    val = notional + (notional * pnl_pct / 100)
                else:
                    pnl_pct = 0.0
                    val = 0.0
                    qty = 0
                self.set_key(rc_to_pos(1, i), render_position_tile(COINS[i], qty, val, pnl_pct, "futures"))

        # Keys 22-23: empty
        for c in range(NUM_COINS, COLS):
            self.set_key(rc_to_pos(1, c), self.img_empty)

        # Row 2 (keys 24-31): orders + stats
        # Keys 24-26: open orders (up to 3)
        for j in range(3):
            pos = rc_to_pos(2, j)
            if j < len(self.open_orders):
                self.set_key(pos, render_order_tile(
                    self.open_orders[j], COINS, self.data.prices))
            else:
                self.set_key(pos, self.img_empty)

        # Key 27: orders button
        self.set_key(rc_to_pos(2, 3), render_orders_btn())

        # Key 28-29: stats
        self.set_key(rc_to_pos(2, 4), render_stats_tile("BEST", f"{self.best_trade_pct:.1f}%", "#22c55e"))
        self.set_key(rc_to_pos(2, 5), render_stats_tile("TRADES", str(self.total_trades), "#60a5fa"))

        # Keys 30-31: empty
        for c in range(6, COLS):
            self.set_key(rc_to_pos(2, c), self.img_empty)

    # -- rendering: trade view -----------------------------------------------

    def _enter_trade(self, coin_idx):
        self.selected_coin = coin_idx
        self.view = "trade"
        self.order_type = "market"
        play_sfx("select")

        # Fetch OHLCV for chart
        sym = SYMBOLS[coin_idx]
        threading.Thread(target=self.data.fetch_ohlcv, args=(sym,), daemon=True).start()

        self._render_hud()
        self._render_trade_view()

    def _exit_trade(self):
        self.selected_coin = -1
        self.view = "market"
        play_sfx("select")
        self._render_market()

    def _render_trade_view(self):
        if self.selected_coin < 0:
            return
        i = self.selected_coin
        coin = COINS[i]
        sym = SYMBOLS[i]
        price = self.data.get_price(sym)

        # Row 0:
        # Key 8: Order type selector
        self.set_key(rc_to_pos(0, 0), render_order_type_btn(self.order_type))

        # Key 9-10: Price info (bid/ask)
        bid = self.data.bids.get(sym, price)
        ask = self.data.asks.get(sym, price)
        self.set_key(rc_to_pos(0, 1), render_price_info(bid, ask))

        # Key 10 (second price tile): empty or extra info
        self.set_key(rc_to_pos(0, 2), self.img_empty)

        # Key 11: 24h chart
        ohlcv = self.data.ohlcv_cache.get(sym, [])
        self.set_key(rc_to_pos(0, 3), render_coin_detail(coin, price, ohlcv))

        # Key 12-13: Current position info
        if self.trade_mode == "spot":
            pos = self.positions.get(sym, {})
            qty = pos.get("qty", 0)
            entry = pos.get("avg_price", 0)
            val = qty * price
            pnl = (price - entry) / entry * 100 if entry > 0 and qty > 0 else 0.0
            self.set_key(rc_to_pos(0, 4), render_position_info(coin, qty, entry, val, pnl, "spot"))
            self.set_key(rc_to_pos(0, 5), self.img_empty)
        else:
            fpos = self.futures_positions.get(sym, {})
            qty = fpos.get("qty", 0)
            entry = fpos.get("entry", 0)
            lev = fpos.get("leverage", 1)
            side = fpos.get("side", "long")
            if qty > 0 and entry > 0 and price > 0:
                notional = qty * entry
                if side == "long":
                    pnl = (price - entry) / entry * 100 * lev
                else:
                    pnl = (entry - price) / entry * 100 * lev
                val = notional + (notional * pnl / 100)
            else:
                pnl = 0.0
                val = 0.0
            self.set_key(rc_to_pos(0, 4), render_position_info(coin, qty, entry, val, pnl, "futures"))
            liq = self._calc_liq_price(fpos) if qty > 0 else 0
            self.set_key(rc_to_pos(0, 5), render_liq_price(liq, price))

        # Key 14-15: Futures leverage / liq
        if self.trade_mode == "futures":
            self.set_key(rc_to_pos(0, 6), render_leverage_btn(self.leverage))
            self.set_key(rc_to_pos(0, 7), self.img_empty)
        else:
            self.set_key(rc_to_pos(0, 6), self.img_empty)
            self.set_key(rc_to_pos(0, 7), self.img_empty)

        # Row 1: BUY buttons (keys 16-19) + SELL buttons (keys 20-23)
        buy_pcts = [0.10, 0.25, 0.50, 1.0]
        buy_labels = ["10%", "25%", "50%", "100%"]
        for j, (pct, label) in enumerate(zip(buy_pcts, buy_labels)):
            amount = self.cash * pct
            can = amount > 0 and price > 0
            self.set_key(rc_to_pos(1, j), render_trade_btn(
                "BUY", label, "#065f46", "#22c55e", "#86efac", can))

        sell_pcts = [0.10, 0.25, 0.50, 1.0]
        sell_labels = ["10%", "25%", "50%", "ALL"]
        for j, (pct, label) in enumerate(zip(sell_pcts, sell_labels)):
            if self.trade_mode == "spot":
                pos = self.positions.get(sym, {})
                qty = pos.get("qty", 0)
                can = qty > 0
            else:
                fpos = self.futures_positions.get(sym, {})
                can = fpos.get("qty", 0) > 0
            self.set_key(rc_to_pos(1, 4 + j), render_trade_btn(
                "SELL", label, "#7f1d1d", "#ef4444", "#fca5a5", can))

        # Row 2: BACK + quick coin select + futures toggle
        self.set_key(rc_to_pos(2, 0), render_back_btn())

        # Quick coin select (other 5 coins in keys 25-30)
        other_coins = [ci for ci in range(NUM_COINS) if ci != i]
        for j, ci in enumerate(other_coins):
            pos = rc_to_pos(2, 1 + j)
            c = COINS[ci]
            bg = _darken(c["color"], 0.12)
            img = Image.new("RGB", SIZE, bg)
            d = ImageDraw.Draw(img)
            d.text((48, 28), c["sym"], font=_font(12), fill=c["color"], anchor="mm")
            cp = self.data.get_price(SYMBOLS[ci])
            d.text((48, 56), _price_str(cp), font=_font(10), fill="#9ca3af", anchor="mm")
            self.set_key(pos, img)

        # Fill remaining quick-select slots
        for j in range(len(other_coins), 6):
            self.set_key(rc_to_pos(2, 1 + j), self.img_empty)

        # Key 31: LONG/SHORT toggle (futures only)
        if self.trade_mode == "futures":
            self.set_key(rc_to_pos(2, 7), render_long_short_btn(self.futures_side))
        else:
            self.set_key(rc_to_pos(2, 7), self.img_empty)

    # -- rendering: limit/stop price setting ---------------------------------

    def _enter_limit_set(self):
        if self.selected_coin < 0:
            return
        sym = SYMBOLS[self.selected_coin]
        price = self.data.get_price(sym)
        if price <= 0:
            play_sfx("error")
            return
        self.limit_price = price
        self.view = "limit_set"
        self._render_hud()
        self._render_limit_set_view()

    def _render_limit_set_view(self):
        if self.selected_coin < 0:
            return
        i = self.selected_coin
        coin = COINS[i]
        sym = SYMBOLS[i]
        price = self.data.get_price(sym)

        # Row 0: same as trade view top section
        self.set_key(rc_to_pos(0, 0), render_order_type_btn(self.order_type))
        bid = self.data.bids.get(sym, price)
        ask = self.data.asks.get(sym, price)
        self.set_key(rc_to_pos(0, 1), render_price_info(bid, ask))
        self.set_key(rc_to_pos(0, 2), self.img_empty)
        ohlcv = self.data.ohlcv_cache.get(sym, [])
        self.set_key(rc_to_pos(0, 3), render_coin_detail(coin, price, ohlcv))

        # Show limit price
        pct_diff = (self.limit_price - price) / price * 100 if price > 0 else 0
        img = Image.new("RGB", SIZE, "#1e3a5f")
        d = ImageDraw.Draw(img)
        d.text((48, 8), "TARGET", font=_font(9), fill="#9ca3af", anchor="mt")
        d.text((48, 32), _price_str(self.limit_price), font=_font(14), fill="#fbbf24", anchor="mm")
        d.text((48, 56), f"{pct_diff:+.1f}%", font=_font(11), fill="#60a5fa", anchor="mm")
        d.text((48, 76), "from now", font=_font(8), fill="#6b7280", anchor="mm")
        d.rectangle([1, 1, 94, 94], outline="#60a5fa", width=2)
        self.set_key(rc_to_pos(0, 4), img)

        for c in range(5, COLS):
            self.set_key(rc_to_pos(0, c), self.img_empty)

        # Row 1: [-5%] [-1%] [PRICE] [+1%] [+5%] [SET BUY] [SET SELL] [CANCEL]
        self.set_key(rc_to_pos(1, 0), render_limit_price_btn("-5%", "", "#374151", "#f87171"))
        self.set_key(rc_to_pos(1, 1), render_limit_price_btn("-1%", "", "#374151", "#fca5a5"))

        # Current target price display
        img = Image.new("RGB", SIZE, "#1f2937")
        d = ImageDraw.Draw(img)
        d.text((48, 20), "PRICE", font=_font(10), fill="#9ca3af", anchor="mt")
        d.text((48, 48), _price_str(self.limit_price), font=_font(14), fill="#fbbf24", anchor="mm")
        d.text((48, 72), coin["sym"], font=_font(10), fill=coin["color"], anchor="mm")
        self.set_key(rc_to_pos(1, 2), img)

        self.set_key(rc_to_pos(1, 3), render_limit_price_btn("+1%", "", "#374151", "#86efac"))
        self.set_key(rc_to_pos(1, 4), render_limit_price_btn("+5%", "", "#374151", "#22c55e"))
        self.set_key(rc_to_pos(1, 5), render_limit_price_btn("SET", "BUY", "#065f46", "#22c55e"))
        self.set_key(rc_to_pos(1, 6), render_limit_price_btn("SET", "SELL", "#7f1d1d", "#ef4444"))
        self.set_key(rc_to_pos(1, 7), render_limit_price_btn("BACK", "", "#374151", "#f87171"))

        # Row 2: clear
        for c in range(COLS):
            self.set_key(rc_to_pos(2, c), self.img_empty)

    # -- trading logic -------------------------------------------------------

    def _buy_spot(self, coin_idx, pct_of_wallet):
        sym = SYMBOLS[coin_idx]
        price = self.data.get_price(sym)
        if price <= 0:
            play_sfx("error")
            return

        amount_usdt = self.cash * pct_of_wallet
        if amount_usdt < 0.01:
            play_sfx("error")
            return

        qty = amount_usdt / price
        pos = self.positions[sym]
        old_qty = pos["qty"]
        old_avg = pos["avg_price"]
        new_qty = old_qty + qty
        if new_qty > 0:
            pos["avg_price"] = (old_qty * old_avg + qty * price) / new_qty
        pos["qty"] = new_qty
        self.cash -= amount_usdt
        self.cash = max(0, self.cash)

        self.total_trades += 1
        play_sfx("buy")
        play_voice("trade")
        self._save_game()

        if self.view == "trade":
            self._render_hud()
            self._render_trade_view()

    def _sell_spot(self, coin_idx, pct_of_holding):
        sym = SYMBOLS[coin_idx]
        price = self.data.get_price(sym)
        pos = self.positions[sym]
        qty = pos["qty"]
        if qty <= 0 or price <= 0:
            play_sfx("error")
            return

        sell_qty = qty * pct_of_holding
        if sell_qty <= 0:
            play_sfx("error")
            return

        revenue = sell_qty * price
        avg = pos["avg_price"]

        # Track best trade
        if avg > 0:
            trade_pct = (price - avg) / avg * 100
            if trade_pct > self.best_trade_pct:
                self.best_trade_pct = trade_pct

        pos["qty"] -= sell_qty
        if pos["qty"] < 0.0000001:
            pos["qty"] = 0.0
            pos["avg_price"] = 0.0
        self.cash += revenue

        self.total_trades += 1
        play_sfx("sell")
        self._save_game()

        if self.view == "trade":
            self._render_hud()
            self._render_trade_view()

    def _buy_futures(self, coin_idx, pct_of_wallet):
        sym = SYMBOLS[coin_idx]
        price = self.data.get_price(sym)
        if price <= 0:
            play_sfx("error")
            return

        margin = self.cash * pct_of_wallet
        if margin < 0.01:
            play_sfx("error")
            return

        qty = margin / price  # qty in base currency (margin amount)
        existing = self.futures_positions.get(sym, {})

        if existing.get("qty", 0) > 0 and existing.get("side") != self.futures_side:
            # Close existing position first
            self._close_futures(coin_idx)

        # Open new or add to position
        if existing.get("qty", 0) > 0 and existing.get("side") == self.futures_side:
            # Average into existing
            old_qty = existing["qty"]
            old_entry = existing["entry"]
            new_qty = old_qty + qty
            existing["entry"] = (old_qty * old_entry + qty * price) / new_qty
            existing["qty"] = new_qty
        else:
            self.futures_positions[sym] = {
                "side": self.futures_side,
                "qty": qty,
                "entry": price,
                "leverage": self.leverage,
            }

        self.cash -= margin
        self.cash = max(0, self.cash)
        self.total_trades += 1
        play_sfx("buy")
        play_voice("trade")
        self._save_game()

        if self.view == "trade":
            self._render_hud()
            self._render_trade_view()

    def _sell_futures(self, coin_idx, pct):
        sym = SYMBOLS[coin_idx]
        fpos = self.futures_positions.get(sym, {})
        if fpos.get("qty", 0) <= 0:
            play_sfx("error")
            return

        price = self.data.get_price(sym)
        if price <= 0:
            play_sfx("error")
            return

        close_qty = fpos["qty"] * pct
        entry = fpos["entry"]
        lev = fpos.get("leverage", 1)
        notional = close_qty * entry

        if fpos["side"] == "long":
            pnl = (price - entry) / entry * notional * lev
        else:
            pnl = (entry - price) / entry * notional * lev

        # Return margin + P/L
        margin_back = close_qty * entry + pnl
        self.cash += max(0, margin_back)

        # Track best trade
        trade_pct = pnl / (close_qty * entry) * 100 if close_qty * entry > 0 else 0
        if trade_pct > self.best_trade_pct:
            self.best_trade_pct = trade_pct

        fpos["qty"] -= close_qty
        if fpos["qty"] < 0.0000001:
            fpos["qty"] = 0
            fpos["entry"] = 0

        self.total_trades += 1
        play_sfx("sell")
        self._save_game()

        if self.view == "trade":
            self._render_hud()
            self._render_trade_view()

    def _close_futures(self, coin_idx):
        """Close entire futures position."""
        self._sell_futures(coin_idx, 1.0)

    def _place_limit_order(self, side):
        if self.selected_coin < 0 or self.limit_price <= 0:
            play_sfx("error")
            return
        if len(self.open_orders) >= MAX_OPEN_ORDERS:
            play_sfx("error")
            return

        sym = SYMBOLS[self.selected_coin]
        price = self.data.get_price(sym)

        if side == "buy":
            amount_usdt = self.cash * 0.25  # default 25% of wallet
            if amount_usdt < 0.01:
                play_sfx("error")
                return
            self.cash -= amount_usdt  # reserve funds
        else:
            pos = self.positions.get(sym, {})
            qty = pos.get("qty", 0)
            if qty <= 0:
                play_sfx("error")
                return
            amount_usdt = qty * price * 0.5  # sell 50% at limit

        order = {
            "id": self._next_order_id,
            "type": self.order_type,  # "limit" or "stop"
            "side": side,
            "symbol_idx": self.selected_coin,
            "price": self.limit_price,
            "amount_usdt": amount_usdt,
            "created_tick": self.tick_count,
        }
        self._next_order_id += 1
        self.open_orders.append(order)

        play_sfx("order_placed")
        self._save_game()

        # Go back to trade view
        self.view = "trade"
        self._render_hud()
        self._render_trade_view()

    def _cancel_order(self, order_idx):
        if order_idx < 0 or order_idx >= len(self.open_orders):
            return
        order = self.open_orders[order_idx]
        # Refund reserved funds for buy orders
        if order["side"] == "buy":
            self.cash += order["amount_usdt"]
        self.open_orders.pop(order_idx)
        play_sfx("select")
        self._save_game()

        if self.view == "market":
            self._render_hud()
            self._render_market_grid()

    # -- key handler ---------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.view == "idle":
                self._on_idle(key)
            elif self.view == "market":
                self._on_market(key)
            elif self.view == "trade":
                self._on_trade(key)
            elif self.view == "limit_set":
                self._on_limit_set(key)

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
            if key == 4:
                # Toggle mode
                self.trade_mode = "futures" if self.trade_mode == "spot" else "spot"
                play_sfx("select")
                self._render_hud()
                self._render_market_grid()
            return

        r, c = pos_to_rc(key)

        # Row 0: coin tiles -> trade view
        if r == 0 and 0 <= c < NUM_COINS:
            self._enter_trade(c)
            return

        # Row 1: position tiles -> trade view
        if r == 1 and 0 <= c < NUM_COINS:
            self._enter_trade(c)
            return

        # Row 2: orders / stats
        if r == 2:
            if 0 <= c < 3 and c < len(self.open_orders):
                self._cancel_order(c)

    def _on_trade(self, key):
        if key < 8:
            return

        r, c = pos_to_rc(key)
        i = self.selected_coin

        if r == 0:
            if c == 0:
                # Cycle order type
                types = ["market", "limit", "stop"]
                idx = types.index(self.order_type) if self.order_type in types else 0
                self.order_type = types[(idx + 1) % len(types)]
                play_sfx("select")
                self._render_trade_view()
            elif c == 6 and self.trade_mode == "futures":
                # Cycle leverage
                idx = LEVERAGE_OPTIONS.index(self.leverage) if self.leverage in LEVERAGE_OPTIONS else 0
                self.leverage = LEVERAGE_OPTIONS[(idx + 1) % len(LEVERAGE_OPTIONS)]
                play_sfx("select")
                self._render_trade_view()
            return

        if r == 1:
            # If order type is limit or stop, enter limit price setter
            if self.order_type in ("limit", "stop"):
                self._enter_limit_set()
                return

            buy_pcts = [0.10, 0.25, 0.50, 1.0]
            sell_pcts = [0.10, 0.25, 0.50, 1.0]

            if 0 <= c < 4:
                # BUY
                if self.trade_mode == "spot":
                    self._buy_spot(i, buy_pcts[c])
                else:
                    self._buy_futures(i, buy_pcts[c])
            elif 4 <= c < 8:
                # SELL
                if self.trade_mode == "spot":
                    self._sell_spot(i, sell_pcts[c - 4])
                else:
                    self._sell_futures(i, sell_pcts[c - 4])
            return

        if r == 2:
            if c == 0:
                self._exit_trade()
            elif c == 7 and self.trade_mode == "futures":
                # Toggle long/short
                self.futures_side = "short" if self.futures_side == "long" else "long"
                play_sfx("select")
                self._render_trade_view()
            elif 1 <= c <= 5:
                # Quick coin select
                other_coins = [ci for ci in range(NUM_COINS) if ci != i]
                if c - 1 < len(other_coins):
                    self._enter_trade(other_coins[c - 1])

    def _on_limit_set(self, key):
        if key < 8:
            return

        r, c = pos_to_rc(key)

        if r == 0:
            if c == 0:
                # Cycle order type
                types = ["limit", "stop"]
                idx = types.index(self.order_type) if self.order_type in types else 0
                self.order_type = types[(idx + 1) % len(types)]
                play_sfx("select")
                self._render_limit_set_view()
            return

        if r == 1:
            if c == 0:
                self.limit_price *= 0.95
                play_sfx("select")
                self._render_limit_set_view()
            elif c == 1:
                self.limit_price *= 0.99
                play_sfx("select")
                self._render_limit_set_view()
            elif c == 3:
                self.limit_price *= 1.01
                play_sfx("select")
                self._render_limit_set_view()
            elif c == 4:
                self.limit_price *= 1.05
                play_sfx("select")
                self._render_limit_set_view()
            elif c == 5:
                self._place_limit_order("buy")
            elif c == 6:
                self._place_limit_order("sell")
            elif c == 7:
                # Back to trade view
                self.view = "trade"
                self._render_hud()
                self._render_trade_view()

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
    print("CRYPTO REAL — live Binance paper trading!")

    game = CryptoRealGame(deck)
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
