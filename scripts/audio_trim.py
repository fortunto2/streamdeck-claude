"""Find the content boundaries of a recorded audio file (trim leading/trailing
silence), so a loop can be aligned to where the sound actually is.

OSC doesn't hand out audio samples — but Ableton saves every recording as a
file on disk, so we read it directly. Energy-based, the same idea as
`librosa.effects.trim` (threshold a dB below the peak) but with no heavy deps:
just soundfile + numpy.
"""

from __future__ import annotations

try:
    import numpy as np
    import soundfile as sf
except Exception:  # pragma: no cover
    np = None
    sf = None


def content_bounds(path: str, floor_pct: float = 20.0, margin_db: float = 10.0,
                   min_span_db: float = 8.0, frame: int = 1024):
    """Return (start_sec, end_sec, duration_sec, sr) of the actual signal.

    Auto-threshold relative to the NOISE FLOOR (not the peak), so steady
    background noise/hum doesn't count as content — we look for where the
    energy jumps well above the floor. The threshold sits `margin_db` above an
    estimate of the noise floor (the `floor_pct` percentile of the envelope),
    or 35 % of the way up to the peak, whichever is higher. If there's no clear
    jump (floor and peak within `min_span_db`), the clip is left untrimmed.

    Also returns the decision info via the `info` attribute for logging.
    """
    if np is None or sf is None:
        return None
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        return None
    mono = y.mean(axis=1)
    n = len(mono)
    dur = n / sr if sr else 0.0
    nf = n // frame
    if nf < 2:
        return (0.0, dur, dur, sr)
    env = np.sqrt((mono[:nf * frame].reshape(nf, frame) ** 2).mean(axis=1))
    env_db = 20.0 * np.log10(env + 1e-9)
    floor = float(np.percentile(env_db, floor_pct))   # background noise level
    peak = float(env_db.max())
    span = peak - floor
    if span < min_span_db:
        return (0.0, dur, dur, sr)                     # no clear jump — leave it
    thresh = floor + max(margin_db, 0.35 * span)
    above = np.where(env_db > thresh)[0]
    if len(above) == 0:
        return (0.0, dur, dur, sr)
    start = above[0] * frame
    end = min((above[-1] + 1) * frame, n)
    res = (start / sr, end / sr, dur, sr)
    content_bounds.info = (f"floor={floor:.1f}dB peak={peak:.1f}dB "
                           f"thresh={thresh:.1f}dB span={span:.1f}dB")
    return res


content_bounds.info = ""


def waveform(path: str, points: int = 46):
    """Downsampled peak envelope (list of 0..1) for drawing a tiny waveform on
    a deck key. None if it can't read the file."""
    if np is None or sf is None:
        return None
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        return None
    mono = np.abs(y.mean(axis=1))
    n = len(mono)
    step = max(1, n // points)
    nb = n // step
    if nb < 1:
        return None
    env = mono[:nb * step].reshape(nb, step).max(axis=1)
    peak = float(env.max())
    if peak <= 0.0:
        return [0.0] * nb
    return [float(x) for x in (env / peak)]


if __name__ == "__main__":
    import sys
    r = content_bounds(sys.argv[1])
    if r:
        s, e, d, sr = r
        print(f"content {s:.3f}s … {e:.3f}s  of {d:.3f}s  (sr={sr})  → trims "
              f"{s:.3f}s head, {d - e:.3f}s tail")
