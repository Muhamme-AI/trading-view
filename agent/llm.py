"""LLM provider helpers — Groq (preferred) or OpenAI."""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _groq_key() -> str | None:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    return key or None


def _openai_key() -> str | None:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return key or None


def is_agent_configured() -> bool:
    return bool(_groq_key() or _openai_key())


def get_provider() -> str:
    if _groq_key():
        return "groq"
    if _openai_key():
        return "openai"
    return "none"


def get_chat_model_name() -> str:
    if get_provider() == "groq":
        return os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    return os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def get_chat_llm(*, temperature: float | None = None, streaming: bool = False) -> ChatOpenAI:
    temp = float(os.getenv("OPENAI_TEMPERATURE", "0.2")) if temperature is None else temperature
    if _groq_key():
        return ChatOpenAI(
            model=get_chat_model_name(),
            api_key=_groq_key(),
            base_url=GROQ_BASE_URL,
            temperature=temp,
            streaming=streaming,
        )
    if _openai_key():
        return ChatOpenAI(
            model=get_chat_model_name(),
            api_key=_openai_key(),
            temperature=temp,
            streaming=streaming,
        )
    raise RuntimeError("No LLM API key configured (set GROQ_API_KEY or OPENAI_API_KEY)")


def agent_config_message() -> str:
    if is_agent_configured():
        return ""
    return (
        "AI advisor is not configured. Add `GROQ_API_KEY` (recommended) or `OPENAI_API_KEY` "
        "to your `.env` file locally, or to Vercel Environment Variables for production."
    )
