"""google-genai 3.0 (2026-11-06) moved the Developer API default from v1beta
to v1, and v1 rejects JSON mode with 400 INVALID_ARGUMENT. Every interactive
client must pin v1beta so the bot keeps working regardless of SDK version."""
from types import SimpleNamespace

import pytest

from synbee_bot import filter as paper_filter
from synbee_bot.models import Paper
from synbee_bot.spam_rescue import classify


@pytest.fixture
def captured(monkeypatch):
    from google import genai

    seen: dict = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen["mime"] = config.response_mime_type
            return SimpleNamespace(text='{"verdict": "NO", "score": 0}',
                                   usage_metadata=None)

    def make(api_key=None, http_options=None):
        seen["http_options"] = http_options
        return SimpleNamespace(models=FakeModels())

    monkeypatch.setattr(genai, "Client", make)
    return seen


def test_spam_rescue_client_pins_v1beta(captured):
    classify.generate_text("p", "gemini-2.5-flash", "k", timeout=5)
    assert captured["http_options"].api_version == "v1beta"
    assert captured["mime"] == "application/json"


def test_paper_filter_client_pins_v1beta(captured):
    paper = Paper(id="doi:10.1/x", title="t", abstract="a", authors=[],
                  journal="j", url="u", source="pubmed", published="2026-11-06",
                  year=2026, doi=None)
    paper_filter._filter_with_gemini(paper, "{title}", "gemini-2.5-flash", "k",
                                     timeout=5)
    assert captured["http_options"].api_version == "v1beta"
    assert captured["mime"] == "application/json"
