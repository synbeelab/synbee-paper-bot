"""Config loader — reads .env and config/*.yml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False


def _optional_cap(value: object) -> int | None:
    """Parse a post-count cap. null / absent / 0 / negative all mean "no cap".

    Truncating the post list silently drops papers that already passed the
    filter, and they get marked seen right after, so they are never revisited.
    Uncapped is therefore the default; the knob only exists as an escape hatch
    if a runaway backfill ever needs one.
    """
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class Config:
    # Sources
    pubmed_enabled: bool
    pubmed_since_days: int
    biorxiv_enabled: bool
    biorxiv_since_days: int
    rss_enabled: bool
    rss_since_days: int

    # LLM filter
    llm_enabled: bool
    llm_provider: str
    llm_model: str
    llm_fallback_models: list[str]
    llm_prompt_path: Path
    llm_min_score: int
    llm_parallel: int
    llm_timeout: int

    # Slack
    slack_enabled: bool
    slack_bot_token: str
    slack_channel_daily: str
    slack_channel_priority: str
    slack_channel_test: str
    slack_use_test: bool
    slack_max_posts: int | None

    # Weekly journal-sweep digest (delta vs daily)
    weekly_enabled: bool
    weekly_channel: str
    weekly_since_days: int
    weekly_min_score: int
    weekly_max_posts: int | None
    weekly_llm_provider: str
    weekly_llm_model: str
    weekly_llm_fallback_models: list[str]

    # Storage
    seen_db_path: Path

    # Wiki queue (GitHub Issue + local raw/ ingest)
    wiki_github_repo: str             # "owner/repo" — for "위키 후보" button URL
    wiki_vault_raw_dir: Path          # local raw/ directory for process_wiki_queue.py

    # Secrets
    gemini_api_key: str
    anthropic_api_key: str
    ncbi_api_key: str
    ncbi_email: str

    def target_channel(self, score: int) -> str:
        """Pick channel by score and test-mode flag."""
        if self.slack_use_test and self.slack_channel_test:
            return self.slack_channel_test
        if score >= 9 and self.slack_channel_priority:
            return self.slack_channel_priority
        return self.slack_channel_daily


def load_config(config_path: Path | None = None) -> Config:
    """Load .env + config/config.yml. Falls back to config.yml.example for defaults."""
    if _HAS_DOTENV:
        load_dotenv(PROJECT_ROOT / ".env")

    if config_path is None:
        config_path = CONFIG_DIR / "config.yml"
        if not config_path.exists():
            config_path = CONFIG_DIR / "config.yml.example"
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    sources = cfg.get("sources", {})
    pubmed = sources.get("pubmed", {})
    biorxiv = sources.get("biorxiv", {})
    rss = sources.get("rss", {})

    llm = cfg.get("llm_filter", {})
    slack = cfg.get("slack", {})
    storage = cfg.get("storage", {})
    wiki = cfg.get("wiki_queue", {})
    weekly = cfg.get("weekly", {}) or {}
    weekly_llm = weekly.get("llm", {}) or {}

    return Config(
        pubmed_enabled=bool(pubmed.get("enabled", True)),
        pubmed_since_days=int(pubmed.get("since_days", 1)),
        biorxiv_enabled=bool(biorxiv.get("enabled", True)),
        biorxiv_since_days=int(biorxiv.get("since_days", 1)),
        rss_enabled=bool(rss.get("enabled", True)),
        rss_since_days=int(rss.get("since_days", 1)),
        llm_enabled=bool(llm.get("enabled", True)),
        llm_provider=str(llm.get("provider", "gemini")),
        llm_model=str(llm.get("model", "gemini-2.5-flash")),
        llm_fallback_models=[str(m) for m in (llm.get("fallback_models") or [])],
        llm_prompt_path=PROJECT_ROOT / str(llm.get("prompt_path", "config/filter_prompt.md")),
        llm_min_score=int(llm.get("min_score", 6)),
        llm_parallel=int(llm.get("parallel_requests", 4)),
        llm_timeout=int(llm.get("timeout_seconds", 30)),
        slack_enabled=bool(slack.get("enabled", True)),
        slack_bot_token=os.environ.get(slack.get("bot_token_env", "SLACK_BOT_TOKEN"), ""),
        slack_channel_daily=str(slack.get("channels", {}).get("daily_digest", "")),
        slack_channel_priority=str(slack.get("channels", {}).get("high_priority", "")),
        slack_channel_test=str(slack.get("channels", {}).get("test", "")),
        slack_use_test=bool(slack.get("use_test_channel", True)),
        slack_max_posts=_optional_cap(slack.get("max_posts_per_run")),
        weekly_enabled=bool(weekly.get("enabled", True)),
        weekly_channel=str(weekly.get("channel", "")),
        weekly_since_days=int(weekly.get("since_days", 7)),
        weekly_min_score=int(weekly.get("min_score", 6)),
        weekly_max_posts=_optional_cap(weekly.get("max_posts")),
        weekly_llm_provider=str(weekly_llm.get("provider", "anthropic")),
        weekly_llm_model=str(weekly_llm.get("model", "claude-haiku-4-5-20251001")),
        weekly_llm_fallback_models=[str(m) for m in (weekly_llm.get("fallback_models") or [])],
        seen_db_path=PROJECT_ROOT / str(storage.get("seen_db", "data/seen.db")),
        wiki_github_repo=str(wiki.get("github_repo", "")),
        wiki_vault_raw_dir=Path(str(wiki.get("vault_raw_dir", "D:/Obsidian_Vault/Dongsoo/raw"))),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        ncbi_api_key=os.environ.get("NCBI_API_KEY", ""),
        ncbi_email=os.environ.get("NCBI_EMAIL", "dosoyang@korea.ac.kr"),
    )
