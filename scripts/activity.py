"""Mac activity tracker — uptime, work session, idle detection.

Uses native macOS APIs via ctypes (no subprocess):
- sysctl kern.boottime → system uptime
- ioreg HIDIdleTime → seconds since last keyboard/mouse input

Tracks continuous work sessions: resets if idle > 5 min.
"""

import ctypes
import ctypes.util
import json
import os
import re
import struct
import subprocess
import time

# ── config ────────────────────────────────────────────────────────────

IDLE_THRESHOLD = 5 * 60  # 5 min idle = session break
STATE_FILE = os.path.expanduser("~/.streamdeck-arcade/activity_state.json")
_SAVE_INTERVAL = 60  # save to disk every 60 seconds max

# ── native macOS APIs ─────────────────────────────────────────────────

_libc = ctypes.CDLL(ctypes.util.find_library("c"))


def _get_boot_time() -> int:
    """Get system boot timestamp via sysctl (ctypes, no subprocess)."""
    try:
        # CTL_KERN=1, KERN_BOOTTIME=21
        mib = (ctypes.c_int * 2)(1, 21)
        buf = ctypes.create_string_buffer(16)
        buf_len = ctypes.c_size_t(16)
        ret = _libc.sysctl(mib, 2, buf, ctypes.byref(buf_len), None, 0)
        if ret == 0:
            return struct.unpack("l", buf.raw[:8])[0]
        return 0
    except Exception:
        return 0


def _get_idle_seconds() -> int:
    """Get seconds since last HID input via ioreg subprocess."""
    try:
        out = subprocess.check_output(
            ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
            text=True, timeout=2, stderr=subprocess.DEVNULL,
        )
        m = re.search(r"HIDIdleTime.*?=\s*(\d+)", out)
        return int(m.group(1)) // 1_000_000_000 if m else 0
    except Exception:
        return 0


# ── session tracker ───────────────────────────────────────────────────

_session_start: float = 0  # timestamp when current work session began
_total_work: float = 0  # accumulated work seconds today
_last_check: float = 0  # last time we checked idle
_day: str = ""  # current day string for daily reset
_last_save: float = 0  # last time state was saved
_loaded: bool = False  # whether state was loaded from disk


def _load_state():
    """Restore session state from disk on first call."""
    global _session_start, _total_work, _last_check, _day, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        today = time.strftime("%Y-%m-%d")
        if data.get("date") == today:
            _total_work = data.get("total_work", 0)
            _session_start = data.get("session_start", 0)
            _last_check = data.get("last_check", 0)
            _day = today
            # If session_start is stale (daemon was down), check if gap is > threshold
            now = time.time()
            if _session_start > 0 and (now - _last_check) > IDLE_THRESHOLD:
                # Session was interrupted by restart — commit old work, start fresh
                gap_work = _last_check - _session_start
                if gap_work > 0:
                    _total_work += gap_work
                _session_start = now
                _last_check = now
    except Exception:
        pass


def _save_state():
    """Persist session state to disk (throttled)."""
    global _last_save
    now = time.time()
    if now - _last_save < _SAVE_INTERVAL:
        return
    _last_save = now
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        data = {
            "date": _day,
            "total_work": _total_work,
            "session_start": _session_start,
            "last_check": _last_check,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def get_activity() -> dict:
    """Return current activity metrics.

    Returns dict with:
        uptime_sec: system uptime in seconds
        idle_sec: seconds since last input
        session_sec: current continuous work session (resets on 5min idle)
        total_work_sec: total work time today
        is_active: True if user is currently active (idle < 60s)
    """
    global _session_start, _total_work, _last_check, _day

    _load_state()

    now = time.time()
    today = time.strftime("%Y-%m-%d")
    idle = _get_idle_seconds()

    # Daily reset
    if today != _day:
        _day = today
        _total_work = 0
        _session_start = now

    # Initialize session on first call
    if _session_start == 0:
        _session_start = now
        _last_check = now
        _day = today

    # If idle > threshold, session was broken
    if idle > IDLE_THRESHOLD:
        # Add time up to when we went idle
        if _last_check > 0 and _session_start > 0:
            active_before_idle = _last_check - _session_start
            if active_before_idle > 0:
                _total_work += active_before_idle
        _session_start = 0  # no active session
    elif _session_start == 0:
        # Was idle, now active again — start new session
        _session_start = now

    _last_check = now

    # Current session duration
    session = now - _session_start if _session_start > 0 else 0

    # Uptime
    boot = _get_boot_time()
    uptime = int(now) - boot if boot > 0 else 0

    _save_state()

    return {
        "uptime_sec": uptime,
        "idle_sec": idle,
        "session_sec": int(session),
        "total_work_sec": int(_total_work + session),
        "is_active": idle < 60,
    }


def reset_session():
    """User acknowledged break — reset current session, keep total."""
    global _session_start, _total_work, _last_check, _last_save

    now = time.time()
    # Commit current session to total
    if _session_start > 0:
        _total_work += now - _session_start
    # Start fresh session
    _session_start = now
    _last_check = now
    _last_save = 0  # force save
    _save_state()
