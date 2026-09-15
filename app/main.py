import os
import asyncio
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

def calculate_ema_series(prices, period):
    if len(prices) < period:
        return []

    multiplier = 2 / (period + 1)

    ema = sum(prices[:period]) / period
    ema_values = [ema]

    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
        ema_values.append(ema)

    return ema_values


def calculate_macd(prices):
    ema12_series = calculate_ema_series(prices, 12)
    ema26_series = calculate_ema_series(prices, 26)

    if len(ema12_series) == 0 or len(ema26_series) == 0:
        return 0, 0, 0

    ema12_aligned = ema12_series[-len(ema26_series):]

    macd_series = [
        ema12 - ema26
        for ema12, ema26 in zip(
            ema12_aligned,
            ema26_series
        )
    ]

    if len(macd_series) < 9:
        return 0, 0, 0

    signal_series = calculate_ema_series(macd_series, 9)

    macd = macd_series[-1]
    signal = signal_series[-1]
    histogram = macd - signal

    return macd, signal, histogram
    
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

def calculate_backtest_score(closes, volumes):
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi14 = calculate_rsi(closes, 14)

    macd, macd_signal, macd_histogram = calculate_macd(closes)

    # 20 günlük momentum
    price_20_days_ago = closes[-21]

    momentum20 = (
        (price - price_20_days_ago)
        / price_20_days_ago
    ) * 100

    # Hacim
    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]

    avg_volume20 = sum(previous_20_volumes) / 20

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0

    # Trend - maksimum 35 puan
    if price > ema20:
        score += 15

    if ema20 > ema50:
        score += 20

    # RSI - maksimum 15 puan
    if 55 <= rsi14 <= 70:
        score += 15
    elif 50 <= rsi14 < 55:
        score += 8
    elif 70 < rsi14 <= 75:
        score += 8

    # Hacim - maksimum 15 puan
    if volume_ratio >= 1.20:
        score += 15
    elif volume_ratio >= 1.00:
        score += 8

    # MACD - maksimum 20 puan
    if macd > macd_signal and macd_histogram > 0:
        score += 20
    elif macd > macd_signal:
        score += 10

    # Momentum - maksimum 15 puan
    if momentum20 >= 5:
        score += 15
    elif momentum20 > 0:
        score += 8

    return score

BACKTEST_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "AVGO", "TSLA", "AMD", "NFLX",
    "PLTR", "ORCL", "CRM", "QCOM", "MU",
    "AMAT", "PANW", "CRWD", "LRCX", "KLAC"
]

@app.get("/backtest/{symbol}")
async def backtest_stock(symbol: str, threshold: int = 75):
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
        "outputsize": 300,
        "apikey": TWELVE_DATA_API_KEY
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        data = response.json()

    if "values" not in data:
        raise HTTPException(status_code=400, detail=data)

    values = list(reversed(data["values"]))

    closes = [float(item["close"]) for item in values]
    opens = [float(item["open"]) for item in values]
    volumes = [float(item["volume"]) for item in values]
    
    signals = []
    previous_score = 0

    # İlk 60 gün indikatör hesaplamak için,
    # son 20 gün ise ileri performansı ölçmek için ayrılıyor.
    for i in range(60, len(closes) - 20):

        historical_closes = closes[:i + 1]
        historical_volumes = volumes[:i + 1]

        score = calculate_backtest_score(
            historical_closes,
            historical_volumes
        )

        if score >= threshold and previous_score < threshold:
            entry_price = opens[i + 1]
            entry_date = values[i + 1]["datetime"]

            return_5d = (
                (closes[i + 5] - entry_price) / entry_price
            ) * 100

            return_10d = (
                (closes[i + 10] - entry_price) / entry_price
            ) * 100

            return_20d = (
                (closes[i + 20] - entry_price) / entry_price
            ) * 100

            signals.append({
                "signal_date": values[i]["datetime"],
                "entry_date": entry_date,
                "entry_price": round(entry_price, 2),
                "score": score,
                "return_5d": round(return_5d, 2),
                "return_10d": round(return_10d, 2),
                "return_20d": round(return_20d, 2)
            })

        previous_score = score
        
    if not signals:
        return {
            "symbol": symbol,
            "signal_count": 0,
            "message": "Backtest döneminde 75+ sinyal bulunamadı"
        }

    avg_5d = sum(
        item["return_5d"] for item in signals
    ) / len(signals)

    avg_10d = sum(
        item["return_10d"] for item in signals
    ) / len(signals)

    avg_20d = sum(
        item["return_20d"] for item in signals
    ) / len(signals)

    wins_20d = sum(
        1 for item in signals
        if item["return_20d"] > 0
    )

    win_rate_20d = (
        wins_20d / len(signals)
    ) * 100

    return {
        "symbol": symbol,
        "signal_count": len(signals),
        "average_return_5d": round(avg_5d, 2),
        "average_return_10d": round(avg_10d, 2),
        "average_return_20d": round(avg_20d, 2),
        "win_rate_20d": round(win_rate_20d, 1),
        "signals": signals
    }

@app.get("/backtest-all")
async def backtest_all(threshold: int = 75):
    results = []

    for group_start in range(0, len(BACKTEST_WATCHLIST), 5):

        group = BACKTEST_WATCHLIST[
            group_start:group_start + 5
        ]

        for symbol in group:
            try:
                result = await backtest_stock(
                    symbol,
                    threshold=threshold
                )

                results.append({
                    "symbol": symbol,
                    "signal_count": result.get("signal_count", 0),
                    "average_return_5d": result.get(
                        "average_return_5d", 0
                    ),
                    "average_return_10d": result.get(
                        "average_return_10d", 0
                    ),
                    "average_return_20d": result.get(
                        "average_return_20d", 0
                    ),
                    "win_rate_20d": result.get(
                        "win_rate_20d", 0
                    )
                })

            except Exception as e:
                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        if group_start + 5 < len(BACKTEST_WATCHLIST):
            await asyncio.sleep(60)

    successful = [
        item for item in results
        if "error" not in item
        and item["signal_count"] > 0
    ]

    total_signals = sum(
        item["signal_count"]
        for item in successful
    )

    if total_signals > 0:

        weighted_5d = sum(
            item["average_return_5d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_10d = sum(
            item["average_return_10d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_20d = sum(
            item["average_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_win_rate = sum(
            item["win_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

    else:
        weighted_5d = 0
        weighted_10d = 0
        weighted_20d = 0
        weighted_win_rate = 0

    results = sorted(
        results,
        key=lambda x: x.get(
            "average_return_20d", -999
        ),
        reverse=True
    )

    return {
        "threshold": threshold,
        "stock_count": len(BACKTEST_WATCHLIST),
        "total_signals": total_signals,
        "overall_average_return_5d": round(
            weighted_5d, 2
        ),
        "overall_average_return_10d": round(
            weighted_10d, 2
        ),
        "overall_average_return_20d": round(
            weighted_20d, 2
        ),
        "overall_win_rate_20d": round(
            weighted_win_rate, 1
        ),
        "results": results
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

    # Verileri eski tarihten yeni tarihe sıralıyoruz
    values = list(reversed(data["values"]))

    closes = [float(item["close"]) for item in values]
    volumes = [float(item["volume"]) for item in values]

    if len(closes) < 51:
        raise HTTPException(
            status_code=400,
            detail="Yeterli fiyat verisi bulunamadı"
        )

    # En son tamamlanmış günlük veriyi kullanıyoruz
    price = closes[-1]

    ema12 = calculate_ema(closes, 12)
    ema20 = calculate_ema(closes, 20)
    ema26 = calculate_ema(closes, 26)
    ema50 = calculate_ema(closes, 50)

    rsi14 = calculate_rsi(closes, 14)

    # MACD ana çizgisi
    macd, macd_signal, macd_histogram = calculate_macd(closes)

    # Son 20 günlük fiyat momentumu
    price_20_days_ago = closes[-21]

    momentum20 = (
        (price - price_20_days_ago)
        / price_20_days_ago
    ) * 100

    # Bugünkü hacmi önceki 20 günün ortalamasıyla karşılaştır
    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]

    avg_volume20 = sum(previous_20_volumes) / 20

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0
    reasons = []

    # 1 - Fiyat kısa vadeli trendin üzerinde: 15 puan
    if price > ema20:
        score += 15
        reasons.append("Fiyat EMA20 üzerinde")

    # 2 - Orta vadeli trend pozitif: 20 puan
    if ema20 > ema50:
        score += 20
        reasons.append("EMA20 EMA50 üzerinde")

    # 3 - RSI: maksimum 15 puan
    if 55 <= rsi14 <= 70:
        score += 15
        reasons.append("RSI güçlü bölgede")
    elif 50 <= rsi14 < 55:
        score += 8
        reasons.append("RSI pozitif bölgede")
    elif 70 < rsi14 <= 75:
        score += 8
        reasons.append("RSI güçlü ancak yüksek")

    # 4 - Hacim: maksimum 15 puan
    if volume_ratio >= 1.20:
        score += 15
        reasons.append("Hacim güçlü")
    elif volume_ratio >= 1.00:
        score += 8
        reasons.append("Hacim ortalama üzerinde")

    # 5 - MACD: 20 puan
    if macd > macd_signal and macd_histogram > 0:
        score += 20
        reasons.append("MACD sinyal çizgisinin üzerinde")
    elif macd > macd_signal:
        score += 10
        reasons.append("MACD pozitif kesişim bölgesinde")

    # 6 - 20 günlük momentum: maksimum 15 puan
    if momentum20 >= 5:
        score += 15
        reasons.append("20 günlük momentum güçlü")
    elif momentum20 > 0:
        score += 8
        reasons.append("20 günlük momentum pozitif")

    # Teknik risk değerlendirmesi
    ema20_distance = ((price - ema20) / ema20) * 100

    if rsi14 > 75 or ema20_distance > 12:
        risk = "YUKSEK"
    elif rsi14 > 70 or ema20_distance > 8:
        risk = "ORTA"
    else:
        risk = "DUSUK"

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
        "macd": round(macd, 2),
        "macd_signal": round(macd_signal, 2),
        "macd_histogram": round(macd_histogram, 2),
        "momentum20": round(momentum20, 2),
        "volume_ratio": round(volume_ratio, 2),
        "score": score,
        "signal": signal,
        "risk": risk,
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
WATCHLIST_GROUPS = [
    ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL"],
    ["META", "AVGO", "TSLA", "AMD", "NFLX"],
    ["PLTR", "ORCL", "CRM", "QCOM", "MU"],
    ["AMAT", "PANW", "CRWD", "LRCX", "KLAC"]
]


@app.get("/scan")
async def scan_watchlist():
    results = []

    for group in WATCHLIST_GROUPS:
        for symbol in group:
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

        # API limitine takılmamak için
        # her 5 hisseden sonra bekle
        await asyncio.sleep(60)

    results = sorted(
        results,
        key=lambda x: x.get("score", 0),
        reverse=True
    )

    return {
        "count": len(results),
        "results": results
    }
@app.get("/send-strong")
async def send_strong_signals():
    results = []

    for group in WATCHLIST_GROUPS:
        for symbol in group:
            try:
                analysis = await analyze_stock(symbol)

                if analysis["score"] >= 75:
                    results.append(analysis)

            except Exception:
                continue

        # Twelve Data ücretsiz planda
        # dakikalık limite takılmamak için bekle
        await asyncio.sleep(60)

    if not results:
        return {
            "status": "ok",
            "message": "Guclu sinyal bulunamadi"
        }

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise HTTPException(
            status_code=500,
            detail="Telegram settings missing"
        )

    results = sorted(
        results,
        key=lambda x: x["score"],
        reverse=True
    )

    message = "📊 ABD BORSA TARAMASI\n\n"

    for item in results:
        message += (
    f"🟢 {item['symbol']}\n"
    f"Puan: {item['score']}/100\n"
    f"Fiyat: ${item['price']}\n"
    f"RSI: {item['rsi14']}\n"
    f"MACD: {item['macd']}\n"
    f"MACD Signal: {item['macd_signal']}\n"
    f"MACD Histogram: {item['macd_histogram']}\n"
    f"Momentum 20G: %{item['momentum20']}\n"
    f"Hacim Orani: {item['volume_ratio']}x\n"
    f"Risk: {item['risk']}\n"
    f"Sinyal: {item['signal']}\n\n"
)

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            }
        )

        response.raise_for_status()

    return {
        "status": "sent",
        "count": len(results),
        "symbols": [item["symbol"] for item in results]
    }
