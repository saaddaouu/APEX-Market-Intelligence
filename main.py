
# =========================================================
# APEX MARKET ANALYSIS ENGINE V2
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = price * multiplier + ema * (1 - multiplier)

    return round(ema, 6)


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
            (avg_gain * (period - 1)) + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1)) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss

    return round(100 - (100 / (1 + rs)), 2)


def calculate_atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):

        high = candles[i]["high"]
        low = candles[i]["low"]
        previous_close = candles[i - 1]["close"]

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(true_range)

    atr = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        atr = ((atr * (period - 1)) + tr) / period

    return round(atr, 6)


def find_swings(candles, strength=3):

    swing_highs = []
    swing_lows = []

    for i in range(strength, len(candles) - strength):

        high = candles[i]["high"]
        low = candles[i]["low"]

        left = candles[i - strength:i]
        right = candles[i + 1:i + strength + 1]

        if all(high > c["high"] for c in left + right):

            swing_highs.append({
                "index": i,
                "price": high
            })

        if all(low < c["low"] for c in left + right):

            swing_lows.append({
                "index": i,
                "price": low
            })

    return swing_highs, swing_lows


def market_structure(swing_highs, swing_lows):

    if len(swing_highs) < 2 or len(swing_lows) < 2:

        return "UNDEFINED"

    last_high = swing_highs[-1]["price"]
    previous_high = swing_highs[-2]["price"]

    last_low = swing_lows[-1]["price"]
    previous_low = swing_lows[-2]["price"]

    if last_high > previous_high and last_low > previous_low:
        return "BULLISH (HH + HL)"

    if last_high < previous_high and last_low < previous_low:
        return "BEARISH (LH + LL)"

    if last_high > previous_high and last_low < previous_low:
        return "EXPANDING"

    return "RANGING / TRANSITION"


def calculate_decision_engine(
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

    bullish_score = 0
    bearish_score = 0
    reasons = []

    # -----------------------------------------------------
    # TREND
    # -----------------------------------------------------

    if ema20 > ema50:
        bullish_score += 2
        reasons.append("EMA20 above EMA50")

    elif ema20 < ema50:
        bearish_score += 2
        reasons.append("EMA20 below EMA50")

    # -----------------------------------------------------
    # MAJOR TREND
    # -----------------------------------------------------

    if price > ema200:
        bullish_score += 1
        reasons.append("Price above EMA200")

    elif price < ema200:
        bearish_score += 1
        reasons.append("Price below EMA200")

    # -----------------------------------------------------
    # MARKET STRUCTURE
    # -----------------------------------------------------

    if structure.startswith("BULLISH"):
        bullish_score += 2
        reasons.append("Bullish market structure")

    elif structure.startswith("BEARISH"):
        bearish_score += 2
        reasons.append("Bearish market structure")

    # -----------------------------------------------------
    # RSI MOMENTUM
    # -----------------------------------------------------

    if rsi is not None:

        if 50 <= rsi < 70:
            bullish_score += 1
            reasons.append("Bullish RSI momentum")

        elif 30 < rsi <= 50:
            bearish_score += 1
            reasons.append("Bearish RSI momentum")

        elif rsi >= 70:
            reasons.append("RSI overbought - buy caution")

        elif rsi <= 30:
            reasons.append("RSI oversold - sell caution")

    # -----------------------------------------------------
    # PRICE EXTENSION
    # -----------------------------------------------------

    extended_buy = (
        rsi is not None and rsi >= 70
    )

    extended_sell = (
        rsi is not None and rsi <= 30
    )

    # -----------------------------------------------------
    # SUPPORT / RESISTANCE PROXIMITY
    # -----------------------------------------------------

    near_support = price > support and (
        price - support
    ) <= atr * 1.0

    near_resistance = price < resistance and (
        resistance - price
    ) <= atr * 1.0

    if near_support:
        reasons.append("Price near support")

    if near_resistance:
        reasons.append("Price near resistance")

    # -----------------------------------------------------
    # FINAL DECISION
    # -----------------------------------------------------

    bias = "WAIT"

    if (
        bullish_score >= 5
        and bullish_score > bearish_score
        and not extended_buy
        and not near_resistance
    ):

        bias = "BUY"

    elif (
        bearish_score >= 5
        and bearish_score > bullish_score
        and not extended_sell
        and not near_support
    ):

        bias = "SELL"

    # -----------------------------------------------------
    # CONFIDENCE SCORE
    # -----------------------------------------------------

    total_score = bullish_score + bearish_score

    if total_score > 0:

        raw_confidence = (
            max(bullish_score, bearish_score)
            / total_score
        ) * 100

    else:

        raw_confidence = 0

    # Confidence is a confluence score, not win probability.
    confidence = min(round(raw_confidence), 85)

    if bias == "WAIT":
        confidence = min(confidence, 55)

    return {
        "bias": bias,
        "confidence": confidence,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "near_support": near_support,
        "near_resistance": near_resistance,
        "extended": extended_buy or extended_sell,
        "reasons": reasons
    }


# =========================================================
# ANALYSIS CORE
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

    raw_values = data.get("values", [])

    if len(raw_values) < 200:

        raise HTTPException(
            status_code=502,
            detail="At least 200 candles are required for analysis."
        )

    candles = []

    try:

        for c in reversed(raw_values):

            candles.append({
                "time": c["datetime"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            })

    except (ValueError, TypeError, KeyError):

        raise HTTPException(
            status_code=502,
            detail="Invalid candle data received."
        )

    closes = [c["close"] for c in candles]

    current = candles[-1]
    price = current["close"]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)

    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles)

    swing_highs, swing_lows = find_swings(candles)

    structure = market_structure(
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

    # -----------------------------------------------------
    # TREND
    # -----------------------------------------------------

    if price > ema20 > ema50:
        trend = "BULLISH"

    elif price < ema20 < ema50:
        trend = "BEARISH"

    else:
        trend = "SIDEWAYS / MIXED"

    # -----------------------------------------------------
    # DECISION
    # -----------------------------------------------------

    decision = calculate_decision_engine(
        price=price,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        rsi=rsi,
        atr=atr,
        structure=structure,
        support=support,
        resistance=resistance
    )

    bias = decision["bias"]

    # -----------------------------------------------------
    # TRADE PLAN
    # -----------------------------------------------------

    entry = None
    stop_loss = None
    take_profit = {
        "TP1": None,
        "TP2": None,
        "TP3": None
    }

    risk_reward = None

    if atr and bias in ["BUY", "SELL"]:

        entry = round(price, 5)

        if bias == "BUY":

            stop_loss = round(
                min(support, price - atr * 1.5),
                5
            )

            risk = entry - stop_loss

            if risk > 0:

                take_profit["TP1"] = round(
                    entry + risk,
                    5
                )

                take_profit["TP2"] = round(
                    entry + risk * 2,
                    5
                )

                take_profit["TP3"] = round(
                    entry + risk * 3,
                    5
                )

                risk_reward = {
                    "TP1": 1.0,
                    "TP2": 2.0,
                    "TP3": 3.0
                }

        elif bias == "SELL":

            stop_loss = round(
                max(resistance, price + atr * 1.5),
                5
            )

            risk = stop_loss - entry

            if risk > 0:

                take_profit["TP1"] = round(
                    entry - risk,
                    5
                )

                take_profit["TP2"] = round(
                    entry - risk * 2,
                    5
                )

                take_profit["TP3"] = round(
                    entry - risk * 3,
                    5
                )

                risk_reward = {
                    "TP1": 1.0,
                    "TP2": 2.0,
                    "TP3": 3.0
                }

    # -----------------------------------------------------
    # ENTRY ZONE
    # -----------------------------------------------------

    entry_zone = None

    if bias == "BUY" and atr:

        entry_zone = {
            "low": round(price - atr * 0.25, 5),
            "high": round(price + atr * 0.10, 5)
        }

    elif bias == "SELL" and atr:

        entry_zone = {
            "low": round(price - atr * 0.10, 5),
            "high": round(price + atr * 0.25, 5)
        }

    # -----------------------------------------------------
    # RETURN
    # -----------------------------------------------------

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
            "confidence_score": decision["confidence"],
            "bullish_score": decision["bullish_score"],
            "bearish_score": decision["bearish_score"],
            "reasons": decision["reasons"],
            "is_extended": decision["extended"]
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
            "near_support": decision["near_support"],
            "near_resistance": decision["near_resistance"]
        },

        "trade_plan": {
            "entry": entry,
            "entry_zone": entry_zone,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward
        },

        "swings": {
            "last_swing_high": (
                swing_highs[-1]["price"]
                if swing_highs else None
            ),
            "last_swing_low": (
                swing_lows[-1]["price"]
                if swing_lows else None
            ),
            "swing_high_count": len(swing_highs),
            "swing_low_count": len(swing_lows)
        },

        "candles_analyzed": len(candles),

        "disclaimer": (
            "Confidence score represents indicator confluence, "
            "not probability of winning. This is market analysis, "
            "not financial advice."
        )
    }


# =========================================================
# APEX ANALYSIS ENDPOINT
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
            detail={
                "error": "Unsupported timeframe",
                "allowed": allowed_intervals
            }
        )

    return analyze_market_data(symbol, interval)
