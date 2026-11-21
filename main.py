
import os
import requests

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="APEX Market Intelligence",
    description="Live Market Data API",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
BASE_URL = "https://api.twelvedata.com"


# ─────────────────────────────────────────────
# HOME
# ─────────────────────────────────────────────

@app.get("/")
def home():
    return {
        "project": "APEX Market Intelligence",
        "status": "online",
        "version": "1.1.0"
    }


# ─────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "service": "APEX Backend"
    }


# ─────────────────────────────────────────────
# ENGINE LIST
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# API CONNECTION
# ─────────────────────────────────────────────

def twelve_data_request(endpoint, params):

    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key is not configured."
        )

    params["apikey"] = API_KEY

    try:
        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=params,
            timeout=15
        )

        data = response.json()

    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Market data provider connection failed."
        )

    if response.status_code != 200 or "status" in data:
        raise HTTPException(
            status_code=502,
            detail=data.get("message", "Market data provider error.")
        )

    return data


# ─────────────────────────────────────────────
# LIVE PRICE
# ─────────────────────────────────────────────

@app.get("/api/market/price")
def get_price(
    symbol: str = Query("XAU/USD")
):

    data = twelve_data_request(
        "price",
        {"symbol": symbol}
    )

    return {
        "symbol": symbol,
        "price": float(data["price"]),
        "source": "Twelve Data"
    }


# ─────────────────────────────────────────────
# MARKET CANDLES
# ─────────────────────────────────────────────

@app.get("/api/market/candles")
def get_candles(
    symbol: str = Query("XAU/USD"),
    interval: str = Query("5min"),
    outputsize: int = Query(100, ge=1, le=5000)
):

    allowed_intervals = [
        "1min", "5min", "15min", "30min",
        "1h", "4h", "1day", "1week", "1month"
    ]

    if interval not in allowed_intervals:
        raise HTTPException(
            status_code=400,
            detail="Invalid timeframe."
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

    candles = [
        {
            "time": candle["datetime"],
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0) or 0)
        }
        for candle in reversed(values)
    ]

    return {
        "symbol": symbol,
        "interval": interval,
        "source": "Twelve Data",
        "count": len(candles),
        "candles": candles
    }
