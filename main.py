
import os
import requests

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse


# =========================================================
# APEX CONFIGURATION
# =========================================================

BASE_URL = "https://api.twelvedata.com"
API_KEY = os.getenv("TWELVE_DATA_API_KEY")

ROOT_DIR = Path(__file__).resolve().parent
INDEX_FILE = ROOT_DIR / "index.html"

app = FastAPI(
    title="APEX Market Intelligence",
    description="APEX Live Market Data and Intelligence API",
    version="1.3.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# FRONTEND
# =========================================================

@app.get("/", include_in_schema=False)
def dashboard():

    if not INDEX_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="APEX dashboard index.html not found."
        )

    return FileResponse(INDEX_FILE)


# =========================================================
# HEALTH
# =========================================================

@app.get("/api/health")
def health():

    return {
        "status": "healthy",
        "project": "APEX Market Intelligence",
        "version": "1.3.0",
        "api_key_configured": bool(API_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# ENGINES
# =========================================================

@app.get("/api/engines")
def engines():

    return {
        "engines": [
            "HTF Key Levels",
            "Supply & Demand",
            "Intraday Supply & Demand",
            "Daily & 4H POI",
            "1H & 15M POI",
            "Market Structure",
            "Market Sentiment",
            "1M Price Action",
            "News & Economic Analysis",
            "Final Decision Engine"
        ]
    }


# =========================================================
# TWELVE DATA REQUEST
# =========================================================

def twelve_data_request(endpoint, params):

    if not API_KEY:

        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key is missing."
        )

    request_params = params.copy()
    request_params["apikey"] = API_KEY

    try:

        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=request_params,
            timeout=20
        )

        data = response.json()

    except requests.Timeout:

        raise HTTPException(
            status_code=504,
            detail="Market data request timed out."
        )

    except requests.RequestException:

        raise HTTPException(
            status_code=502,
            detail="Market data connection failed."
        )

    except ValueError:

        raise HTTPException(
            status_code=502,
            detail="Invalid response from market data provider."
        )

    if response.status_code >= 400 or data.get("status") == "error":

        raise HTTPException(
            status_code=502,
            detail={
                "provider": "Twelve Data",
                "code": data.get("code"),
                "message": data.get("message", "Provider error")
            }
        )

    return data


# =========================================================
# PRICE
# =========================================================

@app.get("/api/market/price")
def market_price(
    symbol: str = Query("XAU/USD")
):

    data = twelve_data_request(
        "price",
        {"symbol": symbol}
    )

    try:
        price = float(data["price"])

    except (KeyError, TypeError, ValueError):

        raise HTTPException(
            status_code=502,
            detail="Invalid price response."
        )

    return {
        "symbol": symbol,
        "price": price,
        "source": "Twelve Data",
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
        "1min", "5min", "15min", "30min", "45min",
        "1h", "2h", "4h", "1day", "1week", "1month"
    ]

    if interval not in allowed:

        raise HTTPException(
            status_code=400,
            detail={"allowed_intervals": allowed}
        )

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

        raise HTTPException(
            status_code=502,
            detail="Candle formatting failed."
        )

    if not candles:

        raise HTTPException(
            status_code=502,
            detail="No market candles returned."
        )

    return {
        "symbol": symbol,
        "interval": interval,
        "count": len(candles),
        "source": "Twelve Data",
        "candles": candles,
        "timestamp": datetime.now(timezone.utc).isoformat()
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
            "1min", "5min", "15min", "30min",
            "1h", "4h", "1day"
        ]
    }

# =========================================================
# APEX MARKET ANALYSIS ENGINE
# =========================================================

def calculate_ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return round(ema, 5)


def calculate_rsi(values, period=14):
    if len(values) <= period:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def find_swings(candles, strength=3):

    highs = []
    lows = []

    for i in range(strength, len(candles) - strength):

        current_high = candles[i]["high"]
        current_low = candles[i]["low"]

        left = candles[i - strength:i]
        right = candles[i + 1:i + strength + 1]

        if all(current_high > c["high"] for c in left + right):
            highs.append({
                "index": i,
                "price": current_high
            })

        if all(current_low < c["low"] for c in left + right):
            lows.append({
                "index": i,
                "price": current_low
            })

    return highs, lows


def detect_structure(highs, lows):

    structure = "UNDEFINED"

    if len(highs) >= 2 and len(lows) >= 2:

        higher_high = highs[-1]["price"] > highs[-2]["price"]
        higher_low = lows[-1]["price"] > lows[-2]["price"]

        lower_high = highs[-1]["price"] < highs[-2]["price"]
        lower_low = lows[-1]["price"] < lows[-2]["price"]

        if higher_high and higher_low:
            structure = "BULLISH (HH + HL)"

        elif lower_high and lower_low:
            structure = "BEARISH (LH + LL)"

        elif higher_high and lower_low:
            structure = "EXPANDING"

        else:
            structure = "RANGING / TRANSITION"

    return structure


def analyze_market_data(symbol, interval):

    data = twelve_data_request(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 200,
            "timezone": "UTC"
        }
    )

    raw_values = data.get("values", [])

    if len(raw_values) < 50:
        raise HTTPException(
            status_code=502,
            detail="Not enough candles for analysis."
        )

    candles = []

    for c in reversed(raw_values):

        candles.append({
            "time": c["datetime"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"])
        })

    closes = [c["close"] for c in candles]
    highs_list = [c["high"] for c in candles]
    lows_list = [c["low"] for c in candles]

    current_price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)

    rsi = calculate_rsi(closes)

    swing_highs, swing_lows = find_swings(candles, 3)

    structure = detect_structure(swing_highs, swing_lows)

    support = (
        round(swing_lows[-1]["price"], 5)
        if swing_lows else round(min(lows_list[-50:]), 5)
    )

    resistance = (
        round(swing_highs[-1]["price"], 5)
        if swing_highs else round(max(highs_list[-50:]), 5)
    )

    # Trend determination
    if ema20 and ema50 and current_price > ema20 > ema50:
        trend = "BULLISH"

    elif ema20 and ema50 and current_price < ema20 < ema50:
        trend = "BEARISH"

    else:
        trend = "SIDEWAYS / MIXED"

    # Market bias
    bullish_points = 0
    bearish_points = 0

    if trend == "BULLISH":
        bullish_points += 2

    elif trend == "BEARISH":
        bearish_points += 2

    if structure.startswith("BULLISH"):
        bullish_points += 2

    elif structure.startswith("BEARISH"):
        bearish_points += 2

    if ema200:

        if current_price > ema200:
            bullish_points += 1

        elif current_price < ema200:
            bearish_points += 1

    if rsi is not None:

        if 50 < rsi < 70:
            bullish_points += 1

        elif 30 < rsi < 50:
            bearish_points += 1

    if bullish_points > bearish_points:
        bias = "BUY"

    elif bearish_points > bullish_points:
        bias = "SELL"

    else:
        bias = "NEUTRAL"

    total_points = bullish_points + bearish_points

    confidence = (
        round(max(bullish_points, bearish_points) / total_points * 100)
        if total_points else 0
    )

    return {
        "symbol": symbol,
        "timeframe": interval,
        "price": round(current_price, 5),
        "trend": trend,
        "market_structure": structure,
        "bias": bias,
        "confidence": confidence,
        "indicators": {
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "rsi14": rsi
        },
        "levels": {
            "support": support,
            "resistance": resistance
        },
        "swings": {
            "last_swing_high": swing_highs[-1]["price"] if swing_highs else None,
            "last_swing_low": swing_lows[-1]["price"] if swing_lows else None,
            "swing_high_count": len(swing_highs),
            "swing_low_count": len(swing_lows)
        },
        "candles_analyzed": len(candles),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# APEX ANALYSIS API
# =========================================================

@app.get("/api/analysis")
def apex_analysis(
    symbol: str = Query("XAU/USD"),
    interval: str = Query("5min")
):

    allowed_intervals = [
        "1min", "5min", "15min", "30min",
        "1h", "2h", "4h", "1day"
    ]

    if interval not in allowed_intervals:

        raise HTTPException(
            status_code=400,
            detail="Unsupported timeframe."
        )

    return analyze_market_data(symbol, interval)
