import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

app = FastAPI(title="ABD Borsa Sinyal", version="2.0")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")


class Signal(BaseModel):
    symbol: str
    price: float
    timeframe: str = "?"
    signal: str = "BULLISH"


def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period

    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "ABD Borsa Sinyal"
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/price/{symbol}")
async def get_price(symbol: str):
    if not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key missing"
        )

    symbol = symbol.upper()

    url = "https://api.twelvedata.com/price"

    params = {
        "symbol": symbol,
        "apikey": TWELVE_DATA_API_KEY
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params)
        data = response.json()

    if "price" not in data:
        raise HTTPException(status_code=400, detail=data)

    return {
        "symbol": symbol,
        "price": float(data["price"])
    }


@app.get("/analyze/{symbol}")
async def analyze_stock(symbol: str):
    if not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key missing"
        )

    symbol = symbol.upper()

    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        data = response.json()

    if "values" not in data:
        raise HTTPException(status_code=400, detail=data)

    values = list(reversed(data["values"]))

    closes = [float(item["close"]) for item in values]
    volumes = [float(item["volume"]) for item in values]

    if len(closes) < 51:
        raise HTTPException(
            status_code=400,
            detail="Yeterli fiyat verisi bulunamadı"
        )

    # Son bar henüz tamamlanmamış olabilir.
    # Bu yüzden analizde bir önceki tamamlanmış günü kullanıyoruz.
    closes_completed = closes[:-1]
    volumes_completed = volumes[:-1]

    price = closes_completed[-1]

    ema20 = calculate_ema(closes_completed, 20)
    ema50 = calculate_ema(closes_completed, 50)
    rsi14 = calculate_rsi(closes_completed, 14)

    current_volume = volumes_completed[-1]
    previous_20_volumes = volumes_completed[-21:-1]

    if len(previous_20_volumes) == 20:
        avg_volume20 = sum(previous_20_volumes) / 20
    else:
        avg_volume20 = 0

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0
    reasons = []

    if price > ema20:
        score += 25
        reasons.append("Fiyat EMA20 üzerinde")

    if ema20 > ema50:
        score += 25
        reasons.append("EMA20 EMA50 üzerinde")

    if 55 <= rsi14 <= 75:
        score += 25
        reasons.append("RSI güçlü bölgede")

    if volume_ratio >= 1.20:
        score += 25
        reasons.append("Hacim 20 günlük ortalamanın üzerinde")

    if score >= 75:
        signal = "GUCLU"
    elif score >= 50:
        signal = "ORTA"
    else:
        signal = "ZAYIF"

    return {
        "symbol": symbol,
        "price": round(price, 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "rsi14": round(rsi14, 2),
        "volume_ratio": round(volume_ratio, 2),
        "score": score,
        "signal": signal,
        "reasons": reasons
    }
@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, signal: Signal):
    key = request.query_params.get("key", "")

    if not WEBHOOK_SECRET or key != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise HTTPException(
            status_code=500,
            detail="Telegram settings missing"
        )

    message = (
        "TRADINGVIEW SIGNAL\n\n"
        f"Symbol: {signal.symbol}\n"
        f"Price: ${signal.price:.2f}\n"
        f"Timeframe: {signal.timeframe}\n"
        f"Signal: {signal.signal}"
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            }
        )
        response.raise_for_status()

    return {
        "status": "received",
        "symbol": signal.symbol
    }
WATCHLIST = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL"
]


@app.get("/scan")
async def scan_watchlist():
    results = []

    for symbol in WATCHLIST:
        try:
            analysis = await analyze_stock(symbol)

            results.append({
                "symbol": analysis["symbol"],
                "price": analysis["price"],
                "score": analysis["score"],
                "signal": analysis["signal"]
            })

        except Exception as e:
            results.append({
                "symbol": symbol,
                "error": str(e)
            })

    results = sorted(
        results,
        key=lambda x: x.get("score", 0),
        reverse=True
    )

    return {
        "count": len(results),
        "results": results
    }
