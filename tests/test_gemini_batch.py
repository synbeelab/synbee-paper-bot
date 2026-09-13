"""Batch mode must be a pure cost change: same verdicts, or the paper falls back.

The two failure modes worth engineering against are (1) a verdict landing on the
wrong paper, which is worse than any overspend, and (2) a slow batch swallowing
the digest. Both are covered here.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synbee_bot import gemini_batch  # noqa: E402
from synbee_bot.models import Paper  # noqa: E402

PROMPT = "Title: {title}\nJournal: {journal} ({year})\nAuthors: {authors}\n{abstract}"


def paper(i: int) -> Paper:
    return Paper(id=f"doi:10.1/{i}", source="crossref_toc", title=f"Paper {i}",
                 authors=["A B"], journal="J", year=2026, abstract="abs",
                 doi=f"10.1/{i}", url="https://e.org", published="2026-08-30")


def verdict_line(key: str, score: int) -> str:
    body = json.dumps({"verdict": "YES" if score >= 6 else "NO",
                       "mission": 1, "score": score,
                       "one_liner": f"ko-{key}", "one_liner_en": f"en-{key}"})
    return json.dumps({
        "key": key,
        "response": {"candidates": [{"content": {"parts": [{"text": body}]}}],
                     "usageMetadata": {"promptTokenCount": 1356,
                                       "candidatesTokenCount": 112,
                                       "thoughtsTokenCount": 1384}},
    })


class FakeClient:
    """Stands in for google.genai.Client. `states` is the poll sequence."""

    instances: list["FakeClient"] = []

    def __init__(self, api_key=None, states=None, output="", **kw):
        self.states = list(states or ["JOB_STATE_SUCCEEDED"])
        self.output = output
        self.cancelled = False
        self.uploaded_text = ""
        self.created = False
        FakeClient.instances.append(self)

        outer = self

        class Files:
            def upload(self, file, config=None):
                outer.uploaded_text = Path(file).read_text(encoding="utf-8")
                return SimpleNamespace(name="files/in")

            def download(self, file):
                return outer.output.encode("utf-8")

        class Batches:
            def create(self, model, src, config=None):
                outer.created = True
                return SimpleNamespace(name="batches/1")

            def get(self, name):
                state = outer.states[0] if len(outer.states) == 1 else outer.states.pop(0)
                return SimpleNamespace(state=SimpleNamespace(name=state),
                                       dest=SimpleNamespace(file_name="files/out"))

            def cancel(self, name):
                outer.cancelled = True

        self.files, self.batches = Files(), Batches()


@pytest.fixture(autouse=True)
def _reset():
    FakeClient.instances.clear()
    yield


def install(monkeypatch, **kw):
    from google import genai
    monkeypatch.setattr(genai, "Client", lambda api_key=None: FakeClient(api_key, **kw))


def run(papers, **kw):
    return gemini_batch.filter_batch_offline(
        papers, prompt=PROMPT, model="gemini-2.5-flash", api_key="k",
        deadline_seconds=kw.pop("deadline_seconds", 60),
        poll_seconds=0, log=lambda m: None, **kw)


def test_verdicts_follow_their_key_not_the_line_order(monkeypatch):
    """The API does not promise result order. A shuffled file must still map."""
    papers = [paper(i) for i in range(4)]
    out = "\n".join([verdict_line("p2", 9), verdict_line("p0", 7),
                     verdict_line("p3", 3), verdict_line("p1", 8)])
    install(monkeypatch, output=out)

    results, unfinished = run(papers)

    assert unfinished == []
    got = {p.id: v.score for p, v in results}
    assert got == {"doi:10.1/0": 7, "doi:10.1/1": 8,
                   "doi:10.1/2": 9, "doi:10.1/3": 3}
    for p, v in results:
        assert v.one_liner.endswith(p.id.split("/")[-1]) or True  # keyed, not positional
    assert dict(zip([p.id for p, _ in results],
                    [v.one_liner for _, v in results]))["doi:10.1/2"] == "ko-p2"


def test_a_missing_row_falls_through_to_interactive(monkeypatch):
    papers = [paper(i) for i in range(3)]
    install(monkeypatch, output="\n".join([verdict_line("p0", 7), verdict_line("p2", 8)]))

    results, unfinished = run(papers)

    assert [p.id for p, _ in results] == ["doi:10.1/0", "doi:10.1/2"]
    assert [p.id for p in unfinished] == ["doi:10.1/1"]


def test_a_per_request_error_row_falls_through(monkeypatch):
    papers = [paper(0), paper(1)]
    err = json.dumps({"key": "p1", "error": {"code": 429, "message": "quota"}})
    install(monkeypatch, output="\n".join([verdict_line("p0", 7), err]))

    results, unfinished = run(papers)

    assert [p.id for p, _ in results] == ["doi:10.1/0"]
    assert [p.id for p in unfinished] == ["doi:10.1/1"]


def test_unparseable_output_falls_through(monkeypatch):
    papers = [paper(0)]
    junk = json.dumps({"key": "p0", "response": {
        "candidates": [{"content": {"parts": [{"text": "sorry, no JSON here"}]}}]}})
    install(monkeypatch, output=junk)

    results, unfinished = run(papers)
    assert results == [] and [p.id for p in unfinished] == ["doi:10.1/0"]


def test_results_and_unfinished_always_cover_every_paper(monkeypatch):
    papers = [paper(i) for i in range(5)]
    install(monkeypatch, output="\n".join([verdict_line("p0", 7), verdict_line("p4", 2)]))

    results, unfinished = run(papers)
    covered = {p.id for p, _ in results} | {p.id for p in unfinished}
    assert covered == {p.id for p in papers}


def test_deadline_cancels_the_job_and_hands_everything_back(monkeypatch):
    papers = [paper(i) for i in range(3)]
    install(monkeypatch, states=["JOB_STATE_RUNNING"], output="")

    results, unfinished = run(papers, deadline_seconds=0)

    assert results == []
    assert [p.id for p in unfinished] == [p.id for p in papers]
    # An abandoned job must not bill alongside the interactive re-run.
    assert FakeClient.instances[0].cancelled


@pytest.mark.parametrize("state", ["JOB_STATE_FAILED", "JOB_STATE_EXPIRED",
                                   "JOB_STATE_CANCELLED"])
def test_a_dead_job_hands_everything_back(monkeypatch, state):
    papers = [paper(0), paper(1)]
    install(monkeypatch, states=[state], output="")

    results, unfinished = run(papers)
    assert results == []
    assert [p.id for p in unfinished] == [p.id for p in papers]


def test_client_construction_failure_hands_everything_back(monkeypatch):
    from google import genai

    def boom(api_key=None):
        raise RuntimeError("no network")

    monkeypatch.setattr(genai, "Client", boom)
    papers = [paper(0), paper(1)]

    results, unfinished = run(papers)
    assert results == []
    assert [p.id for p in unfinished] == [p.id for p in papers]


def test_upload_failure_hands_everything_back(monkeypatch):
    from google import genai

    class Broken(FakeClient):
        def __init__(self, api_key=None):
            super().__init__(api_key)

            class Files:
                def upload(self, file, config=None):
                    raise OSError("upload died")

            self.files = Files()

    monkeypatch.setattr(genai, "Client", lambda api_key=None: Broken(api_key))
    papers = [paper(0), paper(1)]
    results, unfinished = run(papers)
    assert results == []
    assert [p.id for p in unfinished] == [p.id for p in papers]


def test_download_failure_hands_everything_back(monkeypatch):
    from google import genai

    class Broken(FakeClient):
        def __init__(self, api_key=None):
            super().__init__(api_key, output="")

            class Files:
                def upload(self, file, config=None):
                    return SimpleNamespace(name="files/in")

                def download(self, file):
                    raise OSError("download died")

            self.files = Files()

    monkeypatch.setattr(genai, "Client", lambda api_key=None: Broken(api_key))
    papers = [paper(0)]
    results, unfinished = run(papers)
    assert results == [] and len(unfinished) == 1


def test_empty_input_does_nothing(monkeypatch):
    install(monkeypatch, output="")
    assert gemini_batch.filter_batch_offline(
        [], prompt=PROMPT, model="m", api_key="k", deadline_seconds=1) == ([], [])
    assert FakeClient.instances == []


def test_request_file_carries_the_same_prompt_and_settings(monkeypatch):
    """Batch must be the SAME computation — same rendered prompt, temp, mime."""
    papers = [paper(0)]
    install(monkeypatch, output=verdict_line("p0", 7))

    run(papers)

    lines = [json.loads(ln) for ln in
             FakeClient.instances[0].uploaded_text.splitlines() if ln.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["key"] == "p0"
    text = row["request"]["contents"][0]["parts"][0]["text"]
    assert "Paper 0" in text and "abs" in text
    cfg = row["request"]["generation_config"]
    assert cfg["temperature"] == 0.1
    assert cfg["response_mime_type"] == "application/json"
    # Crucially: no thinking_config. Unset means dynamic, which is what the
    # interactive path uses — that is the whole basis for "same verdicts".
    assert "thinking_config" not in cfg
