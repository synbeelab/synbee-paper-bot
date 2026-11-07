"""KU BAZE gateway routing: "baze:" models go through the gateway, failures
fall back down the chain, and unjudged papers hold their source's watermark."""
from __future__ import annotations

import pytest
import requests

from synbee_bot import filter as paper_filter
from synbee_bot import llm_gateway
from synbee_bot.models import Paper, Verdict
from synbee_bot.spam_rescue import classify
from synbee_bot.storage import sources_with_errors

VERDICT_JSON = '{"verdict": "YES", "mission": 1, "score": 8, "one_liner": "ok"}'


def paper(i: int = 0, source: str = "pubmed") -> Paper:
    return Paper(id=f"{source}:{i}", source=source, title=f"t{i}", authors=[],
                 journal="j", year=2026, abstract="a", doi=None, url="u",
                 published=None)


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body
        self.text = text or str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def ok(content: str, **usage) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}],
                              "usage": {"prompt_tokens": 10,
                                        "completion_tokens": 5, **usage}})


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json, headers, timeout):
        self.calls.append({"url": url, "json": json, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(llm_gateway.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm_gateway, "USAGE", llm_gateway._Ledger())


# --- naming -----------------------------------------------------------------

def test_prefix_selects_the_gateway_and_is_stripped_before_sending():
    assert llm_gateway.is_gateway_model("baze:gemini-3.8-flash")
    assert not llm_gateway.is_gateway_model("gemini-3.8-flash")
    assert llm_gateway.gateway_model_id("baze:gemini-3.8-flash") == "gemini-3.8-flash"
    assert llm_gateway.uses_gateway(["gemini-2.5-flash", "baze:gpt-6-luna"])
    assert not llm_gateway.uses_gateway(["gemini-2.5-flash"])


@pytest.mark.parametrize("model,json_mode", [
    ("baze:gemini-3.8-flash", True),
    ("baze:gpt-6-luna", True),
    # Claude on the gateway rejects response_format (400, measured 2026-11-07).
    ("baze:claude-haiku-5-5", False),
])
def test_json_mode_is_only_requested_where_the_gateway_accepts_it(model, json_mode):
    payload = llm_gateway.build_payload("p", model, temperature=0.1, json_mode=True)
    assert payload["model"] == llm_gateway.gateway_model_id(model)
    assert ("response_format" in payload) is json_mode


# --- chat -------------------------------------------------------------------

def test_chat_returns_text_and_records_usage():
    s = FakeSession(ok(VERDICT_JSON, completion_tokens_details={"reasoning_tokens": 3}))
    text = llm_gateway.chat("p", "baze:gemini-3.8-flash", timeout=5, key="k", session=s)
    assert text == VERDICT_JSON
    assert s.calls[0]["url"].endswith("/chat/completions/")
    assert s.calls[0]["headers"]["Authorization"] == "Bearer k"
    usage = llm_gateway.USAGE.total()
    assert (usage.calls, usage.prompt, usage.completion, usage.reasoning) == (1, 10, 5, 3)


def test_chat_retries_transient_failures_then_succeeds():
    s = FakeSession(FakeResponse(429, text="slow down"),
                    requests.ConnectionError("reset"),
                    ok(VERDICT_JSON))
    assert llm_gateway.chat("p", "baze:m", timeout=5, key="k", session=s) == VERDICT_JSON
    assert len(s.calls) == 3


def test_chat_does_not_retry_a_client_error():
    s = FakeSession(FakeResponse(400, text="bad model"), ok(VERDICT_JSON))
    with pytest.raises(llm_gateway.GatewayError, match="HTTP 400"):
        llm_gateway.chat("p", "baze:m", timeout=5, key="k", session=s)
    assert len(s.calls) == 1
    assert llm_gateway.USAGE.total().failures == 1


def test_chat_gives_up_after_the_last_retry():
    s = FakeSession(*[FakeResponse(503, text="down")] * 4)
    with pytest.raises(llm_gateway.GatewayError, match="503"):
        llm_gateway.chat("p", "baze:m", timeout=5, key="k", session=s)
    assert len(s.calls) == 4


def test_missing_key_fails_without_a_network_call(monkeypatch):
    monkeypatch.delenv(llm_gateway.KEY_ENV, raising=False)
    s = FakeSession()
    with pytest.raises(llm_gateway.GatewayError, match="BAZE_API_KEY"):
        llm_gateway.chat("p", "baze:m", timeout=5, session=s)
    assert s.calls == []


# --- paper filter -----------------------------------------------------------

def test_filter_routes_a_baze_model_through_the_gateway(monkeypatch):
    seen = []
    monkeypatch.setattr(paper_filter.llm_gateway, "chat",
                        lambda prompt, model, **kw: seen.append(model) or VERDICT_JSON)
    monkeypatch.setattr(paper_filter, "_filter_with_gemini",
                        lambda *a, **k: pytest.fail("direct path must not run"))
    v = paper_filter.filter_paper(paper(), "{title}", provider="gemini",
                                  model="baze:gemini-3.8-flash", api_key="g")
    assert seen == ["baze:gemini-3.8-flash"]
    assert (v.verdict, v.score, v.is_error) == ("YES", 8, False)
    assert v.model_used == "baze:gemini-3.8-flash"


def test_gateway_failure_falls_back_to_the_same_model_direct(monkeypatch):
    def broken(*a, **k):
        raise llm_gateway.GatewayError("HTTP 503")
    direct = []
    monkeypatch.setattr(paper_filter.llm_gateway, "chat", broken)
    monkeypatch.setattr(
        paper_filter, "_filter_with_gemini",
        lambda p, prompt, model, key, timeout: direct.append((model, key)) or
        Verdict("YES", 1, 7, "direct", model_used=model))
    v = paper_filter.filter_paper(paper(), "{title}", provider="gemini",
                                  model="baze:gemini-3.8-flash",
                                  fallback_models=["gemini-3.8-flash"], api_key="g")
    assert direct == [("gemini-3.8-flash", "g")]
    assert v.model_used == "gemini-3.8-flash" and not v.is_error


def test_whole_chain_failing_leaves_an_error_verdict(monkeypatch):
    def broken(*a, **k):
        raise llm_gateway.GatewayError("down")
    monkeypatch.setattr(paper_filter.llm_gateway, "chat", broken)
    v = paper_filter.filter_paper(paper(), "{title}", provider="gemini",
                                  model="baze:gemini-3.8-flash", api_key="g")
    assert v.is_error


# --- spam rescue / applicant drafts -----------------------------------------

def test_spam_classifier_uses_the_gateway_for_baze_models(monkeypatch):
    monkeypatch.setattr(classify.llm_gateway, "chat",
                        lambda prompt, model, **kw: '{"verdict": "RESCUE"}')
    assert classify.generate_text("p", "baze:gemini-3.8-flash", "g", 5) == (
        '{"verdict": "RESCUE"}', "")


def test_spam_classifier_reports_a_gateway_failure_as_an_error(monkeypatch):
    def broken(*a, **k):
        raise llm_gateway.GatewayError("HTTP 401")
    monkeypatch.setattr(classify.llm_gateway, "chat", broken)
    text, err = classify.generate_text("p", "baze:gemini-3.8-flash", "g", 5)
    assert text is None and "HTTP 401" in err


# --- watermark --------------------------------------------------------------

def test_only_sources_with_unjudged_papers_are_held():
    err = Verdict("NO", None, 0, "(error)", is_error=True)
    good = Verdict("NO", None, 2, "judged")
    results = [(paper(0, "rss"), err), (paper(1, "rss"), good),
               (paper(2, "pubmed"), good), (paper(3, "biorxiv"), good)]
    assert sources_with_errors(results) == {"rss"}
    assert sources_with_errors([(paper(4), good)]) == set()


def test_gateway_usage_summary_reads_cleanly():
    u = llm_gateway.GatewayUsage(calls=2, failures=1, prompt=200, completion=40, reasoning=10)
    assert u.summary().startswith("2 ok / 1 failed")
    assert llm_gateway.GatewayUsage().summary() == "no gateway calls"
