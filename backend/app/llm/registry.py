"""Picks the AI model from settings. Adding a provider = one class + one branch here."""
from __future__ import annotations

from app.config import Settings
from app.llm.anthropic_llm import AnthropicProvider
from app.llm.base import FallbackLLM, LLMError, LLMProvider
from app.llm.openai_compat_llm import OpenAICompatibleProvider

GROQ_URL = "https://api.groq.com/openai/v1"
PROVIDERS = ("anthropic", "groq", "openai_compatible")


def build_llm(settings: Settings, purpose: str = "chat") -> LLMProvider:
    """purpose: "chat" (website chat and calls), "draft" (the internal assistant's proposals and summaries:
    the stronger Claude model), "background" (conversation summaries: on Groq a separate model, so its own
    rate limits)."""
    p = settings.llm_provider
    if p == "anthropic":
        model = settings.anthropic_draft_model if purpose == "draft" else settings.anthropic_chat_model
        return AnthropicProvider(settings.anthropic_api_key, model, settings.llm_timeout_s)
    if p == "groq":
        def groq(model: str) -> OpenAICompatibleProvider:
            return OpenAICompatibleProvider(service="Groq", base_url=GROQ_URL, api_key=settings.groq_api_key,
                                            model=model, key_setting="GROQ_API_KEY",
                                            timeout_s=settings.llm_timeout_s,
                                            reasoning_effort=settings.groq_reasoning_effort)
        if purpose == "background":
            return groq(settings.groq_background_model or settings.groq_chat_model)
        main = groq(settings.groq_chat_model)
        if settings.groq_fallback_model and settings.groq_fallback_model != settings.groq_chat_model:
            return FallbackLLM(main, groq(settings.groq_fallback_model))
        return main
    if p == "openai_compatible":
        return OpenAICompatibleProvider(service="OpenAI-compatible API", base_url=settings.llm_base_url,
                                        api_key=settings.llm_api_key, model=settings.llm_chat_model,
                                        key_setting="LLM_API_KEY", timeout_s=settings.llm_timeout_s)
    raise LLMError(f"Unknown LLM_PROVIDER '{p}'. Available: {', '.join(PROVIDERS)}.")
