"""Environment-driven configuration. Nothing here talks to the network."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

PROMPT_VERSION = "p4"
# Keep in sync with vercel.json -> functions -> app/main.py -> maxDuration
VERCEL_MAX_DURATION_S = 60
MAX_CONTEXT_BYTES = 500 * 1024


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    store_backend: str
    upstash_url: str
    upstash_token: str
    key_prefix: str

    llm_mode: str
    groq_api_key: str
    groq_base_url: str
    primary_model: str
    secondary_model: str
    alt_name: str
    alt_base_url: str
    alt_api_key: str
    alt_model: str
    model_rpm: int
    model_tpm: int
    model_rpd: int
    model_tpd: int
    llm_call_timeout_s: int
    llm_seed: int
    llm_batch_size: int   # >1: compose several decisions per LLM call

    tick_deadline_s: float
    reply_deadline_s: float
    max_actions_per_tick: int

    team_name: str
    team_members: list[str]
    contact_email: str
    bot_version: str
    submitted_at: str
    debug_token: str

    same_version_status: int  # 200 (idempotent no-op, testing brief) or 409 (api-call-examples 1.5)

    @property
    def model_label(self) -> str:
        label = f"groq:{self.primary_model} (+{self.secondary_model.split('/')[-1]} fallback"
        if self.alt_model:
            label += f", {self.alt_name or 'alt'}:{self.alt_model}"
        return label + ", deterministic template fallback)"

    @classmethod
    def from_env(cls) -> "Settings":
        members = [m.strip() for m in _s("TEAM_MEMBERS", "Sobhan").split(",") if m.strip()]
        return cls(
            store_backend=_s("STORE_BACKEND", "redis").lower(),
            upstash_url=_s("UPSTASH_REDIS_REST_URL"),
            upstash_token=_s("UPSTASH_REDIS_REST_TOKEN"),
            key_prefix=_s("REDIS_KEY_PREFIX", "vera:"),
            llm_mode=_s("LLM_MODE", "live").lower(),
            groq_api_key=_s("GROQ_API_KEY"),
            groq_base_url=_s("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            primary_model=_s("PRIMARY_MODEL", "openai/gpt-oss-120b"),
            secondary_model=_s("SECONDARY_MODEL", "openai/gpt-oss-20b"),
            alt_name=_s("ALT_PROVIDER_NAME"),
            alt_base_url=_s("ALT_BASE_URL"),
            alt_api_key=_s("ALT_API_KEY"),
            alt_model=_s("ALT_MODEL"),
            model_rpm=_i("MODEL_RPM", 30),
            model_tpm=_i("MODEL_TPM", 8000),
            model_rpd=_i("MODEL_RPD", 1000),
            model_tpd=_i("MODEL_TPD", 200000),
            llm_call_timeout_s=_i("LLM_CALL_TIMEOUT_S", 12),
            llm_seed=_i("LLM_SEED", 7),
            llm_batch_size=max(1, min(_i("LLM_BATCH_SIZE", 1), 4)),
            tick_deadline_s=float(min(_i("TICK_DEADLINE_S", 20), VERCEL_MAX_DURATION_S - 5)),
            reply_deadline_s=float(min(_i("REPLY_DEADLINE_S", 15), VERCEL_MAX_DURATION_S - 5)),
            max_actions_per_tick=_i("MAX_ACTIONS_PER_TICK", 20),
            team_name=_s("TEAM_NAME", "Sobhan"),
            team_members=members or ["Sobhan"],
            contact_email=_s("CONTACT_EMAIL"),
            bot_version=_s("BOT_VERSION", "1.0.0"),
            submitted_at=_s("SUBMITTED_AT"),
            debug_token=_s("DEBUG_TOKEN"),
            same_version_status=409 if _i("SAME_VERSION_STATUS", 200) == 409 else 200,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings() -> None:
    get_settings.cache_clear()
