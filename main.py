
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="APEX Market Intelligence",
    description="Multi-engine AI market analysis API",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {
        "project": "APEX Market Intelligence",
        "status": "online",
        "version": "1.0.0"
    }

@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "service": "APEX Backend"
    }

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
