
import os
import requests

from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


# =========================================================
# APEX MARKET INTELLIGENCE
# =========================================================

app = FastAPI(
    title="APEX Market Intelligence",
    description="Live market data and trading intelligence API",
    version="1.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# CONFIGURATION
# =========================================================

API_KEY = os.getenv("TWELVE_DATA_API_KEY")
BASE_URL = "https://api.twelvedata.com"

TIMEOUT = 20

ALLOWED_INTERVALS = [
    "1min",
    "5min",
    "15min",
    "30min",
    "45min",
    "1h",
    "2h",
    "4h",
    "1day",
    "1week",
    "1month"
]


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():

    return {
        "project": "APEX Market Intelligence",
        "status": "online",
        "version": "1.2.0",
        "data_provider": "Twelve Data",
        "server_time": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/api/health")
def health():

    return {
        "status": "healthy",
        "service": "APEX Market Intelligence",
        "api_key_configured": bool(API_KEY),
        "server_time": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# ANALYSIS ENGINES
# =========================================================

@app.get("/api/engines")
def get_engines():

    return {
        "engines": [
            {
                "id": 1,
                "name": "HTF Key Levels",
                "status": "planned"
            },
            {
                "id": 2,
                "name": "Supply & Demand",
                "status": "planned"
            },
            {
                "id": 3,
                "name": "Intraday Supply & Demand",
                "status": "planned"
            },
            {
                "id": 4,
                "name": "Daily & 4H POI",
                "status": "planned"
            },
            {
                "id": 5,
                "name": "1H & 15M POI",
                "status": "planned"
            },
            {
                "id": 6,
                "name": "Market Structure",
                "status": "planned"
            },
            {
                "id": 7,
                "name": "Market Sentiment",
                "status": "planned"
            },
            {
                "id": 8,
                "name": "1M Price Action",
                "status": "planned"
            },
            {
                "id": 9,
                "name": "News & Economic Analysis",
                "status": "planned"
            },
            {
                "id": 10,
                "name": "Final Decision Engine",
                "status": "planned"
            }
        ]
    }


# =========================================================
# TWELVE DATA REQUEST ENGINE
# =========================================================

def twelve_data_request(endpoint: str, params: dict):

    if not API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key is not configured on Render."
        )

    request_params = params.copy()
    request_params["apikey"] = API_KEY

    try:

        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=request_params,
            timeout=TIMEOUT
        )

        try:
            data = response.json()
        except ValueException:
            data = {}

    except requests.Timeout:

        raise HTTPException(
            status_code=504,
            detail="Twelve Data request timed out."
        )

    except requests.RequestException:

        raise HTTPException(
            status_code=502,
            detail="Unable to connect to Twelve Data."
        )

    # Provider error detection
    if response.status_code >= 400 or data.get("status") == "error":

        raise HTTPException(
            status_code=502,
            detail={
                "provider": "Twelve Data",
                "http_status": response.status_code,
                "provider_code": data.get("code"),
                "provider_message": data.get("message", "Unknown provider error"),
                "symbol": params.get("symbol"),
                "endpoint": endpoint
            }
        )

    return data


# =========================================================
# LIVE MARKET PRICE
# =========================================================

@app.get("/api/market/price")
def get_market_price(
    symbol: str = Query("XAU/USD", min_length=1, max_length=30)
):

    data = twelve_data_request(
        "price",
        {
            "symbol": symbol
        }
    )

    if "price" not in data:

        raise HTTPException(
            status_code=502,
            detail="Price was not returned by the provider."
        )

    try:
        price = float(data["price"])
    except (TypeError, ValueError):

        raise HTTPException(
            status_code=502,
            detail="Invalid price returned by provider."
        )

    return {
        "symbol": symbol.upper(),
        "price": price,
        "source": "Twelve Data",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# HISTORICAL CANDLES
# =========================================================

@app.get("/api/market/candles")
def get_market_candles(

    symbol: str = Query("XAU/USD", min_length=1, max_length=30),

    interval: str = Query("5min"),

    outputsize: int = Query(100, ge=1, le=5000)

):

    # Validate timeframe

    if interval not in ALLOWED_INTERVALS:

        raise HTTPException(
            status_code=400,
            detail={
                "error": "Invalid interval",
                "allowed_intervals": ALLOWED_INTERVALS
            }
        )

    # Request candles from Twelve Data

    data = twelve_data_request(
        "time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC"
        }
    )

    values = data.get("values")

    if not values:

        raise HTTPException(
            status_code=502,
            detail={
                "provider": "Twelve Data",
                "message": "No candle data returned.",
                "symbol": symbol,
                "interval": interval,
                "provider_response": data
            }
        )

    candles = []

    try:

        for candle in reversed(values):

            candles.append({
                "time": candle["datetime"],
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle.get("volume") or 0)
            })

    except (KeyError, TypeError, ValueError):

        raise HTTPException(
            status_code=502,
            detail="Invalid candle format received from provider."
        )

    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "source": "Twelve Data",
        "count": len(candles),
        "candles": candles,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# =========================================================
# AVAILABLE SYMBOLS
# =========================================================

@app.get("/api/market/symbols")
def get_symbols():

    return {
        "symbols": [
            {"symbol": "XAU/USD", "name": "Gold"},
            {"symbol": "EUR/USD", "name": "Euro / US Dollar"},
            {"symbol": "GBP/USD", "name": "British Pound / US Dollar"},
            {"symbol": "USD/JPY", "name": "US Dollar / Japanese Yen"},
            {"symbol": "BTC/USD", "name": "Bitcoin"},
            {"symbol": "ETH/USD", "name": "Ethereum"}
        ]
    }


# =========================================================
# AVAILABLE TIMEFRAMES
# =========================================================

@app.get("/api/market/timeframes")
def get_timeframes():

    return {
        "timeframes": ALLOWED_INTERVALS
    }
