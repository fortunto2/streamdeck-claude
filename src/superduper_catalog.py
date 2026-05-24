"""SuperDuper DSP plugin catalogue.

Mirror of the `PARAMS` tables in
`/Users/rustam/Music/1music/superduper-dsp/effects/superduper-<name>/src/lib.rs`
so the Stream Deck can build per-plugin control pages without polling
REAPER for parameter metadata at runtime.

Each plugin entry lists its CLAP id (lets us address it via REAPER OSC
`/fxbyname/<id>/...` patterns) and its parameter table. Parameter
indices match the order in the Rust `const PARAMS: &[ParamDef]` slice
— **must stay in sync when a plugin's PARAMS changes**. There's an
audit test in `tests/test_superduper_catalog.py` that warns when the
mirror drifts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    """One parameter row — mirrors a Rust `ParamDef`."""

    idx: int
    name: str
    min: float
    max: float
    default: float
    unit: str = ""
    # `kind` decides how the Stream Deck button renders the value.
    # "continuous" = slider, "toggle" = on/off LED, "choice" = radio.
    kind: str = "continuous"
    # For "choice" kind: human-readable option labels (renders as
    # `Tape / Tube / Soft` etc. on the button).
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plugin:
    """SuperDuper plugin descriptor."""

    id: str  # CLAP id, e.g. "co.superduperai.nam"
    short: str  # short name, used in UI labels
    category: str  # "effect" or "instrument"
    params: tuple[Param, ...]


# ---------------------------------------------------------------------------
# Catalogue — keep in alphabetical order by short name.
# ---------------------------------------------------------------------------

NAM = Plugin(
    id="co.superduperai.nam",
    short="NAM",
    category="effect",
    params=(
        Param(0, "Input", -24, 24, 0, "dB"),
        Param(1, "Drive", 0, 12, 3, "dB"),
        Param(2, "Output", -24, 24, 0, "dB"),
        Param(3, "Mix", 0, 1, 1),
        Param(4, "Tone", -1, 1, 0),
    ),
)

VOCAL = Plugin(
    id="co.superduperai.vocal",
    short="Vocal",
    category="effect",
    params=(
        Param(0, "Ess Thr", -60, 0, -24, "dB"),
        Param(1, "Ess Freq", 2000, 10000, 6000, "Hz"),
        Param(2, "Ess Amt", 0, 18, 6, "dB"),
        Param(3, "Ess Range", 0, 1, 1),
        Param(4, "Clk Sens", 1.5, 8, 3, "x"),
        Param(5, "Clk Amt", 0, 24, 12, "dB"),
        Param(6, "Clk Floor", -60, -20, -40, "dB"),
        Param(7, "Output", -24, 24, 0, "dB"),
        Param(8, "Mix", 0, 1, 1),
        Param(9, "Lo Thr", -60, 0, -24, "dB"),
        Param(10, "Lo Freq", 300, 3000, 1000, "Hz"),
        Param(11, "Lo Amt", 0, 18, 0, "dB"),
        Param(12, "Ext Key", 0, 1, 0, kind="toggle"),
        Param(13, "Plos On", 0, 1, 0, kind="toggle"),
        Param(14, "Plos Thr", -60, 0, -24, "dB"),
        Param(15, "Plos Amt", 0, 24, 12, "dB"),
        Param(16, "Plos Freq", 40, 250, 120, "Hz"),
        Param(17, "Hum On", 0, 1, 0, kind="toggle"),
        Param(18, "Hum Freq", 50, 60, 50, "Hz"),
        Param(19, "Hum Str", 0, 1, 0.7),
        Param(20, "Ess Track", 0, 1, 0, kind="toggle"),
        Param(21, "Ess Listen", 0, 1, 0, kind="toggle"),
        Param(22, "Sub Mode", 0, 1, 0, kind="toggle"),
    ),
)

SOOTHE = Plugin(
    id="co.superduperai.soothe",
    short="Soothe",
    category="effect",
    params=(
        Param(0, "Amount", 0, 24, 6, "dB"),
        Param(1, "Sens", -24, 0, -6, "dB"),
        Param(2, "Q", 2, 12, 5),
        Param(3, "Lo", 100, 2000, 300, "Hz"),
        Param(4, "Hi", 3000, 16000, 10000, "Hz"),
        Param(5, "Attack", 0.5, 30, 5, "ms"),
        Param(6, "Release", 10, 500, 80, "ms"),
        Param(7, "Mix", 0, 1, 1),
        Param(8, "Output", -24, 24, 0, "dB"),
        Param(9, "Mode", 0, 2, 1, kind="choice",
              choices=("Soft", "Sharp", "Hard")),
    ),
)

FILTER = Plugin(
    id="co.superduperai.filter",
    short="Filter",
    category="effect",
    params=(
        Param(0, "Type", 0, 3, 0, kind="choice",
              choices=("LP", "HP", "BP", "Notch")),
        Param(1, "Cutoff", 20, 20000, 1000, "Hz"),
        Param(2, "Reso", 0, 1, 0.3),
        Param(3, "Drive", 0, 1, 0),
        Param(4, "Drive Mode", 0, 2, 0, kind="choice",
              choices=("Tanh", "Tape", "Tube")),
        # LFO + Env follow params skipped for brevity — add as needed.
        Param(8, "LFO Sync", 0, 1, 0, kind="toggle"),
        Param(13, "Mix", 0, 1, 1),
    ),
)

SATURATOR = Plugin(
    id="co.superduperai.saturator",
    short="Saturator",
    category="effect",
    params=(
        Param(0, "Drive", 0, 36, 6, "dB"),
        Param(1, "Type", 0, 2, 0, kind="choice",
              choices=("Tape", "Tube", "Soft")),
        Param(2, "Tone", -1, 1, 0),
        Param(3, "Output", -24, 12, 0, "dB"),
        Param(4, "Mix", 0, 1, 1),
        Param(5, "OS", 0, 2, 1, kind="choice",
              choices=("1x", "2x", "4x")),
    ),
)

COMPRESSOR = Plugin(
    id="co.superduperai.compressor",
    short="Comp",
    category="effect",
    params=(
        Param(0, "Threshold", -60, 0, -18, "dB"),
        Param(1, "Ratio", 1, 20, 2),
        Param(2, "Attack", 0.1, 100, 10, "ms"),
        Param(3, "Release", 10, 1000, 100, "ms"),
        Param(7, "Mix", 0, 1, 1),
        Param(13, "Curve", 0, 2, 0, kind="choice",
              choices=("Clean", "Pump", "Smooth")),
    ),
)

LIMITER = Plugin(
    id="co.superduperai.limiter",
    short="Limit",
    category="effect",
    params=(
        Param(0, "Input", -24, 24, 0, "dB"),
        Param(1, "Ceiling", -3, 0, -1, "dBTP"),
        Param(2, "Release", 1, 500, 50, "ms"),
        Param(3, "Lookahead", 0, 5, 1.5, "ms"),
        Param(4, "True Peak", 0, 1, 1, kind="toggle"),
        Param(5, "Dither", 0, 1, 0, kind="toggle"),
    ),
)

DRUM = Plugin(
    id="co.superduperai.drum",
    short="Drum",
    category="instrument",
    # Drum has 27 params (4 per voice × 6 + master). Listing only the
    # live-relevant ones for the Stream Deck "Drum control" page.
    params=(
        Param(24, "Drive", 0, 1, 0),
        Param(26, "Note Out", 0, 1, 1, kind="toggle"),
    ),
)

WAVE = Plugin(
    id="co.superduperai.wave",
    short="Wave",
    category="instrument",
    params=(
        Param(0, "WT Pos", 0, 1, 0),
        Param(1, "Unison", 1, 7, 1, kind="choice",
              choices=tuple(str(i) for i in range(1, 8))),
        Param(2, "Detune", 0, 50, 0, "ct"),
        Param(3, "Sub", 0, 1, 0),
        Param(6, "Filter", 0, 2, 0, kind="choice",
              choices=("LP", "HP", "BP")),
    ),
)

LOOPER = Plugin(
    id="co.superduperai.looper",
    short="Looper",
    category="effect",
    params=(
        Param(0, "Sync", 0, 1, 1, kind="toggle"),
        Param(2, "Dry", 0, 1, 1),
        # T1..T4 controls live as pidx(n, k) — Stream Deck Looper page
        # builds these from a programmatic index table at runtime.
    ),
)


PLUGINS: dict[str, Plugin] = {
    p.short.lower(): p
    for p in (NAM, VOCAL, SOOTHE, FILTER, SATURATOR, COMPRESSOR, LIMITER,
              DRUM, WAVE, LOOPER)
}


def find_plugin(short_or_id: str) -> Plugin | None:
    """Look up by lowercase short name or full CLAP id."""
    key = short_or_id.lower()
    if key in PLUGINS:
        return PLUGINS[key]
    for p in PLUGINS.values():
        if p.id == short_or_id:
            return p
    return None
