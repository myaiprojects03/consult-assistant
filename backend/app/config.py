"""Central settings. All secrets come from environment variables (or the project-root .env), never code."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = PROJECT_ROOT / "content"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    app_env: str = "development"
    # Sites allowed to embed the chat widget and call the API (comma-separated, no trailing slash).
    allowed_origins: str = "http://localhost:3000"

    # ---- AI model (swappable by config) ----
    # "anthropic" = Claude (production target). "groq" / "openai_compatible" = free stand-ins.
    llm_provider: str = "groq"
    anthropic_api_key: str = ""
    anthropic_chat_model: str = "claude-haiku-5-5"      # fast and low-cost: chat and voice
    anthropic_draft_model: str = "claude-sonnet-5-5"    # longer drafting (proposals, summaries)
    groq_api_key: str = ""
    groq_chat_model: str = "openai/gpt-oss-120b"
    # Background work (summaries) on another model: on Groq each model has its own per-minute allowance, so
    # summaries never take tokens from a live chat or call.
    groq_background_model: str = "openai/gpt-oss-20b"
    # Live chat and calls switch to this model when the main one is at its per-minute limit ("" = off).
    groq_fallback_model: str = "openai/gpt-oss-20b"
    groq_reasoning_effort: str = "low"
    llm_base_url: str = ""          # openai_compatible: e.g. https://openrouter.ai/api/v1
    llm_api_key: str = ""
    llm_chat_model: str = ""
    llm_timeout_s: float = 30.0
    llm_max_output_tokens: int = 700

    # ---- Knowledge search ----
    # "local" = fastembed on this server (no API cost); "none" = keyword search only.
    embedding_provider: str = "local"
    local_embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384 dimensions
    embedding_cache_dir: str = ""          # where the model is kept; default: .cache/fastembed in the project
    embedding_threads: int = 0             # 0 = all CPU cores; 1 on small hosts keeps memory low

    # ---- Infrastructure ----
    client_ip_header: str = ""             # visitor address header set by the host (Render: cf-connecting-ip)
    database_url: str = "postgresql://consult:consult@localhost:5432/consult"
    redis_url: str = "redis://localhost:6379/0"

    # ---- Background jobs (CRM sync, calendar events, emails, reminders, Slack) ----
    # local = a worker inside this server (development, or any always-on host)
    # qstash = Upstash QStash calls /api/jobs/run (serverless hosting such as Vercel)
    # manual = nothing runs jobs automatically (tests)
    job_runner: str = "local"
    public_api_url: str = ""               # this API's public address, for QStash callbacks
    qstash_url: str = "https://qstash.upstash.io"
    qstash_token: str = ""
    qstash_current_signing_key: str = ""
    qstash_next_signing_key: str = ""

    # ---- Integrations: each one is optional. Off = the action is written to the event log instead. ----
    crm_provider: str = "none"             # none | hubspot
    hubspot_access_token: str = ""
    hubspot_pipeline: str = "default"
    hubspot_deal_stage: str = "appointmentscheduled"
    calendar_provider: str = "local"       # local = bookings kept in the database only | google
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    email_provider: str = "none"           # none | gmail (sends from the Google account above)
    email_from_name: str = ""              # defaults to the firm name
    email_from_address: str = ""           # the Google account's address (needed to show the name above)
    ticketing_provider: str = "none"       # none (tickets in the database only) | jira
    jira_base_url: str = ""                # https://yoursite.atlassian.net
    jira_email: str = ""                   # the Atlassian account the API token belongs to
    jira_api_token: str = ""
    jira_project_key: str = "SUP"
    jira_issue_type: str = "Task"
    slack_bot_token: str = ""
    slack_channel_leads: str = "#leads"
    slack_channel_handovers: str = "#handovers"
    slack_channel_alerts: str = ""         # integration failures; defaults to the handovers channel
    slack_channel_support: str = ""        # new client tickets; defaults to the handovers channel

    # ---- Phase 4: voice and SMS (Twilio) ----
    voice_provider: str = "none"           # none (browser voice only) | twilio (phone calls)
    sms_provider: str = "none"             # none (SMS written to the event log) | twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""            # also signs our own call tokens
    twilio_phone_number: str = ""          # E.164, e.g. +441234567890: callers dial it; calls and SMS come from it
    voice_tts_provider: str = "ElevenLabs"   # ElevenLabs | Google | Amazon (spoken by Twilio)
    voice_tts_voice: str = ""              # empty = the provider's default voice
    voice_transcription_provider: str = "Deepgram"

    # ---- Chat limits ----
    chat_max_message_chars: int = 2000
    chat_messages_per_minute: int = 12     # per visitor network
    chat_sessions_per_hour: int = 30       # per visitor network
    chat_max_turns_per_session: int = 60
    chat_history_messages: int = 24        # recent messages sent to the model each turn
    chat_max_tool_rounds: int = 4
    team_max_message_chars: int = 12000    # internal assistant: room to paste meeting notes
    team_messages_per_minute: int = 20
    team_max_output_tokens: int = 1800     # drafts are longer than chat replies
    voice_max_output_tokens: int = 300     # spoken replies are short
    voice_calls_per_hour: int = 10         # browser voice calls per visitor network

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
