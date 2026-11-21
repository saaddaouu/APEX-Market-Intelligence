from __future__ import annotations

import os
import re
import time
import math
import json
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ============================================================
# APEX MARKET INTELLIGENCE V5
# Twelve Data = prices/candles | GDELT = headlines
# Forex Factory weekly XML = economic calendar
# OpenAI Responses API = optional AI interpretation
# ============================================================

APP_VERSION = "5.0.0"
ROOT = Path(__file__).resolve().parent
TWELVE_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna").strip()
TWELVE_BASE = "https://api.twelvedata.com"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
USER_AGENT = "APEX-Market-Intelligence/5.0 (market research dashboard)"

SYMBOLS = [
    "XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY",
    "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
]
TIMEFRAMES = ["1min", "5min", "15min", "30min", "1h", "4h", "1day", "1week", "1month"]
DURATION = {
    "1min": timedelta(minutes=1), "5min": timedelta(minutes=5),
    "15min": timedelta(minutes=15), "30min": timedelta(minutes=30),
    "1h": timedelta(hours=1), "4h": timedelta(hours=4),
    "1day": timedelta(days=1), "1week": timedelta(days=7),
    "1month": timedelta(days=32),
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml, */*"})

app = FastAPI(
    title="APEX Market Intelligence",
    description="Multi-timeframe technical analysis, market news, economic calendar and trade playbooks.",
    version=APP_VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def cache_get(key: str, ttl: int) -> Any | None:
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and time.time() - item[0] <= ttl:
            return item[1]
    return None


def cache_set(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)


def api_error(response: requests.Response, fallback: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or fallback)
    except Exception:
        pass
    return fallback


def twelve_request(endpoint: str, params: dict[str, Any], ttl: int = 45) -> dict[str, Any]:
    if not TWELVE_API_KEY:
        raise HTTPException(503, "TWELVE_DATA_API_KEY is missing from Render environment variables.")
    cache_key = "twelve:" + endpoint + ":" + repr(sorted(params.items()))
    cached = cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    try:
        response = SESSION.get(
            TWELVE_BASE + endpoint,
            params={**params, "apikey": TWELVE_API_KEY},
            timeout=18,
        )
        data = response.json()
    except requests.RequestException:
        raise HTTPException(502, "Twelve Data connection failed. Try again shortly.")
    except ValueError:
        raise HTTPException(502, "Twelve Data returned an unreadable response.")
    if response.status_code >= 400 or not isinstance(data, dict) or data.get("status") == "error":
        raise HTTPException(502, api_error(response, "Twelve Data rejected the request."))
    cache_set(cache_key, data)
    return data


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_candles(symbol: str, interval: str, outputsize: int = 250, ttl: int = 45) -> list[dict[str, Any]]:
    if interval not in TIMEFRAMES:
        raise HTTPException(400, f"Unsupported timeframe: {interval}")
    data = twelve_request(
        "/time_series",
        {"symbol": symbol, "interval": interval, "outputsize": outputsize, "timezone": "UTC"},
        ttl=ttl,
    )
    values = data.get("values") or []
    result: list[dict[str, Any]] = []
    for item in values:
        try:
            result.append({
                "time": item["datetime"],
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    result.sort(key=lambda candle: parse_utc(candle["time"]))
    deduped: dict[str, dict[str, Any]] = {c["time"]: c for c in result}
    result = list(deduped.values())
    result.sort(key=lambda candle: parse_utc(candle["time"]))
    if not result:
        raise HTTPException(502, f"No candles returned for {symbol} {interval}.")
    return result


def closed_candles(candles: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Exclude a candle that has not yet completed. If market is closed, retain the latest closed bar."""
    if len(candles) < 3:
        return candles
    latest = candles[-1]
    start = parse_utc(latest["time"])
    if interval == "1month":
        now = datetime.now(timezone.utc)
        if start.year == now.year and start.month == now.month:
            return candles[:-1]
        return candles
    duration = DURATION.get(interval)
    if duration and start + duration > datetime.now(timezone.utc):
        return candles[:-1]
    return candles


def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * alpha + result * (1.0 - alpha)
    return result


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[index]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[index]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[dict[str, Any]], period: int = 14) -> Optional[float]:
    if len(candles) < 2:
        return None
    ranges = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["close"]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))
    selected = ranges[-period:]
    return sum(selected) / len(selected) if selected else None


def find_pivots(candles: list[dict[str, Any]], left: int = 3, right: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for index in range(left, len(candles) - right):
        window = candles[index - left:index + right + 1]
        high = candles[index]["high"]
        low = candles[index]["low"]
        if high == max(c["high"] for c in window) and sum(c["high"] == high for c in window) == 1:
            highs.append({"index": index, "price": high, "time": candles[index]["time"]})
        if low == min(c["low"] for c in window) and sum(c["low"] == low for c in window) == 1:
            lows.append({"index": index, "price": low, "time": candles[index]["time"]})
    return highs, lows


def market_structure(candles: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    highs, lows = find_pivots(candles)
    labels: list[dict[str, Any]] = []
    for index, pivot in enumerate(highs):
        if index == 0:
            continue
        labels.append({**pivot, "label": "HH" if pivot["price"] > highs[index - 1]["price"] else "LH"})
    for index, pivot in enumerate(lows):
        if index == 0:
            continue
        labels.append({**pivot, "label": "HL" if pivot["price"] > lows[index - 1]["price"] else "LL"})
    labels.sort(key=lambda point: parse_utc(point["time"]))
    if len(highs) < 2 or len(lows) < 2:
        direction = "RANGE"
    else:
        hh = highs[-1]["price"] > highs[-2]["price"]
        hl = lows[-1]["price"] > lows[-2]["price"]
        lh = highs[-1]["price"] < highs[-2]["price"]
        ll = lows[-1]["price"] < lows[-2]["price"]
        direction = "BULLISH" if hh and hl else "BEARISH" if lh and ll else "RANGE"
    return direction, {
        "highs": highs[-6:],
        "lows": lows[-6:],
        "labels": labels[-12:],
        "last_high": highs[-1] if highs else None,
        "last_low": lows[-1] if lows else None,
    }


def zone_touched(candles: list[dict[str, Any]], low: float, high: float) -> bool:
    return any(c["low"] <= high and c["high"] >= low for c in candles)


def detect_supply_demand(candles: list[dict[str, Any]], timeframe: str, category: str) -> list[dict[str, Any]]:
    """Detect opposing base candles followed by ATR-sized displacement; remove invalidated zones."""
    if len(candles) < 35:
        return []
    volatility = atr(candles)
    if not volatility or volatility <= 0:
        return []
    found: list[dict[str, Any]] = []
    # only use confirmed/closed candles supplied by caller
    for index in range(max(5, len(candles) - 100), len(candles) - 4):
        base = candles[index]
        forward = candles[index + 1:index + 4]
        future = candles[index + 4:]
        body_high = max(base["open"], base["close"])
        body_low = min(base["open"], base["close"])
        forward_high = max(c["close"] for c in forward)
        forward_low = min(c["close"] for c in forward)
        bullish_displacement = forward_high - body_high >= volatility * (1.7 if category == "POI" else 1.35)
        bearish_displacement = body_low - forward_low >= volatility * (1.7 if category == "POI" else 1.35)
        # An origin candle should oppose the move or be a small-base candle.
        base_range = max(base["high"] - base["low"], 1e-12)
        small_base = abs(base["close"] - base["open"]) / base_range < 0.45
        kind = None
        if bullish_displacement and (base["close"] < base["open"] or small_base):
            kind = "DEMAND"
            low, high = base["low"], body_high
        elif bearish_displacement and (base["close"] > base["open"] or small_base):
            kind = "SUPPLY"
            low, high = body_low, base["high"]
        if not kind or high <= low:
            continue
        invalidated = any((c["close"] < low if kind == "DEMAND" else c["close"] > high) for c in future)
        if invalidated:
            continue
        touched = zone_touched(future, low, high)
        displacement = (forward_high - body_high) / volatility if kind == "DEMAND" else (body_low - forward_low) / volatility
        found.append({
            "id": f"{timeframe}-{category}-{index}-{kind}",
            "low": round(low, 8), "high": round(high, 8),
            "type": kind, "tf": timeframe, "category": category,
            "poi": category == "POI", "status": "TESTED" if touched else "FRESH",
            "tested": touched, "valid": True, "start_time": base["time"],
            "pattern": "DISPLACEMENT ORIGIN", "strength_atr": round(displacement, 2),
        })
    # De-duplicate overlapping zones of the same type and timeframe, retain the most recent.
    found.sort(key=lambda z: parse_utc(z["start_time"]))
    deduped: list[dict[str, Any]] = []
    for zone in found:
        overlap = next((z for z in deduped if z["type"] == zone["type"] and z["tf"] == zone["tf"] and not (zone["high"] < z["low"] or zone["low"] > z["high"])), None)
        if overlap:
            if zone["strength_atr"] >= overlap["strength_atr"]:
                deduped[deduped.index(overlap)] = zone
        else:
            deduped.append(zone)
    return deduped[-5:]


def detect_double_patterns(candles: list[dict[str, Any]], timeframe: str) -> list[dict[str, Any]]:
    highs, lows = find_pivots(candles, 3, 3)
    volatility = atr(candles) or 0
    tolerance = volatility * 0.22
    patterns: list[dict[str, Any]] = []
    for pivots, pattern, kind in ((highs, "DOUBLE TOP", "SUPPLY"), (lows, "DOUBLE BOTTOM", "DEMAND")):
        if len(pivots) < 2 or tolerance <= 0:
            continue
        for first, second in zip(pivots[-6:-1], pivots[-5:]):
            if second["index"] - first["index"] < 5 or abs(first["price"] - second["price"]) > tolerance:
                continue
            center = (first["price"] + second["price"]) / 2
            patterns.append({
                "id": f"{timeframe}-{pattern}-{first['index']}",
                "low": round(center - tolerance, 8), "high": round(center + tolerance, 8),
                "type": kind, "tf": timeframe, "category": "PATTERN", "poi": False,
                "status": "PATTERN", "tested": False, "valid": True,
                "start_time": first["time"], "pattern": pattern,
                "strength_atr": 1.0,
            })
    return patterns[-3:]


def detect_liquidity(candles: list[dict[str, Any]], timeframe: str) -> list[dict[str, Any]]:
    highs, lows = find_pivots(candles)
    volatility = atr(candles) or 0
    tolerance = volatility * 0.12
    result: list[dict[str, Any]] = []
    for pivots, label in ((highs, "EQUAL HIGHS · BUY-SIDE LIQUIDITY"), (lows, "EQUAL LOWS · SELL-SIDE LIQUIDITY")):
        if len(pivots) < 2 or tolerance <= 0:
            continue
        for first, second in zip(pivots[-5:-1], pivots[-4:]):
            if second["index"] - first["index"] >= 3 and abs(first["price"] - second["price"]) <= tolerance:
                result.append({
                    "price": round((first["price"] + second["price"]) / 2, 8),
                    "label": label, "tf": timeframe, "time": second["time"],
                })
    return result[-4:]


def strong_engulfing(candles: list[dict[str, Any]]) -> Optional[str]:
    if len(candles) < 3:
        return None
    previous, current = candles[-2], candles[-1]
    previous_body_high = max(previous["open"], previous["close"])
    previous_body_low = min(previous["open"], previous["close"])
    body = abs(current["close"] - current["open"])
    span = max(current["high"] - current["low"], 1e-12)
    bullish = (
        current["close"] > current["open"]
        and previous["close"] < previous["open"]
        and current["open"] <= previous_body_low
        and current["close"] >= previous_body_high
        and body / span >= 0.62
    )
    bearish = (
        current["close"] < current["open"]
        and previous["close"] > previous["open"]
        and current["open"] >= previous_body_high
        and current["close"] <= previous_body_low
        and body / span >= 0.62
    )
    return "BULLISH ENGULFING" if bullish else "BEARISH ENGULFING" if bearish else None


def body_key_levels(monthly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    completed = []
    for candle in monthly:
        dt = parse_utc(candle["time"])
        if (dt.year, dt.month) < (now.year, now.month):
            completed.append(candle)
    # oldest -> newest; last 12 completed monthly candles form a rolling 1Y candle.
    completed.sort(key=lambda c: parse_utc(c["time"]))
    if len(completed) < 12:
        return []
    levels: list[dict[str, Any]] = []
    groups = [("1M", completed[-1:]), ("3M", completed[-3:]), ("1Y", completed[-12:])]
    for label, group in groups:
        if not group:
            continue
        period_open = group[0]["open"]
        period_close = group[-1]["close"]
        levels.extend([
            {"label": f"{label} BODY TOP", "price": round(max(period_open, period_close), 8), "period": label, "basis": "OPEN/CLOSE ONLY"},
            {"label": f"{label} BODY BOTTOM", "price": round(min(period_open, period_close), 8), "period": label, "basis": "OPEN/CLOSE ONLY"},
        ])
    return levels


def candle_touches_zone(candle: dict[str, Any], zone: dict[str, Any]) -> bool:
    return candle["low"] <= zone["high"] and candle["high"] >= zone["low"]


def retest_after_confirmation(candles: list[dict[str, Any]], zone: dict[str, Any], direction: str, confirmation_time: str) -> bool:
    confirmation_dt = parse_utc(confirmation_time)
    after = [c for c in candles if parse_utc(c["time"]) > confirmation_dt]
    if len(after) < 2:
        return False
    # A post-confirmation bar must revisit the zone then reject it in the confirmed direction.
    for index, candle in enumerate(after[-8:]):
        if not candle_touches_zone(candle, zone):
            continue
        later = after[-8:][index + 1:]
        if direction == "BUY" and any(c["close"] > zone["high"] and c["close"] > c["open"] for c in later):
            return True
        if direction == "SELL" and any(c["close"] < zone["low"] and c["close"] < c["open"] for c in later):
            return True
    return False


def select_relevant_zone(zones: list[dict[str, Any]], direction: str, price: float, volatility: float, timeframe_set: set[str]) -> Optional[dict[str, Any]]:
    wanted_type = "DEMAND" if direction == "BUY" else "SUPPLY"
    candidates = [z for z in zones if z["type"] == wanted_type and z["tf"] in timeframe_set and z.get("valid", True)]
    if not candidates:
        return None
    max_distance = max((volatility or price * 0.001) * 2.5, price * 0.0005)
    def distance(zone: dict[str, Any]) -> float:
        if zone["low"] <= price <= zone["high"]:
            return 0.0
        return min(abs(price - zone["low"]), abs(price - zone["high"]))
    candidates = [z for z in candidates if distance(z) <= max_distance]
    if not candidates:
        return None
    candidates.sort(key=lambda z: (distance(z), z.get("status") != "FRESH", -parse_utc(z["start_time"]).timestamp()))
    return candidates[0]



def market_news_sentiment(symbol: str, news: dict[str, Any]) -> dict[str, Any]:
    items = news.get("items", [])
    if news.get("status") != "LIVE" or not items:
        return {"bias": "UNAVAILABLE", "score": 0, "source": news.get("provider", "GDELT"), "reason": "No current news items were returned."}
    bullish = 0
    bearish = 0
    for item in items:
        title = (item.get("title") or "").lower()
        tone = item.get("tone")
        if symbol == "XAU/USD":
            bullish += 2 if tone == "GOLD SUPPORTIVE" else 1 if tone == "RISK / SAFE-HAVEN" else 0
            bearish += 2 if tone == "GOLD HEADWIND" else 0
        else:
            usd_bull = any(k in title for k in ("hawkish", "rate hike", "strong dollar", "dollar rises", "higher yields", "rising yields", "hot inflation"))
            usd_bear = any(k in title for k in ("dovish", "rate cut", "weak dollar", "dollar falls", "lower yields", "falling yields", "soft inflation"))
            if usd_bull or usd_bear:
                if symbol.startswith("USD/"):
                    bullish += int(usd_bull)
                    bearish += int(usd_bear)
                elif symbol.endswith("/USD"):
                    bullish += int(usd_bear)
                    bearish += int(usd_bull)
    if bullish == bearish == 0:
        bias = "NEUTRAL"
    elif bullish >= bearish + 2:
        bias = "BULLISH"
    elif bearish >= bullish + 2:
        bias = "BEARISH"
    else:
        bias = "MIXED"
    return {"bias": bias, "score": bullish - bearish, "source": news.get("provider", "GDELT"), "reason": f"Headline read-through: {bullish} supportive point(s), {bearish} opposing point(s). This is not causal proof."}


def high_impact_event_near(symbol: str, calendar: dict[str, Any], minutes: int = 30) -> Optional[dict[str, Any]]:
    if calendar.get("status") != "LIVE":
        return None
    relevant_currencies = {"USD"} if symbol == "XAU/USD" else {symbol.split("/")[0], symbol.split("/")[1]}
    now = datetime.now(timezone.utc)
    new_york_today = datetime.now(ZoneInfo("America/New_York")).date()
    for event in calendar.get("events", []):
        if event.get("impact") != "HIGH" or event.get("currency") not in relevant_currencies:
            continue
        if event.get("datetime_utc"):
            event_time = parse_utc(event["datetime_utc"])
            if -timedelta(minutes=15) <= event_time - now <= timedelta(minutes=minutes):
                return event
            continue
        # A high-impact event with an unknown/TBA release time on today's NY calendar
        # is treated conservatively as a risk window rather than silently ignored.
        date_text = str(event.get("date") or "").strip()
        event_date = None
        for date_format in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                event_date = datetime.strptime(date_text, date_format).date()
                break
            except ValueError:
                continue
        if event_date == new_york_today:
            return event
    return None


def matching_key_level(zone: dict[str, Any], key_levels: list[dict[str, Any]], volatility: float) -> Optional[dict[str, Any]]:
    tolerance = max((volatility or 0) * 0.60, (zone["high"] - zone["low"]) * 1.5)
    closest = None
    closest_distance = float("inf")
    for level in key_levels:
        price = float(level["price"])
        distance = 0 if zone["low"] <= price <= zone["high"] else min(abs(price - zone["low"]), abs(price - zone["high"]))
        if distance < closest_distance:
            closest, closest_distance = level, distance
    return closest if closest and closest_distance <= tolerance else None


def poi_confluence(zone: dict[str, Any], zones: list[dict[str, Any]], timeframe_set: set[str], volatility: float) -> Optional[dict[str, Any]]:
    if zone.get("category") == "POI":
        return zone
    tolerance = (volatility or 0) * 0.35
    for candidate in zones:
        if candidate.get("category") != "POI" or candidate.get("type") != zone.get("type") or candidate.get("tf") not in timeframe_set:
            continue
        if candidate["low"] <= zone["high"] + tolerance and candidate["high"] >= zone["low"] - tolerance:
            return candidate
    return None

def build_setup(
    mode: str,
    frames: dict[str, list[dict[str, Any]]],
    structures: dict[str, str],
    zones: list[dict[str, Any]],
    price: float,
    key_levels: list[dict[str, Any]],
    sentiment: dict[str, Any],
    event_block: Optional[dict[str, Any]],
    calendar_status: str,
) -> dict[str, Any]:
    if mode in ("intraday", "swing"):
        direction = structures.get("1day") if structures.get("1day") in ("BULLISH", "BEARISH") else "RANGE"
        higher_ok = direction in ("BULLISH", "BEARISH") and structures.get("4h") == direction
        zone_tfs = {"1day", "4h"}
        confirm_tfs = ("1h", "15min")
        retest_tf = "5min"
        mandatory_close_tf = "15min"
    else:
        direction = structures.get("1h") if structures.get("1h") in ("BULLISH", "BEARISH") else "RANGE"
        higher_ok = direction in ("BULLISH", "BEARISH") and structures.get("15min") == direction
        zone_tfs = {"1h", "15min"}
        confirm_tfs = ("5min",)
        retest_tf = "1min"
        mandatory_close_tf = "5min"

    reasons: list[str] = []
    checks = {"structure": higher_ok, "zone": False, "key_level": False, "poi": False, "sentiment": False, "event_clear": calendar_status == "LIVE" and event_block is None, "candle_confirmation": False, "timeframe_close": False, "retest": False}
    if not higher_ok:
        reasons.append(f"Higher-timeframe structure is not aligned for {mode.upper()}.")
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR STRUCTURE ALIGNMENT", "confidence": 18, "directional_structure": direction, "zone": None, "confirmations": {}, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}

    bias = "BUY" if direction == "BULLISH" else "SELL"
    trigger_frame = frames.get(mandatory_close_tf, [])
    volatility = atr(trigger_frame) or atr(frames.get(confirm_tfs[-1], [])) or 0
    relevant_zone = select_relevant_zone(zones, bias, price, volatility, zone_tfs)
    if not relevant_zone:
        reasons.append("Price is not close enough to an active matching supply/demand zone.")
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR SUPPLY / DEMAND ZONE", "confidence": 25, "directional_structure": direction, "zone": None, "confirmations": {}, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}
    checks["zone"] = True

    key_level = matching_key_level(relevant_zone, key_levels, volatility)
    checks["key_level"] = key_level is not None
    poi_zone = poi_confluence(relevant_zone, zones, zone_tfs, volatility)
    checks["poi"] = poi_zone is not None
    sentiment_bias = sentiment.get("bias", "UNAVAILABLE")
    checks["sentiment"] = sentiment_bias in ("NEUTRAL", bias_to_sentiment(bias))
    if event_block or calendar_status != "LIVE":
        checks["event_clear"] = False

    confirmations = {tf: strong_engulfing(frames.get(tf, [])) for tf in confirm_tfs}
    expected = "BULLISH ENGULFING" if bias == "BUY" else "BEARISH ENGULFING"
    confirms_ok = all(value == expected for value in confirmations.values())
    checks["candle_confirmation"] = confirms_ok
    close_ok = bool(trigger_frame) and (
        trigger_frame[-1]["close"] > relevant_zone["high"] if bias == "BUY"
        else trigger_frame[-1]["close"] < relevant_zone["low"]
    )
    checks["timeframe_close"] = close_ok

    if not checks["key_level"]:
        reasons.append("The zone does not have sufficient confluence with a previous 1M, 3M or 1Y body level.")
    else:
        reasons.append(f"Key-level confluence: {key_level['label']} at {key_level['price']}.")
    if not checks["poi"]:
        reasons.append("A matching POI origin zone is not overlapping the selected supply/demand area.")
    else:
        reasons.append(f"POI confluence confirmed on {poi_zone['tf']}.")
    reasons.append(f"Market sentiment: {sentiment_bias} — {sentiment.get('reason', 'No sentiment detail.')}")
    if event_block:
        reasons.append(f"High-impact event risk: {event_block.get('currency')} {event_block.get('title')} is close to release; wait for the reaction.")
    elif calendar_status != "LIVE":
        reasons.append("Economic calendar feed is unavailable; the upcoming high-impact event window cannot be verified.")

    # Fail closed: sentiment and event context cannot be skipped for a live setup.
    if not checks["sentiment"] or not checks["event_clear"]:
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR MARKET SENTIMENT / EVENT RISK TO CLEAR", "confidence": 35, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}
    if not checks["key_level"] or not checks["poi"]:
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR KEY LEVEL + POI CONFLUENCE", "confidence": 42, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}
    if not confirms_ok:
        reasons.append("Strong closed-candle engulfing confirmation is incomplete.")
        for timeframe, value in confirmations.items():
            reasons.append(f"{timeframe}: {value or 'NO CONFIRMATION'}")
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR ENGULFING CONFIRMATION", "confidence": 52, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}
    if not close_ok:
        reasons.append(f"The completed {mandatory_close_tf} candle has not closed beyond the zone.")
        return {"mode": mode, "bias": "WAIT", "stage": f"WAIT FOR {mandatory_close_tf.upper()} CLOSE", "confidence": 60, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}

    confirmation_time = trigger_frame[-1]["time"] if trigger_frame else None
    retest_candles = frames.get(retest_tf, [])
    retested = bool(confirmation_time and retest_candles and retest_after_confirmation(retest_candles, relevant_zone, bias, confirmation_time))
    checks["retest"] = retested
    if not retested:
        reasons.append(f"Confirmation is complete. Wait for a {retest_tf} retest and directional rejection.")
        return {"mode": mode, "bias": "WAIT", "stage": "WAIT FOR RETEST", "confidence": 70, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": False, "checks": checks, "reasons": reasons, "trade_plan": empty_trade_plan()}

    reasons.append("All filters passed: structure, supply/demand, key level, POI, sentiment, closed candle confirmation and retest.")
    plan = make_trade_plan(bias, relevant_zone, retest_candles, atr(retest_candles) or volatility)
    return {"mode": mode, "bias": bias, "stage": "RETEST CONFIRMED · SETUP READY", "confidence": 82, "directional_structure": direction, "zone": relevant_zone, "key_level": key_level, "poi_zone": poi_zone, "confirmations": confirmations, "retest": True, "checks": checks, "reasons": reasons, "trade_plan": plan}


def bias_to_sentiment(bias: str) -> str:
    return "BULLISH" if bias == "BUY" else "BEARISH"

def empty_trade_plan() -> dict[str, Any]:
    return {"entry": None, "entry_zone": None, "stop_loss": None, "take_profit": {"TP1": None, "TP2": None, "TP3": None}, "risk_reward": None}


def make_trade_plan(direction: str, zone: dict[str, Any], candles: list[dict[str, Any]], volatility: float) -> dict[str, Any]:
    if not candles or volatility <= 0:
        return empty_trade_plan()
    entry = candles[-1]["close"]
    buffer = volatility * 0.25
    stop = zone["low"] - buffer if direction == "BUY" else zone["high"] + buffer
    risk = entry - stop if direction == "BUY" else stop - entry
    if risk <= 0:
        return empty_trade_plan()
    sign = 1 if direction == "BUY" else -1
    return {
        "entry": round(entry, 8),
        "entry_zone": {"low": round(min(entry - volatility * .12, entry + volatility * .12), 8), "high": round(max(entry - volatility * .12, entry + volatility * .12), 8)},
        "stop_loss": round(stop, 8),
        "take_profit": {
            "TP1": round(entry + sign * risk, 8),
            "TP2": round(entry + sign * risk * 2, 8),
            "TP3": round(entry + sign * risk * 3, 8),
        },
        "risk_reward": {"TP1": "1:1", "TP2": "1:2", "TP3": "1:3"},
    }


def build_technical_analysis(symbol: str, selected_interval: str) -> dict[str, Any]:
    # Cache provider requests; use confirmed closed candles for structure and signals.
    frame_intervals = {"selected": selected_interval, "1day": "1day", "4h": "4h", "1h": "1h", "15min": "15min", "5min": "5min", "1min": "1min"}
    raw_frames: dict[str, list[dict[str, Any]]] = {}
    frames: dict[str, list[dict[str, Any]]] = {}
    structures: dict[str, str] = {}
    structure_details: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for label, interval in frame_intervals.items():
        try:
            raw = get_candles(symbol, interval, 260 if interval in ("1day", "4h", "1h") else 220)
            raw_frames[label] = raw
            closed = closed_candles(raw, interval)
            frames[label] = closed if len(closed) >= 20 else raw
            if label != "selected":
                state, details = market_structure(frames[label])
                structures[label] = state
                structure_details[label] = details
        except HTTPException as exc:
            errors[label] = str(exc.detail)
            raw_frames[label] = []
            frames[label] = []
            if label != "selected":
                structures[label] = "UNAVAILABLE"
                structure_details[label] = {"labels": [], "highs": [], "lows": []}

    selected_raw = raw_frames.get("selected") or raw_frames.get("1day") or []
    selected_closed = closed_candles(selected_raw, selected_interval)
    selected = selected_closed if len(selected_closed) >= 25 else selected_raw
    if not selected:
        raise HTTPException(502, "Selected timeframe candles are unavailable.")

    # Make the selected timeframe available to the indicator panel as well.
    selected_closes = [c["close"] for c in selected]
    selected_structure, selected_details = market_structure(selected)
    selected_atr = atr(selected)

    all_zones: list[dict[str, Any]] = []
    liquidity: list[dict[str, Any]] = []
    for timeframe in ("1day", "4h", "1h", "15min"):
        candles_for_tf = frames.get(timeframe) or []
        if len(candles_for_tf) < 35:
            continue
        # Supply/demand and double tops/bottoms are separate overlays.
        all_zones.extend(detect_supply_demand(candles_for_tf, timeframe, "SUPPLY_DEMAND"))
        all_zones.extend(detect_double_patterns(candles_for_tf, timeframe))
        # POI origins are distinct from the broader supply/demand layer.
        all_zones.extend(detect_supply_demand(candles_for_tf, timeframe, "POI"))
        liquidity.extend(detect_liquidity(candles_for_tf, timeframe))

    # Avoid excessive overlapping drawings: current and nearest zones first, but keep zones by their source timeframe.
    selected_price = selected[-1]["close"]
    one_minute_latest = raw_frames.get("1min") or []
    current_price = one_minute_latest[-1]["close"] if one_minute_latest else selected_price
    selected_atr = selected_atr or current_price * 0.001
    all_zones.sort(key=lambda z: (abs((z["low"] + z["high"]) / 2 - current_price), z["status"] != "FRESH"))
    all_zones = all_zones[:36]

    key_levels: list[dict[str, Any]] = []
    try:
        monthly = get_candles(symbol, "1month", 18, ttl=600)
        key_levels = body_key_levels(monthly)
    except HTTPException as exc:
        errors["key_levels"] = str(exc.detail)

    selected_values = selected_closes
    indicator_values = {
        "ema20": ema(selected_values, 20),
        "ema50": ema(selected_values, 50),
        "ema200": ema(selected_values, 200),
        "rsi14": rsi(selected_values),
        "atr14": selected_atr,
    }
    current_trend = "BULLISH" if indicator_values["ema20"] and indicator_values["ema50"] and current_price > indicator_values["ema20"] > indicator_values["ema50"] else "BEARISH" if indicator_values["ema20"] and indicator_values["ema50"] and current_price < indicator_values["ema20"] < indicator_values["ema50"] else "MIXED"

    # Macro/news context is cached separately; technical triggers remain independent of the AI text output.
    news_context = fetch_news(symbol)
    calendar_context = fetch_calendar()
    sentiment = market_news_sentiment(symbol, news_context)
    event_block = high_impact_event_near(symbol, calendar_context)
    setups = {
        mode: build_setup(mode, frames, structures, all_zones, current_price, key_levels, sentiment, event_block, calendar_context.get("status", "UNAVAILABLE"))
        for mode in ("swing", "intraday", "scalp")
    }
    return {
        "engine": "APEX Market Intelligence V5",
        "symbol": symbol,
        "selected_timeframe": selected_interval,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": {
            "price": current_price,
            "selected_timeframe_close": selected_price,
            "trend": current_trend,
            "structure": selected_structure,
            "htf_structure": structures,
            "structure_details": {**structure_details, "selected": selected_details},
            "sentiment": sentiment,
            "high_impact_event_block": event_block,
            "news_status": news_context.get("status"),
            "calendar_status": calendar_context.get("status"),
        },
        "indicators": indicator_values,
        "levels": {
            "key_levels": key_levels,
            "support": min((z["low"] for z in all_zones if z["type"] == "DEMAND"), default=None),
            "resistance": max((z["high"] for z in all_zones if z["type"] == "SUPPLY"), default=None),
        },
        "zones": all_zones,
        "liquidity": liquidity[-20:],
        "setups": setups,
        "data_warnings": errors,
        "disclaimer": "Technical confluence is not a probability of profit. Zones and patterns are algorithmic estimates, not guaranteed reversal points.",
    }


@app.get("/api/market/watch")
def market_watch():
    watch_symbols = ["XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY"]
    quotes = []
    for item in watch_symbols:
        try:
            data = twelve_request("/price", {"symbol": item}, ttl=30)
            quotes.append({"symbol": item, "price": float(data["price"]), "status": "LIVE"})
        except Exception:
            quotes.append({"symbol": item, "price": None, "status": "UNAVAILABLE"})
    return {"provider": "Twelve Data", "updated_at": datetime.now(timezone.utc).isoformat(), "quotes": quotes}


# -------------------------- NEWS --------------------------


def news_query_for(symbol: str) -> str:
    if symbol == "XAU/USD":
        return '(gold OR bullion OR "Federal Reserve" OR inflation OR "US dollar" OR "Treasury yields" OR FOMC OR geopolitics)'
    if "USD" in symbol:
        return '("US dollar" OR Federal Reserve OR US inflation OR Treasury OR employment OR FOMC OR forex)'
    return '(forex OR central bank OR interest rates OR inflation OR currency OR economy)'


def classify_headline(title: str, symbol: str) -> dict[str, str]:
    text = title.lower()
    high_terms = ("fed", "fomc", "rate decision", "interest rate", "cpi", "inflation", "nfp", "payroll", "employment", "central bank", "war", "sanction", "tariff", "crisis")
    impact = "HIGH" if any(term in text for term in high_terms) else "MEDIUM" if any(term in text for term in ("dollar", "gold", "oil", "bond", "yield", "economy", "gdp", "pmi")) else "LOW"
    if symbol == "XAU/USD":
        if any(term in text for term in ("higher yields", "rising yields", "hawkish", "rate hike", "strong dollar", "dollar rises", "hot inflation")):
            tone, readthrough = "GOLD HEADWIND", "May pressure gold through USD or yield channels; confirm with live price reaction."
        elif any(term in text for term in ("lower yields", "falling yields", "dovish", "rate cut", "weak dollar", "dollar falls", "soft inflation")):
            tone, readthrough = "GOLD SUPPORTIVE", "May support gold through USD or yield channels; confirm with live price reaction."
        elif any(term in text for term in ("war", "attack", "conflict", "geopolitical", "safe haven", "sanction")):
            tone, readthrough = "RISK / SAFE-HAVEN", "May increase safe-haven demand, but the USD and yields can offset the move."
        else:
            tone, readthrough = "MIXED / UNCONFIRMED", "Headline alone does not establish direction; wait for market confirmation."
    else:
        tone, readthrough = "REVIEW REQUIRED", "Headline detected; asset-specific direction needs currency and market context."
    return {"impact": impact, "tone": tone, "readthrough": readthrough}


def fetch_news(symbol: str, max_records: int = 18) -> dict[str, Any]:
    cache_key = f"news:{symbol}"
    cached = cache_get(cache_key, 300)
    if cached is not None:
        return cached
    params = {
        "query": news_query_for(symbol) + " sourcelang:English",
        "mode": "ArtList", "format": "json", "maxrecords": max_records,
        "timespan": "24h", "sort": "DateDesc",
    }
    try:
        response = SESSION.get(GDELT_URL, params=params, timeout=18)
        response.raise_for_status()
        payload = response.json()
        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        items = []
        for article in articles:
            title = str(article.get("title") or "").strip()
            url = str(article.get("url") or "").strip()
            if not title or not url:
                continue
            classification = classify_headline(title, symbol)
            items.append({
                "title": title,
                "url": url,
                "source": article.get("domain") or article.get("sourcecountry") or "News source",
                "published": article.get("seendate") or article.get("date") or "",
                "image": article.get("socialimage") or None,
                **classification,
            })
        result = {
            "status": "LIVE" if items else "EMPTY",
            "provider": "GDELT DOC 2.0",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": items[:max_records],
            "note": "Headline tone is a transparent keyword-based market read-through, not a verified causal forecast.",
        }
    except Exception as exc:
        result = {
            "status": "UNAVAILABLE", "provider": "GDELT DOC 2.0",
            "updated_at": datetime.now(timezone.utc).isoformat(), "items": [],
            "note": f"News feed temporarily unavailable: {type(exc).__name__}.",
        }
    cache_set(cache_key, result)
    return result


# -------------------- ECONOMIC CALENDAR --------------------


def parse_ff_event_date(date_text: str, time_text: str) -> tuple[Optional[str], str]:
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip()
    if not date_text:
        return None, time_text or "Time TBA"
    for date_format in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            date_part = datetime.strptime(date_text, date_format)
            break
        except ValueError:
            date_part = None
    if not date_part:
        return None, time_text or "Time TBA"
    if not time_text or time_text.lower() in ("all day", "tentative", "day 1", "day 2"):
        return None, time_text or "Time TBA"
    clean = time_text.upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            time_part = datetime.strptime(clean, fmt).time()
            local = datetime.combine(date_part.date(), time_part, tzinfo=ZoneInfo("America/New_York"))
            return local.astimezone(timezone.utc).isoformat(), time_text
        except ValueError:
            continue
    return None, time_text


def fetch_calendar() -> dict[str, Any]:
    cache_key = "calendar:forexfactory-week"
    cached = cache_get(cache_key, 600)
    if cached is not None:
        return cached
    try:
        response = SESSION.get(CALENDAR_URL, timeout=18)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        events = []
        for node in root.findall("event"):
            get = lambda tag: (node.findtext(tag) or "").strip()
            title = get("title")
            currency = get("country")
            impact = get("impact").upper()
            event_utc, raw_time = parse_ff_event_date(get("date"), get("time"))
            if not title:
                continue
            events.append({
                "title": title,
                "currency": currency,
                "impact": impact if impact in ("HIGH", "MEDIUM", "LOW", "HOLIDAY") else "UNKNOWN",
                "date": get("date"), "time": raw_time,
                "datetime_utc": event_utc,
                "actual": get("actual") or None,
                "forecast": get("forecast") or None,
                "previous": get("previous") or None,
                "source": "Forex Factory calendar feed",
                "url": "https://www.forexfactory.com/calendar",
            })
        result = {
            "status": "LIVE" if events else "EMPTY",
            "provider": "Forex Factory weekly calendar feed",
            "timezone": "America/New_York (feed time converted to UTC)",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "events": events,
            "note": "Calendar values are shown as supplied by the feed. Actual/forecast values are not inferred.",
        }
    except Exception as exc:
        result = {
            "status": "UNAVAILABLE", "provider": "Forex Factory weekly calendar feed",
            "timezone": "America/New_York", "updated_at": datetime.now(timezone.utc).isoformat(),
            "events": [], "note": f"Calendar feed temporarily unavailable: {type(exc).__name__}.",
        }
    cache_set(cache_key, result)
    return result


def event_playbook(event: dict[str, Any]) -> dict[str, Any]:
    title = (event.get("title") or "").lower()
    currency = event.get("currency") or ""
    if any(word in title for word in ("cpi", "inflation", "pce")):
        theme = "Inflation / price pressure"
        scenarios = [
            {"scenario": "Actual above forecast", "readthrough": "Can lift rate expectations and USD/yields; potential headwind for gold, subject to reaction."},
            {"scenario": "Actual below forecast", "readthrough": "Can soften rate expectations and USD/yields; potential support for gold, subject to reaction."},
            {"scenario": "Near forecast", "readthrough": "Initial move may fade; wait for price and yields confirmation."},
        ]
    elif any(word in title for word in ("employment", "payroll", "nfp", "unemployment", "jobless")):
        theme = "US labour market"
        scenarios = [
            {"scenario": "Stronger than forecast", "readthrough": "Can support USD and yields; possible pressure on gold."},
            {"scenario": "Weaker than forecast", "readthrough": "Can weigh on USD and yields; possible support for gold."},
            {"scenario": "Mixed release", "readthrough": "Compare payrolls, unemployment and wages; avoid a one-number conclusion."},
        ]
    elif any(word in title for word in ("fomc", "federal funds", "interest rate", "rate statement", "powell", "central bank")):
        theme = "Central-bank policy"
        scenarios = [
            {"scenario": "More hawkish than expected", "readthrough": "Can support the currency and yields; reassess gold and risk assets."},
            {"scenario": "More dovish than expected", "readthrough": "Can weaken the currency and yields; reassess gold and risk assets."},
            {"scenario": "In line with expectations", "readthrough": "Guidance, projections and press-conference language may drive the reaction."},
        ]
    else:
        theme = "Macro release"
        scenarios = [
            {"scenario": "Above forecast", "readthrough": "Interpret according to the release, currency and prevailing central-bank expectations."},
            {"scenario": "Below forecast", "readthrough": "Interpret according to the release, currency and prevailing central-bank expectations."},
            {"scenario": "Near forecast", "readthrough": "Wait for a confirmed price reaction rather than assuming direction."},
        ]
    return {"theme": theme, "currency": currency, "scenarios": scenarios}


# -------------------------- AI BRIEF --------------------------


def openai_interpretation(symbol: str, technical: dict[str, Any], news: dict[str, Any], calendar: dict[str, Any]) -> Optional[str]:
    """Generate a cached AI market brief from APEX data; AI never changes trade eligibility."""
    if not OPENAI_API_KEY:
        return None

    market = technical.get("market", {})
    levels = technical.get("levels", {})
    headlines = [
        {
            "title": x.get("title"),
            "source": x.get("source"),
            "published": x.get("published"),
            "impact_tag": x.get("impact"),
            "heuristic_tone": x.get("tone"),
            "readthrough": x.get("readthrough"),
            "url": x.get("url"),
        }
        for x in news.get("items", [])[:8]
    ]
    events = [
        {
            "title": x.get("title"),
            "currency": x.get("currency"),
            "impact": x.get("impact"),
            "datetime_utc": x.get("datetime_utc"),
            "actual": x.get("actual"),
            "forecast": x.get("forecast"),
            "previous": x.get("previous"),
            "source": x.get("source"),
            "url": x.get("url"),
        }
        for x in calendar.get("events", [])[:12]
    ]
    payload = {
        "symbol": symbol,
        "data_status": {
            "news": news.get("status"),
            "calendar": calendar.get("status"),
            "technical_warnings": technical.get("data_warnings", {}),
        },
        "market": {
            "price": market.get("price"),
            "trend": market.get("trend"),
            "selected_structure": market.get("structure"),
            "higher_timeframe_structure": market.get("htf_structure", {}),
            "news_sentiment": market.get("sentiment", {}),
            "high_impact_event_block": market.get("high_impact_event_block"),
        },
        "indicators": technical.get("indicators", {}),
        "key_levels_body_only": levels.get("key_levels", []),
        "active_zones": [
            {k: z.get(k) for k in ("type", "tf", "category", "low", "high", "status", "pattern", "strength_atr")}
            for z in technical.get("zones", [])[:18]
        ],
        "liquidity_levels": technical.get("liquidity", [])[:10],
        "technical_setups": {
            mode: {
                "bias": setup.get("bias"),
                "stage": setup.get("stage"),
                "confidence_confluence_score": setup.get("confidence"),
                "checks": setup.get("checks"),
                "reasons": setup.get("reasons"),
            }
            for mode, setup in technical.get("setups", {}).items()
        },
        "headlines": headlines,
        "economic_events": events,
    }

    cache_key = "openai-brief:" + symbol
    cached = cache_get(cache_key, 600)
    if cached is not None:
        return cached

    instructions = (
        "You are APEX Market Intelligence, a cautious market research analyst. "
        "Use only the supplied APEX technical data, news headlines and economic calendar. "
        "Never invent live prices, news, event results, forecast ranges, sources, probabilities or institutional positioning. "
        "Identify which items are reported facts and which are heuristic interpretations. "
        "Explain likely USD, yield, risk-appetite and gold transmission channels only where relevant. "
        "Compare Swing, Intraday and Scalp technical states without changing them. "
        "Do not create or override BUY/SELL signals, entry prices, stop losses or targets. "
        "The deterministic APEX trade engine alone controls setup readiness. "
        "If news or calendar status is unavailable, say so and do not assume there are no events. "
        "Keep the answer concise and useful for a trader. Use these headings: "
        "MARKET OVERVIEW; WHAT IS MOVING THE MARKET; ECONOMIC PLAYBOOK; "
        "SCENARIOS; APEX TECHNICAL ALIGNMENT; WHAT WOULD INVALIDATE THE VIEW. "
        "When evidence conflicts or is insufficient, explicitly conclude MIXED / WAIT."
    )

    try:
        response = SESSION.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "instructions": instructions,
                "input": json.dumps(payload, ensure_ascii=False),
                "max_output_tokens": 900,
            },
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        text = str(body.get("output_text") or "").strip()
        if not text:
            chunks = []
            for output in body.get("output", []):
                for content in output.get("content", []):
                    if content.get("type") in ("output_text", "text") and content.get("text"):
                        chunks.append(content["text"])
            text = "\n".join(chunks).strip()
        if text:
            cache_set(cache_key, text)
            return text
        return None
    except Exception:
        return None


def rule_based_brief(symbol: str, technical: dict[str, Any], news: dict[str, Any], calendar: dict[str, Any]) -> str:
    structures = technical.get("market", {}).get("htf_structure", {})
    news_items = news.get("items", [])
    events = calendar.get("events", [])
    lines = [f"MARKET READ: {symbol} is being assessed with technical structure and current headline/calendar context."]
    lines.append("TECHNICAL: " + ", ".join(f"{tf} {state}" for tf, state in structures.items()) + ".")
    if news_items:
        lines.append(f"NEWS: {len(news_items)} recent headline(s) returned. Headlines are a context feed; keyword read-through is not proof of causation.")
        lines.append("TOP HEADLINE: " + news_items[0]["title"])
    else:
        lines.append("NEWS: Live news feed unavailable or no matching headlines returned; no news bias is assumed.")
    relevant = [e for e in events if e.get("impact") == "HIGH"][:4]
    if relevant:
        lines.append("EVENT RISK: High-impact calendar event(s): " + "; ".join(f"{e['currency']} {e['title']} ({e['date']} {e['time']})" for e in relevant) + ".")
    else:
        lines.append("EVENT RISK: No high-impact events were returned by the calendar feed, or the feed is unavailable.")
    lines.append("ACTION: Use the selected APEX setup state. No trade should be triggered by news interpretation alone; wait for the technical confirmation and retest rules.")
    return "\n".join(lines)


@app.get("/", include_in_schema=False)
def dashboard():
    html_file = ROOT / "index.html"
    if not html_file.exists():
        raise HTTPException(404, "index.html is missing from the repository root.")
    return FileResponse(html_file)


@app.get("/api/health")
def health():
    return {
        "project": "APEX Market Intelligence",
        "version": APP_VERSION,
        "status": "online",
        "market_data_provider": "Twelve Data",
        "market_data_key_configured": bool(TWELVE_API_KEY),
        "ai_mode": "OPENAI" if OPENAI_API_KEY else "RULE_BASED_FALLBACK",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/market/symbols")
def market_symbols():
    return {"symbols": SYMBOLS}


@app.get("/api/market/timeframes")
def market_timeframes():
    return {"timeframes": TIMEFRAMES}


@app.get("/api/market/price")
def market_price(symbol: str = Query("XAU/USD")):
    if symbol not in SYMBOLS:
        raise HTTPException(400, "Unsupported symbol.")
    data = twelve_request("/price", {"symbol": symbol}, ttl=15)
    try:
        return {"symbol": symbol, "price": float(data["price"]), "source": "Twelve Data", "timestamp": datetime.now(timezone.utc).isoformat()}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(502, "Twelve Data price is unavailable for this symbol.")


@app.get("/api/market/candles")
def market_candles(symbol: str = Query("XAU/USD"), interval: str = Query("5min"), outputsize: int = Query(250, ge=20, le=500)):
    if symbol not in SYMBOLS:
        raise HTTPException(400, "Unsupported symbol.")
    return {"symbol": symbol, "interval": interval, "source": "Twelve Data", "candles": get_candles(symbol, interval, outputsize)}


@app.get("/api/analysis")
def market_analysis(symbol: str = Query("XAU/USD"), interval: str = Query("5min")):
    if symbol not in SYMBOLS:
        raise HTTPException(400, "Unsupported symbol.")
    if interval not in TIMEFRAMES:
        raise HTTPException(400, "Unsupported timeframe.")
    return build_technical_analysis(symbol, interval)


@app.get("/api/news")
def market_news(symbol: str = Query("XAU/USD")):
    if symbol not in SYMBOLS:
        raise HTTPException(400, "Unsupported symbol.")
    return fetch_news(symbol)


@app.get("/api/calendar")
def economic_calendar():
    return fetch_calendar()


@app.get("/api/intelligence/brief")
def intelligence_brief(symbol: str = Query("XAU/USD")):
    if symbol not in SYMBOLS:
        raise HTTPException(400, "Unsupported symbol.")
    technical = build_technical_analysis(symbol, "15min")
    news = fetch_news(symbol)
    calendar = fetch_calendar()
    ai_text = openai_interpretation(symbol, technical, news, calendar)
    return {
        "symbol": symbol,
        "mode": "OPENAI" if ai_text else "RULE_BASED",
        "model": OPENAI_MODEL if ai_text else None,
        "brief": ai_text or rule_based_brief(symbol, technical, news, calendar),
        "news_status": news.get("status"),
        "calendar_status": calendar.get("status"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": "AI interpretation is contextual research, not a trade signal or financial advice.",
    }
