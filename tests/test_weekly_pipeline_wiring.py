"""The weekly run must actually use the three new stages, in the right order.

Unit tests cover each piece; this covers the wiring, which is where a stage
quietly stops being called.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from synbee_bot.models import Paper, Verdict  # noqa: E402
from synbee_bot.storage import SeenDB  # noqa: E402


def _load_run_weekly():
    spec = importlib.util.spec_from_file_location(
        "run_weekly", ROOT / "scripts" / "run_weekly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def paper(pid: str, title: str) -> Paper:
    return Paper(id=pid, source="pubmed", title=title, authors=["A"],
                 journal="J", year=2026, abstract="", doi=None,
                 url=f"https://example.org/{pid}", published="2026-08-28")


def _cfg(db_path, **over):
    class Full:
        slack_enabled = False
        slack_bot_token = ""
        weekly_channel = ""
        weekly_enabled = True
        weekly_since_days = 7
        weekly_min_score = 6
        weekly_max_posts = None
        weekly_llm_provider = "gemini"
        weekly_llm_model = "gemini-2.5-flash"
        weekly_llm_fallback_models: list[str] = []
        weekly_batch_enabled = True
        weekly_batch_deadline_minutes = 1
        weekly_batch_poll_seconds = 0
        llm_enabled = True
        llm_prompt_path = ROOT / "config" / "filter_prompt.md"
        llm_timeout = 30
        prefilter_non_articles = True
        abstract_backfill_enabled = True
        abstract_backfill_timeout = 5
        gemini_api_key = "k"
        anthropic_api_key = ""
        max_since_days = 30
        seen_db_path = db_path

    for k, v in over.items():
        setattr(Full, k, v)
    return Full


def _wire(rw, monkeypatch, db_path, papers, cfg):
    monkeypatch.setattr(rw, "fetch_from_pubmed_weekly", lambda since_days: papers)
    monkeypatch.setattr(rw, "load_toc_config", lambda: ([], 3))
    monkeypatch.setattr(rw, "SeenDB", lambda _p: SeenDB(db_path))
    monkeypatch.setattr(rw, "load_config", lambda: cfg())
    monkeypatch.setattr(sys, "argv", ["run_weekly.py", "--no-slack"])


def test_corrections_never_reach_the_filter(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    papers = [paper("p1", "Author Correction: something"),
              paper("p2", "A modular PKS platform for polyketide diversification")]
    judged: list[list[str]] = []

    monkeypatch.setattr(rw, "backfill_abstracts",
                        lambda ps, **kw: ps)
    monkeypatch.setattr(rw, "filter_batch_offline",
                        lambda ps, **kw: ([], list(ps)))
    monkeypatch.setattr(rw, "filter_batch",
                        lambda ps, **kw: judged.append([p.id for p in ps])
                        or [(p, Verdict("NO", None, 1, "no")) for p in ps])
    _wire(rw, monkeypatch, tmp_path / "seen.db", papers, lambda: _cfg(tmp_path / "seen.db"))

    assert rw.main() == 0
    assert judged == [["p2"]], "the correction must be dropped before Stage 2"


def test_backfill_runs_after_the_prefilter_and_before_the_filter(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    papers = [paper("p1", "Retraction Note: gone"),
              paper("p2", "Directed evolution of a thermostable lipase")]
    order: list[str] = []
    seen_by_backfill: list[list[str]] = []

    def fake_backfill(ps, **kw):
        order.append("backfill")
        seen_by_backfill.append([p.id for p in ps])
        return ps

    monkeypatch.setattr(rw, "backfill_abstracts", fake_backfill)
    monkeypatch.setattr(rw, "filter_batch_offline", lambda ps, **kw: ([], list(ps)))
    monkeypatch.setattr(rw, "filter_batch",
                        lambda ps, **kw: order.append("filter")
                        or [(p, Verdict("NO", None, 1, "no")) for p in ps])
    _wire(rw, monkeypatch, tmp_path / "seen.db", papers, lambda: _cfg(tmp_path / "seen.db"))

    rw.main()

    assert order == ["backfill", "filter"]
    # Not asked to look up an abstract for a paper that was already dropped.
    assert seen_by_backfill == [["p2"]]


def test_batch_verdicts_are_kept_and_only_the_rest_go_interactive(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    papers = [paper(f"p{i}", f"Real paper {i}") for i in range(3)]
    interactive: list[list[str]] = []

    def fake_batch(ps, **kw):
        done = [(ps[0], Verdict("YES", 1, 9, "batch-judged"))]
        return done, list(ps[1:])

    monkeypatch.setattr(rw, "backfill_abstracts", lambda ps, **kw: ps)
    monkeypatch.setattr(rw, "filter_batch_offline", fake_batch)
    monkeypatch.setattr(rw, "filter_batch",
                        lambda ps, **kw: interactive.append([p.id for p in ps])
                        or [(p, Verdict("YES", 1, 7, "interactive")) for p in ps])
    _wire(rw, monkeypatch, tmp_path / "seen.db", papers, lambda: _cfg(tmp_path / "seen.db"))

    assert rw.main() == 0
    assert interactive == [["p1", "p2"]]

    db = SeenDB(tmp_path / "seen.db")
    rows = {r["id"]: r["one_liner"] for r in
            db.conn.execute("SELECT id, one_liner FROM seen").fetchall()}
    db.close()
    # Every paper is judged exactly once, by whichever path took it.
    assert rows == {"p0": "batch-judged", "p1": "interactive", "p2": "interactive"}


def test_batch_is_skipped_when_disabled(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    papers = [paper("p0", "Real paper")]
    called = {"batch": False}

    def fake_batch(ps, **kw):
        called["batch"] = True
        return [], list(ps)

    monkeypatch.setattr(rw, "backfill_abstracts", lambda ps, **kw: ps)
    monkeypatch.setattr(rw, "filter_batch_offline", fake_batch)
    monkeypatch.setattr(rw, "filter_batch",
                        lambda ps, **kw: [(p, Verdict("NO", None, 1, "no")) for p in ps])
    _wire(rw, monkeypatch, tmp_path / "seen.db", papers,
          lambda: _cfg(tmp_path / "seen.db", weekly_batch_enabled=False))

    rw.main()
    assert not called["batch"]


def test_batch_is_skipped_for_a_non_gemini_provider(tmp_path, monkeypatch):
    rw = _load_run_weekly()
    papers = [paper("p0", "Real paper")]
    called = {"batch": False}

    def fake_batch(ps, **kw):
        called["batch"] = True
        return [], list(ps)

    monkeypatch.setattr(rw, "backfill_abstracts", lambda ps, **kw: ps)
    monkeypatch.setattr(rw, "filter_batch_offline", fake_batch)
    monkeypatch.setattr(rw, "filter_batch",
                        lambda ps, **kw: [(p, Verdict("NO", None, 1, "no")) for p in ps])
    _wire(rw, monkeypatch, tmp_path / "seen.db", papers,
          lambda: _cfg(tmp_path / "seen.db", weekly_llm_provider="anthropic",
                       anthropic_api_key="k"))

    rw.main()
    assert not called["batch"], "the batch path is Gemini-only"
