"""Output guardrails — refuse uncertain directional advice."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

DIRECTIONAL_PATTERNS = re.compile(
    r"\b(buy|sell|long|short|enter now|go long|go short|definitely|guaranteed|certainly will)\b",
    re.I,
)

UNCERTAIN_PATTERNS = re.compile(
    r"\b(insufficient data|not enough data|cannot advise|no clear edge|sit out|skip this|wait for clarity|uncertain|low confidence)\b",
    re.I,
)

REFUSAL_TEMPLATE = (
    "I don't have enough grounded evidence in your data to give a confident directional call on GBP/USD here. "
    "What I can say from the records: {summary}. "
    "Recommendation: **wait for the release and confirm reaction** before committing size — or skip if historical power is below 10 pips / consistency is weak."
)


class GuardrailVerdict(BaseModel):
    confidence: str = Field(description="high, medium, or low")
    has_directional_advice: bool
    should_refuse: bool
    reason: str
    safe_summary: str = ""


def assess_confidence_from_context(
    retrieved_text: str,
    tool_outputs: list[str],
    live_context: dict | None,
) -> str:
    """Heuristic confidence from available grounding."""
    text = (retrieved_text + " ".join(tool_outputs)).lower()
    has_reactions = "pip" in text and ("avg" in text or "reaction" in text)
    has_consistency = "consistency" in text
    tradeable = "tradeable" in text and "yes" in text
    low_power = "no reaction data" in text or "tradeable (10p+ avg 5m): no" in text

    if live_context:
        snap = live_context.get("snapshot") or {}
        if snap.get("avg_pip_5m") is not None and snap["avg_pip_5m"] < 8:
            low_power = True
        if snap.get("tradeable") is False:
            low_power = True

    if has_reactions and has_consistency and tradeable and not low_power:
        return "high"
    if has_reactions or has_consistency:
        return "medium"
    return "low"


def apply_guardrails(
    response: str,
    confidence: str,
    retrieved_summary: str = "",
) -> tuple[str, GuardrailVerdict]:
    has_directional = bool(DIRECTIONAL_PATTERNS.search(response))
    already_cautious = bool(UNCERTAIN_PATTERNS.search(response))

    should_refuse = confidence == "low" and has_directional and not already_cautious
    if confidence == "low" and has_directional and not already_cautious:
        should_refuse = True

    if should_refuse:
        summary = retrieved_summary[:400] if retrieved_summary else "historical reaction data is limited or inconclusive"
        safe = REFUSAL_TEMPLATE.format(summary=summary)
        verdict = GuardrailVerdict(
            confidence=confidence,
            has_directional_advice=has_directional,
            should_refuse=True,
            reason="Low confidence with directional language — replaced with cautious guidance.",
            safe_summary=summary,
        )
        return safe, verdict

    verdict = GuardrailVerdict(
        confidence=confidence,
        has_directional_advice=has_directional,
        should_refuse=False,
        reason="Response passed guardrails.",
        safe_summary="",
    )
    return response, verdict


def llm_guardrail_check(response: str, context_snippet: str) -> GuardrailVerdict | None:
    """Optional LLM-based guardrail when OPENAI_API_KEY is set."""
    api_key = __import__("os").getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        llm = ChatOpenAI(
            model=__import__("os").getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0,
        ).with_structured_output(GuardrailVerdict)
        prompt = f"""Review this trading assistant reply for a GBP/USD fundamental news trader.
Context available:
{context_snippet[:2000]}

Assistant reply:
{response}

Rules:
- confidence=low if reaction data missing, avg 5M pips < 10, or direction consistency < 60%
- has_directional_advice=true if it tells user to buy/sell/enter with conviction
- should_refuse=true if giving directional trade advice without sufficient grounded data
- provide safe_summary: one sentence of what IS known from data"""
        return llm.invoke(prompt)
    except Exception:
        return None
