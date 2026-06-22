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


def content_bounds(path: str, top_db: float = 30.0, frame: int = 1024):
    """Return (start_sec, end_sec, duration_sec, sr) of the non-silent region.

    `top_db` — how far below the peak still counts as silence (bigger = trims
    more aggressively). Returns the full clip if it can't read / is all silence.
    """
    if np is None or sf is None:
        return None
    try:
        y, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        return None
    mono = y.mean(axis=1)
    n = len(mono)
    if n == 0:
        return (0.0, 0.0, 0.0, sr)
    nf = n // frame
    if nf < 1:
        dur = n / sr
        return (0.0, dur, dur, sr)
    env = np.sqrt((mono[:nf * frame].reshape(nf, frame) ** 2).mean(axis=1))
    peak = float(env.max())
    dur = n / sr
    if peak <= 0.0:
        return (0.0, dur, dur, sr)
    thresh = peak * (10.0 ** (-top_db / 20.0))
    above = np.where(env > thresh)[0]
    if len(above) == 0:
        return (0.0, dur, dur, sr)
    start = above[0] * frame
    end = min((above[-1] + 1) * frame, n)
    return (start / sr, end / sr, dur, sr)


if __name__ == "__main__":
    import sys
    r = content_bounds(sys.argv[1])
    if r:
        s, e, d, sr = r
        print(f"content {s:.3f}s … {e:.3f}s  of {d:.3f}s  (sr={sr})  → trims "
              f"{s:.3f}s head, {d - e:.3f}s tail")
