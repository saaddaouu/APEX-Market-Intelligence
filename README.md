# APEX Market Intelligence V5

A single-service FastAPI + HTML dashboard combining APEX technical analysis with live market context.

## Repository root — exact file names

Keep these files together at the repository root (not inside another folder):

- `main.py`
- `index.html`
- `requirements.txt`
- `Procfile`

Do not rename them to `main (2).py`, `index (2).html`, `procfile.txt`, or similar.

## Render settings

**Start command** (if Render does not use the Procfile automatically):

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

### Required environment variable

- `TWELVE_DATA_API_KEY` — keep your existing Twelve Data key in Render → Environment. Never put it in GitHub or in the browser code.

### Optional environment variables

- `OPENAI_API_KEY` — enables the APEX AI Market Playbook. Create a separate API key in the OpenAI Platform and add it directly to Render → Environment; do not paste it into chat or commit it to GitHub.
- `OPENAI_MODEL` — optional. Default: `gpt-6-luna` (cost-conscious market brief model). You can change it to another model available to your API project.

Without `OPENAI_API_KEY`, APEX still works with its rule-based market context, headlines, calendar, chart, and technical analysis. The interface labels this as `RULE-BASED`; it does not pretend that an LLM is running.

## Data sources and roles

- **Twelve Data:** quotes and OHLC candles used by APEX technical calculations.
- **GDELT DOC 2.0:** recent article headlines and source links. Headline impact/tone labels are heuristic classifications, not proof that a headline caused a price move.
- **Forex Factory calendar feed:** event time, currency, impact, actual, forecast, and previous values when the public feed is available. A missing feed is marked unavailable; APEX does not invent events or values.
- **OpenAI Responses API (optional):** explains the supplied APEX price/structure/news/calendar context. It cannot override the deterministic APEX entry rules or manufacture trade prices.

## APEX technical logic

- Previous completed 1M, rolling 3M, and rolling 1Y candle body levels use Open/Close only (not wick highs/lows).
- Daily + 4H zones are used for Swing and Intraday profiles.
- 1H + 15M zones are used for Scalp profiles.
- Supply/Demand zones are generated from base candles followed by ATR-sized displacement; Double Top/Bottom patterns are added as separate pattern zones.
- POI is a separate displacement-origin zone layer.
- Bullish structure requires both HH and HL; bearish structure requires both LH and LL; otherwise it is Range.
- Swing/Intraday setups require aligned Daily + 4H structure, 1H and 15M engulf confirmation, a completed 15M close beyond the zone, then a 5M retest/rejection.
- Scalp setups use aligned 1H + 15M structure/zones, a completed 5M engulf/close, then a 1M retest/rejection.
- A trade plan stays blank and bias remains WAIT until all required technical, key-level, POI, sentiment, and event checks pass.

## API routes

- `/api/health`
- `/api/market/symbols`
- `/api/market/timeframes`
- `/api/market/price?symbol=XAU/USD`
- `/api/market/candles?symbol=XAU/USD&interval=5min&outputsize=250`
- `/api/analysis?symbol=XAU/USD&interval=5min`
- `/api/market/watch`
- `/api/news?symbol=XAU/USD`
- `/api/calendar`
- `/api/intelligence/brief?symbol=XAU/USD`

## Important limitations

This is an automated technical-research tool, not a broker execution system. Zone detection, double-pattern detection, news tone, and trade setups are algorithmic estimates and require chart review and backtesting before live use. Provider symbol coverage, plan limits, delays, and feed availability can affect the dashboard. When data is missing, the app reports it instead of fabricating it.
