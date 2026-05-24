"""Drum sequencer state — pattern + voice selection logic only.

The clock thread that fires MIDI notes is exercised separately via the
real `MidiOut` (skipped in CI because it opens a system-wide virtual
port and we don't want CI to claim one).
"""

import pytest

from src.drum_seq import DrumPattern, VOICES, N_STEPS


def test_pattern_initially_empty():
    p = DrumPattern()
    for voice in VOICES:
        for step in range(N_STEPS):
            assert not p.is_armed(voice, step)


def test_toggle_flips():
    p = DrumPattern()
    p.toggle("kick", 0)
    assert p.is_armed("kick", 0)
    p.toggle("kick", 0)
    assert not p.is_armed("kick", 0)


def test_toggle_unknown_voice_is_noop():
    p = DrumPattern()
    p.toggle("triangle", 0)  # not a real voice
    # Other voices untouched.
    assert not any(p.is_armed(v, 0) for v in VOICES)


def test_toggle_out_of_range_step_is_noop():
    p = DrumPattern()
    p.toggle("kick", -1)
    p.toggle("kick", N_STEPS)
    assert not p.is_armed("kick", 0)


def test_clear_resets_all_voices():
    p = DrumPattern()
    p.toggle("kick", 0)
    p.toggle("snare", 4)
    p.clear()
    assert not p.is_armed("kick", 0)
    assert not p.is_armed("snare", 4)


def test_clear_voice_only_affects_that_voice():
    p = DrumPattern()
    p.toggle("kick", 0)
    p.toggle("snare", 4)
    p.clear_voice("kick")
    assert not p.is_armed("kick", 0)
    assert p.is_armed("snare", 4)
