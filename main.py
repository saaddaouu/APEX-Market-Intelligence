
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
