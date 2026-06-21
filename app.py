"""
GBP/USD Trading Intelligence — Backend
Run with: python app.py
Then open: http://localhost:8000
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx
import json
import os
import asyncio
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from db import get_db, check_connection
from scraper import (
    run_sync, get_analysis_summary, get_event_history,
    get_last_sync, get_journal_rows, save_journal_note,
    get_news_event, scrape_upcoming, get_brief, NEWS_MAP,
)

# ── APP SETUP ─────────────────────────────────────────────
app = FastAPI(title="GBP/USD Trading Intelligence")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DATABASE ──────────────────────────────────────────────
def init_db():
    check_connection()
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) AS count FROM news_ratings")
    if c.fetchone()["count"] == 0:
        seed_ratings = [
            ("Non Farm Payroll US",             "Good",      "Pair with Unemployment for strongest signal"),
            ("Unemployment US",                  "Good",      "Pair with Non Farm Payroll"),
            ("ISM Services US",                  "Good",      "Check 1-2 hour trend before entry"),
            ("Retail UK",                        "Good",      ""),
            ("GDP UK",                           "Good",      "Good recently"),
            ("Existing Home Sales US",           "Good",      "Check Forecast vs Actual before trade"),
            ("Core Inflation MoM US",            "Good",      ""),
            ("Inflation Rate YoY US",            "Caution",   "Check Forecast vs Actual before trade"),
            ("Inflation Rate YoY UK",            "Caution",   "Not good with US news - fluctuate a lot"),
            ("FOMC US",                          "Caution",   "Not sure - check alone"),
            ("Building Permits US",              "Caution",   "Not sure - check alone"),
            ("Housing Starts US",                "Caution",   "Not sure - check alone"),
            ("S&P Global Manufacturing PMI UK",  "Caution",   "Not sure - check alone"),
            ("S&P Global Services PMI UK",       "Caution",   "Not sure - check alone"),
            ("BOE Interest Rate US",             "Caution",   "Use forecasting before entering"),
            ("Fed Chair Powell Speech US",       "Caution",   "Check reaction carefully"),
            ("ISM Manufacturing US",             "Bad",       "Not sure - check alone"),
            ("Personal Spending US",             "Bad",       "Trade with reduced size only"),
            ("Core PCE MoM US",                  "Bad",       "Trade with reduced size only"),
            ("PPI US",                           "Bad",       "Not powerful alone"),
            ("PPI MoM US",                       "Bad",       "Not powerful"),
            ("Michigan Consumer Sentiment US",   "Bad",       "Not powerful"),
            ("Retail US",                        "Very Bad",  ""),
            ("Durable Goods US",                 "Bad",       ""),
            ("JOLTs Job Openings US",            "Bad",       "Don't take this trade"),
            ("GDP US",                           "Unreliable",""),
            ("Unemployment UK",                  "Bad",       ""),
        ]
        c.executemany(
            "INSERT INTO news_ratings (name, type, comment) VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
            seed_ratings,
        )

    c.execute("SELECT COUNT(*) AS count FROM trades")
    if c.fetchone()["count"] == 0:
        seed_trades = [
            ("2026-03-25","Fed Chair Powell Speech US","","","5 min after checking reaction",5,15,"","","","Loss","Didn't react as expected"),
            ("2025-02-01","Core Inflation MoM US","Inflation Rate YoY US","Fed Chair Powell Speech US","5 min after checking reaction",5,10,"","","","Win","Could have entered before 10 min and still won"),
            ("2025-02-01","GDP UK","PPI MoM US","","5 min after checking reaction",5,10,"","","","Win","Could have entered the second news 5 min after without checking reaction and won"),
            ("2025-02-01","Retail UK","S&P Global Manufacturing PMI UK","Existing Home Sales US","5 min after checking reaction",5,10,"","","","Win","Perfect trade. 3-news combo worked. Took around 3 days for TP"),
            ("2025-02-01","GDP US","Durable Goods US","","10 min before news",5,10,"","","","Loss","Should have made it 15:3 as I was not sure of the news"),
            ("2025-03-01","ISM Services US","","","10 min after",5,10,"","","","Win","Not sure with the reaction so waited 10 min. Had to wait 1 day to TP"),
            ("2025-03-01","Non Farm Payroll US","Unemployment US","Fed Chair Powell Speech US","5 min after checking reaction",5,10,"","","","Loss","Reacted a lot before the news - risky but take it either way"),
            ("2026-06-01","ISM Manufacturing US","","","5 min after checking reaction",5,10,"52.7","53","54","Win","Very risky, may be one time thing"),
            ("2026-06-02","JOLTs Job Openings US","","","-",None,None,"","","","Loss","Don't take this trade"),
            ("2026-06-03","ISM Services US","","","-",None,None,"","","","Loss","Didn't react this time"),
            ("2026-06-05","Non Farm Payroll US","Unemployment US","","5 min after checking reaction",10,10,"179","85","172","Win","Very very good reaction!"),
            ("2026-06-09","Existing Home Sales US","","","5 min after checking reaction",4,10,"4.02","4.06","-","Win","Was a perfect trade"),
            ("2026-06-10","Inflation Rate YoY US","Core Inflation MoM US","","10 min before news",3.5,10,"3.8","4.2","3.8","Win","Tricky one - had to close earlier at 2.66 R"),
            ("2026-06-11","PPI MoM US","","","10 min before news",3,13,"1.1","0.7","1.1","Loss","Don't trade PPI - doesn't react, very low power"),
        ]
        c.executemany("""
            INSERT INTO trades (date,news1,news2,news3,entry,ratio,sl,previous,forecast,actual,outcome,improvement)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, seed_trades)

    conn.commit()
    conn.close()

# ── MODELS ────────────────────────────────────────────────
class Trade(BaseModel):
    date: str
    news1: str
    news2: Optional[str] = ""
    news3: Optional[str] = ""
    entry: Optional[str] = ""
    ratio: Optional[float] = None
    sl: Optional[float] = None
    previous: Optional[str] = ""
    forecast: Optional[str] = ""
    actual: Optional[str] = ""
    outcome: str
    improvement: Optional[str] = ""
    news_event_id: Optional[int] = None

class JournalNote(BaseModel):
    note: str

class JournalTrade(BaseModel):
    outcome: str
    entry: Optional[str] = ""
    ratio: Optional[float] = None
    sl: Optional[float] = None
    improvement: Optional[str] = ""
    news2: Optional[str] = ""
    news3: Optional[str] = ""

class NewsRating(BaseModel):
    name: str
    type: str
    comment: Optional[str] = ""

class SettingsUpdate(BaseModel):
    token: str
    account_id: str
    env: str

class ScoreRequest(BaseModel):
    events: list

class TestRequest(BaseModel):
    token: str
    account_id: str
    env: str = "practice"

# ── ROUTES: TRADES ────────────────────────────────────────
@app.get("/api/trades")
def get_trades():
    conn = get_db()
    rows = conn.execute("SELECT * FROM trades ORDER BY date DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/trades")
def create_trade(trade: Trade):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (date,news1,news2,news3,entry,ratio,sl,previous,forecast,actual,outcome,improvement,news_event_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (trade.date, trade.news1, trade.news2 or "", trade.news3 or "",
          trade.entry or "", trade.ratio, trade.sl,
          trade.previous or "", trade.forecast or "", trade.actual or "",
          trade.outcome, trade.improvement or "", trade.news_event_id))
    trade_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return {"id": trade_id, "message": "Trade saved"}

@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int):
    conn = get_db()
    conn.execute("DELETE FROM trades WHERE id = %s", (trade_id,))
    conn.commit()
    conn.close()
    return {"message": "Deleted"}

def _no_reaction_response() -> dict:
    return {
        "error": "No price reaction data found for this event",
        "entry_analysis": {
            "suggestion": "No OANDA data available for this trade date. Run a sync to fetch historical reactions."
        },
        "sl_analysis": {"suggestion": "No data available."},
        "rr_analysis": {
            "your_rr": None,
            "achievable_rr": None,
            "suggestion": "No data available.",
        },
        "overall": "Could not analyse this trade — no matching price reaction data in the database.",
    }

def get_related_event_names(your_name: str) -> list[str]:
    """Watchlist aliases that may share the same FF release (e.g. PPI MoM US ↔ PPI US)."""
    names = [your_name]
    match = next(((n, kw, country) for n, kw, country, _ in NEWS_MAP if n == your_name), None)
    if not match:
        return names
    _, keyword, country = match
    kw_root = keyword.lower().split()[0]
    for n, kw, c, _ in NEWS_MAP:
        if c != country or n == your_name or n in names:
            continue
        other_root = kw.lower().split()[0]
        if kw_root == other_root or keyword.lower() in kw.lower() or kw.lower() in keyword.lower():
            names.append(n)
    return names

def _lookup_event_reaction(conn, where: str, params: tuple, order: str) -> dict | None:
    row = conn.execute(f"""
        SELECT ne.id AS event_id, pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        WHERE {where}
        ORDER BY {order}
        LIMIT 1
    """, params).fetchone()
    if not row or row["pip_5m"] is None:
        return None
    return dict(row)

def get_trade_analysis(trade_id: int) -> dict:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM trades WHERE id = %s", (trade_id,)).fetchone()
        if not row:
            return {"error": "Trade not found"}
        trade = dict(row)

        reaction = None
        if trade.get("news_event_id"):
            reaction = _lookup_event_reaction(conn, "ne.id = %s", (trade["news_event_id"],), "ne.id DESC")
        if not reaction:
            reaction = _lookup_event_reaction(
                conn, "ne.your_name = %s AND ne.event_date = %s",
                (trade["news1"], trade["date"]), "ne.id DESC",
            )
        if not reaction:
            reaction = _lookup_event_reaction(
                conn, "ne.your_name = %s",
                (trade["news1"],), "ne.event_date DESC",
            )
        if not reaction:
            for alt_name in get_related_event_names(trade["news1"])[1:]:
                reaction = _lookup_event_reaction(
                    conn, "ne.your_name = %s AND ne.event_date = %s",
                    (alt_name, trade["date"]), "ne.id DESC",
                )
                if reaction:
                    break
        if not reaction:
            for alt_name in get_related_event_names(trade["news1"])[1:]:
                reaction = _lookup_event_reaction(
                    conn, "ne.your_name = %s",
                    (alt_name,), "ne.event_date DESC",
                )
                if reaction:
                    break
        if not reaction:
            return _no_reaction_response()

        pip_5m = reaction.get("pip_5m")
        pip_15m = reaction.get("pip_15m")
        pip_30m = reaction.get("pip_30m")
        pip_60m = reaction.get("pip_60m")
        your_sl = trade.get("sl") or 10
        your_rr = trade.get("ratio") or 5
        your_entry = trade.get("entry") or "5 min after checking reaction"

        if pip_5m and pip_30m and pip_30m > 0:
            speed = pip_5m / pip_30m
            if speed > 0.7:
                entry_suggestion = "Most of the move happened quickly. Your timing was good."
            elif speed < 0.4:
                entry_suggestion = "The move developed slowly. Waiting 5 min was right — entering earlier would have been riskier."
            else:
                entry_suggestion = "Moderate speed move. 5 min entry captured a reasonable portion."
        else:
            entry_suggestion = "Insufficient price data to evaluate entry timing."

        if pip_5m and your_sl:
            if your_sl < pip_5m * 0.6:
                sl_suggestion = (
                    f"SL was very tight relative to the move. Risk of early stopout on this event is high. "
                    f"Suggest {math.ceil(pip_5m * 0.8)}p SL."
                )
            elif your_sl <= pip_5m:
                sl_suggestion = "SL was reasonable. Standard 10-15p works well for this event."
            else:
                sl_suggestion = (
                    f"SL was wider than the 5M move. You had plenty of room — could tighten to "
                    f"{math.ceil(pip_5m * 0.8)}p next time."
                )
        else:
            sl_suggestion = "Insufficient data to evaluate SL."

        achievable_rr = round(pip_30m / your_sl, 1) if pip_30m and your_sl else None
        if achievable_rr is not None and your_rr:
            if achievable_rr > your_rr + 2:
                left = (achievable_rr - your_rr) * your_sl
                rr_suggestion = (
                    f"A {achievable_rr}:1 target was achievable. You left {left:.0f}p on the table."
                )
            elif achievable_rr >= your_rr:
                rr_suggestion = "Your target was close to optimal."
            else:
                rr_suggestion = (
                    f"The move didn't reach your target on this occurrence. "
                    f"Consider {max(3, math.ceil(achievable_rr))}:1 for this event type."
                )
        else:
            rr_suggestion = "Insufficient data to evaluate R:R."

        overall_parts = []
        if pip_5m and your_sl and your_sl < pip_5m * 0.6:
            overall_parts.append(f"Consider {math.ceil(pip_5m * 0.8)}p SL for this event type to avoid early stopouts.")
        if achievable_rr and your_rr and achievable_rr > your_rr + 1:
            overall_parts.append(
                f"A 1:{math.floor(achievable_rr)} to 1:{math.ceil(achievable_rr)} target is achievable based on historical 30M moves."
            )
        if trade.get("outcome") == "Win" and not overall_parts:
            overall_parts.append("Trade was well-executed.")
        overall = " ".join(overall_parts) if overall_parts else "Review entry timing and SL against historical pip moves for this event."

        return {
            "entry_analysis": {
                "your_entry": your_entry,
                "pip_at_5m": pip_5m,
                "pip_at_15m": pip_15m,
                "pip_at_30m": pip_30m,
                "suggestion": entry_suggestion,
            },
            "sl_analysis": {
                "your_sl": your_sl,
                "pip_move_against": None,
                "suggestion": sl_suggestion,
            },
            "rr_analysis": {
                "your_rr": your_rr,
                "pip_at_30m": pip_30m,
                "pip_at_60m": pip_60m,
                "achievable_rr": achievable_rr,
                "suggestion": rr_suggestion,
            },
            "overall": overall,
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@app.get("/api/trade/{trade_id}/analysis")
def trade_analysis(trade_id: int):
    result = get_trade_analysis(trade_id)
    if result.get("error") == "Trade not found":
        raise HTTPException(status_code=404, detail="Trade not found")
    return result

# ── ROUTES: NEWS RATINGS ──────────────────────────────────
@app.get("/api/ratings")
def get_ratings():
    conn = get_db()
    rows = conn.execute("SELECT * FROM news_ratings ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/ratings")
def upsert_rating(rating: NewsRating):
    conn = get_db()
    conn.execute("""
        INSERT INTO news_ratings (name, type, comment) VALUES (%s, %s, %s)
        ON CONFLICT(name) DO UPDATE SET type=excluded.type, comment=excluded.comment
    """, (rating.name, rating.type, rating.comment or ""))
    conn.commit()
    conn.close()
    return {"message": "Saved"}

@app.delete("/api/ratings/{name}")
def delete_rating(name: str):
    conn = get_db()
    conn.execute("DELETE FROM news_ratings WHERE name = %s", (name,))
    conn.commit()
    conn.close()
    return {"message": "Deleted"}

# ── ROUTES: SETTINGS ──────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {r["key"]: r["value"] for r in rows}
    return {"token": result.get("token",""), "account_id": result.get("account_id",""), "env": result.get("env","practice")}

@app.post("/api/settings")
def save_settings(s: SettingsUpdate):
    conn = get_db()
    for key, val in [("token", s.token), ("account_id", s.account_id), ("env", s.env)]:
        conn.execute("INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
    conn.commit()
    conn.close()
    return {"message": "Saved"}

# ── ROUTES: OANDA PROXY ───────────────────────────────────
@app.get("/api/oanda/price")
async def get_price():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    cfg = {r["key"]: r["value"] for r in rows}
    token = cfg.get("token", "")
    env = cfg.get("env", "practice")
    if not token:
        raise HTTPException(status_code=400, detail="No API token configured")
    base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{base}/v3/instruments/GBP_USD/candles?count=2&granularity=M5&price=M",
                headers={"Authorization": f"Bearer {token}"}
            )
            if not r.is_success:
                raise HTTPException(status_code=r.status_code, detail="OANDA error")
            data = r.json()
            candles = data.get("candles", [])
            if len(candles) < 2:
                raise HTTPException(status_code=404, detail="Not enough candle data")
            latest = candles[-1]
            prev = candles[-2]
            return {
                "close": float(latest["mid"]["c"]),
                "open": float(latest["mid"]["o"]),
                "high": float(latest["mid"]["h"]),
                "low": float(latest["mid"]["l"]),
                "prev_close": float(prev["mid"]["c"]),
                "time": latest["time"]
            }
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Request failed: {str(e)}")

@app.get("/api/oanda/current-price")
async def get_current_price():
    data = await get_price()
    return {"price": data["close"], "timestamp": data["time"]}

@app.post("/api/oanda/test")
async def test_connection(req: TestRequest):
    token = req.token.strip()
    account_id = req.account_id.strip()
    env = req.env
    if not token or not account_id:
        return {"ok": False, "detail": "Token or account ID not set"}
    base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(
                f"{base}/v3/accounts/{account_id}/summary",
                headers={"Authorization": f"Bearer {token}"}
            )
            if r.is_success:
                data = r.json()
                balance = data.get("account", {}).get("balance", "")
                currency = data.get("account", {}).get("currency", "")
                return {"ok": True, "balance": balance, "currency": currency}
            else:
                return {"ok": False, "detail": f"OANDA returned {r.status_code}: {r.text}"}
        except httpx.RequestError as e:
            return {"ok": False, "detail": str(e)}

# ── ROUTES: SCORING ───────────────────────────────────────
@app.post("/api/score")
def score_news(req: ScoreRequest):
    conn = get_db()
    ratings_rows = conn.execute("SELECT name, type, comment FROM news_ratings").fetchall()
    trades_rows = conn.execute("SELECT * FROM trades ORDER BY date DESC").fetchall()
    conn.close()

    ratings = {r["name"]: {"type": r["type"], "comment": r["comment"]} for r in ratings_rows}
    trades = [dict(t) for t in trades_rows]
    events = [e for e in req.events if e.get("key")]

    if not events:
        return {"signal": "neutral", "reasons": ["Select at least one news event."], "entry": "", "past_matches": []}

    def get_type(key):
        r = ratings.get(key)
        return r["type"].lower() if r else None

    def fund_signal(actual, forecast):
        if not actual or not forecast or actual == '-' or forecast == '-':
            return None
        try:
            a, f = float(actual), float(forecast)
            return "beat" if a > f else "miss" if a < f else "inline"
        except:
            return None

    reasons = []
    good_c = bad_c = caution_c = no_c = 0

    for ev in events:
        key = ev.get("key", "")
        rt = get_type(key)
        comment = ratings.get(key, {}).get("comment", "")
        fs = fund_signal(ev.get("actual", ""), ev.get("forecast", ""))

        if rt == "unreliable":
            no_c += 1
            reasons.append(f"{key}: UNRELIABLE — skip")
        elif rt in ("bad", "very bad"):
            if fs == "beat":
                caution_c += 1
                reasons.append(f"{key}: Low-power BUT data beat forecast — watch for unusual reaction")
            else:
                no_c += 1
                reasons.append(f"{key}: BAD — low reaction power{'. ' + comment if comment else ''}")
        elif rt == "caution":
            caution_c += 1
            reasons.append(f"{key}: CAUTION — {comment or 'check before entering'}")
        elif rt == "good":
            good_c += 1
            if fs == "beat":
                reasons.append(f"{key}: GOOD + data BEAT forecast — strong directional signal")
            elif fs == "miss":
                reasons.append(f"{key}: GOOD + data MISSED forecast — strong opposing signal")
            else:
                reasons.append(f"{key}: GOOD — enter 5 min after checking reaction{'. ' + comment if comment else ''}")

    if len(events) >= 2:
        goods = sum(1 for e in events if get_type(e.get("key","")) == "good")
        bads  = sum(1 for e in events if get_type(e.get("key","")) in ("bad","very bad"))
        unrels= sum(1 for e in events if get_type(e.get("key","")) == "unreliable")
        if not unrels:
            if goods >= 2:
                caution_c += 1
                reasons.append("⚠ 2 Good news events: your rules say this combo can be unpredictable")
            elif bads >= 2:
                good_c += 1
                reasons.append("✓ 2 Bad news events: your rules say this tends to produce a tradeable reaction")
            elif goods >= 1 and bads >= 1:
                good_c += 1
                reasons.append("✓ Good + Bad combo: your rules say this tends to produce a tradeable reaction")

    if no_c > 0 and good_c == 0 and caution_c == 0:
        signal, entry = "no-trade", "Do not trade this event."
    elif caution_c > 0 and good_c == 0:
        signal, entry = "caution", "Watch for 5-10 min before committing. Check reaction closely."
    elif good_c > 0 and caution_c == 0 and no_c == 0:
        signal, entry = "go", "Enter 5 min after news. Confirm direction then commit."
    else:
        signal, entry = "caution", "Mixed signals — watch the reaction for at least 5 min before deciding."

    news_keys = [e.get("key","") for e in events if e.get("key")]
    past = [t for t in trades if any(n in [t.get("news1",""), t.get("news2",""), t.get("news3","")] for n in news_keys)][:5]

    return {"signal": signal, "reasons": reasons, "entry": entry, "past_matches": past}

# ── ROUTES: ANALYTICS ─────────────────────────────────────
@app.get("/api/analytics")
def get_analytics():
    conn = get_db()
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades").fetchall()]
    conn.close()
    wins   = [t for t in trades if t["outcome"] == "Win"]
    losses = [t for t in trades if t["outcome"] == "Loss"]
    win_rate = round(len(wins) / len(trades) * 100) if trades else 0
    win_ratios = [t["ratio"] for t in wins if t.get("ratio")]
    avg_ratio = round(sum(win_ratios) / len(win_ratios), 1) if win_ratios else None
    news_perf = {}
    for t in trades:
        for n in [t.get("news1",""), t.get("news2",""), t.get("news3","")]:
            if n:
                if n not in news_perf:
                    news_perf[n] = {"wins": 0, "losses": 0}
                if t["outcome"] == "Win": news_perf[n]["wins"] += 1
                else: news_perf[n]["losses"] += 1
    sorted_news = sorted(
        [{"news": k, **v, "total": v["wins"]+v["losses"]} for k, v in news_perf.items()],
        key=lambda x: x["wins"], reverse=True
    )
    return {"wins": len(wins), "losses": len(losses), "total": len(trades),
            "win_rate": win_rate, "avg_ratio": avg_ratio, "news_performance": sorted_news}

MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

@app.get("/api/performance")
def get_performance():
    conn = get_db()
    rows = conn.execute("SELECT * FROM trades ORDER BY date DESC, id DESC").fetchall()
    conn.close()
    trades = [dict(r) for r in rows]
    chronological = list(reversed(trades))

    wins = [t for t in trades if t.get("outcome") == "Win"]
    losses = [t for t in trades if t.get("outcome") == "Loss"]
    total = len(trades)
    win_rate = round(len(wins) / total * 100, 1) if total else 0.0
    win_rate_dec = len(wins) / total if total else 0.0
    loss_rate_dec = len(losses) / total if total else 0.0

    all_ratios = [t["ratio"] for t in trades if t.get("ratio") is not None]
    win_ratios = [t["ratio"] for t in wins if t.get("ratio") is not None]
    sls = [t["sl"] for t in trades if t.get("sl") is not None]

    avg_rr_all = round(sum(all_ratios) / len(all_ratios), 2) if all_ratios else 0.0
    avg_rr_wins = round(sum(win_ratios) / len(win_ratios), 2) if win_ratios else 0.0
    avg_sl = round(sum(sls) / len(sls), 1) if sls else 0.0
    expectancy = round((win_rate_dec * avg_rr_wins) - (loss_rate_dec * 1), 2) if total else 0.0

    month_buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "ratios": []})
    for t in chronological:
        date_str = t.get("date") or ""
        if len(date_str) < 7:
            continue
        month_key = date_str[:7]
        if t.get("outcome") == "Win":
            month_buckets[month_key]["wins"] += 1
        elif t.get("outcome") == "Loss":
            month_buckets[month_key]["losses"] += 1
        if t.get("ratio") is not None:
            month_buckets[month_key]["ratios"].append(t["ratio"])

    by_month = []
    for month_key in sorted(month_buckets.keys()):
        bucket = month_buckets[month_key]
        month_total = bucket["wins"] + bucket["losses"]
        year = int(month_key[:4])
        month_num = int(month_key[5:7])
        by_month.append({
            "month": month_key,
            "label": f"{MONTH_SHORT[month_num - 1]} {year}",
            "total": month_total,
            "wins": bucket["wins"],
            "losses": bucket["losses"],
            "win_rate": round(bucket["wins"] / month_total * 100, 1) if month_total else 0.0,
            "avg_rr": round(sum(bucket["ratios"]) / len(bucket["ratios"]), 2) if bucket["ratios"] else 0.0,
        })

    event_buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "ratios": []})
    for t in trades:
        for event in [t.get("news1"), t.get("news2"), t.get("news3")]:
            if not event:
                continue
            if t.get("outcome") == "Win":
                event_buckets[event]["wins"] += 1
            elif t.get("outcome") == "Loss":
                event_buckets[event]["losses"] += 1
            if t.get("ratio") is not None:
                event_buckets[event]["ratios"].append(t["ratio"])

    by_event = []
    for event, bucket in event_buckets.items():
        event_total = bucket["wins"] + bucket["losses"]
        by_event.append({
            "event": event,
            "total": event_total,
            "wins": bucket["wins"],
            "losses": bucket["losses"],
            "win_rate": round(bucket["wins"] / event_total * 100, 1) if event_total else 0.0,
            "avg_rr": round(sum(bucket["ratios"]) / len(bucket["ratios"]), 2) if bucket["ratios"] else 0.0,
        })
    by_event.sort(key=lambda x: x["win_rate"], reverse=True)

    timing_buckets = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in trades:
        timing = (t.get("entry") or "").strip() or "—"
        if t.get("outcome") == "Win":
            timing_buckets[timing]["wins"] += 1
        elif t.get("outcome") == "Loss":
            timing_buckets[timing]["losses"] += 1

    by_entry_timing = []
    for timing, bucket in timing_buckets.items():
        timing_total = bucket["wins"] + bucket["losses"]
        by_entry_timing.append({
            "timing": timing,
            "total": timing_total,
            "wins": bucket["wins"],
            "win_rate": round(bucket["wins"] / timing_total * 100, 1) if timing_total else 0.0,
        })
    by_entry_timing.sort(key=lambda x: x["win_rate"], reverse=True)

    recent_streak = 0
    if trades:
        latest_outcome = trades[0].get("outcome")
        if latest_outcome in ("Win", "Loss"):
            for t in trades:
                if t.get("outcome") == latest_outcome:
                    recent_streak += 1 if latest_outcome == "Win" else -1
                else:
                    break

    last_10 = [
        {
            "date": t.get("date"),
            "outcome": t.get("outcome"),
            "event": t.get("news1") or "—",
        }
        for t in trades[:10]
    ]

    return {
        "trades": trades,
        "overall": {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_rr_all": avg_rr_all,
            "avg_rr_wins": avg_rr_wins,
            "avg_sl": avg_sl,
            "total_trades_with_rr": len(all_ratios),
            "expectancy": expectancy,
        },
        "by_month": by_month,
        "by_event": by_event,
        "by_entry_timing": by_entry_timing,
        "recent_streak": recent_streak,
        "last_10": last_10,
    }

# ── ROUTES: SYNC & ANALYSIS ───────────────────────────────
sync_status = {"running": False, "progress": 0, "message": "", "stage": "idle"}

@app.post("/api/sync")
async def trigger_sync():
    global sync_status
    if sync_status["running"]:
        return {"message": "Sync already running"}
    sync_status = {"running": True, "progress": 0, "message": "Starting...", "stage": "starting"}

    async def progress_callback(update: dict):
        global sync_status
        sync_status.update(update)
        sync_status["running"] = True

    async def do_sync():
        global sync_status
        try:
            result = await run_sync(progress_callback=progress_callback)
            sync_status = {"running": False, "progress": 100, "stage": "done",
                          "message": f"Complete — {result['new_events']} new events, {result['reactions_fetched']} reactions fetched",
                          "result": result}
        except Exception as e:
            sync_status = {"running": False, "progress": 0, "stage": "error", "message": str(e)}

    asyncio.create_task(do_sync())
    return {"message": "Sync started"}

@app.post("/api/sync/full")
async def trigger_full_sync():
    global sync_status
    if sync_status["running"]:
        return {"message": "Sync already running"}
    sync_status = {"running": True, "progress": 0, "message": "Starting full resync from Jan 2020...", "stage": "starting"}

    async def progress_callback(update: dict):
        global sync_status
        sync_status.update(update)
        sync_status["running"] = True

    async def do_sync():
        global sync_status
        try:
            result = await run_sync(
                progress_callback=progress_callback,
                start_date=datetime(2020, 1, 1),
            )
            sync_status = {"running": False, "progress": 100, "stage": "done",
                          "message": f"Full resync complete — {result['new_events']} new events, {result['reactions_fetched']} reactions fetched",
                          "result": result}
        except Exception as e:
            sync_status = {"running": False, "progress": 0, "stage": "error", "message": str(e)}

    asyncio.create_task(do_sync())
    return {"message": "Full resync started — this will take 20-30 minutes"}

@app.get("/api/sync/status")
def get_sync_status():
    last = get_last_sync()
    return {**sync_status, "last_sync": last}

@app.get("/api/analysis")
def get_analysis():
    try:
        summary = get_analysis_summary()
        last = get_last_sync()
        conn = get_db()
        total_events = conn.execute("SELECT COUNT(*) AS count FROM news_events").fetchone()["count"]
        total_reactions = conn.execute("SELECT COUNT(*) AS count FROM price_reactions").fetchone()["count"]
        conn.close()
        return {
            "summary": summary,
            "last_sync": last,
            "total_events": total_events,
            "total_reactions": total_reactions
        }
    except Exception as e:
        return {"summary": [], "last_sync": None, "total_events": 0, "total_reactions": 0, "error": str(e)}

@app.get("/api/analysis/{event_name}")
def get_event_detail(event_name: str):
    history = get_event_history(event_name)
    return {"history": history}

# ── ROUTES: JOURNAL ───────────────────────────────────────
@app.get("/api/journal")
def get_journal():
    try:
        return {"events": get_journal_rows()}
    except Exception as e:
        return {"events": [], "error": str(e)}

@app.post("/api/journal/{event_id}/note")
def save_journal_event_note(event_id: int, body: JournalNote):
    if not save_journal_note(event_id, body.note):
        raise HTTPException(status_code=404, detail="Event not found")
    return {"message": "Note saved"}

@app.post("/api/journal/{event_id}/trade")
def create_journal_trade(event_id: int, body: JournalTrade):
    event = get_news_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM trades WHERE news_event_id = %s", (event_id,)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Trade already logged for this event")
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (date, news1, news2, news3, entry, ratio, sl, previous, forecast, actual, outcome, improvement, news_event_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        event["event_date"], event["your_name"], body.news2 or "", body.news3 or "",
        body.entry or "", body.ratio, body.sl, event.get("previous") or "",
        event.get("forecast") or "", event.get("actual") or "",
        body.outcome, body.improvement or "", event_id,
    ))
    trade_id = c.fetchone()["id"]
    conn.commit()
    conn.close()
    return {"id": trade_id, "message": "Trade saved"}

# ── ROUTES: THIS WEEK ─────────────────────────────────────
_upcoming_cache = {"data": None, "expires": None}

@app.get("/api/upcoming")
async def get_upcoming():
    global _upcoming_cache
    now = datetime.now()
    if _upcoming_cache["data"] and _upcoming_cache["expires"] and now < _upcoming_cache["expires"]:
        return {"events": _upcoming_cache["data"], "cached": True}
    try:
        events = await scrape_upcoming(7)
        _upcoming_cache["data"] = events
        _upcoming_cache["expires"] = now + timedelta(hours=1)
        return {"events": events, "cached": False}
    except Exception as e:
        return {"events": [], "error": str(e)}

@app.get("/api/brief/{event_name}")
def get_trade_brief(event_name: str):
    try:
        return get_brief(event_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── SERVE FRONTEND ────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(content=html_path.read_text())

# ── RUN ───────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    init_db()
    print("\n" + "="*50)
    print("  GBP/USD Trading Intelligence")
    print("  Running at: http://localhost:8000")
    print("  Press Ctrl+C to stop")
    print("="*50 + "\n")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
