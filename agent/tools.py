"""Structured DB tools for precise grounding."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from db import get_db
from scraper import get_analysis_summary, get_brief, get_event_history, get_upcoming_from_db


@tool
def lookup_event_analysis(event_name: str) -> str:
    """Get aggregated historical analysis for a specific news event (pip moves, beat/miss direction)."""
    summary = get_analysis_summary()
    match = next((r for r in summary if r["your_name"] == event_name), None)
    if not match:
        # partial match
        match = next((r for r in summary if event_name.lower() in r["your_name"].lower()), None)
    if not match:
        return json.dumps({"error": f"No analysis found for '{event_name}'"})
    return json.dumps(match, default=str)


@tool
def lookup_event_history(event_name: str, limit: int = 12) -> str:
    """Get recent release history with actuals and GBP/USD price reactions for an event."""
    rows = get_event_history(event_name)[:limit]
    if not rows:
        return json.dumps({"error": f"No history for '{event_name}'"})
    slim = [
        {
            "date": r.get("event_date"),
            "time": r.get("event_time"),
            "previous": r.get("previous"),
            "forecast": r.get("forecast"),
            "actual": r.get("actual"),
            "beat_miss": r.get("beat_miss"),
            "pip_5m": r.get("pip_5m"),
            "pip_15m": r.get("pip_15m"),
            "direction_5m": r.get("direction_5m"),
        }
        for r in rows
    ]
    return json.dumps(slim, default=str)


@tool
def lookup_trade_brief(event_name: str) -> str:
    """Get the pre-built trading brief for an upcoming or recent event."""
    brief = get_brief(event_name)
    return json.dumps(brief, default=str)


@tool
def lookup_news_rating(event_name: str) -> str:
    """Get the trader's personal news quality rating (Good/Caution/Bad etc.)."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT name, type, comment FROM news_ratings WHERE name ILIKE %s LIMIT 1",
            (f"%{event_name}%",),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return json.dumps({"error": f"No rating for '{event_name}'"})
    return json.dumps(dict(row))


@tool
def lookup_upcoming_news(days: int = 7) -> str:
    """List upcoming USD/GBP news events in the next N days."""
    events = get_upcoming_from_db(days)
    slim = [
        {
            "event": e.get("your_name"),
            "date": e.get("event_date"),
            "time": e.get("event_time"),
            "country": e.get("country"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
        }
        for e in events
    ]
    return json.dumps(slim, default=str)


@tool
def lookup_recent_trades(limit: int = 15) -> str:
    """Get recent logged trades with outcomes and lessons."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT date, news1, news2, news3, outcome, ratio, improvement, trade_type
            FROM trades ORDER BY date DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return json.dumps([dict(r) for r in rows], default=str)


AGENT_TOOLS = [
    lookup_event_analysis,
    lookup_event_history,
    lookup_trade_brief,
    lookup_news_rating,
    lookup_upcoming_news,
    lookup_recent_trades,
]
