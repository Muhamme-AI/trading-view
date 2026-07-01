"""
scraper.py — Forex Factory calendar scraper + OANDA price reaction engine
Pulls news events, matches to your watchlist, fetches GBP/USD reactions from OANDA.
"""

import httpx
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import re
import json

from db import get_db, check_connection

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
except ImportError:
    _CurlAsyncSession = None


class _FfResponse:
    """Wrap curl_cffi response to match httpx Response interface."""

    def __init__(self, response):
        self.status_code = response.status_code
        self.text = response.text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FfHttpClient:
    """HTTP client for Forex Factory (bypasses Cloudflare via curl_cffi)."""

    def __init__(self, session):
        self._session = session

    async def get(self, url, headers=None, timeout=30, follow_redirects=True):
        r = await self._session.get(
            url,
            headers=headers or {},
            impersonate="chrome131",
            timeout=timeout,
        )
        return _FfResponse(r)


@asynccontextmanager
async def ff_client():
    """Yield an HTTP client suitable for scraping Forex Factory."""
    if _CurlAsyncSession is not None:
        async with _CurlAsyncSession() as session:
            yield FfHttpClient(session)
    else:
        async with httpx.AsyncClient() as client:
            yield client

# ── NEWS EVENT MAP ────────────────────────────────────────
# Maps your internal name → (forex_factory_keyword, country, impact_colours)
# impact: 'red' = high, 'orange' = medium
# We match FF event titles using keyword (case-insensitive contains match)

NEWS_MAP = [
    # YOUR NAME                          FF KEYWORD                          COUNTRY   MIN_IMPACT
    ("Non Farm Payroll US",              "Non-Farm Employment Change",        "USD",    "red"),
    ("Unemployment US",                  "Unemployment Rate",                 "USD",    "red"),
    ("ISM Services US",                  "ISM Services PMI",                  "USD",    "red"),
    ("ISM Manufacturing US",             "ISM Manufacturing PMI",             "USD",    "red"),
    ("Retail UK",                        "Retail Sales",                      "GBP",    "orange"),
    ("GDP UK",                           "GDP",                               "GBP",    "red"),
    ("GDP US",                           "GDP",                               "USD",    "red"),
    ("Existing Home Sales US",           "Existing Home Sales",               "USD",    "orange"),
    ("Core Inflation MoM US",            "Core CPI",                         "USD",    "red"),
    ("Inflation Rate YoY US",            "CPI y/y",                          "USD",    "red"),
    ("Inflation Rate YoY UK",            "CPI y/y",                          "GBP",    "orange"),
    ("FOMC US",                          "FOMC",                              "USD",    "red"),
    ("Building Permits US",              "Building Permits",                  "USD",    "orange"),
    ("Housing Starts US",                "Housing Starts",                    "USD",    "orange"),
    ("S&P Global Manufacturing PMI UK",  "Manufacturing PMI",                 "GBP",    "orange"),
    ("S&P Global Services PMI UK",       "Services PMI",                      "GBP",    "orange"),
    ("BOE Interest Rate US",             "BOE",                               "GBP",    "red"),
    ("Fed Chair Powell Speech US",       "Fed Chair Powell",                  "USD",    "red"),
    ("Personal Spending US",             "Personal Spending",                 "USD",    "orange"),
    ("Core PCE MoM US",                  "Core PCE",                         "USD",    "red"),
    ("PPI US",                           "PPI",                               "USD",    "orange"),
    ("PPI MoM US",                       "PPI m/m",                          "USD",    "orange"),
    ("Michigan Consumer Sentiment US",   "Michigan",                          "USD",    "orange"),
    ("Retail US",                        "Retail Sales",                      "USD",    "orange"),
    ("Durable Goods US",                 "Durable Goods",                     "USD",    "orange"),
    ("JOLTs Job Openings US",            "JOLTS",                             "USD",    "orange"),
    ("Unemployment UK",                  "Claimant Count",                    "GBP",    "orange"),
]

# ── DB HELPERS ────────────────────────────────────────────
def ensure_tables():
    """Schema is managed by Supabase migrations; verify connectivity."""
    check_connection()

# ── FOREX FACTORY SCRAPER ─────────────────────────────────
def build_ff_url(year: int, month: int) -> str:
    dt = datetime(year, month, 1)
    return f"https://www.forexfactory.com/calendar?month={dt.strftime('%b').lower()}.{year}"

def parse_impact(td) -> str:
    """Extract impact colour from FF impact cell."""
    if not td:
        return ""
    span = td.find("span", class_=re.compile(r"impact|ff-impact"))
    cls = " ".join(span.get("class", [])) if span else td.get("class", [])
    if isinstance(cls, list):
        cls = " ".join(cls)
    cls = cls.lower()
    if "impact-red" in cls or "impact--red" in cls or "high" in cls:
        return "red"
    if "impact-ora" in cls or "impact--orange" in cls or "medium" in cls:
        return "orange"
    if "impact-yel" in cls or "impact--yellow" in cls or "low" in cls:
        return "yellow"
    return ""

def match_event(ff_title: str, ff_currency: str) -> dict | None:
    """Match a FF event row to one of our tracked events."""
    ff_title_lower = ff_title.lower()
    for (your_name, keyword, country, min_impact) in NEWS_MAP:
        # Currency must match
        currency_map = {"USD": "USD", "GBP": "GBP"}
        if ff_currency.upper() != country:
            continue
        if keyword.lower() in ff_title_lower:
            return {"your_name": your_name, "country": country, "min_impact": min_impact}
    return None

def classify_beat_miss(actual: str, forecast: str) -> str:
    """Return beat/miss/inline/unknown."""
    try:
        if not actual or not forecast or actual in ("-", "N/A", "") or forecast in ("-", "N/A", ""):
            return "unknown"
        # Strip % signs, K, M suffixes for comparison
        def clean(v):
            v = v.strip().replace("%", "").replace("K", "000").replace("M", "000000")
            v = v.replace(",", "")
            return float(v)
        a, f = clean(actual), clean(forecast)
        if a > f:
            return "beat"
        elif a < f:
            return "miss"
        else:
            return "inline"
    except:
        return "unknown"

async def scrape_month(year: int, month: int, client) -> list:
    """Scrape one month of FF calendar. Returns list of matched event dicts."""
    url = build_ff_url(year, month)
    events = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.forexfactory.com/calendar",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        r = await client.get(url, headers=headers, timeout=30, follow_redirects=True)
        if not r.is_success:
            print(f"  FF returned {r.status_code} for {url}")
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", class_=re.compile(r"calendar"))
        if not table:
            # Try alternative structure
            table = soup.find("table")
        if not table:
            print(f"  No calendar table found for {year}-{month:02d}")
            return []

        current_date = None
        current_time = None

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            # Date cell
            date_cell = row.find("td", class_=re.compile(r"date|calendar__date"))
            if date_cell and date_cell.get_text(strip=True):
                raw_date = date_cell.get_text(strip=True)
                # FF format varies: "Wed Jan 20", "Jan 20", or "WedJan 20" (no space)
                inner = date_cell.find("span", class_="date")
                if inner:
                    day_span = inner.find("span")
                    if day_span:
                        raw_date = day_span.get_text(strip=True)
                    else:
                        raw_date = re.sub(r"^[A-Za-z]{3}\s*", "", inner.get_text(strip=True))
                else:
                    raw_date = re.sub(r"^[A-Za-z]{3}", "", raw_date).strip()
                try:
                    for fmt in ["%a %b %d", "%b %d", "%A %b %d"]:
                        try:
                            parsed = datetime.strptime(f"{raw_date} {year}", f"{fmt} %Y")
                            current_date = parsed.strftime("%Y-%m-%d")
                            break
                        except:
                            continue
                except:
                    pass

            # Time cell
            time_cell = row.find("td", class_=re.compile(r"time|calendar__time"))
            if time_cell:
                t = time_cell.get_text(strip=True)
                if t and t not in ("", "All Day", "Tentative"):
                    current_time = t

            # Currency cell
            currency_cell = row.find("td", class_=re.compile(r"currency|calendar__currency"))
            if not currency_cell:
                continue
            currency = currency_cell.get_text(strip=True).upper()
            if currency not in ("USD", "GBP"):
                continue

            # Impact cell
            impact_cell = row.find("td", class_=re.compile(r"impact|calendar__impact"))
            impact = parse_impact(impact_cell) if impact_cell else ""
            if impact not in ("red", "orange"):
                continue

            # Event name cell
            event_cell = row.find("td", class_=re.compile(r"event|calendar__event"))
            if not event_cell:
                continue
            ff_title = event_cell.get_text(strip=True)

            # Data cells
            def get_cell_text(cls):
                cell = row.find("td", class_=re.compile(cls))
                return cell.get_text(strip=True) if cell else ""

            actual = get_cell_text(r"actual|calendar__actual")
            forecast = get_cell_text(r"forecast|calendar__forecast")
            previous = get_cell_text(r"previous|calendar__previous")

            # Match to our watchlist
            match = match_event(ff_title, currency)
            if not match:
                continue

            if not current_date:
                continue

            beat_miss = classify_beat_miss(actual, forecast)

            events.append({
                "your_name": match["your_name"],
                "ff_title": ff_title,
                "country": match["country"],
                "event_date": current_date,
                "event_time": current_time or "",
                "previous": previous,
                "forecast": forecast,
                "actual": actual,
                "impact": impact,
                "beat_miss": beat_miss,
            })

    except Exception as e:
        print(f"  Error scraping {year}-{month:02d}: {e}")

    return events

# ── OANDA PRICE REACTIONS ─────────────────────────────────
def get_oanda_config() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    cfg = {r["key"]: r["value"] for r in rows}
    return cfg

def parse_event_datetime(event_date: str, event_time: str) -> datetime | None:
    """Convert event date + time string to UTC datetime."""
    try:
        if not event_time or event_time in ("", "Tentative", "All Day"):
            # Default to 13:30 UTC (typical US news time)
            dt_str = f"{event_date} 13:30"
        else:
            # FF times are in US Eastern — convert to UTC (ET + 5h in winter, +4h in summer)
            # Use approximate: most major news is 8:30am ET = 13:30 UTC
            # We'll store the raw time and let OANDA window handle it
            dt_str = f"{event_date} {event_time}"

        # Try parsing various formats
        for fmt in ["%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
            except:
                continue
        # Fallback: just use date at 13:30 UTC
        return datetime.strptime(f"{event_date} 13:30", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except:
        return None

async def fetch_price_reaction(event: dict, client: httpx.AsyncClient, cfg: dict) -> dict | None:
    """Fetch GBP/USD candles around an event and compute pip moves."""
    token = cfg.get("token", "")
    env = cfg.get("env", "practice")
    if not token:
        return None

    base_url = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
    event_dt = parse_event_datetime(event["event_date"], event["event_time"])
    if not event_dt:
        return None

    # Fetch 5M candles: 15 min before to 75 min after
    from_dt = event_dt - timedelta(minutes=15)
    to_dt = event_dt + timedelta(minutes=75)

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")

    try:
        r = await client.get(
            f"{base_url}/v3/instruments/GBP_USD/candles",
            params={"from": from_str, "to": to_str, "granularity": "M5", "price": "M"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if not r.is_success:
            return None

        candles = r.json().get("candles", [])
        if len(candles) < 2:
            return None

        # Find the candle at/just after event time
        def candle_close(idx):
            if 0 <= idx < len(candles):
                return float(candles[idx]["mid"]["c"])
            return None

        # Open price = last candle before event
        open_price = float(candles[0]["mid"]["c"])

        # Price at 5, 15, 30, 60 min = candle index 1, 3, 6, 12 (each 5M candle)
        # Candles start 15 min before event, so event is at index 3
        event_idx = 3
        p5 = candle_close(event_idx + 1)   # 5 min after
        p15 = candle_close(event_idx + 3)  # 15 min after
        p30 = candle_close(event_idx + 6)  # 30 min after
        p60 = candle_close(event_idx + 12) # 60 min after

        def pips(a, b):
            if a is None or b is None:
                return None
            return round(abs(a - b) * 10000, 1)

        def direction(a, b):
            if a is None or b is None:
                return None
            return "up" if b > a else "down"

        return {
            "open_price": open_price,
            "price_5m": p5,
            "price_15m": p15,
            "price_30m": p30,
            "price_60m": p60,
            "pip_5m": pips(open_price, p5),
            "pip_15m": pips(open_price, p15),
            "pip_30m": pips(open_price, p30),
            "pip_60m": pips(open_price, p60),
            "direction_5m": direction(open_price, p5),
            "direction_15m": direction(open_price, p15),
        }
    except Exception as e:
        print(f"  OANDA error for {event['your_name']} {event['event_date']}: {e}")
        return None

# ── SAVE TO DB ────────────────────────────────────────────
def save_events(events: list) -> tuple[int, int]:
    """Save news events to DB. Returns (total, new)."""
    conn = get_db()
    c = conn.cursor()
    new_count = 0
    for ev in events:
        try:
            c.execute("""
                INSERT INTO news_events
                (your_name, ff_title, country, event_date, event_time, previous, forecast, actual, impact, beat_miss)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (your_name, event_date, event_time) DO NOTHING
            """, (ev["your_name"], ev["ff_title"], ev["country"], ev["event_date"],
                  ev["event_time"], ev["previous"], ev["forecast"], ev["actual"],
                  ev["impact"], ev["beat_miss"]))
            if c.rowcount > 0:
                new_count += 1
            else:
                c.execute("""
                    UPDATE news_events SET actual=%s, beat_miss=%s
                    WHERE your_name=%s AND event_date=%s AND event_time=%s
                    AND (actual='' OR actual IS NULL)
                """, (ev["actual"], ev["beat_miss"], ev["your_name"], ev["event_date"], ev["event_time"]))
        except Exception as e:
            print(f"  DB error saving event: {e}")
    conn.commit()
    conn.close()
    return len(events), new_count

def save_reaction(event_id: int, reaction: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO price_reactions
        (news_event_id, pip_5m, pip_15m, pip_30m, pip_60m, direction_5m, direction_15m,
         open_price, price_5m, price_15m, price_30m, price_60m)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (news_event_id) DO UPDATE SET
            pip_5m=excluded.pip_5m, pip_15m=excluded.pip_15m, pip_30m=excluded.pip_30m, pip_60m=excluded.pip_60m,
            direction_5m=excluded.direction_5m, direction_15m=excluded.direction_15m,
            open_price=excluded.open_price, price_5m=excluded.price_5m, price_15m=excluded.price_15m,
            price_30m=excluded.price_30m, price_60m=excluded.price_60m,
            fetched_at=NOW()
    """, (event_id, reaction.get("pip_5m"), reaction.get("pip_15m"),
          reaction.get("pip_30m"), reaction.get("pip_60m"),
          reaction.get("direction_5m"), reaction.get("direction_15m"),
          reaction.get("open_price"), reaction.get("price_5m"),
          reaction.get("price_15m"), reaction.get("price_30m"), reaction.get("price_60m")))
    conn.commit()
    conn.close()

def get_events_without_reactions() -> list:
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.* FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        WHERE pr.id IS NULL
        AND ne.actual != '' AND ne.actual IS NOT NULL
        AND ne.event_date <= CURRENT_DATE::text
        ORDER BY ne.event_date DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── MAIN SYNC FUNCTION ────────────────────────────────────
async def run_sync(progress_callback=None, start_date: datetime | None = None) -> dict:
    """Full sync: scrape FF + fetch OANDA reactions for new events."""
    ensure_tables()
    cfg = get_oanda_config()

    log_conn = get_db()
    log_c = log_conn.cursor()
    log_c.execute(
        "INSERT INTO sync_log (started_at, status) VALUES (%s, %s) RETURNING id",
        (datetime.now().isoformat(), "running"),
    )
    log_id = log_c.fetchone()["id"]
    log_conn.commit()
    log_conn.close()

    total_events = 0
    new_events = 0
    reactions_fetched = 0
    errors = []

    try:
        start = start_date or datetime(2020, 1, 1)
        end = datetime.now()

        # Build list of (year, month) tuples to scrape
        months = []
        current = datetime(start.year, start.month, 1)
        while current <= end:
            months.append((current.year, current.month))
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1)
            else:
                current = datetime(current.year, current.month + 1, 1)

        if progress_callback:
            await progress_callback({"stage": "scraping", "message": f"Scraping {len(months)} months from Forex Factory...", "progress": 0})

        # Scrape all months (curl_cffi bypasses FF Cloudflare)
        async with ff_client() as ff:
            all_events = []
            for i, (year, month) in enumerate(months):
                if progress_callback:
                    pct = int((i / len(months)) * 50)
                    await progress_callback({"stage": "scraping", "message": f"Scraping {datetime(year, month, 1).strftime('%b %Y')}...", "progress": pct})

                month_events = await scrape_month(year, month, ff)
                all_events.extend(month_events)
                print(f"  {datetime(year, month, 1).strftime('%b %Y')}: {len(month_events)} events found")
                await asyncio.sleep(2)  # Be polite to FF

        total_events, new_events = save_events(all_events)
        print(f"\n  Total events: {total_events}, New: {new_events}")

        # Fetch OANDA price reactions for events without them
        async with httpx.AsyncClient() as client:
            if progress_callback:
                await progress_callback({"stage": "reactions", "message": "Fetching price reactions from OANDA...", "progress": 50})

            events_needing_reactions = get_events_without_reactions()
            print(f"  Events needing price reactions: {len(events_needing_reactions)}")

            for i, event in enumerate(events_needing_reactions):
                if progress_callback:
                    pct = 50 + int((i / max(len(events_needing_reactions), 1)) * 48)
                    await progress_callback({
                        "stage": "reactions",
                        "message": f"Fetching reaction: {event['your_name']} {event['event_date']}",
                        "progress": pct
                    })

                reaction = await fetch_price_reaction(event, client, cfg)
                if reaction:
                    save_reaction(event["id"], reaction)
                    reactions_fetched += 1

                await asyncio.sleep(0.3)  # Rate limit OANDA

        status = "success"

    except Exception as e:
        status = "error"
        errors.append(str(e))
        print(f"  Sync error: {e}")

    # Update sync log
    log_conn = get_db()
    log_conn.execute("""
        UPDATE sync_log SET finished_at=%s, events_found=%s, events_new=%s,
        reactions_fetched=%s, status=%s, error=%s WHERE id=%s
    """, (datetime.now().isoformat(), total_events, new_events,
          reactions_fetched, status, "; ".join(errors) if errors else None, log_id))
    log_conn.commit()
    log_conn.close()

    if progress_callback:
        await progress_callback({"stage": "done", "message": "Sync complete", "progress": 100})

    return {
        "status": status,
        "total_events": total_events,
        "new_events": new_events,
        "reactions_fetched": reactions_fetched,
        "errors": errors
    }

# ── ANALYSIS QUERIES ──────────────────────────────────────
def get_analysis_summary() -> list:
    """Per-event analysis: avg pip moves, direction consistency, beat/miss rate."""
    conn = get_db()
    rows = conn.execute("""
        SELECT
            ne.your_name,
            ne.country,
            ne.impact,
            COUNT(ne.id) as total_occurrences,
            SUM(CASE WHEN ne.beat_miss='beat' THEN 1 ELSE 0 END) as beat_count,
            SUM(CASE WHEN ne.beat_miss='miss' THEN 1 ELSE 0 END) as miss_count,
            AVG(pr.pip_5m) as avg_pip_5m,
            AVG(pr.pip_15m) as avg_pip_15m,
            AVG(pr.pip_30m) as avg_pip_30m,
            AVG(pr.pip_60m) as avg_pip_60m,
            SUM(CASE WHEN pr.direction_5m='up' THEN 1 ELSE 0 END) as dir_up_5m,
            SUM(CASE WHEN pr.direction_5m='down' THEN 1 ELSE 0 END) as dir_down_5m,
            -- Beat direction consistency
            SUM(CASE WHEN ne.beat_miss='beat' AND pr.direction_5m='up' THEN 1 ELSE 0 END) as beat_up,
            SUM(CASE WHEN ne.beat_miss='beat' AND pr.direction_5m='down' THEN 1 ELSE 0 END) as beat_down,
            SUM(CASE WHEN ne.beat_miss='miss' AND pr.direction_5m='up' THEN 1 ELSE 0 END) as miss_up,
            SUM(CASE WHEN ne.beat_miss='miss' AND pr.direction_5m='down' THEN 1 ELSE 0 END) as miss_down,
            MAX(pr.pip_5m) as max_pip_5m,
            MIN(pr.pip_5m) as min_pip_5m
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        GROUP BY ne.your_name, ne.country, ne.impact
        ORDER BY avg_pip_5m DESC NULLS LAST
    """).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        total = d["total_occurrences"]
        beat = d["beat_count"] or 0
        miss = d["miss_count"] or 0
        beat_up = d["beat_up"] or 0
        beat_down = d["beat_down"] or 0
        miss_up = d["miss_up"] or 0
        miss_down = d["miss_down"] or 0

        d["beat_rate"] = round(beat / total * 100) if total > 0 else None
        d["tradeable"] = (d["avg_pip_5m"] or 0) >= 10

        # Direction consistency on beat
        if beat > 0:
            d["beat_direction"] = "up" if beat_up > beat_down else "down"
            d["beat_consistency"] = round(max(beat_up, beat_down) / beat * 100)
        else:
            d["beat_direction"] = None
            d["beat_consistency"] = None

        # Direction consistency on miss
        if miss > 0:
            d["miss_direction"] = "up" if miss_up > miss_down else "down"
            d["miss_consistency"] = round(max(miss_up, miss_down) / miss * 100)
        else:
            d["miss_direction"] = None
            d["miss_consistency"] = None

        results.append(d)

    return results

def get_event_history(your_name: str) -> list:
    """Get all occurrences of a specific event with reactions."""
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.*, pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m,
               pr.direction_5m, pr.direction_15m, pr.open_price
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        WHERE ne.your_name = %s
        ORDER BY ne.event_date DESC
    """, (your_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_last_sync() -> dict | None:
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM sync_log ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    return dict(row) if row else None

# ── JOURNAL & UPCOMING ────────────────────────────────────
def get_journal_rows() -> list:
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.*,
               pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m,
               pr.direction_5m, pr.direction_15m, pr.open_price,
               t.id AS trade_id, t.outcome AS trade_outcome
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        LEFT JOIN trades t ON t.news_event_id = ne.id
        ORDER BY ne.event_date DESC, ne.event_time DESC, ne.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_journal_note(event_id: int, note: str) -> bool:
    conn = get_db()
    cur = conn.execute(
        "UPDATE news_events SET user_note = %s WHERE id = %s",
        (note, event_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

def get_news_event(event_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM news_events WHERE id = %s", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_upcoming_from_db(days: int = 7) -> list:
    """Upcoming watchlist events from synced news_events (fallback when FF scrape is blocked)."""
    today = datetime.now().date()
    end_date = today + timedelta(days=days)
    conn = get_db()
    rows = conn.execute("""
        SELECT your_name, ff_title, country, event_date, event_time,
               previous, forecast, actual, impact, beat_miss
        FROM news_events
        WHERE event_date >= %s AND event_date <= %s
        ORDER BY event_date ASC, event_time ASC
    """, (today.isoformat(), end_date.isoformat())).fetchall()
    conn.close()
    return [dict(r) for r in rows]

async def scrape_upcoming(days: int = 7) -> list:
    """Scrape FF for events in the next N days."""
    today = datetime.now().date()
    end_date = today + timedelta(days=days)
    months = set()
    d = today
    while d <= end_date:
        months.add((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1, day=1)
        else:
            d = d.replace(month=d.month + 1, day=1)
    async with ff_client() as client:
        all_events = []
        for year, month in sorted(months):
            all_events.extend(await scrape_month(year, month, client))
            await asyncio.sleep(1)
    upcoming = []
    for ev in all_events:
        try:
            ed = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
            if today <= ed <= end_date:
                upcoming.append(ev)
        except Exception:
            continue
    upcoming.sort(key=lambda e: (e["event_date"], e.get("event_time") or ""))
    return upcoming

def _consistency_stats(rows: list) -> dict:
    beat = miss = 0
    beat_up = beat_down = miss_up = miss_down = 0
    pips_5 = pips_15 = pips_30 = []
    for r in rows:
        if r.get("pip_5m") is not None:
            pips_5.append(r["pip_5m"])
        if r.get("pip_15m") is not None:
            pips_15.append(r["pip_15m"])
        if r.get("pip_30m") is not None:
            pips_30.append(r["pip_30m"])
        bm = r.get("beat_miss")
        d5 = r.get("direction_5m")
        if bm == "beat":
            beat += 1
            if d5 == "up":
                beat_up += 1
            elif d5 == "down":
                beat_down += 1
        elif bm == "miss":
            miss += 1
            if d5 == "up":
                miss_up += 1
            elif d5 == "down":
                miss_down += 1
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    result = {
        "total_occurrences": len(rows),
        "avg_pip_5m": avg(pips_5),
        "avg_pip_15m": avg(pips_15),
        "avg_pip_30m": avg(pips_30),
        "beat_count": beat,
        "miss_count": miss,
        "beat_direction": None,
        "beat_consistency": None,
        "miss_direction": None,
        "miss_consistency": None,
        "max_pip_5m": max(pips_5) if pips_5 else None,
        "min_pip_5m": min(pips_5) if pips_5 else None,
    }
    if beat > 0:
        result["beat_direction"] = "up" if beat_up >= beat_down else "down"
        result["beat_consistency"] = round(max(beat_up, beat_down) / beat * 100)
    if miss > 0:
        result["miss_direction"] = "up" if miss_up >= miss_down else "down"
        result["miss_consistency"] = round(max(miss_up, miss_down) / miss * 100)
    return result

def format_release_times(event_date: str, event_time: str, country: str) -> dict:
    """Best-effort FF time → UTC and London labels."""
    try:
        from zoneinfo import ZoneInfo
        if not event_time or event_time in ("", "Tentative", "All Day"):
            return {"utc": "13:30 UTC", "london": "13:30 London", "raw": "13:30 (typical US release)"}
        local_dt = None
        for fmt in ["%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                local_dt = datetime.strptime(f"{event_date} {event_time}", fmt)
                break
            except Exception:
                continue
        if not local_dt:
            return {"utc": event_time, "london": event_time, "raw": event_time}
        if country == "GBP":
            uk = local_dt.replace(tzinfo=ZoneInfo("Europe/London"))
            utc = uk.astimezone(ZoneInfo("UTC"))
        else:
            et = local_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            utc = et.astimezone(ZoneInfo("UTC"))
            uk = utc.astimezone(ZoneInfo("Europe/London"))
        return {
            "utc": utc.strftime("%H:%M UTC"),
            "london": uk.strftime("%H:%M London"),
            "raw": event_time,
        }
    except Exception:
        return {"utc": event_time or "—", "london": event_time or "—", "raw": event_time or "—"}

def _confidence_label(consistency: int | None, scenario: str) -> str:
    if consistency is None:
        return "Not enough historical data — wait and confirm direction manually."
    if consistency >= 75:
        return "STRONG SIGNAL — high confidence trade"
    if consistency >= 60:
        return "MODERATE — confirm direction after release"
    if consistency >= 50:
        return "WEAK — skip unless other signals align"
    return f"AVOID — direction is unpredictable on {scenario}s"

def get_brief(event_name: str) -> dict:
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ne.*, pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m, pr.direction_5m
            FROM news_events ne
            LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE ne.your_name = %s
            AND ne.event_date >= '2020-01-01'
            ORDER BY ne.event_date DESC
        """, (event_name,)).fetchall()
        trades = conn.execute("SELECT * FROM trades ORDER BY date DESC").fetchall()
        rating_row = conn.execute(
            "SELECT type, comment FROM news_ratings WHERE name = %s", (event_name,)
        ).fetchone()

        events = [dict(r) for r in rows]
        if not events:
            return {"error": f"No data for {event_name}"}

        stats = _consistency_stats(events)
        today = datetime.now().strftime("%Y-%m-%d")
        upcoming_row = conn.execute("""
            SELECT * FROM news_events
            WHERE your_name = %s AND event_date >= %s
            ORDER BY event_date ASC LIMIT 1
        """, (event_name, today)).fetchone()
        upcoming = dict(upcoming_row) if upcoming_row else dict(events[0])
        sample = upcoming
        country = sample.get("country", "USD")
        release_date = sample.get("event_date", "")
        release_time = sample.get("event_time") or "—"
        forecast = sample.get("forecast") or "—"
        times = format_release_times(release_date, release_time, country)

        same_day_rows = conn.execute("""
            SELECT DISTINCT your_name FROM news_events
            WHERE event_date = %s AND your_name != %s
            ORDER BY your_name
        """, (release_date, event_name)).fetchall()
        same_day_events = [r["your_name"] for r in same_day_rows]

        matched_trades = [
            dict(t) for t in trades
            if event_name in (t["news1"] or "", t["news2"] or "", t["news3"] or "")
            or t["news_event_id"] in [e["id"] for e in events]
        ]
        wins = [t for t in matched_trades if t.get("outcome") == "Win"]
        win_entries = [t["entry"] for t in wins if t.get("entry")]
        suggested_entry = (
            max(set(win_entries), key=win_entries.count) if win_entries
            else "5 min after checking reaction"
        )
        sls = [t["sl"] for t in matched_trades if t.get("sl")]
        suggested_sl = round(sum(sls) / len(sls), 1) if sls else 10
        win_ratios = [t["ratio"] for t in wins if t.get("ratio")]
        suggested_rr = round(sum(win_ratios) / len(win_ratios), 1) if win_ratios else 5

        avg5 = stats["avg_pip_5m"] or 0
        if avg5 >= 15:
            power_verdict = "HIGH-POWER"
        elif avg5 >= 10:
            power_verdict = "MEDIUM-POWER"
        else:
            power_verdict = "LOW-POWER"

        total_trades = len(matched_trades)
        win_rate = round(len(wins) / total_trades * 100) if total_trades else 0
        avg_win_rr = (
            round(sum(win_ratios) / len(win_ratios), 1) if win_ratios else None
        )

        return {
            "event_name": event_name,
            "country": country,
            "release_date": release_date,
            "release_time": release_time,
            "release_time_utc": times["utc"],
            "release_time_london": times["london"],
            "forecast": forecast,
            "stats": stats,
            "beat_direction": stats["beat_direction"],
            "miss_direction": stats["miss_direction"],
            "beat_consistency": stats["beat_consistency"],
            "miss_consistency": stats["miss_consistency"],
            "beat_count": stats["beat_count"],
            "miss_count": stats["miss_count"],
            "min_pip_5m": stats["min_pip_5m"],
            "max_pip_5m": stats["max_pip_5m"],
            "avg_pip_5m": stats["avg_pip_5m"],
            "rating_type": rating_row["type"] if rating_row else None,
            "rating_comment": rating_row["comment"] if rating_row else None,
            "same_day_events": same_day_events,
            "suggested_entry": suggested_entry,
            "suggested_sl": suggested_sl,
            "suggested_rr": suggested_rr,
            "power_verdict": power_verdict,
            "user_history": {
                "count": total_trades,
                "win_rate": win_rate,
                "avg_winning_rr": avg_win_rr,
            },
            "past_trades": [
                {
                    "date": t["date"],
                    "outcome": t["outcome"],
                    "ratio": t.get("ratio"),
                    "improvement": t.get("improvement") or "",
                    "entry": t.get("entry") or "",
                }
                for t in matched_trades[:3]
            ],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()
