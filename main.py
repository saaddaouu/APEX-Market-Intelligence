
import os
import requests

from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


# =========================================================
# APEX MARKET INTELLIGENCE
# =========================================================

APP_VERSION = "2.0.0"
BASE_URL = "https://api.twelvedata.com"
API_KEY = os.getenv("TWELVE_DATA_API_KEY")
ROOT_DIR = Path(__file__).resolve().parent
INDEX_FILE = ROOT_DIR / "index.html"

app = FastAPI(
    title="APEX Market Intelligence",
    version=APP_VERSION
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# DASHBOARD
# =========================================================

@app.get("/", include_in_schema=False)
def dashboard():

    if not INDEX_FILE.exists():
        raise HTTPException(404, "Dashboard file not found")

    return FileResponse(INDEX_FILE)


# =========================================================
# HEALTH
# =========================================================

@app.get("/api/health")
def health():

    return {
        "status": "online",
        "project": "APEX Market Intelligence",
        "version": APP_VERSION,
        "provider": "Twelve Data",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# ENGINES
# =========================================================

@app.get("/api/engines")
def engines():

    return {
        "engines": [
            "Market Data Engine",
            "Trend Engine",
            "Market Structure Engine",
            "Support & Resistance Engine",
            "Momentum Engine",
            "Decision Engine V2",
            "Risk Management Engine"
        ]
    }


# =========================================================
# TWELVE DATA CONNECTION
# =========================================================

def twelve_data_request(endpoint, params):

    if not API_KEY:
        raise HTTPException(500, "API key not configured")

    params = dict(params)
    params["apikey"] = API_KEY

    try:

        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        data = response.json()

    except requests.RequestException:

        raise HTTPException(502, "Market data connection failed")

    except ValueError:

        raise HTTPException(502, "Invalid provider response")

    if not isinstance(data, dict):
        raise HTTPException(502, "Unexpected provider response")

    if response.status_code >= 400 or data.get("status") == "error":

        raise HTTPException(
            502,
            detail={
                "provider": "Twelve Data",
                "message": data.get("message", "Market data error"),
                "code": data.get("code")
            }
        )

    return data


# =========================================================
# LIVE PRICE
# =========================================================

@app.get("/api/market/price")
def market_price(symbol: str = Query("XAU/USD")):

    data = twelve_data_request(
        "price",
        {"symbol": symbol}
    )

    try:
        price = float(data["price"])

    except (KeyError, TypeError, ValueError):

        raise HTTPException(502, "Invalid price data")

    return {
        "symbol": symbol,
        "price": price,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# CANDLES
# =========================================================

@app.get("/api/market/candles")
def market_candles(
    symbol: str = Query("XAU/USD"),
    interval: str = Query("5min"),
    outputsize: int = Query(100, ge=1, le=5000)
):

    allowed = [
        "1min", "5min", "15min", "30min",
        "45min", "1h", "2h", "4h",
        "1day", "1week", "1month"
    ]

    if interval not in allowed:
        raise HTTPException(400, "Unsupported timeframe")

    data = twelve_data_request(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC"
        }
    )

    values = data.get("values", [])

    candles = []

    try:

        for c in reversed(values):

            candles.append({
                "time": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0)
            })

    except (KeyError, TypeError, ValueError):

        raise HTTPException(502, "Candle formatting error")

    if not candles:
        raise HTTPException(502, "No candles received")

    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(candles),
        "candles": candles
    }


# =========================================================
# SYMBOLS
# =========================================================

@app.get("/api/market/symbols")
def symbols():

    return {
        "symbols": [
            "XAU/USD",
            "EUR/USD",
            "GBP/USD",
            "USD/JPY",
            "BTC/USD",
            "ETH/USD"
        ]
    }


# =========================================================
# TIMEFRAMES
# =========================================================

@app.get("/api/market/timeframes")
def timeframes():

    return {
        "timeframes": [
            "1min", "5min", "15min",
            "30min", "1h", "4h", "1day"
        ]
    }


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for value in values[period:]:
        ema = value * multiplier + ema * (1 - multiplier)

    return round(ema, 6)


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period=14):

    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:

        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss

    return round(100 - (100 / (1 + rs)), 2)


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):

        current = candles[i]
        previous = candles[i - 1]["close"]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous),
            abs(current["low"] - previous)
        )

        ranges.append(tr)

    atr = sum(ranges[:period]) / period

    for value in ranges[period:]:

        atr = (
            atr * (period - 1) + value
        ) / period

    return round(atr, 6)


# =========================================================
# SWING DETECTION
# =========================================================

def find_swings(candles, strength=3):

    highs = []
    lows = []

    for i in range(strength, len(candles) - strength):

        current = candles[i]

        left = candles[i - strength:i]
        right = candles[i + 1:i + strength + 1]

        if all(
            current["high"] > x["high"]
            for x in left + right
        ):

            highs.append({
                "index": i,
                "price": current["high"]
            })

        if all(
            current["low"] < x["low"]
            for x in left + right
        ):

            lows.append({
                "index": i,
                "price": current["low"]
            })

    return highs, lows


# =========================================================
# MARKET STRUCTURE
# =========================================================

def detect_structure(highs, lows):

    if len(highs) < 2 or len(lows) < 2:
        return "UNDEFINED"

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]

    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]

    if hh and hl:
        return "BULLISH (HH + HL)"

    if lh and ll:
        return "BEARISH (LH + LL)"

    if hh and ll:
        return "EXPANDING"

    return "RANGING / TRANSITION"


# =========================================================
# DECISION ENGINE V2
# =========================================================

def decision_engine(
    price,
    ema20,
    ema50,
    ema200,
    rsi,
    atr,
    structure,
    support,
    resistance
):

    bull = 0
    bear = 0
    reasons = []

    # EMA direction
    if ema20 > ema50:

        bull += 2
        reasons.append("EMA20 above EMA50")

    elif ema20 < ema50:

        bear += 2
        reasons.append("EMA20 below EMA50")

    # Major trend
    if price > ema200:

        bull += 1
        reasons.append("Price above EMA200")

    elif price < ema200:

        bear += 1
        reasons.append("Price below EMA200")

    # Structure
    if structure.startswith("BULLISH"):

        bull += 2
        reasons.append("Bullish structure")

    elif structure.startswith("BEARISH"):

        bear += 2
        reasons.append("Bearish structure")

    # Momentum
    if rsi is not None:

        if 50 < rsi < 70:

            bull += 1
            reasons.append("Positive RSI momentum")

        elif 30 < rsi < 50:

            bear += 1
            reasons.append("Negative RSI momentum")

        elif rsi >= 70:

            reasons.append("Overbought")

        elif rsi <= 30:

            reasons.append("Oversold")

    # Location
    near_support = (
        atr is not None
        and price >= support
        and price - support <= atr
    )

    near_resistance = (
        atr is not None
        and price <= resistance
        and resistance - price <= atr
    )

    if near_support:
        reasons.append("Near support")

    if near_resistance:
        reasons.append("Near resistance")

    # Overextension
    overbought = rsi is not None and rsi >= 70
    oversold = rsi is not None and rsi <= 30

    # Decision
    bias = "WAIT"

    if (
        bull >= 5
        and bull > bear
        and not overbought
        and not near_resistance
    ):

        bias = "BUY"

    elif (
        bear >= 5
        and bear > bull
        and not oversold
        and not near_support
    ):

        bias = "SELL"

    total = bull + bear

    confidence = (
        round(max(bull, bear) / total * 100)
        if total else 0
    )

    confidence = min(confidence, 85)

    if bias == "WAIT":
        confidence = min(confidence, 55)

    return {
        "bias": bias,
        "confidence": confidence,
        "bullish_score": bull,
        "bearish_score": bear,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "overextended": overbought or oversold,
        "reasons": reasons
    }


# =========================================================
# ANALYSIS ENGINE
# =========================================================

def analyze_market_data(symbol, interval):

    data = twelve_data_request(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 500,
            "timezone": "UTC"
        }
    )

    values = data.get("values", [])

    if len(values) < 200:

        raise HTTPException(
            502,
            "Insufficient candle history"
        )

    candles = []

    try:

        for c in reversed(values):

            candles.append({
                "time": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            })

    except (ValueError, TypeError, KeyError):

        raise HTTPException(
            502,
            "Invalid candle data"
        )

    closes = [c["close"] for c in candles]

    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)

    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles)

    swing_highs, swing_lows = find_swings(candles)

    structure = detect_structure(
        swing_highs,
        swing_lows
    )

    support = (
        swing_lows[-1]["price"]
        if swing_lows
        else min(c["low"] for c in candles[-50:])
    )

    resistance = (
        swing_highs[-1]["price"]
        if swing_highs
        else max(c["high"] for c in candles[-50:])
    )

    # Trend
    if price > ema20 > ema50:
        trend = "BULLISH"

    elif price < ema20 < ema50:
        trend = "BEARISH"

    else:
        trend = "SIDEWAYS / MIXED"

    # Decision
    result = decision_engine(
        price,
        ema20,
        ema50,
        ema200,
        rsi,
        atr,
        structure,
        support,
        resistance
    )

    bias = result["bias"]

    entry = None
    entry_zone = None
    stop_loss = None

    targets = {
        "TP1": None,
        "TP2": None,
        "TP3": None
    }

    rr = None

    # Trade plan
    if bias in ["BUY", "SELL"] and atr:

        entry = round(price, 5)

        if bias == "BUY":

            stop_loss = round(
                min(support, price - atr * 1.5),
                5
            )

            risk = entry - stop_loss

            if risk > 0:

                targets = {
                    "TP1": round(entry + risk, 5),
                    "TP2": round(entry + risk * 2, 5),
                    "TP3": round(entry + risk * 3, 5)
                }

                rr = "1:1 / 1:2 / 1:3"

            entry_zone = {
                "low": round(price - atr * 0.25, 5),
                "high": round(price + atr * 0.10, 5)
            }

        else:

            stop_loss = round(
                max(resistance, price + atr * 1.5),
                5
            )

            risk = stop_loss - entry

            if risk > 0:

                targets = {
                    "TP1": round(entry - risk, 5),
                    "TP2": round(entry - risk * 2, 5),
                    "TP3": round(entry - risk * 3, 5)
                }

                rr = "1:1 / 1:2 / 1:3"

            entry_zone = {
                "low": round(price - atr * 0.10, 5),
                "high": round(price + atr * 0.25, 5)
            }

    return {
        "engine": "APEX Decision Engine V2",
        "symbol": symbol,
        "timeframe": interval,
        "timestamp": datetime.now(timezone.utc).isoformat(),

        "market": {
            "price": round(price, 5),
            "trend": trend,
            "structure": structure
        },

        "decision": {
            "bias": bias,
            "confidence_score": result["confidence"],
            "bullish_score": result["bullish_score"],
            "bearish_score": result["bearish_score"],
            "reasons": result["reasons"],
            "overextended": result["overextended"]
        },

        "indicators": {
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi14": rsi,
            "atr14": atr
        },

        "levels": {
            "support": round(support, 5),
            "resistance": round(resistance, 5),
            "near_support": result["near_support"],
            "near_resistance": result["near_resistance"]
        },

        "trade_plan": {
            "entry": entry,
            "entry_zone": entry_zone,
            "stop_loss": stop_loss,
            "take_profit": targets,
            "risk_reward": rr
        },

        "swings": {
            "last_swing_high": (
                swing_highs[-1]["price"]
                if swing_highs else None
            ),
            "last_swing_low": (
                swing_lows[-1]["price"]
                if swing_lows else None
            )
        },

        "candles_analyzed": len(candles),

        "disclaimer": (
            "Confidence is indicator confluence, "
            "not win probability. Not financial advice."
        )
    }


# =========================================================
# ANALYSIS API
# =========================================================

@app.get("/api/analysis")
def apex_analysis(
    symbol: str = Query("XAU/USD"),
    interval: str = Query("5min")
):

    allowed = [
        "1min", "5min", "15min", "30min",
        "1h", "2h", "4h", "1day"
    ]

    if interval not in allowed:

        raise HTTPException(
            400,
            "Unsupported timeframe"
        )

    return analyze_market_data(symbol, interval)
