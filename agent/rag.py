"""RAG index built from trading news, analysis, and journal data."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

from db import get_db
from scraper import get_analysis_summary, get_upcoming_from_db

_INDEX_LOCK = threading.Lock()
_INDEX: FAISS | None = None
_INDEX_BUILT_AT: float = 0.0
_INDEX_TTL_SEC = 600  # 10 minutes


def _impact_label(impact: str | None) -> str:
    if impact == "red":
        return "HIGH impact"
    if impact == "orange":
        return "MEDIUM impact"
    return impact or "unknown impact"


def _build_documents() -> list[Document]:
    docs: list[Document] = []

    for row in get_analysis_summary():
        name = row.get("your_name", "")
        avg5 = row.get("avg_pip_5m")
        tradeable = row.get("tradeable")
        text = (
            f"Event analysis: {name} ({row.get('country', '')}, {_impact_label(row.get('impact'))}). "
            f"Occurrences: {row.get('total_occurrences', 0)}. "
            f"Avg 5M pips: {avg5 if avg5 is not None else 'no reaction data'}. "
            f"Avg 15M: {row.get('avg_pip_15m')}. Avg 30M: {row.get('avg_pip_30m')}. "
            f"Beat rate: {row.get('beat_rate')}%. "
            f"On beat, direction {row.get('beat_direction')} ({row.get('beat_consistency')}% consistency). "
            f"On miss, direction {row.get('miss_direction')} ({row.get('miss_consistency')}% consistency). "
            f"Tradeable (10p+ avg 5M): {'yes' if tradeable else 'no'}."
        )
        docs.append(Document(page_content=text, metadata={"type": "analysis", "event": name}))

    conn = get_db()
    try:
        ratings = conn.execute("SELECT name, type, comment FROM news_ratings ORDER BY name").fetchall()
        trades = conn.execute(
            "SELECT date, news1, news2, news3, outcome, ratio, improvement, trade_type FROM trades ORDER BY date DESC LIMIT 80"
        ).fetchall()
        recent_events = conn.execute("""
            SELECT ne.your_name, ne.event_date, ne.event_time, ne.country, ne.impact,
                   ne.previous, ne.forecast, ne.actual, ne.beat_miss,
                   pr.pip_5m, pr.pip_15m, pr.direction_5m
            FROM news_events ne
            LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE ne.actual IS NOT NULL AND ne.actual != ''
            ORDER BY ne.event_date DESC
            LIMIT 120
        """).fetchall()
    finally:
        conn.close()

    for r in ratings:
        d = dict(r)
        text = (
            f"News rating: {d['name']} — quality {d['type']}. "
            f"Trader note: {d.get('comment') or 'none'}."
        )
        docs.append(Document(page_content=text, metadata={"type": "rating", "event": d["name"]}))

    for t in trades:
        d = dict(t)
        news = ", ".join(x for x in [d.get("news1"), d.get("news2"), d.get("news3")] if x)
        text = (
            f"Logged trade ({d.get('trade_type', 'backtesting')}) on {d.get('date')}: {news}. "
            f"Outcome: {d.get('outcome')}. R:R 1:{d.get('ratio')}. "
            f"Lesson: {d.get('improvement') or 'none recorded'}."
        )
        docs.append(Document(page_content=text, metadata={"type": "trade", "date": d.get("date")}))

    for ev in recent_events:
        d = dict(ev)
        text = (
            f"Release {d['your_name']} on {d['event_date']} {d.get('event_time') or ''} "
            f"({d.get('country')}, {_impact_label(d.get('impact'))}). "
            f"Prev {d.get('previous') or '—'}, forecast {d.get('forecast') or '—'}, "
            f"actual {d.get('actual') or '—'} ({d.get('beat_miss') or 'unknown'}). "
            f"GBP/USD reaction: {d.get('pip_5m')} pips 5M, {d.get('pip_15m')} pips 15M, "
            f"direction {d.get('direction_5m') or 'unknown'}."
        )
        docs.append(Document(
            page_content=text,
            metadata={"type": "release", "event": d["your_name"], "date": d["event_date"]},
        ))

    for ev in get_upcoming_from_db(14):
        text = (
            f"Upcoming: {ev.get('your_name')} on {ev.get('event_date')} "
            f"{ev.get('event_time') or 'TBD'} ({ev.get('country')}). "
            f"Forecast {ev.get('forecast') or '—'}, previous {ev.get('previous') or '—'}."
        )
        docs.append(Document(
            page_content=text,
            metadata={"type": "upcoming", "event": ev.get("your_name"), "date": ev.get("event_date")},
        ))

    return docs


def get_vectorstore(force_refresh: bool = False) -> FAISS | None:
    global _INDEX, _INDEX_BUILT_AT
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    now = time.time()
    with _INDEX_LOCK:
        if _INDEX is not None and not force_refresh and (now - _INDEX_BUILT_AT) < _INDEX_TTL_SEC:
            return _INDEX

        docs = _build_documents()
        if not docs:
            return None

        embeddings = OpenAIEmbeddings(model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"))
        _INDEX = FAISS.from_documents(docs, embeddings)
        _INDEX_BUILT_AT = now
        return _INDEX


def retrieve(query: str, k: int = 8, live_context: dict | None = None) -> list[Document]:
    store = get_vectorstore()
    if store is None:
        return []

    enriched = query
    if live_context:
        page = live_context.get("page")
        event = live_context.get("selectedEvent") or live_context.get("selected_event")
        if page:
            enriched += f" Current app tab: {page}."
        if event:
            enriched += f" User is viewing event: {event}."

    docs = store.similarity_search(enriched, k=k)

    # Boost docs matching live context event
    if live_context:
        focus = live_context.get("selectedEvent") or live_context.get("selected_event")
        if focus:
            boosted = [d for d in docs if d.metadata.get("event") == focus]
            others = [d for d in docs if d.metadata.get("event") != focus]
            docs = boosted + others

    return docs[:k]


def format_retrieved_docs(docs: list[Document]) -> str:
    if not docs:
        return "No matching documents in knowledge base."
    parts = []
    for i, doc in enumerate(docs, 1):
        meta = doc.metadata or {}
        label = meta.get("type", "doc")
        parts.append(f"[{i}] ({label}) {doc.page_content}")
    return "\n\n".join(parts)
