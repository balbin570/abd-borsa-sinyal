import os
import asyncio
import statistics
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import httpx

app = FastAPI(title="ABD Borsa Sinyal", version="2.0")
SPY_CACHE = None
SPY_CACHE_DATE = None
STOCK_CACHE = {}

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

def calculate_backtest_score_momentum_test(closes, volumes):
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
    if 5 <= momentum20 <= 15:
       score += 15
    elif 0 < momentum20 < 5:
       score += 8
    elif 15 < momentum20 <= 20:
       score += 5

    return score

def calculate_backtest_score_v2(closes, volumes):
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi14 = calculate_rsi(closes, 14)

    macd, macd_signal, macd_histogram = calculate_macd(closes)

    price_20_days_ago = closes[-21]
    momentum20 = (
        (price - price_20_days_ago)
        / price_20_days_ago
    ) * 100

    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]
    avg_volume20 = sum(previous_20_volumes) / 20

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0

    # Trend - maksimum 40 puan
    if price > ema20:
        score += 15

    if ema20 > ema50:
        score += 25

    # RSI - maksimum 15 puan
    if 55 <= rsi14 <= 65:
        score += 15
    elif 50 <= rsi14 < 55:
        score += 8
    elif 65 < rsi14 <= 70:
        score += 8

    # Hacim - maksimum 10 puan
    if volume_ratio >= 1.50:
        score += 15
    elif volume_ratio >= 1.20:
        score += 8

    # MACD - maksimum 20 puan
    if macd > macd_signal and macd_histogram > 0:
        score += 20

    # Momentum - maksimum 15 puan
    if 5 <= momentum20 <= 15:
        score += 15
    elif 0 < momentum20 < 5:
        score += 8
    elif 15 < momentum20 <= 20:
        score += 5

    return score

def calculate_backtest_score_volume_test(closes, volumes):
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi14 = calculate_rsi(closes, 14)

    macd, macd_signal, macd_histogram = calculate_macd(closes)

    price_20_days_ago = closes[-21]

    momentum20 = (
        (price - price_20_days_ago)
        / price_20_days_ago
    ) * 100

    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]
    avg_volume20 = sum(previous_20_volumes) / 20

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0

    if price > ema20:
        score += 15

    if ema20 > ema50:
        score += 20

    if 55 <= rsi14 <= 70:
        score += 15
    elif 50 <= rsi14 < 55:
        score += 8
    elif 70 < rsi14 <= 75:
        score += 8

    # Sadece hacim kuralı değiştirildi
    if volume_ratio >= 1.50:
        score += 15
    elif volume_ratio >= 1.20:
        score += 8

    if macd > macd_signal and macd_histogram > 0:
        score += 20
    elif macd > macd_signal:
        score += 10

    if momentum20 >= 5:
        score += 15
    elif momentum20 > 0:
        score += 8

    return score

def calculate_backtest_score_rsi_test(closes, volumes):
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi14 = calculate_rsi(closes, 14)

    macd, macd_signal, macd_histogram = calculate_macd(closes)

    price_20_days_ago = closes[-21]

    momentum20 = (
        (price - price_20_days_ago)
        / price_20_days_ago
    ) * 100

    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]
    avg_volume20 = sum(previous_20_volumes) / 20

    if avg_volume20 > 0:
        volume_ratio = current_volume / avg_volume20
    else:
        volume_ratio = 0

    score = 0

    if price > ema20:
        score += 15

    if ema20 > ema50:
        score += 20

    if 55 <= rsi14 <= 65:
        score += 15
    elif 50 <= rsi14 < 55:
        score += 8
    elif 65 < rsi14 <= 70:
        score += 8

    # Sadece hacim kuralı değiştirildi
    if volume_ratio >= 1.20:
        score += 15
    elif volume_ratio >= 1.00:
        score += 8

    if macd > macd_signal and macd_histogram > 0:
        score += 20
    elif macd > macd_signal:
        score += 10

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

async def get_spy_data():
    global SPY_CACHE

    if SPY_CACHE is not None:
        return SPY_CACHE

    url = "https://api.twelvedata.com/time_series"

    spy_params = {
        "symbol": "SPY",
        "interval": "1day",
        "outputsize": 1000,
        "apikey": TWELVE_DATA_API_KEY
    }

    async with httpx.AsyncClient(timeout=20) as client:
        spy_response = await client.get(
            url,
            params=spy_params
        )
        spy_data = spy_response.json()

    if "values" not in spy_data:
        raise HTTPException(
            status_code=400,
            detail=spy_data
        )

    SPY_CACHE = list(reversed(spy_data["values"]))

    return SPY_CACHE

@app.get("/backtest/{symbol}")
async def backtest_stock(
    symbol: str,
    threshold: int = 75,
    start_date: str = None,
    end_date: str = None
):
    if not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key missing"
        )

    symbol = symbol.upper()

    url = "https://api.twelvedata.com/time_series"

    if symbol in STOCK_CACHE:
        data = STOCK_CACHE[symbol]
    else:
        params = {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": 1000,
            "apikey": TWELVE_DATA_API_KEY
        }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url, params=params)
            data = response.json()

        if "values" not in data:
            raise HTTPException(
                status_code=400,
                detail=data
            )

        STOCK_CACHE[symbol] = data
    
    spy_values = await get_spy_data()
    
    values = list(reversed(data["values"]))

    dates = [item["datetime"] for item in values]
    print(
        symbol,
        "BACKTEST DATA:",
        dates[0],
        "->",
        dates[-1],
        "COUNT:",
        len(dates)
    )
    closes = [float(item["close"]) for item in values]
    opens = [float(item["open"]) for item in values]
    volumes = [float(item["volume"]) for item in values]
    
    spy_by_date = {
        item["datetime"]: {
            "open": float(item["open"]),
            "close": float(item["close"])
        }
        for item in spy_values
   }
        
    signals = []
    all_forward_returns_20d = []
    previous_score = 0
    last_signal_index = -999

    # Out-of-sample test için veriyi iki döneme ayır
    split_index = int(len(closes) * 0.30)

    previous_score = calculate_backtest_score_momentum_test(
        closes[:split_index],
        volumes[:split_index]
    )

    # İlk 60 gün indikatör hesaplamak için,
    # son 20 gün ise ileri performansı ölçmek için ayrılıyor.
    for i in range(split_index, len(closes) - 20):

        signal_date = dates[i]
        exit_date_20d = dates[i + 20]
      
        baseline_entry_price = opens[i + 1]

        baseline_return_20d = (
            (closes[i + 20] - baseline_entry_price)
            / baseline_entry_price
        ) * 100

        if (
            (not start_date or signal_date >= start_date)
            and (not end_date or exit_date_20d <= end_date)
        ):
            all_forward_returns_20d.append(
                baseline_return_20d
            )

        historical_closes = closes[:i + 1]
        historical_volumes = volumes[:i + 1]

        score = calculate_backtest_score_momentum_test(
            historical_closes,
            historical_volumes
        )

        if (
            score >= threshold
            and previous_score < threshold
            and i - last_signal_index >= 20
            and (not start_date or signal_date >= start_date)
            and (not end_date or exit_date_20d <= end_date)
        ):
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

            spy_entry_date = values[i + 1]["datetime"]
            spy_exit_date = values[i + 20]["datetime"]

            if spy_entry_date not in spy_by_date or spy_exit_date not in spy_by_date:
                previous_score = score
                continue
                
            spy_entry_price = spy_by_date[spy_entry_date]["open"]
            spy_exit_price = spy_by_date[spy_exit_date]["close"]

            spy_return_20d = (
                (spy_exit_price - spy_entry_price)
                / spy_entry_price
            ) * 100

            excess_return_20d = (
                return_20d - spy_return_20d
            )

            signals.append({
                "signal_date": values[i]["datetime"],
                "entry_date": entry_date,
                "exit_date": values[i + 20]["datetime"],
                "entry_price": round(entry_price, 2),
                "score": score,
                "return_5d": round(return_5d, 2),
                "return_10d": round(return_10d, 2),
                "return_20d": round(return_20d, 2),
                "spy_return_20d": round(spy_return_20d, 2),
                "excess_return_20d": round(
                    excess_return_20d, 2
                )
            })

            last_signal_index = i

        previous_score = score
                 
    if not signals:
        avg_baseline_20d = (
            sum(all_forward_returns_20d) / len(all_forward_returns_20d)
            if all_forward_returns_20d
            else 0
        )

        return {
            "symbol": symbol,
            "threshold": threshold,
            "signal_count": 0,
            "average_return_5d": 0,
            "average_return_10d": 0,
            "average_return_20d": 0,
            "baseline_average_return_20d": round(avg_baseline_20d, 2),
            "signal_advantage_20d": 0,
            "win_rate_20d": 0,
            "average_spy_return_20d": 0,
            "average_excess_return_20d": 0,
            "beat_spy_rate_20d": 0,
            "message": f"Backtest döneminde {threshold}+ sinyal bulunamadı"
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

    median_20d = statistics.median(
        item["return_20d"] for item in signals
    )
    
    avg_baseline_20d = sum(
        all_forward_returns_20d
    ) / len(all_forward_returns_20d)

    signal_advantage_20d = (
        avg_20d - avg_baseline_20d
    )
    
    avg_spy_20d = sum(
        item["spy_return_20d"] for item in signals
    ) / len(signals)

    avg_excess_20d = sum(
        item["excess_return_20d"] for item in signals
    ) / len(signals)

    beats_spy_20d = sum(
        1 for item in signals
        if item["excess_return_20d"] > 0
    )

    beat_spy_rate_20d = (
       beats_spy_20d / len(signals)
    ) * 100

    wins_20d = sum(
        1 for item in signals
        if item["return_20d"] > 0
    )

    win_rate_20d = (
        wins_20d / len(signals)
    ) * 100

    return {
        "symbol": symbol,
        "threshold": threshold,
        "signal_count": len(signals),
        "average_return_5d": round(avg_5d, 2),
        "average_return_10d": round(avg_10d, 2),
        "average_return_20d": round(avg_20d, 2),
        "median_return_20d": round(median_20d, 2),
        "baseline_average_return_20d": round(avg_baseline_20d, 2),
        "signal_advantage_20d": round(signal_advantage_20d, 2),
        "win_rate_20d": round(win_rate_20d, 1),
        "average_spy_return_20d": round(avg_spy_20d, 2),
        "average_excess_return_20d": round(avg_excess_20d, 2),
        "beat_spy_rate_20d": round(beat_spy_rate_20d, 1),
        "signals": signals
    }

@app.get("/backtest-all")
async def backtest_all(
    threshold: int = 75,
    start_date: str = None,
    end_date: str = None
):
    results = []
    all_signal_returns_20d = []

    await get_spy_data()

    for group_start in range(0, len(BACKTEST_WATCHLIST), 3):

        group = BACKTEST_WATCHLIST[
            group_start:group_start + 3
        ]

        for symbol in group:
            try:
                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                # Tüm hisselerin tüm sinyallerinin
                # 20 günlük getirilerini tek havuzda topluyoruz.
                if "signals" in result:
                    all_signal_returns_20d.extend(
                        item["return_20d"]
                        for item in result["signals"]
                    )

                results.append({
                    "symbol": symbol,
                    "signal_count": result.get(
                        "signal_count", 0
                    ),

                    "average_return_5d": result.get(
                        "average_return_5d", 0
                    ),

                    "average_return_10d": result.get(
                        "average_return_10d", 0
                    ),

                    "average_return_20d": result.get(
                        "average_return_20d", 0
                    ),

                    "median_return_20d": result.get(
                        "median_return_20d", 0
                    ),

                    "baseline_average_return_20d": result.get(
                        "baseline_average_return_20d", 0
                    ),

                    "signal_advantage_20d": result.get(
                        "signal_advantage_20d", 0
                    ),

                    "win_rate_20d": result.get(
                        "win_rate_20d", 0
                    ),

                    "average_spy_return_20d": result.get(
                        "average_spy_return_20d", 0
                    ),

                    "average_excess_return_20d": result.get(
                        "average_excess_return_20d", 0
                    ),

                    "beat_spy_rate_20d": result.get(
                        "beat_spy_rate_20d", 0
                    )
                })

            except Exception as e:
                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })

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

        weighted_signal_advantage_20d = sum(
            item["signal_advantage_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_win_rate = sum(
            item["win_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_spy_20d = sum(
            item["average_spy_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_excess_20d = sum(
            item["average_excess_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_beat_spy_rate = sum(
            item["beat_spy_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

    else:
        weighted_5d = 0
        weighted_10d = 0
        weighted_20d = 0
        weighted_signal_advantage_20d = 0
        weighted_win_rate = 0
        weighted_spy_20d = 0
        weighted_excess_20d = 0
        weighted_beat_spy_rate = 0

    # Gerçek genel medyan:
    # Hisse medyanlarının medyanını almıyoruz.
    # 20 hissedeki bütün sinyal getirilerinin
    # medyanını hesaplıyoruz.
    if all_signal_returns_20d:
        overall_median_20d = statistics.median(
            all_signal_returns_20d
        )
    else:
        overall_median_20d = 0

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

        "overall_median_return_20d": round(
            overall_median_20d, 2
        ),

        "overall_signal_advantage_20d": round(
            weighted_signal_advantage_20d, 2
        ),

        "overall_win_rate_20d": round(
            weighted_win_rate, 1
        ),

        "overall_average_spy_return_20d": round(
            weighted_spy_20d, 2
        ),

        "overall_average_excess_return_20d": round(
            weighted_excess_20d, 2
        ),

        "overall_beat_spy_rate_20d": round(
            weighted_beat_spy_rate, 1
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

@app.get("/backtest-monthly")
async def backtest_monthly(
    year: int,
    threshold: int = 75,
    end_date: str = None
):
    from datetime import date

    start_date = f"{year}-01-01"

    if end_date is None:
        end_date = f"{year}-12-31"

    # Her ay için ham sinyalleri burada toplayacağız.
    monthly_signals = {
        month: []
        for month in range(1, 13)
    }

    errors = []

    # SPY verisini bir kez cache'e al.
    await get_spy_data()

        # Twelve Data Free planındaki dakika limitini
    # aşmamak için hisseleri 7'li gruplar halinde işliyoruz.
    batch_size = 7

    for group_start in range(
        0,
        len(BACKTEST_WATCHLIST),
        batch_size
    ):
        group = BACKTEST_WATCHLIST[
            group_start:group_start + batch_size
        ]

        for symbol in group:
            try:
                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                for signal in result.get(
                    "signals", []
                ):
                    signal_date = signal.get(
                        "signal_date"
                    )

                    if not signal_date:
                        continue

                    try:
                        month = int(
                            signal_date.split("-")[1]
                        )
                    except Exception:
                        continue

                    if 1 <= month <= 12:
                        monthly_signals[
                            month
                        ].append(signal)

            except Exception as e:
                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        # Son gruptan sonra beklemeye gerek yok.
        if (
            group_start + batch_size
            < len(BACKTEST_WATCHLIST)
        ):
            await asyncio.sleep(60) 

    monthly_results = []

    for month in range(1, 13):

        signals = monthly_signals[month]
        signal_count = len(signals)

        if signal_count > 0:

            returns_5d = [
                item["return_5d"]
                for item in signals
            ]

            returns_10d = [
                item["return_10d"]
                for item in signals
            ]

            returns_20d = [
                item["return_20d"]
                for item in signals
            ]

            spy_returns_20d = [
                item["spy_return_20d"]
                for item in signals
            ]

            excess_returns_20d = [
                item["excess_return_20d"]
                for item in signals
            ]

            avg_5d = (
                sum(returns_5d) / signal_count
            )

            avg_10d = (
                sum(returns_10d) / signal_count
            )

            avg_20d = (
                sum(returns_20d) / signal_count
            )

            median_20d = statistics.median(
                returns_20d
            )

            avg_spy_20d = (
                sum(spy_returns_20d)
                / signal_count
            )

            avg_excess_20d = (
                sum(excess_returns_20d)
                / signal_count
            )

            winners = sum(
                1
                for value in returns_20d
                if value > 0
            )

            win_rate = (
                winners / signal_count
            ) * 100

            beat_spy = sum(
                1
                for item in signals
                if item["return_20d"]
                > item["spy_return_20d"]
            )

            beat_spy_rate = (
                beat_spy / signal_count
            ) * 100

        else:
            avg_5d = 0
            avg_10d = 0
            avg_20d = 0
            median_20d = 0
            avg_spy_20d = 0
            avg_excess_20d = 0
            win_rate = 0
            beat_spy_rate = 0

        monthly_results.append({
            "month": f"{year}-{month:02d}",
            "signal_count": signal_count,

            "average_return_5d": round(
                avg_5d, 2
            ),

            "average_return_10d": round(
                avg_10d, 2
            ),

            "average_return_20d": round(
                avg_20d, 2
            ),

            "median_return_20d": round(
                median_20d, 2
            ),

            "win_rate_20d": round(
                win_rate, 1
            ),

            "average_spy_return_20d": round(
                avg_spy_20d, 2
            ),

            "average_excess_return_20d": round(
                avg_excess_20d, 2
            ),

            "beat_spy_rate_20d": round(
                beat_spy_rate, 1
            )
        })

    months_with_signals = [
        item
        for item in monthly_results
        if item["signal_count"] > 0
    ]

    positive_return_months = sum(
        1
        for item in months_with_signals
        if item["average_return_20d"] > 0
    )

    positive_median_months = sum(
        1
        for item in months_with_signals
        if item["median_return_20d"] > 0
    )

    positive_excess_months = sum(
        1
        for item in months_with_signals
        if item["average_excess_return_20d"] > 0
    )

    total_signals = sum(
        item["signal_count"]
        for item in monthly_results
    )

    return {
        "year": year,
        "threshold": threshold,
        "stock_count": len(
            BACKTEST_WATCHLIST
        ),

        "total_signals": total_signals,

        "months_with_signals": len(
            months_with_signals
        ),

        "positive_return_months": (
            positive_return_months
        ),

        "positive_median_months": (
            positive_median_months
        ),

        "positive_excess_return_months": (
            positive_excess_months
        ),

        "error_count": len(errors),

        "errors": errors,

        "months": monthly_results
    }

@app.get("/backtest-spy-regime")
async def backtest_spy_regime(
    year: int,
    threshold: int = 75,
    end_date: str = None
):
    """
    Mevcut backtest sinyallerini SPY EMA50 piyasa rejimine göre
    iki gruba ayırır.

    BULL = SPY kapanış > SPY EMA50
    WEAK = SPY kapanış <= SPY EMA50

    Mevcut skor ve sinyal üretme mantığını değiştirmez.
    """

    start_date = f"{year}-01-01"

    if end_date is None:
        end_date = f"{year}-12-31"

    # SPY verisini mevcut cache fonksiyonundan al
    spy_values = await get_spy_data()

    spy_dates = [
        item["datetime"]
        for item in spy_values
    ]

    spy_closes = [
        float(item["close"])
        for item in spy_values
    ]

    spy_index_by_date = {
        date: index
        for index, date in enumerate(spy_dates)
    }

    bull_signals = []
    weak_signals = []
    unavailable_signals = []

    errors = []

    # Twelve Data Free plan limitini aşmamak için
    # hisseleri gruplar halinde işle.
    batch_size = 7

    for group_start in range(
        0,
        len(BACKTEST_WATCHLIST),
        batch_size
    ):
        group = BACKTEST_WATCHLIST[
            group_start:group_start + batch_size
        ]

        for symbol in group:
            try:
                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                for signal in result.get(
                    "signals", []
                ):
                    signal_date = signal.get(
                        "signal_date"
                    )

                    if not signal_date:
                        continue

                    spy_index = spy_index_by_date.get(
                        signal_date
                    )

                    # EMA50 hesaplamak için en az
                    # 50 SPY kapanışı gerekli.
                    if (
                        spy_index is None
                        or spy_index < 49
                    ):
                        unavailable_signals.append(
                            signal
                        )
                        continue

                    spy_history = spy_closes[
                        :spy_index + 1
                    ]

                    spy_close = spy_history[-1]

                    spy_ema50 = calculate_ema(
                        spy_history,
                        50
                    )

                    enriched_signal = {
                        **signal,
                        "spy_close_at_signal": round(
                            spy_close, 2
                        ),
                        "spy_ema50_at_signal": round(
                            spy_ema50, 2
                        )
                    }

                    if spy_close > spy_ema50:
                        bull_signals.append(
                            enriched_signal
                        )
                    else:
                        weak_signals.append(
                            enriched_signal
                        )

            except Exception as e:
                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        if (
            group_start + batch_size
            < len(BACKTEST_WATCHLIST)
        ):
            await asyncio.sleep(60)

    def summarize_regime(signals):

        signal_count = len(signals)

        if signal_count == 0:
            return {
                "signal_count": 0,
                "average_return_5d": 0,
                "average_return_10d": 0,
                "average_return_20d": 0,
                "median_return_20d": 0,
                "win_rate_20d": 0,
                "average_spy_return_20d": 0,
                "average_excess_return_20d": 0,
                "beat_spy_rate_20d": 0
            }

        returns_5d = [
            item["return_5d"]
            for item in signals
        ]

        returns_10d = [
            item["return_10d"]
            for item in signals
        ]

        returns_20d = [
            item["return_20d"]
            for item in signals
        ]

        spy_returns_20d = [
            item["spy_return_20d"]
            for item in signals
        ]

        excess_returns_20d = [
            item["excess_return_20d"]
            for item in signals
        ]

        win_count = sum(
            1
            for value in returns_20d
            if value > 0
        )

        beat_spy_count = sum(
            1
            for value in excess_returns_20d
            if value > 0
        )

        return {
            "signal_count": signal_count,

            "average_return_5d": round(
                sum(returns_5d) / signal_count,
                2
            ),

            "average_return_10d": round(
                sum(returns_10d) / signal_count,
                2
            ),

            "average_return_20d": round(
                sum(returns_20d) / signal_count,
                2
            ),

            "median_return_20d": round(
                statistics.median(
                    returns_20d
                ),
                2
            ),

            "win_rate_20d": round(
                win_count
                / signal_count
                * 100,
                1
            ),

            "average_spy_return_20d": round(
                sum(spy_returns_20d)
                / signal_count,
                2
            ),

            "average_excess_return_20d": round(
                sum(excess_returns_20d)
                / signal_count,
                2
            ),

            "beat_spy_rate_20d": round(
                beat_spy_count
                / signal_count
                * 100,
                1
            )
        }

    bull_summary = summarize_regime(
        bull_signals
    )

    weak_summary = summarize_regime(
        weak_signals
    )

    classified_signals = (
        len(bull_signals)
        + len(weak_signals)
    )

    return {
        "year": year,
        "threshold": threshold,
        "start_date": start_date,
        "end_date": end_date,

        "stock_count": len(
            BACKTEST_WATCHLIST
        ),

        "total_classified_signals":
            classified_signals,

        "bull_definition":
            "SPY close > SPY EMA50",

        "weak_definition":
            "SPY close <= SPY EMA50",

        "bull_market": bull_summary,

        "weak_market": weak_summary,

        "unavailable_signal_count":
            len(unavailable_signals),

        "error_count": len(errors),

        "errors": errors
    }

@app.get("/backtest-spy-trend")
async def backtest_spy_trend(
    year: int,
    threshold: int = 75,
    end_date: str = None
):
    """
    Mevcut backtest sinyallerini SPY EMA20 / EMA50 trendine göre
    iki gruba ayırır.

    STRONG_TREND = SPY EMA20 > SPY EMA50
    WEAK_TREND   = SPY EMA20 <= SPY EMA50

    Mevcut skor ve sinyal üretme mantığını değiştirmez.
    """

    start_date = f"{year}-01-01"

    if end_date is None:
        end_date = f"{year}-12-31"

    # Mevcut cache üzerinden SPY verisini al
    spy_values = await get_spy_data()

    spy_dates = [
        item["datetime"]
        for item in spy_values
    ]

    spy_closes = [
        float(item["close"])
        for item in spy_values
    ]

    spy_index_by_date = {
        date: index
        for index, date in enumerate(spy_dates)
    }

    strong_trend_signals = []
    weak_trend_signals = []
    unavailable_signals = []

    errors = []

    # Twelve Data Free plan dakika limitini
    # aşmamak için hisseleri gruplar halinde işle.
    batch_size = 7

    for group_start in range(
        0,
        len(BACKTEST_WATCHLIST),
        batch_size
    ):
        group = BACKTEST_WATCHLIST[
            group_start:group_start + batch_size
        ]

        for symbol in group:
            try:
                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                for signal in result.get(
                    "signals", []
                ):
                    signal_date = signal.get(
                        "signal_date"
                    )

                    if not signal_date:
                        continue

                    spy_index = spy_index_by_date.get(
                        signal_date
                    )

                    # EMA50 için en az 50 kapanış gerekir.
                    if (
                        spy_index is None
                        or spy_index < 49
                    ):
                        unavailable_signals.append(
                            signal
                        )
                        continue

                    spy_history = spy_closes[
                        :spy_index + 1
                    ]

                    spy_close = spy_history[-1]

                    spy_ema20 = calculate_ema(
                        spy_history,
                        20
                    )

                    spy_ema50 = calculate_ema(
                        spy_history,
                        50
                    )

                    enriched_signal = {
                        **signal,

                        "spy_close_at_signal": round(
                            spy_close,
                            2
                        ),

                        "spy_ema20_at_signal": round(
                            spy_ema20,
                            2
                        ),

                        "spy_ema50_at_signal": round(
                            spy_ema50,
                            2
                        ),

                        "ema20_vs_ema50_percent": round(
                            (
                                (
                                    spy_ema20
                                    - spy_ema50
                                )
                                / spy_ema50
                            )
                            * 100,
                            2
                        )
                    }

                    if spy_ema20 > spy_ema50:
                        strong_trend_signals.append(
                            enriched_signal
                        )
                    else:
                        weak_trend_signals.append(
                            enriched_signal
                        )

            except Exception as e:
                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        if (
            group_start + batch_size
            < len(BACKTEST_WATCHLIST)
        ):
            await asyncio.sleep(60)

    def summarize_trend(signals):

        signal_count = len(signals)

        if signal_count == 0:
            return {
                "signal_count": 0,
                "average_return_5d": 0,
                "average_return_10d": 0,
                "average_return_20d": 0,
                "median_return_20d": 0,
                "win_rate_20d": 0,
                "average_spy_return_20d": 0,
                "average_excess_return_20d": 0,
                "beat_spy_rate_20d": 0
            }

        returns_5d = [
            item["return_5d"]
            for item in signals
        ]

        returns_10d = [
            item["return_10d"]
            for item in signals
        ]

        returns_20d = [
            item["return_20d"]
            for item in signals
        ]

        spy_returns_20d = [
            item["spy_return_20d"]
            for item in signals
        ]

        excess_returns_20d = [
            item["excess_return_20d"]
            for item in signals
        ]

        win_count = sum(
            1
            for value in returns_20d
            if value > 0
        )

        beat_spy_count = sum(
            1
            for value in excess_returns_20d
            if value > 0
        )

        return {
            "signal_count": signal_count,

            "average_return_5d": round(
                sum(returns_5d)
                / signal_count,
                2
            ),

            "average_return_10d": round(
                sum(returns_10d)
                / signal_count,
                2
            ),

            "average_return_20d": round(
                sum(returns_20d)
                / signal_count,
                2
            ),

            "median_return_20d": round(
                statistics.median(
                    returns_20d
                ),
                2
            ),

            "win_rate_20d": round(
                win_count
                / signal_count
                * 100,
                1
            ),

            "average_spy_return_20d": round(
                sum(spy_returns_20d)
                / signal_count,
                2
            ),

            "average_excess_return_20d": round(
                sum(excess_returns_20d)
                / signal_count,
                2
            ),

            "beat_spy_rate_20d": round(
                beat_spy_count
                / signal_count
                * 100,
                1
            )
        }

    strong_summary = summarize_trend(
        strong_trend_signals
    )

    weak_summary = summarize_trend(
        weak_trend_signals
    )

    classified_signals = (
        len(strong_trend_signals)
        + len(weak_trend_signals)
    )

    return {
        "year": year,
        "threshold": threshold,
        "start_date": start_date,
        "end_date": end_date,

        "stock_count": len(
            BACKTEST_WATCHLIST
        ),

        "total_classified_signals":
            classified_signals,

        "strong_trend_definition":
            "SPY EMA20 > SPY EMA50",

        "weak_trend_definition":
            "SPY EMA20 <= SPY EMA50",

        "strong_trend": strong_summary,

        "weak_trend": weak_summary,

        "unavailable_signal_count":
            len(unavailable_signals),

        "error_count": len(errors),

        "errors": errors
    }

# ============================================================
# UNSEEN VALIDATION BACKTEST
# Mevcut v1.1 skor modeli değiştirilmeden farklı hisselerde test edilir.
# Bu hisseler ana BACKTEST_WATCHLIST içinde yer almaz.
# ============================================================

VALIDATION_WATCHLIST = [
    # Financials
    "JPM",
    "BAC",
    "GS",
    "V",
    "MA",

    # Healthcare
    "LLY",
    "JNJ",
    "ABBV",
    "MRK",
    "UNH",

    # Consumer / Retail
    "WMT",
    "COST",
    "HD",
    "MCD",
    "NKE",

    # Energy / Industrials
    "XOM",
    "CVX",
    "CAT",
    "GE",
    "RTX",

    # Additional diversified companies
    "KO",
    "PEP",
    "DIS",
    "UPS",
    "LOW"
]


@app.get("/backtest-validation")
async def backtest_validation(
    threshold: int = 75,
    start_date: str = None,
    end_date: str = None
):
    """
    Mevcut v1.1 backtest modelini daha önce model geliştirmede
    kullanılmamış farklı hisseler üzerinde test eder.

    Skor algoritması değiştirilmez.
    """

    results = []

    all_signal_returns_20d = []

    errors = []

    # SPY verisini önceden cache'e al.
    await get_spy_data()

    # Twelve Data Free plan limitine karşı
    # hisseleri küçük gruplar halinde işle.
    batch_size = 7

    for group_start in range(
        0,
        len(VALIDATION_WATCHLIST),
        batch_size
    ):

        group = VALIDATION_WATCHLIST[
            group_start:group_start + batch_size
        ]

        for symbol in group:

            try:

                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                signal_count = result.get(
                    "signal_count",
                    0
                )

                if "signals" in result:
                    all_signal_returns_20d.extend(
                        item["return_20d"]
                        for item in result["signals"]
                    )

                results.append({
                    "symbol": symbol,

                    "signal_count":
                        signal_count,

                    "average_return_5d":
                        result.get(
                            "average_return_5d",
                            0
                        ),

                    "average_return_10d":
                        result.get(
                            "average_return_10d",
                            0
                        ),

                    "average_return_20d":
                        result.get(
                            "average_return_20d",
                            0
                        ),

                    "median_return_20d":
                        result.get(
                            "median_return_20d",
                            0
                        ),

                    "baseline_average_return_20d":
                        result.get(
                            "baseline_average_return_20d",
                            0
                        ),

                    "signal_advantage_20d":
                        result.get(
                            "signal_advantage_20d",
                            0
                        ),

                    "win_rate_20d":
                        result.get(
                            "win_rate_20d",
                            0
                        ),

                    "average_spy_return_20d":
                        result.get(
                            "average_spy_return_20d",
                            0
                        ),

                    "average_excess_return_20d":
                        result.get(
                            "average_excess_return_20d",
                            0
                        ),

                    "beat_spy_rate_20d":
                        result.get(
                            "beat_spy_rate_20d",
                            0
                        )
                })

            except Exception as e:

                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        # API dakika limitini koru.
        if (
            group_start + batch_size
            < len(VALIDATION_WATCHLIST)
        ):
            await asyncio.sleep(60)

    successful = [
        item
        for item in results
        if "error" not in item
        and item.get(
            "signal_count",
            0
        ) > 0
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

        weighted_signal_advantage_20d = sum(
            item["signal_advantage_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_win_rate = sum(
            item["win_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_spy_20d = sum(
            item["average_spy_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_excess_20d = sum(
            item["average_excess_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_beat_spy_rate = sum(
            item["beat_spy_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

    else:

        weighted_5d = 0
        weighted_10d = 0
        weighted_20d = 0
        weighted_signal_advantage_20d = 0
        weighted_win_rate = 0
        weighted_spy_20d = 0
        weighted_excess_20d = 0
        weighted_beat_spy_rate = 0

    if all_signal_returns_20d:

        overall_median_20d = statistics.median(
            all_signal_returns_20d
        )

    else:

        overall_median_20d = 0

    results = sorted(
        results,
        key=lambda x: x.get(
            "average_return_20d",
            -999
        ),
        reverse=True
    )

    return {
        "validation_type":
            "unseen_stock_validation",

        "model":
            "v1.1 unchanged",

        "threshold":
            threshold,

        "start_date":
            start_date,

        "end_date":
            end_date,

        "stock_count":
            len(VALIDATION_WATCHLIST),

        "successful_stock_count":
            len([
                item
                for item in results
                if "error" not in item
            ]),

        "error_count":
            len(errors),

        "total_signals":
            total_signals,

        "overall_average_return_5d":
            round(
                weighted_5d,
                2
            ),

        "overall_average_return_10d":
            round(
                weighted_10d,
                2
            ),

        "overall_average_return_20d":
            round(
                weighted_20d,
                2
            ),

        "overall_median_return_20d":
            round(
                overall_median_20d,
                2
            ),

        "overall_signal_advantage_20d":
            round(
                weighted_signal_advantage_20d,
                2
            ),

        "overall_win_rate_20d":
            round(
                weighted_win_rate,
                1
            ),

        "overall_average_spy_return_20d":
            round(
                weighted_spy_20d,
                2
            ),

        "overall_average_excess_return_20d":
            round(
                weighted_excess_20d,
                2
            ),

        "overall_beat_spy_rate_20d":
            round(
                weighted_beat_spy_rate,
                1
            ),

        "errors":
            errors,

        "results":
            results
    }

# ============================================================
# GROWTH / TECHNOLOGY UNSEEN VALIDATION
#
# Amaç:
# v1.1 modelinin yalnızca ilk 20 hisseye mi,
# yoksa growth / technology tipi hisselere mi
# genellenebildiğini test etmek.
#
# Ana BACKTEST_WATCHLIST ve ilk VALIDATION_WATCHLIST
# değiştirilmez.
# ============================================================

GROWTH_VALIDATION_WATCHLIST = [
    # Software / Cloud
    "NOW",
    "ADBE",
    "INTU",
    "SNOW",
    "DDOG",

    # Cybersecurity / Infrastructure
    "FTNT",
    "ZS",
    "NET",
    "OKTA",
    "MDB",

    # Semiconductor / Technology
    "MRVL",
    "ADI",
    "NXPI",
    "MCHP",
    "ON",

    # Internet / Digital platforms
    "SHOP",
    "UBER",
    "ABNB",
    "DASH",
    "RBLX",

    # Growth / Technology services
    "APP",
    "TTD",
    "TEAM",
    "HUBS",
    "WDAY"
]


@app.get("/backtest-growth-validation")
async def backtest_growth_validation(
    threshold: int = 75,
    start_date: str = None,
    end_date: str = None
):
    """
    Mevcut v1.1 modelini daha önce geliştirme amacıyla
    kullanılmamış growth / technology hisselerinde test eder.

    Skor algoritması değiştirilmez.
    """

    results = []
    all_signal_returns_20d = []
    errors = []

    # SPY verisini cache'e al.
    await get_spy_data()

    # Twelve Data Free plan limitini korumak için
    # hisseleri gruplar halinde işle.
    batch_size = 7

    for group_start in range(
        0,
        len(GROWTH_VALIDATION_WATCHLIST),
        batch_size
    ):

        group = GROWTH_VALIDATION_WATCHLIST[
            group_start:group_start + batch_size
        ]

        for symbol in group:

            try:

                result = await backtest_stock(
                    symbol,
                    threshold=threshold,
                    start_date=start_date,
                    end_date=end_date
                )

                signal_count = result.get(
                    "signal_count",
                    0
                )

                if "signals" in result:

                    all_signal_returns_20d.extend(
                        item["return_20d"]
                        for item in result["signals"]
                    )

                results.append({

                    "symbol":
                        symbol,

                    "signal_count":
                        signal_count,

                    "average_return_5d":
                        result.get(
                            "average_return_5d",
                            0
                        ),

                    "average_return_10d":
                        result.get(
                            "average_return_10d",
                            0
                        ),

                    "average_return_20d":
                        result.get(
                            "average_return_20d",
                            0
                        ),

                    "median_return_20d":
                        result.get(
                            "median_return_20d",
                            0
                        ),

                    "baseline_average_return_20d":
                        result.get(
                            "baseline_average_return_20d",
                            0
                        ),

                    "signal_advantage_20d":
                        result.get(
                            "signal_advantage_20d",
                            0
                        ),

                    "win_rate_20d":
                        result.get(
                            "win_rate_20d",
                            0
                        ),

                    "average_spy_return_20d":
                        result.get(
                            "average_spy_return_20d",
                            0
                        ),

                    "average_excess_return_20d":
                        result.get(
                            "average_excess_return_20d",
                            0
                        ),

                    "beat_spy_rate_20d":
                        result.get(
                            "beat_spy_rate_20d",
                            0
                        )
                })

            except Exception as e:

                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

                results.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        if (
            group_start + batch_size
            < len(GROWTH_VALIDATION_WATCHLIST)
        ):
            await asyncio.sleep(60)

    successful = [
        item
        for item in results
        if "error" not in item
        and item.get("signal_count", 0) > 0
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

        weighted_signal_advantage_20d = sum(
            item["signal_advantage_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_win_rate = sum(
            item["win_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_spy_20d = sum(
            item["average_spy_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_excess_20d = sum(
            item["average_excess_return_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

        weighted_beat_spy_rate = sum(
            item["beat_spy_rate_20d"]
            * item["signal_count"]
            for item in successful
        ) / total_signals

    else:

        weighted_5d = 0
        weighted_10d = 0
        weighted_20d = 0
        weighted_signal_advantage_20d = 0
        weighted_win_rate = 0
        weighted_spy_20d = 0
        weighted_excess_20d = 0
        weighted_beat_spy_rate = 0

    if all_signal_returns_20d:

        overall_median_20d = statistics.median(
            all_signal_returns_20d
        )

    else:

        overall_median_20d = 0

    results = sorted(
        results,
        key=lambda x: x.get(
            "average_return_20d",
            -999
        ),
        reverse=True
    )

    return {

        "validation_type":
            "unseen_growth_stock_validation",

        "model":
            "v1.1 unchanged",

        "threshold":
            threshold,

        "start_date":
            start_date,

        "end_date":
            end_date,

        "stock_count":
            len(GROWTH_VALIDATION_WATCHLIST),

        "successful_stock_count":
            len([
                item
                for item in results
                if "error" not in item
            ]),

        "error_count":
            len(errors),

        "total_signals":
            total_signals,

        "overall_average_return_5d":
            round(weighted_5d, 2),

        "overall_average_return_10d":
            round(weighted_10d, 2),

        "overall_average_return_20d":
            round(weighted_20d, 2),

        "overall_median_return_20d":
            round(overall_median_20d, 2),

        "overall_signal_advantage_20d":
            round(
                weighted_signal_advantage_20d,
                2
            ),

        "overall_win_rate_20d":
            round(weighted_win_rate, 1),

        "overall_average_spy_return_20d":
            round(weighted_spy_20d, 2),

        "overall_average_excess_return_20d":
            round(weighted_excess_20d, 2),

        "overall_beat_spy_rate_20d":
            round(weighted_beat_spy_rate, 1),

        "errors":
            errors,

        "results":
            results
    }
