"""Unit tests for token accounting and thinking-budget handling in filter.py.

These cover the two things that are easy to regress and expensive to get wrong:
  1. cost arithmetic (thinking billed as output; cached input discounted 75%)
  2. max_output_tokens must scale with thinking_budget, because in Gemini 2.5 the
     output cap counts thinking tokens — a small fixed cap starves the JSON and
     every response fails to parse.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot import filter as flt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_usage():
    flt.reset_usage()
    yield
    flt.reset_usage()


def _usage(prompt=0, out=0, think=0, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=out,
        thoughts_token_count=think,
        cached_content_token_count=cached,
    )


def test_reset_usage_zeroes_all_counters():
    flt._record_usage(_usage(prompt=100, out=10, think=500, cached=50))
    assert flt.get_usage()["calls"] == 1

    flt.reset_usage()

    assert flt.get_usage() == {"calls": 0, "in": 0, "out": 0, "think": 0, "cached": 0}


def test_record_usage_accumulates_across_calls():
    flt._record_usage(_usage(prompt=100, out=10, think=500, cached=40))
    flt._record_usage(_usage(prompt=200, out=20, think=300, cached=60))

    u = flt.get_usage()

    assert u == {"calls": 2, "in": 300, "out": 30, "think": 800, "cached": 100}


def test_record_usage_ignores_none():
    flt._record_usage(None)

    assert flt.get_usage()["calls"] == 0


def test_missing_usage_fields_count_as_zero():
    """Providers/SDK versions that omit thoughts_token_count must not crash."""
    flt._record_usage(SimpleNamespace(prompt_token_count=50))

    u = flt.get_usage()

    assert u["calls"] == 1
    assert u["in"] == 50
    assert u["think"] == 0


def test_thinking_tokens_are_billed_at_the_output_rate():
    # gemini-2.5-flash: $0.30/1M in, $2.50/1M out
    flt._record_usage(_usage(prompt=1_000_000, out=0, think=1_000_000))

    summary = flt.format_usage_summary("gemini-2.5-flash")

    # 1M in * 0.30 + 1M thinking * 2.50 = 2.80
    assert "$2.8000" in summary


def test_cached_input_is_discounted_to_a_quarter():
    # 1M input of which all is cached -> 0.30 * 0.25 = 0.075
    flt._record_usage(_usage(prompt=1_000_000, cached=1_000_000))

    summary = flt.format_usage_summary("gemini-2.5-flash")

    assert "$0.0750" in summary


def test_cached_tokens_are_a_subset_not_an_addition():
    """`cached` overlaps `in`; double-counting it would overstate the bill."""
    flt._record_usage(_usage(prompt=1_000_000, cached=400_000))

    summary = flt.format_usage_summary("gemini-2.5-flash")

    # 600k fresh * 0.30 + 400k cached * 0.075 = 0.18 + 0.03 = 0.21
    assert "$0.2100" in summary


def test_no_calls_reports_cleanly():
    assert flt.format_usage_summary("gemini-2.5-flash") == "LLM usage: no calls"


def test_unpriced_model_is_flagged_rather_than_silently_zero():
    flt._record_usage(_usage(prompt=1000, out=100))

    summary = flt.format_usage_summary("some-future-model")

    assert "unpriced model" in summary


class _FakeThinkingConfig:
    def __init__(self, thinking_budget: int) -> None:
        self.thinking_budget = thinking_budget


class _FakeConfig:
    """Captures whatever GenerateContentConfig was constructed with."""

    last: dict | None = None

    def __init__(self, **kwargs) -> None:
        _FakeConfig.last = kwargs


def _run_gemini_capture(monkeypatch, budget: int) -> dict:
    """Invoke _filter_with_gemini against a stubbed SDK and return the config."""
    captured_response = SimpleNamespace(
        text='{"verdict":"YES","mission":1,"score":7,'
             '"one_liner":"kr","one_liner_en":"en"}',
        usage_metadata=_usage(prompt=100, out=20, think=budget if budget > 0 else 900),
    )

    fake_types = SimpleNamespace(
        HttpOptions=lambda **kw: SimpleNamespace(**kw),
        GenerateContentConfig=_FakeConfig,
        ThinkingConfig=_FakeThinkingConfig,
    )
    fake_client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kw: captured_response))
    fake_genai = SimpleNamespace(Client=lambda **kw: fake_client)

    google_mod = SimpleNamespace(genai=fake_genai)
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    fake_genai.types = fake_types

    paper = SimpleNamespace(
        id="test-1", title="T", journal="J", year=2026,
        abstract="A" * 500, authors_short=lambda n: "X")

    _FakeConfig.last = None
    flt._filter_with_gemini(paper, "{title} {abstract}", "gemini-2.5-flash",
                            "key", 30, thinking_budget=budget)
    assert _FakeConfig.last is not None
    return _FakeConfig.last


@pytest.mark.parametrize("budget", [0, 256, 1024])
def test_output_cap_leaves_room_beyond_the_thinking_budget(monkeypatch, budget):
    """Regression guard: the cap must exceed the budget, or the JSON gets cut off."""
    cfg = _run_gemini_capture(monkeypatch, budget)

    assert cfg["thinking_config"].thinking_budget == budget
    assert cfg["max_output_tokens"] > budget


def test_negative_budget_means_dynamic_and_omits_the_output_cap(monkeypatch):
    cfg = _run_gemini_capture(monkeypatch, -1)

    assert cfg["thinking_config"].thinking_budget == -1
    assert cfg["max_output_tokens"] is None


def test_gemini_call_records_usage(monkeypatch):
    _run_gemini_capture(monkeypatch, 1024)

    assert flt.get_usage()["calls"] == 1
