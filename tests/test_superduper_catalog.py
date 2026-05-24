"""Catalogue invariants — surface drift between Python mirror and Rust source."""

import pytest

from src.superduper_catalog import PLUGINS, find_plugin, NAM, VOCAL, SOOTHE, DRUM


def test_unique_param_indices():
    for plugin in PLUGINS.values():
        seen = set()
        for p in plugin.params:
            assert p.idx not in seen, f"{plugin.short}: dup param idx {p.idx}"
            seen.add(p.idx)


def test_param_ranges_sane():
    for plugin in PLUGINS.values():
        for p in plugin.params:
            assert p.min <= p.max, f"{plugin.short}.{p.name}: min > max"
            assert p.min <= p.default <= p.max, (
                f"{plugin.short}.{p.name}: default {p.default} outside [{p.min}, {p.max}]"
            )


def test_choice_params_have_options():
    for plugin in PLUGINS.values():
        for p in plugin.params:
            if p.kind == "choice":
                assert len(p.choices) > 0, (
                    f"{plugin.short}.{p.name}: kind=choice but no choices listed"
                )


def test_toggle_params_are_binary():
    for plugin in PLUGINS.values():
        for p in plugin.params:
            if p.kind == "toggle":
                assert p.min == 0 and p.max == 1, (
                    f"{plugin.short}.{p.name}: toggle param must be [0,1], got [{p.min}, {p.max}]"
                )


def test_find_plugin_by_short_name():
    assert find_plugin("nam") is NAM
    assert find_plugin("Vocal") is VOCAL  # case-insensitive
    assert find_plugin("soothe") is SOOTHE


def test_find_plugin_by_clap_id():
    assert find_plugin("co.superduperai.nam") is NAM


def test_find_plugin_returns_none_for_unknown():
    assert find_plugin("not-a-plugin") is None


def test_vocal_has_sub_mode():
    """Sub Mode (param id 22) was the most recent addition — guard against
    rebases dropping it."""
    sub_mode = next((p for p in VOCAL.params if p.idx == 22), None)
    assert sub_mode is not None
    assert sub_mode.name == "Sub Mode"
    assert sub_mode.kind == "toggle"


def test_drum_note_out_toggle():
    note_out = next((p for p in DRUM.params if p.idx == 26), None)
    assert note_out is not None
    assert note_out.kind == "toggle"
