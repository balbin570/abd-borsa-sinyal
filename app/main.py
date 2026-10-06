import os
import asyncio
import statistics
from datetime import datetime, timedelta
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import httpx

app = FastAPI(title="ABD Borsa Sinyal", version="2.0")  
SPY_CACHE = None
SPY_CACHE_DATE = None
STOCK_CACHE = {}

# Fixed historical research snapshot.
# Live endpoints still use Twelve Data; this snapshot is only preloaded
# into the historical backtest/research caches.
RESEARCH_SNAPSHOT_PATH = Path("/code/data/research_snapshot.json")
RESEARCH_SNAPSHOT_LOADED = False
RESEARCH_SNAPSHOT_META = {}

def load_research_snapshot():
    global SPY_CACHE, RESEARCH_SNAPSHOT_LOADED, RESEARCH_SNAPSHOT_META

    if RESEARCH_SNAPSHOT_LOADED:
        return True

    if not RESEARCH_SNAPSHOT_PATH.exists():
        return False

    try:
        with RESEARCH_SNAPSHOT_PATH.open("r", encoding="utf-8") as file:
            snapshot = json.load(file)

        symbols = snapshot.get("symbols", {})
        spy_data = symbols.get("SPY")

        if not spy_data or "values" not in spy_data:
            return False

        SPY_CACHE = list(reversed(spy_data["values"]))

        loaded_stocks = 0
        for symbol, data in symbols.items():
            if symbol == "SPY":
                continue
            if isinstance(data, dict) and "values" in data:
                STOCK_CACHE[symbol] = data
                loaded_stocks += 1

        RESEARCH_SNAPSHOT_META = {
            "generated_at_utc": snapshot.get("generated_at_utc"),
            "provider": snapshot.get("provider"),
            "symbol_count": snapshot.get("symbol_count"),
            "research_stock_count": snapshot.get("research_stock_count"),
            "loaded_stock_count": loaded_stocks
        }
        RESEARCH_SNAPSHOT_LOADED = True
        print("Research snapshot loaded:", RESEARCH_SNAPSHOT_META)
        return True

    except Exception as exc:
        print("Research snapshot load failed:", str(exc))
        return False

# Load once when the Render process starts. If unavailable, existing
# Twelve Data fallback behavior remains intact.
load_research_snapshot()

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


HISTORICAL_WEEKLY_STATE = {
    "running": False,
    "requested_date": None,
    "last_result": None,
    "last_error": None,
}


async def _analyze_stock_historical(symbol: str, target_date: str):
    """Run the existing weekly scoring rules using data available through target_date."""
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Twelve Data API key missing")

    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    # Twelve Data end_date acts as an upper boundary; use the following day
    # so the requested market day's completed daily bar is included.
    api_end_date = (target + timedelta(days=1)).isoformat()

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol.upper(),
        "interval": "1day",
        "outputsize": 100,
        "end_date": api_end_date,
        "apikey": TWELVE_DATA_API_KEY,
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    if "values" not in data:
        raise RuntimeError(str(data))

    values = sorted(data["values"], key=lambda x: x["datetime"])
    values = [row for row in values if row["datetime"][:10] <= target_date]
    if len(values) < 51:
        raise RuntimeError("Yeterli fiyat verisi bulunamadi")

    used_date = values[-1]["datetime"][:10]
    if used_date != target_date:
        raise RuntimeError(f"Hedef tarih barı bulunamadı; son bar {used_date}")

    closes = [float(item["close"]) for item in values]
    volumes = [float(item["volume"]) for item in values]
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi14 = calculate_rsi(closes, 14)
    macd, macd_signal, macd_histogram = calculate_macd(closes)

    price_20_days_ago = closes[-21]
    momentum20 = ((price - price_20_days_ago) / price_20_days_ago) * 100

    current_volume = volumes[-1]
    previous_20_volumes = volumes[-21:-1]
    avg_volume20 = sum(previous_20_volumes) / 20
    volume_ratio = current_volume / avg_volume20 if avg_volume20 > 0 else 0

    score = 0
    reasons = []
    if price > ema20:
        score += 15
        reasons.append("Fiyat EMA20 üzerinde")
    if ema20 > ema50:
        score += 20
        reasons.append("EMA20 EMA50 üzerinde")
    if 55 <= rsi14 <= 70:
        score += 15
        reasons.append("RSI güçlü bölgede")
    elif 50 <= rsi14 < 55:
        score += 8
        reasons.append("RSI pozitif bölgede")
    elif 70 < rsi14 <= 75:
        score += 8
        reasons.append("RSI güçlü ancak yüksek")
    if volume_ratio >= 1.20:
        score += 15
        reasons.append("Hacim güçlü")
    elif volume_ratio >= 1.00:
        score += 8
        reasons.append("Hacim ortalama üzerinde")
    if macd > macd_signal and macd_histogram > 0:
        score += 20
        reasons.append("MACD sinyal çizgisinin üzerinde")
    elif macd > macd_signal:
        score += 10
        reasons.append("MACD pozitif kesişim bölgesinde")
    if momentum20 >= 5:
        score += 15
        reasons.append("20 günlük momentum güçlü")
    elif momentum20 > 0:
        score += 8
        reasons.append("20 günlük momentum pozitif")

    ema20_distance = ((price - ema20) / ema20) * 100
    if rsi14 > 75 or ema20_distance > 12:
        risk = "YUKSEK"
    elif rsi14 > 70 or ema20_distance > 8:
        risk = "ORTA"
    else:
        risk = "DUSUK"

    signal = "GUCLU" if score >= 75 else ("ORTA" if score >= 50 else "ZAYIF")
    return {
        "symbol": symbol.upper(),
        "data_date": used_date,
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
        "reasons": reasons,
    }


async def _run_historical_weekly_scan(target_date: str):
    HISTORICAL_WEEKLY_STATE["running"] = True
    HISTORICAL_WEEKLY_STATE["requested_date"] = target_date
    HISTORICAL_WEEKLY_STATE["last_error"] = None
    HISTORICAL_WEEKLY_STATE["last_result"] = None
    try:
        candidates = []
        errors = []
        batch_size = 7
        for group_start in range(0, len(V2_UNIVERSE), batch_size):
            group = V2_UNIVERSE[group_start:group_start + batch_size]
            for symbol in group:
                try:
                    analysis = await _analyze_stock_historical(symbol, target_date)
                    if analysis["score"] >= 75:
                        candidates.append(analysis)
                except Exception as exc:
                    errors.append({"symbol": symbol, "error": str(exc)})
            if group_start + batch_size < len(V2_UNIVERSE):
                await asyncio.sleep(61)

        candidates.sort(
            key=lambda x: (x["score"], x["momentum20"], x["macd_histogram"]),
            reverse=True,
        )
        picks = candidates[:5]
        HISTORICAL_WEEKLY_STATE["last_result"] = {
            "status": "complete",
            "mode": "weekly_historical",
            "requested_date": target_date,
            "universe_count": len(V2_UNIVERSE),
            "strong_candidate_count": len(candidates),
            "top5": picks,
            "error_count": len(errors),
            "errors": errors,
            "note": "Telegram'a gonderilmedi; sadece gecmis tarih arastirma sonucu.",
        }
    except Exception as exc:
        HISTORICAL_WEEKLY_STATE["last_error"] = str(exc)
    finally:
        HISTORICAL_WEEKLY_STATE["running"] = False


@app.get("/weekly-backtest")
async def weekly_backtest(date: str, background_tasks: BackgroundTasks):
    """One-off historical weekly scan. Does not send Telegram messages."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date YYYY-MM-DD olmali")

    if HISTORICAL_WEEKLY_STATE["running"]:
        return {
            "status": "already_running",
            "requested_date": HISTORICAL_WEEKLY_STATE["requested_date"],
        }

    background_tasks.add_task(_run_historical_weekly_scan, date)
    return {
        "status": "started",
        "mode": "weekly_historical",
        "requested_date": date,
        "note": "Tarama arka planda devam ediyor. Sonucu /weekly-backtest/status adresinden kontrol edin.",
    }


@app.get("/weekly-backtest/status")
async def weekly_backtest_status():
    return HISTORICAL_WEEKLY_STATE


WEEKLY_STATE = {
    "running": False,
    "last_result": None,
    "last_error": None,
}


async def _run_weekly_scan():
    WEEKLY_STATE["running"] = True
    WEEKLY_STATE["last_error"] = None
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            raise RuntimeError("Telegram settings missing")

        candidates = []
        errors = []
        batch_size = 7

        for group_start in range(0, len(V2_UNIVERSE), batch_size):
            group = V2_UNIVERSE[group_start:group_start + batch_size]

            for symbol in group:
                try:
                    analysis = await analyze_stock(symbol)
                    if analysis["score"] >= 75:
                        candidates.append(analysis)
                except Exception as exc:
                    errors.append({"symbol": symbol, "error": str(exc)})

            if group_start + batch_size < len(V2_UNIVERSE):
                await asyncio.sleep(61)

        candidates = sorted(
            candidates,
            key=lambda x: (
                x["score"],
                x["momentum20"],
                x["macd_histogram"]
            ),
            reverse=True
        )
        picks = candidates[:5]

        if picks:
            message = "📊 HAFTALIK ABD HISSE LISTESI\n"
            message += "Gelecek hafta icin teknik olarak en guclu adaylar\n\n"
            for rank, item in enumerate(picks, start=1):
                message += (
                    f"{rank}. {item['symbol']}\n"
                    f"Puan: {item['score']}/100\n"
                    f"Fiyat: ${item['price']}\n"
                    f"RSI: {item['rsi14']}\n"
                    f"Momentum 20G: %{item['momentum20']}\n"
                    f"Hacim Orani: {item['volume_ratio']}x\n"
                    f"MACD Histogram: {item['macd_histogram']}\n"
                    f"Risk: {item['risk']}\n\n"
                )
        else:
            message = (
                "📊 HAFTALIK ABD HISSE LISTESI\n\n"
                "Bu hafta 75+ puan alan guclu aday bulunamadi.\n"
                "Yeni pozisyon icin zorunlu hisse secilmedi."
            )

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message}
            )
            response.raise_for_status()

        WEEKLY_STATE["last_result"] = {
            "status": "sent",
            "mode": "weekly",
            "universe_count": len(V2_UNIVERSE),
            "strong_candidate_count": len(candidates),
            "sent_count": len(picks),
            "symbols": [item["symbol"] for item in picks],
            "error_count": len(errors),
            "errors": errors,
        }
    except Exception as exc:
        WEEKLY_STATE["last_error"] = str(exc)
    finally:
        WEEKLY_STATE["running"] = False


@app.get("/send-weekly")
async def send_weekly_signals(background_tasks: BackgroundTasks):
    """Cron'a hemen cevap verir; haftalik taramayi arka planda tamamlar."""
    if WEEKLY_STATE["running"]:
        return {"status": "already_running", "mode": "weekly"}

    background_tasks.add_task(_run_weekly_scan)
    return {
        "status": "started",
        "mode": "weekly",
        "note": "Tarama arka planda devam ediyor; sonuc Telegram'a gonderilecek."
    }


@app.get("/send-weekly/status")
async def send_weekly_status():
    return WEEKLY_STATE


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

# ============================================================
# V2 RESEARCH ENGINE - STAGE 1
# ============================================================
#
# Amaç:
# Mevcut v1.1 puanlarını değiştirmeden, teknik değişkenlerin
# gelecekteki 20 günlük getiri ve SPY'a göre excess return ile
# ilişkisini ölçmek.
#
# İlk geliştirme dönemi: 2023
#
# NOT:
# 2024-2026 sonuçları daha önce görüldüğü için bunlar artık
# tamamen "unseen final test" değildir.
# ============================================================


V2_UNIVERSE = list(dict.fromkeys(
    BACKTEST_WATCHLIST
    + VALIDATION_WATCHLIST
    + GROWTH_VALIDATION_WATCHLIST
))


def v2_pearson(x_values, y_values):

    if len(x_values) < 2:
        return 0

    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)

    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(
            x_values,
            y_values
        )
    )

    x_denominator = sum(
        (x - x_mean) ** 2
        for x in x_values
    )

    y_denominator = sum(
        (y - y_mean) ** 2
        for y in y_values
    )

    denominator = (
        x_denominator
        * y_denominator
    ) ** 0.5

    if denominator == 0:
        return 0

    return numerator / denominator


def v2_percentile(values, percentile):

    if not values:
        return 0

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (
        len(ordered) - 1
    ) * percentile

    lower_index = int(position)
    upper_index = min(
        lower_index + 1,
        len(ordered) - 1
    )

    fraction = (
        position - lower_index
    )

    return (
        ordered[lower_index]
        * (1 - fraction)
        + ordered[upper_index]
        * fraction
    )


def v2_feature_summary(
    rows,
    feature_name
):

    usable = [
        row
        for row in rows
        if row.get(feature_name) is not None
    ]

    if not usable:

        return {
            "feature": feature_name,
            "observation_count": 0
        }

    feature_values = [
        row[feature_name]
        for row in usable
    ]

    returns_20d = [
        row["return_20d"]
        for row in usable
    ]

    excess_returns = [
        row["excess_return_20d"]
        for row in usable
    ]

    q1 = v2_percentile(
        feature_values,
        0.25
    )

    q2 = v2_percentile(
        feature_values,
        0.50
    )

    q3 = v2_percentile(
        feature_values,
        0.75
    )

    quartiles = {
        "Q1_low": [],
        "Q2": [],
        "Q3": [],
        "Q4_high": []
    }

    for row in usable:

        value = row[feature_name]

        if value <= q1:
            group = "Q1_low"

        elif value <= q2:
            group = "Q2"

        elif value <= q3:
            group = "Q3"

        else:
            group = "Q4_high"

        quartiles[group].append(row)

    quartile_results = {}

    for group_name, group_rows in quartiles.items():

        count = len(group_rows)

        if count == 0:

            quartile_results[group_name] = {
                "count": 0,
                "average_return_20d": 0,
                "median_return_20d": 0,
                "average_excess_return_20d": 0,
                "positive_return_rate": 0,
                "beat_spy_rate": 0
            }

            continue

        group_returns = [
            row["return_20d"]
            for row in group_rows
        ]

        group_excess = [
            row["excess_return_20d"]
            for row in group_rows
        ]

        positive_count = sum(
            1
            for value in group_returns
            if value > 0
        )

        beat_spy_count = sum(
            1
            for value in group_excess
            if value > 0
        )

        quartile_results[group_name] = {

            "count":
                count,

            "average_return_20d":
                round(
                    sum(group_returns)
                    / count,
                    2
                ),

            "median_return_20d":
                round(
                    statistics.median(
                        group_returns
                    ),
                    2
                ),

            "average_excess_return_20d":
                round(
                    sum(group_excess)
                    / count,
                    2
                ),

            "positive_return_rate":
                round(
                    positive_count
                    / count
                    * 100,
                    1
                ),

            "beat_spy_rate":
                round(
                    beat_spy_count
                    / count
                    * 100,
                    1
                )
        }

    return {

        "feature":
            feature_name,

        "observation_count":
            len(usable),

        "feature_q1":
            round(q1, 4),

        "feature_median":
            round(q2, 4),

        "feature_q3":
            round(q3, 4),

        "correlation_with_return_20d":
            round(
                v2_pearson(
                    feature_values,
                    returns_20d
                ),
                4
            ),

        "correlation_with_excess_return_20d":
            round(
                v2_pearson(
                    feature_values,
                    excess_returns
                ),
                4
            ),

        "quartiles":
            quartile_results
    }


@app.get("/v2/feature-study")
async def v2_feature_study(
    start_date: str = "2023-01-01",
    end_date: str = "2023-12-31"
):

    """
    V2 Stage 1.

    70 hisselik evrende teknik feature'ların gelecekteki
    20 günlük performans ile ilişkisini araştırır.

    Varsayılan geliştirme dönemi 2023'tür.

    Her 5 işlem gününde bir gözlem alınarak aşırı örtüşen
    örneklerin sayısı azaltılır.
    """

    all_rows = []

    errors = []

    await get_spy_data()

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(
                item["open"]
            ),
            "close": float(
                item["close"]
            )
        }
        for item in spy_values
    }

    batch_size = 7

    for group_start in range(
        0,
        len(V2_UNIVERSE),
        batch_size
    ):

        group = V2_UNIVERSE[
            group_start:
            group_start + batch_size
        ]

        for symbol in group:

            try:

                # backtest_stock çağrısı mevcut cache sistemini
                # kullanarak hisse verisini yükler.
                await backtest_stock(
                    symbol,
                    threshold=75
                )

                data = STOCK_CACHE.get(
                    symbol
                )

                if (
                    not data
                    or "values" not in data
                ):
                    raise Exception(
                        "Historical stock data unavailable"
                    )

                values = list(
                    reversed(
                        data["values"]
                    )
                )

                dates = [
                    item["datetime"]
                    for item in values
                ]

                closes = [
                    float(item["close"])
                    for item in values
                ]

                opens = [
                    float(item["open"])
                    for item in values
                ]

                volumes = [
                    float(item["volume"])
                    for item in values
                ]

                # En az 50 günlük geçmiş,
                # sonraki gün giriş ve 20 günlük sonuç gerekir.
                for i in range(
                    60,
                    len(closes) - 20,
                    5
                ):

                    signal_date = dates[i]

                    entry_date = dates[
                        i + 1
                    ]

                    exit_date = dates[
                        i + 20
                    ]

                    if (
                        signal_date
                        < start_date
                        or exit_date
                        > end_date
                    ):
                        continue

                    if (
                        entry_date
                        not in spy_by_date
                        or exit_date
                        not in spy_by_date
                    ):
                        continue

                    history_closes = closes[
                        :i + 1
                    ]

                    history_volumes = volumes[
                        :i + 1
                    ]

                    price = history_closes[-1]

                    ema20 = calculate_ema(
                        history_closes,
                        20
                    )

                    ema50 = calculate_ema(
                        history_closes,
                        50
                    )

                    rsi14 = calculate_rsi(
                        history_closes,
                        14
                    )

                    (
                        macd,
                        macd_signal,
                        macd_histogram
                    ) = calculate_macd(
                        history_closes
                    )

                    price_20_days_ago = (
                        history_closes[-21]
                    )

                    momentum20 = (
                        (
                            price
                            - price_20_days_ago
                        )
                        / price_20_days_ago
                        * 100
                    )

                    previous_20_volumes = (
                        history_volumes[
                            -21:-1
                        ]
                    )

                    avg_volume20 = (
                        sum(
                            previous_20_volumes
                        )
                        / 20
                    )

                    if avg_volume20 > 0:

                        volume_ratio = (
                            history_volumes[-1]
                            / avg_volume20
                        )

                    else:

                        volume_ratio = 0

                    distance_ema20 = (
                        (
                            price - ema20
                        )
                        / ema20
                        * 100
                    )

                    ema20_vs_ema50 = (
                        (
                            ema20 - ema50
                        )
                        / ema50
                        * 100
                    )

                    if price != 0:

                        macd_histogram_pct = (
                            macd_histogram
                            / price
                            * 100
                        )

                    else:

                        macd_histogram_pct = 0

                    entry_price = opens[
                        i + 1
                    ]

                    exit_price = closes[
                        i + 20
                    ]

                    return_20d = (
                        (
                            exit_price
                            - entry_price
                        )
                        / entry_price
                        * 100
                    )

                    spy_entry_price = (
                        spy_by_date[
                            entry_date
                        ]["open"]
                    )

                    spy_exit_price = (
                        spy_by_date[
                            exit_date
                        ]["close"]
                    )

                    spy_return_20d = (
                        (
                            spy_exit_price
                            - spy_entry_price
                        )
                        / spy_entry_price
                        * 100
                    )

                    excess_return_20d = (
                        return_20d
                        - spy_return_20d
                    )

                    all_rows.append({

                        "symbol":
                            symbol,

                        "signal_date":
                            signal_date,

                        "return_20d":
                            return_20d,

                        "spy_return_20d":
                            spy_return_20d,

                        "excess_return_20d":
                            excess_return_20d,

                        "distance_ema20":
                            distance_ema20,

                        "ema20_vs_ema50":
                            ema20_vs_ema50,

                        "rsi14":
                            rsi14,

                        "momentum20":
                            momentum20,

                        "volume_ratio":
                            volume_ratio,

                        "macd_histogram_pct":
                            macd_histogram_pct
                    })

            except Exception as e:

                errors.append({
                    "symbol":
                        symbol,

                    "error":
                        str(e)
                })

        if (
            group_start + batch_size
            < len(V2_UNIVERSE)
        ):
            if not RESEARCH_SNAPSHOT_LOADED:
                await asyncio.sleep(60)

    features = [
        "distance_ema20",
        "ema20_vs_ema50",
        "rsi14",
        "momentum20",
        "volume_ratio",
        "macd_histogram_pct"
    ]

    feature_results = [
        v2_feature_summary(
            all_rows,
            feature
        )
        for feature in features
    ]

    if all_rows:

        all_returns = [
            row["return_20d"]
            for row in all_rows
        ]

        all_excess = [
            row["excess_return_20d"]
            for row in all_rows
        ]

        positive_count = sum(
            1
            for value in all_returns
            if value > 0
        )

        beat_spy_count = sum(
            1
            for value in all_excess
            if value > 0
        )

        overall = {

            "observation_count":
                len(all_rows),

            "average_return_20d":
                round(
                    sum(all_returns)
                    / len(all_returns),
                    2
                ),

            "median_return_20d":
                round(
                    statistics.median(
                        all_returns
                    ),
                    2
                ),

            "average_excess_return_20d":
                round(
                    sum(all_excess)
                    / len(all_excess),
                    2
                ),

            "positive_return_rate":
                round(
                    positive_count
                    / len(all_rows)
                    * 100,
                    1
                ),

            "beat_spy_rate":
                round(
                    beat_spy_count
                    / len(all_rows)
                    * 100,
                    1
                )
        }

    else:

        overall = {
            "observation_count": 0
        }

    return {

        "engine":
            "V2 research stage 1",

        "purpose":
            "Feature study only - no V2 score yet",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "start_date":
            start_date,

        "end_date":
            end_date,

        "sampling":
            "Every 5 trading days",

        "target":
            "Next-day open to 20-trading-day close",

        "benchmark":
            "SPY same entry and exit dates",

        "error_count":
            len(errors),

        "errors":
            errors,

        "overall":
            overall,

        "features":
            feature_results
    }

# ============================================================
# V2 RESEARCH ENGINE - STAGE 2
# MONTHLY FEATURE STABILITY
# ============================================================

from collections import defaultdict


def v2_assign_quartile(value, q1, q2, q3):

    if value <= q1:
        return "Q1_low"
    elif value <= q2:
        return "Q2"
    elif value <= q3:
        return "Q3"
    else:
        return "Q4_high"


def v2_monthly_group_summary(rows):

    if not rows:
        return {
            "count": 0,
            "average_return_20d": 0,
            "median_return_20d": 0,
            "average_excess_return_20d": 0,
            "positive_return_rate": 0,
            "beat_spy_rate": 0
        }

    returns = [
        row["return_20d"]
        for row in rows
    ]

    excess = [
        row["excess_return_20d"]
        for row in rows
    ]

    positive_count = sum(
        1 for value in returns
        if value > 0
    )

    beat_count = sum(
        1 for value in excess
        if value > 0
    )

    return {
        "count":
            len(rows),

        "average_return_20d":
            round(
                sum(returns) / len(returns),
                2
            ),

        "median_return_20d":
            round(
                statistics.median(returns),
                2
            ),

        "average_excess_return_20d":
            round(
                sum(excess) / len(excess),
                2
            ),

        "positive_return_rate":
            round(
                positive_count
                / len(rows)
                * 100,
                1
            ),

        "beat_spy_rate":
            round(
                beat_count
                / len(rows)
                * 100,
                1
            )
    }


@app.get("/v2/monthly-feature-study")
async def v2_monthly_feature_study(
    start_date: str = "2023-01-01",
    end_date: str = "2023-12-31"
):

    all_rows = []
    errors = []

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(item["open"]),
            "close": float(item["close"])
        }
        for item in spy_values
    }

    batch_size = 7

    for group_start in range(
        0,
        len(V2_UNIVERSE),
        batch_size
    ):

        group = V2_UNIVERSE[
            group_start:
            group_start + batch_size
        ]

        for symbol in group:

            try:

                # Cache yoksa mevcut backtest motoru
                # hisse verisini yükler.
                if symbol not in STOCK_CACHE:

                    await backtest_stock(
                        symbol,
                        threshold=75
                    )

                data = STOCK_CACHE.get(symbol)

                if (
                    not data
                    or "values" not in data
                ):
                    raise Exception(
                        "Historical stock data unavailable"
                    )

                values = list(
                    reversed(data["values"])
                )

                dates = [
                    item["datetime"]
                    for item in values
                ]

                closes = [
                    float(item["close"])
                    for item in values
                ]

                opens = [
                    float(item["open"])
                    for item in values
                ]

                volumes = [
                    float(item["volume"])
                    for item in values
                ]

                for i in range(
                    60,
                    len(closes) - 20,
                    5
                ):

                    signal_date = dates[i]
                    entry_date = dates[i + 1]
                    exit_date = dates[i + 20]

                    if (
                        signal_date < start_date
                        or exit_date > end_date
                    ):
                        continue

                    if (
                        entry_date not in spy_by_date
                        or exit_date not in spy_by_date
                    ):
                        continue

                    history_closes = closes[
                        :i + 1
                    ]

                    history_volumes = volumes[
                        :i + 1
                    ]

                    price = history_closes[-1]

                    ema20 = calculate_ema(
                        history_closes,
                        20
                    )

                    ema50 = calculate_ema(
                        history_closes,
                        50
                    )

                    rsi14 = calculate_rsi(
                        history_closes,
                        14
                    )

                    (
                        macd,
                        macd_signal,
                        macd_histogram
                    ) = calculate_macd(
                        history_closes
                    )

                    price_20_days_ago = (
                        history_closes[-21]
                    )

                    momentum20 = (
                        (
                            price
                            - price_20_days_ago
                        )
                        / price_20_days_ago
                        * 100
                    )

                    previous_20_volumes = (
                        history_volumes[-21:-1]
                    )

                    avg_volume20 = (
                        sum(previous_20_volumes)
                        / 20
                    )

                    if avg_volume20 > 0:
                        volume_ratio = (
                            history_volumes[-1]
                            / avg_volume20
                        )
                    else:
                        volume_ratio = 0

                    distance_ema20 = (
                        (
                            price - ema20
                        )
                        / ema20
                        * 100
                    )

                    ema20_vs_ema50 = (
                        (
                            ema20 - ema50
                        )
                        / ema50
                        * 100
                    )

                    if price != 0:
                        macd_histogram_pct = (
                            macd_histogram
                            / price
                            * 100
                        )
                    else:
                        macd_histogram_pct = 0

                    entry_price = opens[i + 1]
                    exit_price = closes[i + 20]

                    return_20d = (
                        (
                            exit_price
                            - entry_price
                        )
                        / entry_price
                        * 100
                    )

                    spy_entry = (
                        spy_by_date[
                            entry_date
                        ]["open"]
                    )

                    spy_exit = (
                        spy_by_date[
                            exit_date
                        ]["close"]
                    )

                    spy_return_20d = (
                        (
                            spy_exit - spy_entry
                        )
                        / spy_entry
                        * 100
                    )

                    excess_return_20d = (
                        return_20d
                        - spy_return_20d
                    )

                    all_rows.append({
                        "symbol":
                            symbol,

                        "signal_date":
                            signal_date,

                        "month":
                            signal_date[:7],

                        "return_20d":
                            return_20d,

                        "excess_return_20d":
                            excess_return_20d,

                        "distance_ema20":
                            distance_ema20,

                        "ema20_vs_ema50":
                            ema20_vs_ema50,

                        "rsi14":
                            rsi14,

                        "momentum20":
                            momentum20,

                        "volume_ratio":
                            volume_ratio,

                        "macd_histogram_pct":
                            macd_histogram_pct
                    })

            except Exception as e:

                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        if (
            group_start + batch_size
            < len(V2_UNIVERSE)
        ):
            if not RESEARCH_SNAPSHOT_LOADED:
                await asyncio.sleep(60)

    features = [
        "distance_ema20",
        "ema20_vs_ema50",
        "rsi14",
        "momentum20",
        "volume_ratio",
        "macd_histogram_pct"
    ]

    feature_results = []

    for feature in features:

        usable = [
            row
            for row in all_rows
            if row.get(feature) is not None
        ]

        feature_values = [
            row[feature]
            for row in usable
        ]

        if not feature_values:
            continue

        # Çok önemli:
        # Quartile sınırlarını her ay yeniden hesaplamıyoruz.
        # Bütün geliştirme dönemindeki sınırları sabitliyoruz.
        q1 = v2_percentile(
            feature_values,
            0.25
        )

        q2 = v2_percentile(
            feature_values,
            0.50
        )

        q3 = v2_percentile(
            feature_values,
            0.75
        )

        monthly_groups = defaultdict(
            lambda: {
                "Q1_low": [],
                "Q2": [],
                "Q3": [],
                "Q4_high": []
            }
        )

        for row in usable:

            quartile = v2_assign_quartile(
                row[feature],
                q1,
                q2,
                q3
            )

            monthly_groups[
                row["month"]
            ][quartile].append(row)

        monthly_results = {}

        q1_positive_excess_months = 0
        q4_positive_excess_months = 0

        q1_beat_spy_50_months = 0
        q4_beat_spy_50_months = 0

        q1_better_than_q4_months = 0
        q4_better_than_q1_months = 0

        months_compared = 0

        for month in sorted(
            monthly_groups.keys()
        ):

            groups = monthly_groups[month]

            month_result = {}

            for quartile_name in [
                "Q1_low",
                "Q2",
                "Q3",
                "Q4_high"
            ]:

                month_result[
                    quartile_name
                ] = v2_monthly_group_summary(
                    groups[quartile_name]
                )

            q1_result = month_result[
                "Q1_low"
            ]

            q4_result = month_result[
                "Q4_high"
            ]

            if q1_result["count"] > 0:

                if (
                    q1_result[
                        "average_excess_return_20d"
                    ] > 0
                ):
                    q1_positive_excess_months += 1

                if (
                    q1_result[
                        "beat_spy_rate"
                    ] > 50
                ):
                    q1_beat_spy_50_months += 1

            if q4_result["count"] > 0:

                if (
                    q4_result[
                        "average_excess_return_20d"
                    ] > 0
                ):
                    q4_positive_excess_months += 1

                if (
                    q4_result[
                        "beat_spy_rate"
                    ] > 50
                ):
                    q4_beat_spy_50_months += 1

            if (
                q1_result["count"] > 0
                and q4_result["count"] > 0
            ):

                months_compared += 1

                if (
                    q1_result[
                        "average_excess_return_20d"
                    ]
                    >
                    q4_result[
                        "average_excess_return_20d"
                    ]
                ):
                    q1_better_than_q4_months += 1

                elif (
                    q4_result[
                        "average_excess_return_20d"
                    ]
                    >
                    q1_result[
                        "average_excess_return_20d"
                    ]
                ):
                    q4_better_than_q1_months += 1

            monthly_results[month] = (
                month_result
            )

        feature_results.append({

            "feature":
                feature,

            "quartile_boundaries": {
                "q1": round(q1, 4),
                "q2": round(q2, 4),
                "q3": round(q3, 4)
            },

            "stability_summary": {

                "months_compared":
                    months_compared,

                "q1_positive_excess_months":
                    q1_positive_excess_months,

                "q4_positive_excess_months":
                    q4_positive_excess_months,

                "q1_beat_spy_over_50_months":
                    q1_beat_spy_50_months,

                "q4_beat_spy_over_50_months":
                    q4_beat_spy_50_months,

                "q1_better_than_q4_months":
                    q1_better_than_q4_months,

                "q4_better_than_q1_months":
                    q4_better_than_q1_months
            },

            "months":
                monthly_results
        })

    return {

        "engine":
            "V2 research stage 2",

        "purpose":
            "Monthly feature stability study",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "start_date":
            start_date,

        "end_date":
            end_date,

        "sampling":
            "Every 5 trading days",

        "observation_count":
            len(all_rows),

        "error_count":
            len(errors),

        "errors":
            errors,

        "features":
            feature_results
    }

# ============================================================
# V2 RESEARCH ENGINE - STAGE 3
# FROZEN CANDIDATE MODEL 1
# ============================================================
#
# Model geliştirme kaynağı:
# 2023 Stage 1 + Stage 2
#
# Amaç:
# 2023'te gözlenen feature ilişkilerini TEK bir sabit
# puanlama modeline dönüştürmek.
#
# Bu model oluşturulduktan sonra 2024/2025/2026 testleri
# arasında puanlama kuralları DEĞİŞTİRİLMEYECEK.
#
# V1.1 sistemi değiştirilmez.
# ============================================================


V2_CANDIDATE_NAME = "V2-C1-FROZEN-2023"


def calculate_v2_candidate_score(
    closes,
    volumes
):

    if (
        len(closes) < 60
        or len(volumes) < 21
    ):
        return None

    price = closes[-1]

    ema20 = calculate_ema(
        closes,
        20
    )

    ema50 = calculate_ema(
        closes,
        50
    )

    rsi14 = calculate_rsi(
        closes,
        14
    )

    (
        macd,
        macd_signal,
        macd_histogram
    ) = calculate_macd(
        closes
    )

    price_20_days_ago = closes[-21]

    momentum20 = (
        (
            price
            - price_20_days_ago
        )
        / price_20_days_ago
        * 100
    )

    previous_20_volumes = (
        volumes[-21:-1]
    )

    avg_volume20 = (
        sum(previous_20_volumes)
        / 20
    )

    if avg_volume20 > 0:
        volume_ratio = (
            volumes[-1]
            / avg_volume20
        )
    else:
        volume_ratio = 0

    distance_ema20 = (
        (
            price - ema20
        )
        / ema20
        * 100
    )

    ema20_vs_ema50 = (
        (
            ema20 - ema50
        )
        / ema50
        * 100
    )

    if price != 0:
        macd_histogram_pct = (
            macd_histogram
            / price
            * 100
        )
    else:
        macd_histogram_pct = 0

    # --------------------------------------------------------
    # FROZEN 2023 QUARTILE BOUNDARIES
    # --------------------------------------------------------

    score = 0
    reasons = []

    # 1) PRICE DISTANCE FROM EMA20
    #
    # 2023:
    # Q1 <= -1.7830
    # Q2 <=  0.7691
    # Q3 <=  3.6235
    #
    # Q1 was strongest and showed high monthly stability.

    if distance_ema20 <= -1.7830:

        score += 25

        reasons.append(
            "Price below EMA20 recovery zone"
        )

    elif distance_ema20 <= 0.7691:

        score += 15

        reasons.append(
            "Price near EMA20"
        )

    elif distance_ema20 <= 3.6235:

        score += 5

    else:

        score += 10

        reasons.append(
            "Price extended above EMA20"
        )

    # 2) RSI14
    #
    # 2023 Q1 <= 40.2119 showed strongest
    # forward relative performance.

    if rsi14 <= 40.2119:

        score += 25

        reasons.append(
            "Low RSI recovery zone"
        )

    elif rsi14 <= 54.3162:

        score += 15

    elif rsi14 <= 67.0387:

        score += 5

    else:

        score += 8

    # 3) MOMENTUM20
    #
    # 2023 Q1 <= -3.6834 showed strongest
    # and most stable relative performance.

    if momentum20 <= -3.6834:

        score += 25

        reasons.append(
            "Negative 20-day momentum recovery zone"
        )

    elif momentum20 <= 1.7306:

        score += 15

    elif momentum20 <= 7.8249:

        score += 5

    else:

        score += 10

    # 4) EMA20 VS EMA50
    #
    # Lower quartile showed strongest aggregate result,
    # but monthly Q1/Q4 separation was weaker than
    # distance/RSI/momentum, so lower weight.

    if ema20_vs_ema50 <= -0.7516:

        score += 10

        reasons.append(
            "Short trend below medium trend"
        )

    elif ema20_vs_ema50 <= 1.3274:

        score += 7

    elif ema20_vs_ema50 <= 3.6626:

        score += 3

    else:

        score += 5

    # 5) VOLUME
    #
    # Weak relationship in Stage 1/2.
    # Only small contribution.

    if 0.7634 < volume_ratio <= 1.1784:

        score += 8

    elif volume_ratio <= 0.7634:

        score += 5

    else:

        score += 3

    # 6) MACD HISTOGRAM
    #
    # U-shaped relationship in 2023.
    # Extreme positive/negative values performed better
    # than the middle groups. Low weight to avoid
    # overfitting.

    if (
        macd_histogram_pct <= -0.3933
        or macd_histogram_pct > 0.4582
    ):

        score += 7

    else:

        score += 2

    # Maximum score = 100

    features = {

        "distance_ema20":
            round(
                distance_ema20,
                4
            ),

        "ema20_vs_ema50":
            round(
                ema20_vs_ema50,
                4
            ),

        "rsi14":
            round(
                rsi14,
                2
            ),

        "momentum20":
            round(
                momentum20,
                4
            ),

        "volume_ratio":
            round(
                volume_ratio,
                4
            ),

        "macd_histogram_pct":
            round(
                macd_histogram_pct,
                4
            )
    }

    return {
        "score": score,
        "features": features,
        "reasons": reasons
    }


@app.get("/v2/backtest")
async def v2_backtest(
    start_date: str = "2024-01-01",
    end_date: str = "2024-12-31",
    threshold: int = 75
):

    all_signals = []
    stock_results = []
    errors = []

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(
                item["open"]
            ),
            "close": float(
                item["close"]
            )
        }
        for item in spy_values
    }

    batch_size = 7

    for group_start in range(
        0,
        len(V2_UNIVERSE),
        batch_size
    ):

        group = V2_UNIVERSE[
            group_start:
            group_start + batch_size
        ]

        for symbol in group:

            try:

                if symbol not in STOCK_CACHE:

                    await backtest_stock(
                        symbol,
                        threshold=75
                    )

                data = STOCK_CACHE.get(
                    symbol
                )

                if (
                    not data
                    or "values" not in data
                ):

                    raise Exception(
                        "Historical data unavailable"
                    )

                values = list(
                    reversed(
                        data["values"]
                    )
                )

                dates = [
                    item["datetime"]
                    for item in values
                ]

                closes = [
                    float(item["close"])
                    for item in values
                ]

                opens = [
                    float(item["open"])
                    for item in values
                ]

                volumes = [
                    float(item["volume"])
                    for item in values
                ]

                symbol_signals = []

                previous_score = 0
                last_signal_index = -100

                for i in range(
                    60,
                    len(closes) - 20
                ):

                    signal_date = dates[i]
                    entry_date = dates[i + 1]
                    exit_date = dates[i + 20]

                    history_closes = (
                        closes[:i + 1]
                    )

                    history_volumes = (
                        volumes[:i + 1]
                    )

                    result = (
                        calculate_v2_candidate_score(
                            history_closes,
                            history_volumes
                        )
                    )

                    if result is None:
                        continue

                    score = result["score"]

                    # Same crossing/cooldown concept as V1
                    if (
                        score >= threshold
                        and previous_score < threshold
                        and i - last_signal_index >= 20
                        and signal_date >= start_date
                        and exit_date <= end_date
                    ):

                        if (
                            entry_date
                            not in spy_by_date
                            or exit_date
                            not in spy_by_date
                        ):

                            previous_score = score
                            continue

                        entry_price = (
                            opens[i + 1]
                        )

                        exit_price_5d = (
                            closes[i + 5]
                        )

                        exit_price_10d = (
                            closes[i + 10]
                        )

                        exit_price_20d = (
                            closes[i + 20]
                        )

                        return_5d = (
                            (
                                exit_price_5d
                                - entry_price
                            )
                            / entry_price
                            * 100
                        )

                        return_10d = (
                            (
                                exit_price_10d
                                - entry_price
                            )
                            / entry_price
                            * 100
                        )

                        return_20d = (
                            (
                                exit_price_20d
                                - entry_price
                            )
                            / entry_price
                            * 100
                        )

                        spy_entry = (
                            spy_by_date[
                                entry_date
                            ]["open"]
                        )

                        spy_exit = (
                            spy_by_date[
                                exit_date
                            ]["close"]
                        )

                        spy_return_20d = (
                            (
                                spy_exit
                                - spy_entry
                            )
                            / spy_entry
                            * 100
                        )

                        excess_return_20d = (
                            return_20d
                            - spy_return_20d
                        )

                        signal = {

                            "symbol":
                                symbol,

                            "signal_date":
                                signal_date,

                            "entry_date":
                                entry_date,

                            "exit_date":
                                exit_date,

                            "score":
                                score,

                            "entry_price":
                                round(
                                    entry_price,
                                    2
                                ),

                            "return_5d":
                                round(
                                    return_5d,
                                    2
                                ),

                            "return_10d":
                                round(
                                    return_10d,
                                    2
                                ),

                            "return_20d":
                                round(
                                    return_20d,
                                    2
                                ),

                            "spy_return_20d":
                                round(
                                    spy_return_20d,
                                    2
                                ),

                            "excess_return_20d":
                                round(
                                    excess_return_20d,
                                    2
                                ),

                            "features":
                                result[
                                    "features"
                                ],

                            "reasons":
                                result[
                                    "reasons"
                                ]
                        }

                        symbol_signals.append(
                            signal
                        )

                        all_signals.append(
                            signal
                        )

                        last_signal_index = i

                    previous_score = score

                stock_results.append({

                    "symbol":
                        symbol,

                    "signal_count":
                        len(
                            symbol_signals
                        )
                })

            except Exception as e:

                errors.append({

                    "symbol":
                        symbol,

                    "error":
                        str(e)
                })

        if (
            group_start + batch_size
            < len(V2_UNIVERSE)
        ):

            if not RESEARCH_SNAPSHOT_LOADED:
                await asyncio.sleep(60)

    if all_signals:

        returns_5d = [
            x["return_5d"]
            for x in all_signals
        ]

        returns_10d = [
            x["return_10d"]
            for x in all_signals
        ]

        returns_20d = [
            x["return_20d"]
            for x in all_signals
        ]

        spy_returns = [
            x["spy_return_20d"]
            for x in all_signals
        ]

        excess_returns = [
            x["excess_return_20d"]
            for x in all_signals
        ]

        winners = sum(
            1
            for x in returns_20d
            if x > 0
        )

        beat_spy = sum(
            1
            for x in excess_returns
            if x > 0
        )

        summary = {

            "signal_count":
                len(all_signals),

            "average_return_5d":
                round(
                    sum(returns_5d)
                    / len(returns_5d),
                    2
                ),

            "average_return_10d":
                round(
                    sum(returns_10d)
                    / len(returns_10d),
                    2
                ),

            "average_return_20d":
                round(
                    sum(returns_20d)
                    / len(returns_20d),
                    2
                ),

            "median_return_20d":
                round(
                    statistics.median(
                        returns_20d
                    ),
                    2
                ),

            "win_rate_20d":
                round(
                    winners
                    / len(all_signals)
                    * 100,
                    1
                ),

            "average_spy_return_20d":
                round(
                    sum(spy_returns)
                    / len(spy_returns),
                    2
                ),

            "average_excess_return_20d":
                round(
                    sum(excess_returns)
                    / len(excess_returns),
                    2
                ),

            "median_excess_return_20d":
                round(
                    statistics.median(
                        excess_returns
                    ),
                    2
                ),

            "beat_spy_rate_20d":
                round(
                    beat_spy
                    / len(all_signals)
                    * 100,
                    1
                )
        }

    else:

        summary = {
            "signal_count": 0
        }

    return {

        "engine":
            "V2 Stage 3",

        "model":
            V2_CANDIDATE_NAME,

        "model_status":
            "FROZEN",

        "threshold":
            threshold,

        "start_date":
            start_date,

        "end_date":
            end_date,

        "universe_stock_count":
            len(V2_UNIVERSE),

        "successful_stock_count":
            len(stock_results),

        "error_count":
            len(errors),

        "errors":
            errors,

        "summary":
            summary,

        "stock_results":
            stock_results,

        "signals":
            all_signals
    }

# ============================================================
# V2 STAGE 4
# C1 DIAGNOSTIC ENGINE
# ============================================================
#
# PURPOSE:
# Diagnose WHY frozen V2-C1 performance changes by year.
#
# IMPORTANT:
# - Does NOT modify C1 scoring.
# - Does NOT create a new trading model.
# - Uses the exact frozen C1 score.
# - Evaluates 2024, 2025 and 2026 separately.
# ============================================================


def v2_diag_bucket(value, boundaries, labels):
    for boundary, label in zip(boundaries, labels):
        if value <= boundary:
            return label
    return labels[-1]


def v2_diag_stats(rows):

    if not rows:
        return {
            "count": 0
        }

    returns = [
        x["return_20d"]
        for x in rows
    ]

    excess = [
        x["excess_return_20d"]
        for x in rows
    ]

    return {
        "count":
            len(rows),

        "average_return_20d":
            round(
                sum(returns) / len(returns),
                2
            ),

        "median_return_20d":
            round(
                statistics.median(returns),
                2
            ),

        "win_rate_20d":
            round(
                sum(
                    1 for x in returns
                    if x > 0
                )
                / len(returns)
                * 100,
                1
            ),

        "average_excess_return_20d":
            round(
                sum(excess) / len(excess),
                2
            ),

        "median_excess_return_20d":
            round(
                statistics.median(excess),
                2
            ),

        "beat_spy_rate_20d":
            round(
                sum(
                    1 for x in excess
                    if x > 0
                )
                / len(excess)
                * 100,
                1
            )
    }


def v2_diag_group(rows, key_function):

    groups = defaultdict(list)

    for row in rows:
        key = key_function(row)
        groups[key].append(row)

    return {
        str(key): v2_diag_stats(group_rows)
        for key, group_rows
        in sorted(
            groups.items(),
            key=lambda x: str(x[0])
        )
    }


@app.get("/v2/c1-diagnostics")
async def v2_c1_diagnostics():

    periods = [
        (
            "2024",
            "2024-01-01",
            "2024-12-31"
        ),
        (
            "2025",
            "2025-01-01",
            "2025-12-31"
        ),
        (
            "2026",
            "2026-01-01",
            "2026-08-15"
        )
    ]

    all_year_results = {}

    # --------------------------------------------------------
    # Run the EXACT same frozen C1 model for each period.
    # --------------------------------------------------------

    for (
        year_label,
        start_date,
        end_date
    ) in periods:

        result = await v2_backtest(
            start_date=start_date,
            end_date=end_date,
            threshold=75
        )

        signals = result.get(
            "signals",
            []
        )

        # ----------------------------------------------------
        # SCORE BUCKETS
        # Does higher C1 score actually mean better outcome?
        # ----------------------------------------------------

        score_groups = v2_diag_group(
            signals,
            lambda x:
                "75-79"
                if x["score"] < 80
                else (
                    "80-84"
                    if x["score"] < 85
                    else (
                        "85-89"
                        if x["score"] < 90
                        else "90-100"
                    )
                )
        )

        # ----------------------------------------------------
        # DISTANCE FROM EMA20
        # ----------------------------------------------------

        distance_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_very_low"
                if x["features"]["distance_ema20"] <= -1.7830
                else (
                    "Q2_near"
                    if x["features"]["distance_ema20"] <= 0.7691
                    else (
                        "Q3_above"
                        if x["features"]["distance_ema20"] <= 3.6235
                        else "Q4_extended"
                    )
                )
        )

        # ----------------------------------------------------
        # RSI
        # ----------------------------------------------------

        rsi_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_low"
                if x["features"]["rsi14"] <= 40.2119
                else (
                    "Q2"
                    if x["features"]["rsi14"] <= 54.3162
                    else (
                        "Q3"
                        if x["features"]["rsi14"] <= 67.0387
                        else "Q4_high"
                    )
                )
        )

        # ----------------------------------------------------
        # MOMENTUM
        # ----------------------------------------------------

        momentum_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_negative"
                if x["features"]["momentum20"] <= -3.6834
                else (
                    "Q2"
                    if x["features"]["momentum20"] <= 1.7306
                    else (
                        "Q3"
                        if x["features"]["momentum20"] <= 7.8249
                        else "Q4_high"
                    )
                )
        )

        # ----------------------------------------------------
        # EMA20 VS EMA50
        # ----------------------------------------------------

        ema_trend_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_weak"
                if x["features"]["ema20_vs_ema50"] <= -0.7516
                else (
                    "Q2"
                    if x["features"]["ema20_vs_ema50"] <= 1.3274
                    else (
                        "Q3"
                        if x["features"]["ema20_vs_ema50"] <= 3.6626
                        else "Q4_strong"
                    )
                )
        )

        # ----------------------------------------------------
        # VOLUME
        # ----------------------------------------------------

        volume_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_low"
                if x["features"]["volume_ratio"] <= 0.7634
                else (
                    "Q2"
                    if x["features"]["volume_ratio"] <= 0.9325
                    else (
                        "Q3"
                        if x["features"]["volume_ratio"] <= 1.1784
                        else "Q4_high"
                    )
                )
        )

        # ----------------------------------------------------
        # MACD
        # ----------------------------------------------------

        macd_groups = v2_diag_group(
            signals,
            lambda x:
                "Q1_negative_extreme"
                if x["features"]["macd_histogram_pct"] <= -0.3933
                else (
                    "Q2"
                    if x["features"]["macd_histogram_pct"] <= 0.0267
                    else (
                        "Q3"
                        if x["features"]["macd_histogram_pct"] <= 0.4582
                        else "Q4_positive_extreme"
                    )
                )
        )

        # ----------------------------------------------------
        # RECOVERY COMBINATION COUNT
        #
        # How many of the three main recovery conditions
        # are active at the same time?
        # ----------------------------------------------------

        def recovery_count(row):

            count = 0

            if (
                row["features"]["distance_ema20"]
                <= -1.7830
            ):
                count += 1

            if (
                row["features"]["rsi14"]
                <= 40.2119
            ):
                count += 1

            if (
                row["features"]["momentum20"]
                <= -3.6834
            ):
                count += 1

            return str(count)

        recovery_groups = v2_diag_group(
            signals,
            recovery_count
        )

        # ----------------------------------------------------
        # MONTHLY PERFORMANCE
        # Helps detect regime/time concentration.
        # ----------------------------------------------------

        monthly_groups = v2_diag_group(
            signals,
            lambda x:
                x["signal_date"][:7]
        )

        # ----------------------------------------------------
        # STOCK LEVEL
        # Detect whether a few stocks dominate results.
        # ----------------------------------------------------

        stock_groups = v2_diag_group(
            signals,
            lambda x:
                x["symbol"]
        )

        # ----------------------------------------------------
        # BEST / WORST STOCKS BY AVERAGE EXCESS
        # Only stocks with at least 3 signals.
        # ----------------------------------------------------

        stock_rankable = []

        for symbol, stats in stock_groups.items():

            if stats["count"] >= 3:

                stock_rankable.append({
                    "symbol":
                        symbol,

                    "count":
                        stats["count"],

                    "average_excess_return_20d":
                        stats[
                            "average_excess_return_20d"
                        ],

                    "median_excess_return_20d":
                        stats[
                            "median_excess_return_20d"
                        ],

                    "beat_spy_rate_20d":
                        stats[
                            "beat_spy_rate_20d"
                        ]
                })

        stock_rankable.sort(
            key=lambda x:
                x["average_excess_return_20d"],
            reverse=True
        )

        best_stocks = (
            stock_rankable[:10]
        )

        worst_stocks = (
            list(
                reversed(
                    stock_rankable[-10:]
                )
            )
        )

        # ----------------------------------------------------
        # OUTLIER CHECK
        #
        # Compare full mean with trimmed mean.
        # Removes top/bottom 5% excess-return observations.
        # ----------------------------------------------------

        excess_values = sorted(
            [
                x["excess_return_20d"]
                for x in signals
            ]
        )

        trimmed_mean_excess = None

        if len(excess_values) >= 20:

            trim_count = max(
                1,
                int(
                    len(excess_values)
                    * 0.05
                )
            )

            trimmed = (
                excess_values[
                    trim_count:
                    len(excess_values)
                    - trim_count
                ]
            )

            if trimmed:

                trimmed_mean_excess = round(
                    sum(trimmed)
                    / len(trimmed),
                    2
                )

        # ----------------------------------------------------
        # TOP / BOTTOM OUTLIERS
        # ----------------------------------------------------

        sorted_signals = sorted(
            signals,
            key=lambda x:
                x["excess_return_20d"],
            reverse=True
        )

        top_outliers = [
            {
                "symbol":
                    x["symbol"],

                "signal_date":
                    x["signal_date"],

                "score":
                    x["score"],

                "return_20d":
                    x["return_20d"],

                "spy_return_20d":
                    x["spy_return_20d"],

                "excess_return_20d":
                    x["excess_return_20d"]
            }
            for x in sorted_signals[:10]
        ]

        bottom_outliers = [
            {
                "symbol":
                    x["symbol"],

                "signal_date":
                    x["signal_date"],

                "score":
                    x["score"],

                "return_20d":
                    x["return_20d"],

                "spy_return_20d":
                    x["spy_return_20d"],

                "excess_return_20d":
                    x["excess_return_20d"]
            }
            for x in sorted_signals[-10:]
        ]

        bottom_outliers.reverse()

        # ----------------------------------------------------
        # YEAR RESULT
        # ----------------------------------------------------

        all_year_results[
            year_label
        ] = {

            "period": {
                "start":
                    start_date,

                "end":
                    end_date
            },

            "overall":
                result.get(
                    "summary",
                    {}
                ),

            "trimmed_mean_excess_return_20d":
                trimmed_mean_excess,

            "score_buckets":
                score_groups,

            "distance_ema20_buckets":
                distance_groups,

            "rsi14_buckets":
                rsi_groups,

            "momentum20_buckets":
                momentum_groups,

            "ema20_vs_ema50_buckets":
                ema_trend_groups,

            "volume_ratio_buckets":
                volume_groups,

            "macd_histogram_buckets":
                macd_groups,

            "recovery_condition_count":
                recovery_groups,

            "monthly":
                monthly_groups,

            "best_stocks":
                best_stocks,

            "worst_stocks":
                worst_stocks,

            "top_outliers":
                top_outliers,

            "bottom_outliers":
                bottom_outliers
        }

    # --------------------------------------------------------
    # CROSS-YEAR SCORE COMPARISON
    # --------------------------------------------------------

    score_cross_year = {}

    score_labels = [
        "75-79",
        "80-84",
        "85-89",
        "90-100"
    ]

    for score_label in score_labels:

        score_cross_year[
            score_label
        ] = {}

        for year in [
            "2024",
            "2025",
            "2026"
        ]:

            year_bucket = (
                all_year_results[
                    year
                ][
                    "score_buckets"
                ].get(
                    score_label,
                    {
                        "count": 0
                    }
                )
            )

            score_cross_year[
                score_label
            ][year] = (
                year_bucket
            )

    return {

        "engine":
            "V2 Stage 4",

        "purpose":
            "Frozen C1 diagnostic analysis",

        "model":
            V2_CANDIDATE_NAME,

        "model_status":
            "FROZEN",

        "threshold":
            75,

        "universe_stock_count":
            len(V2_UNIVERSE),

        "diagnostic_note":
            (
                "No C1 weights or thresholds "
                "were changed in this analysis."
            ),

        "score_cross_year":
            score_cross_year,

        "years":
            all_year_results
    }

# ============================================================
# V2 STAGE 4B
# SINGLE-YEAR C1 DIAGNOSTIC ENGINE
# ============================================================
#
# Fix for long /v2/c1-diagnostics request.
#
# Each endpoint analyzes ONE year only:
#
# /v2/c1-diagnostics/2024
# /v2/c1-diagnostics/2025
# /v2/c1-diagnostics/2026
#
# IMPORTANT:
# Frozen C1 model is NOT modified.
# Threshold remains 75.
# ============================================================


async def v2_c1_diagnostic_single_year(
    year: str
):

    period_map = {

        "2024": (
            "2024-01-01",
            "2024-12-31"
        ),

        "2025": (
            "2025-01-01",
            "2025-12-31"
        ),

        "2026": (
            "2026-01-01",
            "2026-08-15"
        )
    }

    if year not in period_map:

        raise HTTPException(
            status_code=400,
            detail=(
                "Year must be "
                "2024, 2025 or 2026"
            )
        )

    start_date, end_date = (
        period_map[year]
    )

    # --------------------------------------------------------
    # Run EXACT frozen C1 model
    # --------------------------------------------------------

    result = await v2_backtest(
        start_date=start_date,
        end_date=end_date,
        threshold=75
    )

    signals = result.get(
        "signals",
        []
    )

    # --------------------------------------------------------
    # SCORE BUCKETS
    # --------------------------------------------------------

    score_groups = v2_diag_group(
        signals,
        lambda x:
            "75-79"
            if x["score"] < 80
            else (
                "80-84"
                if x["score"] < 85
                else (
                    "85-89"
                    if x["score"] < 90
                    else "90-100"
                )
            )
    )

    # --------------------------------------------------------
    # DISTANCE EMA20
    # --------------------------------------------------------

    distance_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_very_low"
            if (
                x["features"][
                    "distance_ema20"
                ]
                <= -1.7830
            )
            else (
                "Q2_near"
                if (
                    x["features"][
                        "distance_ema20"
                    ]
                    <= 0.7691
                )
                else (
                    "Q3_above"
                    if (
                        x["features"][
                            "distance_ema20"
                        ]
                        <= 3.6235
                    )
                    else "Q4_extended"
                )
            )
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_low"
            if (
                x["features"]["rsi14"]
                <= 40.2119
            )
            else (
                "Q2"
                if (
                    x["features"]["rsi14"]
                    <= 54.3162
                )
                else (
                    "Q3"
                    if (
                        x["features"]["rsi14"]
                        <= 67.0387
                    )
                    else "Q4_high"
                )
            )
    )

    # --------------------------------------------------------
    # MOMENTUM20
    # --------------------------------------------------------

    momentum_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_negative"
            if (
                x["features"][
                    "momentum20"
                ]
                <= -3.6834
            )
            else (
                "Q2"
                if (
                    x["features"][
                        "momentum20"
                    ]
                    <= 1.7306
                )
                else (
                    "Q3"
                    if (
                        x["features"][
                            "momentum20"
                        ]
                        <= 7.8249
                    )
                    else "Q4_high"
                )
            )
    )

    # --------------------------------------------------------
    # EMA20 VS EMA50
    # --------------------------------------------------------

    ema_trend_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_weak"
            if (
                x["features"][
                    "ema20_vs_ema50"
                ]
                <= -0.7516
            )
            else (
                "Q2"
                if (
                    x["features"][
                        "ema20_vs_ema50"
                    ]
                    <= 1.3274
                )
                else (
                    "Q3"
                    if (
                        x["features"][
                            "ema20_vs_ema50"
                        ]
                        <= 3.6626
                    )
                    else "Q4_strong"
                )
            )
    )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    volume_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_low"
            if (
                x["features"][
                    "volume_ratio"
                ]
                <= 0.7634
            )
            else (
                "Q2"
                if (
                    x["features"][
                        "volume_ratio"
                    ]
                    <= 0.9325
                )
                else (
                    "Q3"
                    if (
                        x["features"][
                            "volume_ratio"
                        ]
                        <= 1.1784
                    )
                    else "Q4_high"
                )
            )
    )

    # --------------------------------------------------------
    # MACD HISTOGRAM
    # --------------------------------------------------------

    macd_groups = v2_diag_group(
        signals,
        lambda x:
            "Q1_negative_extreme"
            if (
                x["features"][
                    "macd_histogram_pct"
                ]
                <= -0.3933
            )
            else (
                "Q2"
                if (
                    x["features"][
                        "macd_histogram_pct"
                    ]
                    <= 0.0267
                )
                else (
                    "Q3"
                    if (
                        x["features"][
                            "macd_histogram_pct"
                        ]
                        <= 0.4582
                    )
                    else (
                        "Q4_positive_extreme"
                    )
                )
            )
    )

    # --------------------------------------------------------
    # RECOVERY CONDITION COUNT
    #
    # Main recovery features:
    # 1. distance EMA20
    # 2. RSI
    # 3. momentum
    # --------------------------------------------------------

    def recovery_count(row):

        count = 0

        if (
            row["features"][
                "distance_ema20"
            ]
            <= -1.7830
        ):
            count += 1

        if (
            row["features"]["rsi14"]
            <= 40.2119
        ):
            count += 1

        if (
            row["features"][
                "momentum20"
            ]
            <= -3.6834
        ):
            count += 1

        return str(count)

    recovery_groups = v2_diag_group(
        signals,
        recovery_count
    )

    # --------------------------------------------------------
    # MONTHLY RESULTS
    # --------------------------------------------------------

    monthly_groups = v2_diag_group(
        signals,
        lambda x:
            x["signal_date"][:7]
    )

    # --------------------------------------------------------
    # STOCK RESULTS
    # --------------------------------------------------------

    stock_groups = v2_diag_group(
        signals,
        lambda x:
            x["symbol"]
    )

    stock_rankable = []

    for (
        symbol,
        stats
    ) in stock_groups.items():

        if stats["count"] >= 3:

            stock_rankable.append({

                "symbol":
                    symbol,

                "count":
                    stats["count"],

                "average_excess_return_20d":
                    stats[
                        "average_excess_return_20d"
                    ],

                "median_excess_return_20d":
                    stats[
                        "median_excess_return_20d"
                    ],

                "beat_spy_rate_20d":
                    stats[
                        "beat_spy_rate_20d"
                    ]
            })

    stock_rankable.sort(
        key=lambda x:
            x[
                "average_excess_return_20d"
            ],
        reverse=True
    )

    best_stocks = (
        stock_rankable[:10]
    )

    worst_stocks = (
        sorted(
            stock_rankable,
            key=lambda x:
                x[
                    "average_excess_return_20d"
                ]
        )[:10]
    )

    # --------------------------------------------------------
    # OUTLIER / TRIMMED MEAN ANALYSIS
    # --------------------------------------------------------

    excess_values = sorted(
        [
            x["excess_return_20d"]
            for x in signals
        ]
    )

    trimmed_mean_excess = None

    if len(excess_values) >= 20:

        trim_count = max(
            1,
            int(
                len(excess_values)
                * 0.05
            )
        )

        trimmed_values = (
            excess_values[
                trim_count:
                len(excess_values)
                - trim_count
            ]
        )

        if trimmed_values:

            trimmed_mean_excess = round(
                sum(trimmed_values)
                / len(trimmed_values),
                2
            )

    # --------------------------------------------------------
    # TOP / BOTTOM OUTLIERS
    # --------------------------------------------------------

    sorted_signals = sorted(
        signals,
        key=lambda x:
            x["excess_return_20d"],
        reverse=True
    )

    top_outliers = [

        {
            "symbol":
                x["symbol"],

            "signal_date":
                x["signal_date"],

            "score":
                x["score"],

            "return_20d":
                x["return_20d"],

            "spy_return_20d":
                x["spy_return_20d"],

            "excess_return_20d":
                x["excess_return_20d"]
        }

        for x in sorted_signals[:10]
    ]

    bottom_outliers = [

        {
            "symbol":
                x["symbol"],

            "signal_date":
                x["signal_date"],

            "score":
                x["score"],

            "return_20d":
                x["return_20d"],

            "spy_return_20d":
                x["spy_return_20d"],

            "excess_return_20d":
                x["excess_return_20d"]
        }

        for x in sorted_signals[-10:]
    ]

    bottom_outliers.reverse()

    # --------------------------------------------------------
    # RETURN COMPACT DIAGNOSTIC RESULT
    #
    # Important:
    # We intentionally do NOT return all 300+
    # individual signals.
    # This keeps response smaller and faster.
    # --------------------------------------------------------

    return {

        "engine":
            "V2 Stage 4B",

        "purpose":
            "Single-year frozen C1 diagnostic",

        "model":
            V2_CANDIDATE_NAME,

        "model_status":
            "FROZEN",

        "threshold":
            75,

        "year":
            year,

        "start_date":
            start_date,

        "end_date":
            end_date,

        "universe_stock_count":
            len(V2_UNIVERSE),

        "successful_stock_count":
            result.get(
                "successful_stock_count",
                0
            ),

        "error_count":
            result.get(
                "error_count",
                0
            ),

        "errors":
            result.get(
                "errors",
                []
            ),

        "overall":
            result.get(
                "summary",
                {}
            ),

        "trimmed_mean_excess_return_20d":
            trimmed_mean_excess,

        "score_buckets":
            score_groups,

        "distance_ema20_buckets":
            distance_groups,

        "rsi14_buckets":
            rsi_groups,

        "momentum20_buckets":
            momentum_groups,

        "ema20_vs_ema50_buckets":
            ema_trend_groups,

        "volume_ratio_buckets":
            volume_groups,

        "macd_histogram_buckets":
            macd_groups,

        "recovery_condition_count":
            recovery_groups,

        "monthly":
            monthly_groups,

        "best_stocks":
            best_stocks,

        "worst_stocks":
            worst_stocks,

        "top_outliers":
            top_outliers,

        "bottom_outliers":
            bottom_outliers
    }


# ============================================================
# SEPARATE YEAR ENDPOINTS
# ============================================================


@app.get("/v2/c1-diagnostics/2024")
async def v2_c1_diagnostics_2024():

    return await (
        v2_c1_diagnostic_single_year(
            "2024"
        )
    )


@app.get("/v2/c1-diagnostics/2025")
async def v2_c1_diagnostics_2025():

    return await (
        v2_c1_diagnostic_single_year(
            "2025"
        )
    )


@app.get("/v2/c1-diagnostics/2026")
async def v2_c1_diagnostics_2026():

    return await (
        v2_c1_diagnostic_single_year(
            "2026"
        )
    )

# ============================================================
# V2 DATA PREPARATION
# Preload historical data into RAM cache
# ============================================================

@app.get("/v2/prepare-data")
async def v2_prepare_data():

    if not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Twelve Data API key missing"
        )

    # SPY'yi önce cache'e al.
    await get_spy_data()

    already_cached = []
    downloaded = []
    errors = []

    url = "https://api.twelvedata.com/time_series"

    # Twelve Data Free:
    # SPY + en fazla 7 hisse / dakika.
    batch_size = 7

    for group_start in range(
        0,
        len(V2_UNIVERSE),
        batch_size
    ):

        group = V2_UNIVERSE[
            group_start:
            group_start + batch_size
        ]

        api_calls_this_group = 0

        for symbol in group:

            # Veri zaten RAM'deyse API çağrısı yapma.
            if symbol in STOCK_CACHE:
                already_cached.append(symbol)
                continue

            try:

                params = {
                    "symbol": symbol,
                    "interval": "1day",
                    "outputsize": 1000,
                    "apikey": TWELVE_DATA_API_KEY
                }

                async with httpx.AsyncClient(
                    timeout=20
                ) as client:

                    response = await client.get(
                        url,
                        params=params
                    )

                    data = response.json()

                if "values" not in data:

                    errors.append({
                        "symbol": symbol,
                        "error": data
                    })

                    continue

                STOCK_CACHE[symbol] = data

                downloaded.append(symbol)

                api_calls_this_group += 1

            except Exception as e:

                errors.append({
                    "symbol": symbol,
                    "error": str(e)
                })

        # Sadece gerçekten API çağrısı yaptıysak
        # ve daha işlenecek hisseler varsa bekle.
        if (
            api_calls_this_group > 0
            and group_start + batch_size
            < len(V2_UNIVERSE)
        ):
            await asyncio.sleep(60)

    return {

        "status":
            "ready"
            if len(errors) == 0
            else "completed_with_errors",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "cache_stock_count":
            sum(
                1
                for symbol in V2_UNIVERSE
                if symbol in STOCK_CACHE
            ),

        "downloaded_now_count":
            len(downloaded),

        "already_cached_count":
            len(already_cached),

        "error_count":
            len(errors),

        "downloaded_now":
            downloaded,

        "already_cached":
            already_cached,

        "errors":
            errors,

        "next_step":
            (
                "If cache_stock_count is 70, "
                "run Stage 4B diagnostics "
                "without redeploying Render."
            )
    }


# ============================================================
# RESEARCH SNAPSHOT STATUS
# ============================================================

@app.get("/v2/snapshot-status")
async def v2_snapshot_status():
    loaded = load_research_snapshot()
    return {
        "snapshot_loaded": loaded,
        "snapshot_path": str(RESEARCH_SNAPSHOT_PATH),
        "cached_stock_count": len(STOCK_CACHE),
        "spy_cached": SPY_CACHE is not None,
        "metadata": RESEARCH_SNAPSHOT_META
    }

# ============================================================
# V2 STAGE 5
# TREND vs RECOVERY RESEARCH
# ============================================================

def stage5_stats(rows):
    if not rows:
        return {
            "count": 0,
            "average_return_5d": 0,
            "average_return_10d": 0,
            "average_return_20d": 0,
            "median_return_20d": 0,
            "win_rate_20d": 0,
            "average_excess_return_20d": 0,
            "median_excess_return_20d": 0,
            "beat_spy_rate_20d": 0
        }

    returns_5d = [x["return_5d"] for x in rows]
    returns_10d = [x["return_10d"] for x in rows]
    returns_20d = [x["return_20d"] for x in rows]
    excess_20d = [x["excess_return_20d"] for x in rows]

    return {
        "count": len(rows),

        "average_return_5d": round(
            sum(returns_5d) / len(rows), 2
        ),

        "average_return_10d": round(
            sum(returns_10d) / len(rows), 2
        ),

        "average_return_20d": round(
            sum(returns_20d) / len(rows), 2
        ),

        "median_return_20d": round(
            statistics.median(returns_20d), 2
        ),

        "win_rate_20d": round(
            sum(1 for x in returns_20d if x > 0)
            / len(rows) * 100,
            1
        ),

        "average_excess_return_20d": round(
            sum(excess_20d) / len(rows), 2
        ),

        "median_excess_return_20d": round(
            statistics.median(excess_20d), 2
        ),

        "beat_spy_rate_20d": round(
            sum(1 for x in excess_20d if x > 0)
            / len(rows) * 100,
            1
        )
    }


def stage5_trimmed_mean(values, trim_fraction=0.05):
    if not values:
        return 0

    ordered = sorted(values)

    trim_count = int(
        len(ordered) * trim_fraction
    )

    if (
        trim_count == 0
        or trim_count * 2 >= len(ordered)
    ):
        trimmed = ordered
    else:
        trimmed = ordered[
            trim_count:-trim_count
        ]

    return round(
        sum(trimmed) / len(trimmed),
        2
    )


@app.get("/v2/stage5-trend-recovery")
async def stage5_trend_recovery(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(item["open"]),
            "close": float(item["close"])
        }
        for item in spy_values
    }

    all_rows = []
    errors = []

    for symbol in V2_UNIVERSE:

        try:
            # Snapshot başlangıçta STOCK_CACHE'e
            # yüklenmiş olmalı.
            if symbol not in STOCK_CACHE:
                await backtest_stock(
                    symbol,
                    threshold=75
                )

            data = STOCK_CACHE.get(symbol)

            if not data or "values" not in data:
                errors.append({
                    "symbol": symbol,
                    "error": "Historical data unavailable"
                })
                continue

            values = list(
                reversed(data["values"])
            )

            dates = [
                item["datetime"]
                for item in values
            ]

            closes = [
                float(item["close"])
                for item in values
            ]

            opens = [
                float(item["open"])
                for item in values
            ]

            volumes = [
                float(item["volume"])
                for item in values
            ]

            for i in range(
                60,
                len(closes) - 20,
                5
            ):

                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit_date = dates[i + 20]

                if signal_date < start_date:
                    continue

                if exit_date > end_date:
                    continue

                if (
                    entry_date not in spy_by_date
                    or exit_date not in spy_by_date
                ):
                    continue

                history_closes = closes[:i + 1]
                history_volumes = volumes[:i + 1]

                price = history_closes[-1]

                ema20 = calculate_ema(
                    history_closes,
                    20
                )

                ema50 = calculate_ema(
                    history_closes,
                    50
                )

                rsi14 = calculate_rsi(
                    history_closes,
                    14
                )

                _, _, macd_histogram = (
                    calculate_macd(
                        history_closes
                    )
                )

                momentum20 = (
                    (
                        price
                        - history_closes[-21]
                    )
                    / history_closes[-21]
                ) * 100

                previous_20_volumes = (
                    history_volumes[-21:-1]
                )

                avg_volume20 = (
                    sum(previous_20_volumes)
                    / 20
                )

                if avg_volume20 > 0:
                    volume_ratio = (
                        history_volumes[-1]
                        / avg_volume20
                    )
                else:
                    volume_ratio = 0

                distance_ema20 = (
                    (price - ema20)
                    / ema20
                ) * 100

                ema20_vs_ema50 = (
                    (ema20 - ema50)
                    / ema50
                ) * 100

                macd_histogram_pct = (
                    macd_histogram
                    / price
                ) * 100

                entry_price = opens[i + 1]

                return_5d = (
                    (
                        closes[i + 5]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_10d = (
                    (
                        closes[i + 10]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_20d = (
                    (
                        closes[i + 20]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                spy_entry = (
                    spy_by_date[
                        entry_date
                    ]["open"]
                )

                spy_exit = (
                    spy_by_date[
                        exit_date
                    ]["close"]
                )

                spy_return_20d = (
                    (spy_exit - spy_entry)
                    / spy_entry
                ) * 100

                excess_return_20d = (
                    return_20d
                    - spy_return_20d
                )

                # --------------------------------
                # TREND CONDITIONS
                # --------------------------------

                trend_conditions = {
                    "price_above_ema20":
                        price > ema20,

                    "ema20_above_ema50":
                        ema20 > ema50,

                    "momentum_positive":
                        momentum20 > 0,

                    "momentum_5_to_15":
                        5 <= momentum20 <= 15,

                    "rsi_50_to_70":
                        50 <= rsi14 <= 70,

                    "macd_hist_positive":
                        macd_histogram > 0
                }

                trend_count = sum(
                    1
                    for condition
                    in trend_conditions.values()
                    if condition
                )

                # --------------------------------
                # RECOVERY CONDITIONS
                # --------------------------------

                recovery_conditions = {
                    "below_ema20":
                        distance_ema20 < 0,

                    "far_below_ema20":
                        distance_ema20 <= -1.7830,

                    "rsi_below_40":
                        rsi14 <= 40.2119,

                    "momentum_negative":
                        momentum20 < 0,

                    "momentum_very_negative":
                        momentum20 <= -3.6834,

                    "macd_negative":
                        macd_histogram < 0
                }

                recovery_count = sum(
                    1
                    for condition
                    in recovery_conditions.values()
                    if condition
                )

                # --------------------------------
                # EXCLUSIVE RESEARCH REGIMES
                # --------------------------------

                if (
                    trend_count >= 5
                    and recovery_count <= 1
                ):
                    regime = "TREND"

                elif (
                    recovery_count >= 4
                    and trend_count <= 2
                ):
                    regime = "RECOVERY"

                else:
                    regime = "MIXED"

                all_rows.append({
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_date": exit_date,

                    "regime": regime,

                    "trend_count":
                        trend_count,

                    "recovery_count":
                        recovery_count,

                    "return_5d":
                        return_5d,

                    "return_10d":
                        return_10d,

                    "return_20d":
                        return_20d,

                    "spy_return_20d":
                        spy_return_20d,

                    "excess_return_20d":
                        excess_return_20d,

                    "distance_ema20":
                        distance_ema20,

                    "ema20_vs_ema50":
                        ema20_vs_ema50,

                    "rsi14":
                        rsi14,

                    "momentum20":
                        momentum20,

                    "volume_ratio":
                        volume_ratio,

                    "macd_histogram_pct":
                        macd_histogram_pct
                })

        except Exception as exc:
            errors.append({
                "symbol": symbol,
                "error": str(exc)
            })

    # ========================================================
    # MAIN REGIME GROUPS
    # ========================================================

    trend_rows = [
        x for x in all_rows
        if x["regime"] == "TREND"
    ]

    recovery_rows = [
        x for x in all_rows
        if x["regime"] == "RECOVERY"
    ]

    mixed_rows = [
        x for x in all_rows
        if x["regime"] == "MIXED"
    ]

    # ========================================================
    # YEARLY RESULTS
    # ========================================================

    yearly = {}

    for year in ["2024", "2025", "2026"]:

        year_rows = [
            x for x in all_rows
            if x["signal_date"].startswith(year)
        ]

        yearly[year] = {
            "ALL":
                stage5_stats(
                    year_rows
                ),

            "TREND":
                stage5_stats([
                    x for x in year_rows
                    if x["regime"] == "TREND"
                ]),

            "RECOVERY":
                stage5_stats([
                    x for x in year_rows
                    if x["regime"] == "RECOVERY"
                ]),

            "MIXED":
                stage5_stats([
                    x for x in year_rows
                    if x["regime"] == "MIXED"
                ])
        }

    # ========================================================
    # TREND CONDITION COUNTS
    # ========================================================

    trend_count_results = {}

    for count in range(0, 7):

        rows = [
            x for x in all_rows
            if x["trend_count"] == count
        ]

        trend_count_results[str(count)] = (
            stage5_stats(rows)
        )

    # ========================================================
    # RECOVERY CONDITION COUNTS
    # ========================================================

    recovery_count_results = {}

    for count in range(0, 7):

        rows = [
            x for x in all_rows
            if x["recovery_count"] == count
        ]

        recovery_count_results[str(count)] = (
            stage5_stats(rows)
        )

    # ========================================================
    # ROBUSTNESS
    # ========================================================

    trend_excess = [
        x["excess_return_20d"]
        for x in trend_rows
    ]

    recovery_excess = [
        x["excess_return_20d"]
        for x in recovery_rows
    ]

    mixed_excess = [
        x["excess_return_20d"]
        for x in mixed_rows
    ]

    return {
        "engine":
            "V2 Stage 5 Trend vs Recovery Research",

        "purpose":
            (
                "Separate continuation and "
                "reversal structures before "
                "designing C2"
            ),

        "model_status":
            "RESEARCH_ONLY",

        "sampling":
            "Every 5 trading days",

        "entry":
            "Next trading day open",

        "target":
            "5d / 10d / 20d forward return",

        "benchmark":
            "SPY same entry and 20d exit",

        "start_date":
            start_date,

        "end_date":
            end_date,

        "universe_stock_count":
            len(V2_UNIVERSE),

        "observation_count":
            len(all_rows),

        "error_count":
            len(errors),

        "errors":
            errors,

        "overall": {
            "ALL":
                stage5_stats(all_rows),

            "TREND":
                stage5_stats(trend_rows),

            "RECOVERY":
                stage5_stats(recovery_rows),

            "MIXED":
                stage5_stats(mixed_rows)
        },

        "trimmed_mean_excess_20d": {
            "TREND":
                stage5_trimmed_mean(
                    trend_excess
                ),

            "RECOVERY":
                stage5_trimmed_mean(
                    recovery_excess
                ),

            "MIXED":
                stage5_trimmed_mean(
                    mixed_excess
                )
        },

        "yearly":
            yearly,

        "trend_condition_count":
            trend_count_results,

        "recovery_condition_count":
            recovery_count_results
    }

# ============================================================
# V2 STAGE 5B
# YEAR-BY-YEAR TREND / RECOVERY CONDITION DIAGNOSTICS
# ============================================================

@app.get("/v2/stage5b-yearly-counts")
async def stage5b_yearly_counts(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(item["open"]),
            "close": float(item["close"])
        }
        for item in spy_values
    }

    all_rows = []
    errors = []

    for symbol in V2_UNIVERSE:

        try:
            if symbol not in STOCK_CACHE:
                await backtest_stock(
                    symbol,
                    threshold=75
                )

            data = STOCK_CACHE.get(symbol)

            if not data or "values" not in data:
                errors.append({
                    "symbol": symbol,
                    "error": "Historical data unavailable"
                })
                continue

            values = list(
                reversed(data["values"])
            )

            dates = [
                item["datetime"]
                for item in values
            ]

            closes = [
                float(item["close"])
                for item in values
            ]

            opens = [
                float(item["open"])
                for item in values
            ]

            volumes = [
                float(item["volume"])
                for item in values
            ]

            for i in range(
                60,
                len(closes) - 20,
                5
            ):

                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit_date = dates[i + 20]

                if signal_date < start_date:
                    continue

                if exit_date > end_date:
                    continue

                if (
                    entry_date not in spy_by_date
                    or exit_date not in spy_by_date
                ):
                    continue

                history_closes = closes[:i + 1]
                history_volumes = volumes[:i + 1]

                price = history_closes[-1]

                ema20 = calculate_ema(
                    history_closes,
                    20
                )

                ema50 = calculate_ema(
                    history_closes,
                    50
                )

                rsi14 = calculate_rsi(
                    history_closes,
                    14
                )

                _, _, macd_histogram = (
                    calculate_macd(
                        history_closes
                    )
                )

                momentum20 = (
                    (
                        price
                        - history_closes[-21]
                    )
                    / history_closes[-21]
                ) * 100

                previous_20_volumes = (
                    history_volumes[-21:-1]
                )

                avg_volume20 = (
                    sum(previous_20_volumes)
                    / 20
                )

                if avg_volume20 > 0:
                    volume_ratio = (
                        history_volumes[-1]
                        / avg_volume20
                    )
                else:
                    volume_ratio = 0

                distance_ema20 = (
                    (price - ema20)
                    / ema20
                ) * 100

                # ----------------------------------------
                # SAME TREND CONDITIONS AS STAGE 5
                # ----------------------------------------

                trend_conditions = {
                    "price_above_ema20":
                        price > ema20,

                    "ema20_above_ema50":
                        ema20 > ema50,

                    "momentum_positive":
                        momentum20 > 0,

                    "momentum_5_to_15":
                        5 <= momentum20 <= 15,

                    "rsi_50_to_70":
                        50 <= rsi14 <= 70,

                    "macd_hist_positive":
                        macd_histogram > 0
                }

                trend_count = sum(
                    1
                    for condition
                    in trend_conditions.values()
                    if condition
                )

                # ----------------------------------------
                # SAME RECOVERY CONDITIONS AS STAGE 5
                # ----------------------------------------

                recovery_conditions = {
                    "below_ema20":
                        distance_ema20 < 0,

                    "far_below_ema20":
                        distance_ema20 <= -1.7830,

                    "rsi_below_40":
                        rsi14 <= 40.2119,

                    "momentum_negative":
                        momentum20 < 0,

                    "momentum_very_negative":
                        momentum20 <= -3.6834,

                    "macd_negative":
                        macd_histogram < 0
                }

                recovery_count = sum(
                    1
                    for condition
                    in recovery_conditions.values()
                    if condition
                )

                entry_price = opens[i + 1]

                return_5d = (
                    (
                        closes[i + 5]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_10d = (
                    (
                        closes[i + 10]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_20d = (
                    (
                        closes[i + 20]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                spy_entry_price = (
                    spy_by_date[
                        entry_date
                    ]["open"]
                )

                spy_exit_price = (
                    spy_by_date[
                        exit_date
                    ]["close"]
                )

                spy_return_20d = (
                    (
                        spy_exit_price
                        - spy_entry_price
                    )
                    / spy_entry_price
                ) * 100

                excess_return_20d = (
                    return_20d
                    - spy_return_20d
                )

                all_rows.append({
                    "symbol":
                        symbol,

                    "signal_date":
                        signal_date,

                    "trend_count":
                        trend_count,

                    "recovery_count":
                        recovery_count,

                    "return_5d":
                        return_5d,

                    "return_10d":
                        return_10d,

                    "return_20d":
                        return_20d,

                    "excess_return_20d":
                        excess_return_20d,

                    "volume_ratio":
                        volume_ratio
                })

        except Exception as exc:

            errors.append({
                "symbol": symbol,
                "error": str(exc)
            })

    # ========================================================
    # YEAR-BY-YEAR CONDITION COUNT RESULTS
    # ========================================================

    yearly = {}

    for year in [
        "2024",
        "2025",
        "2026"
    ]:

        year_rows = [
            row
            for row in all_rows
            if row["signal_date"].startswith(
                year
            )
        ]

        trend_counts = {}
        recovery_counts = {}

        for count in range(0, 7):

            trend_rows = [
                row
                for row in year_rows
                if row["trend_count"] == count
            ]

            recovery_rows = [
                row
                for row in year_rows
                if row["recovery_count"] == count
            ]

            trend_counts[str(count)] = (
                stage5_stats(
                    trend_rows
                )
            )

            recovery_counts[str(count)] = (
                stage5_stats(
                    recovery_rows
                )
            )

        yearly[year] = {
            "observation_count":
                len(year_rows),

            "trend_condition_count":
                trend_counts,

            "recovery_condition_count":
                recovery_counts
        }

    # ========================================================
    # SPECIAL 6/6 COMPARISON
    # ========================================================

    trend_6_by_year = {}
    recovery_6_by_year = {}

    for year in [
        "2024",
        "2025",
        "2026"
    ]:

        trend_rows = [
            row
            for row in all_rows
            if (
                row["signal_date"].startswith(
                    year
                )
                and row["trend_count"] == 6
            )
        ]

        recovery_rows = [
            row
            for row in all_rows
            if (
                row["signal_date"].startswith(
                    year
                )
                and row["recovery_count"] == 6
            )
        ]

        trend_6_by_year[year] = (
            stage5_stats(
                trend_rows
            )
        )

        recovery_6_by_year[year] = (
            stage5_stats(
                recovery_rows
            )
        )

    # ========================================================
    # RETURN
    # ========================================================

    return {
        "engine":
            "V2 Stage 5B Yearly Condition Diagnostics",

        "model_status":
            "RESEARCH_ONLY",

        "purpose":
            (
                "Test year-by-year stability "
                "of Stage 5 trend and recovery "
                "condition counts"
            ),

        "start_date":
            start_date,

        "end_date":
            end_date,

        "sampling":
            "Every 5 trading days",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "observation_count":
            len(all_rows),

        "error_count":
            len(errors),

        "errors":
            errors,

        "trend_6_of_6_by_year":
            trend_6_by_year,

        "recovery_6_of_6_by_year":
            recovery_6_by_year,

        "yearly":
            yearly
    }

# ============================================================
# V2 STAGE 6
# TREND CANDIDATE ABLATION STUDY
# ============================================================

@app.get("/v2/stage6-trend-ablation")
async def stage6_trend_ablation(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):

    spy_values = await get_spy_data()

    spy_by_date = {
        item["datetime"]: {
            "open": float(item["open"]),
            "close": float(item["close"])
        }
        for item in spy_values
    }

    all_rows = []
    errors = []

    for symbol in V2_UNIVERSE:

        try:
            if symbol not in STOCK_CACHE:
                await backtest_stock(
                    symbol,
                    threshold=75
                )

            data = STOCK_CACHE.get(symbol)

            if not data or "values" not in data:
                errors.append({
                    "symbol": symbol,
                    "error": "Historical data unavailable"
                })
                continue

            values = list(
                reversed(data["values"])
            )

            dates = [
                item["datetime"]
                for item in values
            ]

            closes = [
                float(item["close"])
                for item in values
            ]

            opens = [
                float(item["open"])
                for item in values
            ]

            for i in range(
                60,
                len(closes) - 20,
                5
            ):

                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit_date = dates[i + 20]

                if signal_date < start_date:
                    continue

                if exit_date > end_date:
                    continue

                if (
                    entry_date not in spy_by_date
                    or exit_date not in spy_by_date
                ):
                    continue

                history = closes[:i + 1]

                price = history[-1]

                ema20 = calculate_ema(
                    history,
                    20
                )

                ema50 = calculate_ema(
                    history,
                    50
                )

                rsi14 = calculate_rsi(
                    history,
                    14
                )

                _, _, macd_histogram = (
                    calculate_macd(
                        history
                    )
                )

                momentum20 = (
                    (
                        price
                        - history[-21]
                    )
                    / history[-21]
                ) * 100

                # ==========================================
                # FIVE INDEPENDENT TREND CONDITIONS
                # ==========================================

                conditions = {
                    "PRICE_ABOVE_EMA20":
                        price > ema20,

                    "EMA20_ABOVE_EMA50":
                        ema20 > ema50,

                    "MOMENTUM_5_TO_15":
                        5 <= momentum20 <= 15,

                    "RSI_50_TO_70":
                        50 <= rsi14 <= 70,

                    "MACD_POSITIVE":
                        macd_histogram > 0
                }

                # ==========================================
                # FUTURE RETURNS
                # ==========================================

                entry_price = opens[i + 1]

                return_5d = (
                    (
                        closes[i + 5]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_10d = (
                    (
                        closes[i + 10]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_20d = (
                    (
                        closes[i + 20]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                spy_entry_price = (
                    spy_by_date[
                        entry_date
                    ]["open"]
                )

                spy_exit_price = (
                    spy_by_date[
                        exit_date
                    ]["close"]
                )

                spy_return_20d = (
                    (
                        spy_exit_price
                        - spy_entry_price
                    )
                    / spy_entry_price
                ) * 100

                excess_return_20d = (
                    return_20d
                    - spy_return_20d
                )

                all_rows.append({
                    "symbol":
                        symbol,

                    "signal_date":
                        signal_date,

                    "conditions":
                        conditions,

                    "return_5d":
                        return_5d,

                    "return_10d":
                        return_10d,

                    "return_20d":
                        return_20d,

                    "excess_return_20d":
                        excess_return_20d
                })

        except Exception as exc:
            errors.append({
                "symbol": symbol,
                "error": str(exc)
            })

    # ========================================================
    # MODEL DEFINITIONS
    #
    # FULL = all five independent conditions.
    #
    # Ablation variants remove ONE condition while all
    # remaining conditions must still be true.
    # ========================================================

    model_requirements = {

        "FULL": [
            "PRICE_ABOVE_EMA20",
            "EMA20_ABOVE_EMA50",
            "MOMENTUM_5_TO_15",
            "RSI_50_TO_70",
            "MACD_POSITIVE"
        ],

        "NO_PRICE_EMA": [
            "EMA20_ABOVE_EMA50",
            "MOMENTUM_5_TO_15",
            "RSI_50_TO_70",
            "MACD_POSITIVE"
        ],

        "NO_EMA_TREND": [
            "PRICE_ABOVE_EMA20",
            "MOMENTUM_5_TO_15",
            "RSI_50_TO_70",
            "MACD_POSITIVE"
        ],

        "NO_MOMENTUM_BAND": [
            "PRICE_ABOVE_EMA20",
            "EMA20_ABOVE_EMA50",
            "RSI_50_TO_70",
            "MACD_POSITIVE"
        ],

        "NO_RSI": [
            "PRICE_ABOVE_EMA20",
            "EMA20_ABOVE_EMA50",
            "MOMENTUM_5_TO_15",
            "MACD_POSITIVE"
        ],

        "NO_MACD": [
            "PRICE_ABOVE_EMA20",
            "EMA20_ABOVE_EMA50",
            "MOMENTUM_5_TO_15",
            "RSI_50_TO_70"
        ]
    }

    # ========================================================
    # SELECT ROWS FOR EACH VARIANT
    # ========================================================

    variant_rows = {}

    for model_name, required_conditions in (
        model_requirements.items()
    ):

        selected = []

        for row in all_rows:

            if all(
                row["conditions"][condition]
                for condition
                in required_conditions
            ):
                selected.append(row)

        variant_rows[model_name] = selected

    # ========================================================
    # OVERALL RESULTS
    # ========================================================

    overall = {}

    for model_name, rows in (
        variant_rows.items()
    ):

        stats = stage5_stats(rows)

        excess_values = [
            row["excess_return_20d"]
            for row in rows
        ]

        stats[
            "trimmed_mean_excess_20d"
        ] = stage5_trimmed_mean(
            excess_values
        )

        overall[model_name] = stats

    # ========================================================
    # YEAR-BY-YEAR RESULTS
    # ========================================================

    yearly = {}

    for year in [
        "2024",
        "2025",
        "2026"
    ]:

        yearly[year] = {}

        for model_name, rows in (
            variant_rows.items()
        ):

            year_rows = [
                row
                for row in rows
                if row[
                    "signal_date"
                ].startswith(year)
            ]

            stats = stage5_stats(
                year_rows
            )

            excess_values = [
                row["excess_return_20d"]
                for row in year_rows
            ]

            stats[
                "trimmed_mean_excess_20d"
            ] = stage5_trimmed_mean(
                excess_values
            )

            yearly[year][
                model_name
            ] = stats

    # ========================================================
    # MONTHLY STABILITY
    # ========================================================

    monthly_stability = {}

    for model_name, rows in (
        variant_rows.items()
    ):

        months = {}

        for row in rows:

            month = row[
                "signal_date"
            ][:7]

            if month not in months:
                months[month] = []

            months[month].append(row)

        positive_excess_months = 0
        positive_median_excess_months = 0
        beat_spy_over_50_months = 0

        month_results = {}

        for month, month_rows in sorted(
            months.items()
        ):

            stats = stage5_stats(
                month_rows
            )

            month_results[
                month
            ] = stats

            if (
                stats[
                    "average_excess_return_20d"
                ] > 0
            ):
                positive_excess_months += 1

            if (
                stats[
                    "median_excess_return_20d"
                ] > 0
            ):
                positive_median_excess_months += 1

            if (
                stats[
                    "beat_spy_rate_20d"
                ] > 50
            ):
                beat_spy_over_50_months += 1

        monthly_stability[
            model_name
        ] = {
            "months_with_observations":
                len(months),

            "positive_average_excess_months":
                positive_excess_months,

            "positive_median_excess_months":
                positive_median_excess_months,

            "beat_spy_over_50_months":
                beat_spy_over_50_months,

            "months":
                month_results
        }

    # ========================================================
    # RETURN
    # ========================================================

    return {
        "engine":
            "V2 Stage 6 Trend Ablation Study",

        "model_status":
            "RESEARCH_ONLY",

        "purpose":
            (
                "Test whether each component "
                "of the five-condition trend "
                "candidate adds useful information"
            ),

        "start_date":
            start_date,

        "end_date":
            end_date,

        "sampling":
            "Every 5 trading days",

        "entry":
            "Next trading day open",

        "benchmark":
            "SPY same entry and 20d exit",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "observation_count":
            len(all_rows),

        "error_count":
            len(errors),

        "errors":
            errors,

        "condition_definitions": {
            "PRICE_ABOVE_EMA20":
                "Price > EMA20",

            "EMA20_ABOVE_EMA50":
                "EMA20 > EMA50",

            "MOMENTUM_5_TO_15":
                "20-day momentum between 5% and 15%",

            "RSI_50_TO_70":
                "RSI14 between 50 and 70",

            "MACD_POSITIVE":
                "MACD histogram > 0"
        },

        "variant_meaning": {
            "FULL":
                "All five conditions required",

            "NO_PRICE_EMA":
                "All except Price > EMA20",

            "NO_EMA_TREND":
                "All except EMA20 > EMA50",

            "NO_MOMENTUM_BAND":
                "All except 5%-15% momentum band",

            "NO_RSI":
                "All except RSI 50-70",

            "NO_MACD":
                "All except positive MACD histogram"
        },

        "overall":
            overall,

        "yearly":
            yearly,

        "monthly_stability":
            monthly_stability
    }

# ============================================================
# V2 STAGE 7
# FULL TREND SIGNAL vs SPY MARKET REGIME STUDY
# ============================================================

@app.get("/v2/stage7-market-regime")
async def stage7_market_regime(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):

    spy_values = await get_spy_data()

    # SPY chronological order
    spy_dates = [
        item["datetime"]
        for item in spy_values
    ]

    spy_closes = [
        float(item["close"])
        for item in spy_values
    ]

    spy_opens = [
        float(item["open"])
        for item in spy_values
    ]

    spy_index_by_date = {
        date: i
        for i, date in enumerate(spy_dates)
    }

    all_signals = []
    errors = []

    # ========================================================
    # FIND FULL TREND SIGNALS
    # ========================================================

    for symbol in V2_UNIVERSE:

        try:
            if symbol not in STOCK_CACHE:
                await backtest_stock(
                    symbol,
                    threshold=75
                )

            data = STOCK_CACHE.get(symbol)

            if not data or "values" not in data:
                errors.append({
                    "symbol": symbol,
                    "error": "Historical data unavailable"
                })
                continue

            values = list(
                reversed(data["values"])
            )

            dates = [
                item["datetime"]
                for item in values
            ]

            closes = [
                float(item["close"])
                for item in values
            ]

            opens = [
                float(item["open"])
                for item in values
            ]

            for i in range(
                60,
                len(closes) - 20,
                5
            ):

                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit_date = dates[i + 20]

                if signal_date < start_date:
                    continue

                if exit_date > end_date:
                    continue

                if (
                    signal_date not in spy_index_by_date
                    or entry_date not in spy_index_by_date
                    or exit_date not in spy_index_by_date
                ):
                    continue

                history = closes[:i + 1]

                price = history[-1]

                ema20 = calculate_ema(
                    history,
                    20
                )

                ema50 = calculate_ema(
                    history,
                    50
                )

                rsi14 = calculate_rsi(
                    history,
                    14
                )

                _, _, macd_histogram = (
                    calculate_macd(
                        history
                    )
                )

                momentum20 = (
                    (
                        price
                        - history[-21]
                    )
                    / history[-21]
                ) * 100

                # ==========================================
                # FROZEN FULL TREND CANDIDATE
                # EXACTLY SAME FIVE CONDITIONS AS STAGE 6
                # ==========================================

                full_trend = (
                    price > ema20
                    and ema20 > ema50
                    and 5 <= momentum20 <= 15
                    and 50 <= rsi14 <= 70
                    and macd_histogram > 0
                )

                if not full_trend:
                    continue

                spy_i = spy_index_by_date[
                    signal_date
                ]

                spy_entry_i = spy_index_by_date[
                    entry_date
                ]

                spy_exit_i = spy_index_by_date[
                    exit_date
                ]

                # Need enough SPY history
                if spy_i < 60:
                    continue

                spy_history = (
                    spy_closes[:spy_i + 1]
                )

                spy_price = (
                    spy_history[-1]
                )

                spy_ema20 = calculate_ema(
                    spy_history,
                    20
                )

                spy_ema50 = calculate_ema(
                    spy_history,
                    50
                )

                spy_momentum20 = (
                    (
                        spy_price
                        - spy_history[-21]
                    )
                    / spy_history[-21]
                ) * 100

                spy_distance_ema20 = (
                    (
                        spy_price
                        - spy_ema20
                    )
                    / spy_ema20
                ) * 100

                spy_ema20_vs_ema50 = (
                    (
                        spy_ema20
                        - spy_ema50
                    )
                    / spy_ema50
                ) * 100

                # ==========================================
                # SPY REGIME LABELS
                # DESCRIPTIVE ONLY - NOT FILTERS
                # ==========================================

                spy_above_ema20 = (
                    spy_price > spy_ema20
                )

                spy_ema20_above_ema50 = (
                    spy_ema20 > spy_ema50
                )

                spy_momentum_positive = (
                    spy_momentum20 > 0
                )

                # Broad structural regime
                if (
                    spy_above_ema20
                    and spy_ema20_above_ema50
                    and spy_momentum_positive
                ):
                    market_regime = (
                        "STRONG_UPTREND"
                    )

                elif (
                    not spy_above_ema20
                    and not spy_ema20_above_ema50
                    and not spy_momentum_positive
                ):
                    market_regime = (
                        "WEAK_DOWNTREND"
                    )

                else:
                    market_regime = (
                        "MIXED"
                    )

                # Momentum buckets
                if spy_momentum20 < 0:
                    momentum_regime = (
                        "NEGATIVE"
                    )

                elif spy_momentum20 < 5:
                    momentum_regime = (
                        "POSITIVE_0_TO_5"
                    )

                else:
                    momentum_regime = (
                        "POSITIVE_5_PLUS"
                    )

                # Distance from EMA20 buckets
                if spy_distance_ema20 < -2:
                    distance_regime = (
                        "BELOW_EMA20_GT_2"
                    )

                elif spy_distance_ema20 < 0:
                    distance_regime = (
                        "BELOW_EMA20_0_TO_2"
                    )

                elif spy_distance_ema20 <= 2:
                    distance_regime = (
                        "ABOVE_EMA20_0_TO_2"
                    )

                else:
                    distance_regime = (
                        "ABOVE_EMA20_GT_2"
                    )

                # EMA trend-strength buckets
                if spy_ema20_vs_ema50 < 0:
                    ema_regime = (
                        "EMA20_BELOW_EMA50"
                    )

                elif spy_ema20_vs_ema50 <= 2:
                    ema_regime = (
                        "EMA20_ABOVE_EMA50_0_TO_2"
                    )

                else:
                    ema_regime = (
                        "EMA20_ABOVE_EMA50_GT_2"
                    )

                # ==========================================
                # FUTURE STOCK RETURN
                # ==========================================

                entry_price = (
                    opens[i + 1]
                )

                return_5d = (
                    (
                        closes[i + 5]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_10d = (
                    (
                        closes[i + 10]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                return_20d = (
                    (
                        closes[i + 20]
                        - entry_price
                    )
                    / entry_price
                ) * 100

                # ==========================================
                # FUTURE SPY RETURN
                # ==========================================

                spy_entry_price = (
                    spy_opens[
                        spy_entry_i
                    ]
                )

                spy_exit_price = (
                    spy_closes[
                        spy_exit_i
                    ]
                )

                spy_return_20d = (
                    (
                        spy_exit_price
                        - spy_entry_price
                    )
                    / spy_entry_price
                ) * 100

                excess_return_20d = (
                    return_20d
                    - spy_return_20d
                )

                all_signals.append({
                    "symbol":
                        symbol,

                    "signal_date":
                        signal_date,

                    "return_5d":
                        return_5d,

                    "return_10d":
                        return_10d,

                    "return_20d":
                        return_20d,

                    "spy_return_20d":
                        spy_return_20d,

                    "excess_return_20d":
                        excess_return_20d,

                    "market_regime":
                        market_regime,

                    "momentum_regime":
                        momentum_regime,

                    "distance_regime":
                        distance_regime,

                    "ema_regime":
                        ema_regime,

                    "spy_momentum20":
                        spy_momentum20,

                    "spy_distance_ema20":
                        spy_distance_ema20,

                    "spy_ema20_vs_ema50":
                        spy_ema20_vs_ema50
                })

        except Exception as exc:

            errors.append({
                "symbol": symbol,
                "error": str(exc)
            })

    # ========================================================
    # GROUP HELPER
    # ========================================================

    def stage7_group(rows, field):

        groups = {}

        for row in rows:

            key = row[field]

            if key not in groups:
                groups[key] = []

            groups[key].append(row)

        result = {}

        for key, group_rows in sorted(
            groups.items()
        ):

            stats = stage5_stats(
                group_rows
            )

            excess_values = [
                row[
                    "excess_return_20d"
                ]
                for row in group_rows
            ]

            stats[
                "trimmed_mean_excess_20d"
            ] = stage5_trimmed_mean(
                excess_values
            )

            result[key] = stats

        return result

    # ========================================================
    # OVERALL
    # ========================================================

    overall_stats = stage5_stats(
        all_signals
    )

    overall_stats[
        "trimmed_mean_excess_20d"
    ] = stage5_trimmed_mean(
        [
            row["excess_return_20d"]
            for row in all_signals
        ]
    )

    # ========================================================
    # YEARLY REGIME RESULTS
    # ========================================================

    yearly = {}

    for year in [
        "2024",
        "2025",
        "2026"
    ]:

        year_rows = [
            row
            for row in all_signals
            if row[
                "signal_date"
            ].startswith(year)
        ]

        yearly[year] = {
            "signal_count":
                len(year_rows),

            "overall":
                stage5_stats(
                    year_rows
                ),

            "market_regime":
                stage7_group(
                    year_rows,
                    "market_regime"
                ),

            "spy_momentum_regime":
                stage7_group(
                    year_rows,
                    "momentum_regime"
                ),

            "spy_distance_regime":
                stage7_group(
                    year_rows,
                    "distance_regime"
                ),

            "spy_ema_regime":
                stage7_group(
                    year_rows,
                    "ema_regime"
                )
        }

    # ========================================================
    # RETURN
    # ========================================================

    return {
        "engine":
            "V2 Stage 7 Market Regime Study",

        "model_status":
            "RESEARCH_ONLY",

        "purpose":
            (
                "Measure FULL trend candidate "
                "performance across SPY market "
                "conditions without adding filters"
            ),

        "full_trend_definition": {
            "price_above_ema20":
                True,

            "ema20_above_ema50":
                True,

            "momentum20":
                "5% to 15%",

            "rsi14":
                "50 to 70",

            "macd_histogram":
                "greater than 0"
        },

        "start_date":
            start_date,

        "end_date":
            end_date,

        "sampling":
            "Every 5 trading days",

        "entry":
            "Next trading day open",

        "benchmark":
            "SPY same entry and 20d exit",

        "universe_stock_count":
            len(V2_UNIVERSE),

        "signal_count":
            len(all_signals),

        "error_count":
            len(errors),

        "errors":
            errors,

        "overall":
            overall_stats,

        "market_regime":
            stage7_group(
                all_signals,
                "market_regime"
            ),

        "spy_momentum_regime":
            stage7_group(
                all_signals,
                "momentum_regime"
            ),

        "spy_distance_regime":
            stage7_group(
                all_signals,
                "distance_regime"
            ),

        "spy_ema_regime":
            stage7_group(
                all_signals,
                "ema_regime"
            ),

        "yearly":
            yearly
    }

# ============================================================
# STAGE 8
# C2-TREND-FROZEN FORWARD VALIDATION
# ============================================================

C2_FORWARD_MODEL = {
    "name": "C2-TREND-FROZEN",
    "version": "1.0",
    "frozen_at": "2026-09-19",
    "rules": {
        "price_above_ema20": True,
        "ema20_above_ema50": True,
        "momentum20_min": 5.0,
        "momentum20_max": 15.0,
        "rsi14_min": 50.0,
        "rsi14_max": 70.0,
        "macd_histogram_positive": True
    }
}


def c2_forward_metrics(closes):

    if len(closes) < 60:
        return None

    price = closes[-1]

    ema20 = calculate_ema(
        closes,
        20
    )

    ema50 = calculate_ema(
        closes,
        50
    )

    rsi14 = calculate_rsi(
        closes,
        14
    )

    _, _, macd_histogram = (
        calculate_macd(
            closes
        )
    )

    momentum20 = (
        (
            price
            - closes[-21]
        )
        / closes[-21]
    ) * 100

    conditions = {
        "price_above_ema20":
            price > ema20,

        "ema20_above_ema50":
            ema20 > ema50,

        "momentum_5_to_15":
            5 <= momentum20 <= 15,

        "rsi_50_to_70":
            50 <= rsi14 <= 70,

        "macd_histogram_positive":
            macd_histogram > 0
    }

    full_trend = all(
        conditions.values()
    )

    return {
        "price":
            round(price, 4),

        "ema20":
            round(ema20, 4),

        "ema50":
            round(ema50, 4),

        "momentum20":
            round(momentum20, 4),

        "rsi14":
            round(rsi14, 4),

        "macd_histogram":
            round(
                macd_histogram,
                6
            ),

        "conditions":
            conditions,

        "full_trend":
            full_trend
    }


@app.get("/v2/stage8-forward-scan")
async def stage8_forward_scan():

    results = []
    errors = []

    for symbol in V2_UNIVERSE:

        try:

            params = {
                "symbol": symbol,
                "interval": "1day",
                "outputsize": 100,
                "apikey":
                    TWELVE_DATA_API_KEY
            }

            async with httpx.AsyncClient(
                timeout=30.0
            ) as client:

                response = await client.get(
                    "https://api.twelvedata.com/time_series",
                    params=params
                )

            data = response.json()

            if (
                "values" not in data
                or not data["values"]
            ):
                errors.append({
                    "symbol": symbol,
                    "error": data
                })
                continue

            values = list(
                reversed(
                    data["values"]
                )
            )

            dates = [
                item["datetime"]
                for item in values
            ]

            closes = [
                float(item["close"])
                for item in values
            ]

            if len(closes) < 60:
                errors.append({
                    "symbol": symbol,
                    "error":
                        "Not enough historical data"
                })
                continue

            metrics = (
                c2_forward_metrics(
                    closes
                )
            )

            previous_metrics = (
                c2_forward_metrics(
                    closes[:-1]
                )
            )

            if (
                metrics is None
                or previous_metrics is None
            ):
                continue

            current_full = (
                metrics[
                    "full_trend"
                ]
            )

            previous_full = (
                previous_metrics[
                    "full_trend"
                ]
            )

            # --------------------------------------------
            # FROZEN SIGNAL DEFINITION:
            # False -> True transition only
            # --------------------------------------------

            new_signal = (
                current_full
                and not previous_full
            )

            results.append({
                "symbol":
                    symbol,

                "signal_date":
                    dates[-1],

                "current_full_trend":
                    current_full,

                "previous_full_trend":
                    previous_full,

                "new_signal":
                    new_signal,

                "metrics":
                    metrics
            })

        except Exception as exc:

            errors.append({
                "symbol":
                    symbol,

                "error":
                    str(exc)
            })

        # Twelve Data free-plan protection.
        # 7 calls per minute keeps us below
        # the 8-credit/minute limit.
        if (
            (
                V2_UNIVERSE.index(
                    symbol
                ) + 1
            ) % 7 == 0
            and symbol
            != V2_UNIVERSE[-1]
        ):
            await asyncio.sleep(65)

    new_signals = [
        row
        for row in results
        if row["new_signal"]
    ]

    active_full_trend = [
        row
        for row in results
        if row[
            "current_full_trend"
        ]
    ]

    dates_found = [
        row["signal_date"]
        for row in results
    ]

    evaluated_date = (
        max(dates_found)
        if dates_found
        else None
    )

    return {
        "engine":
            "Stage 8 C2 Forward Scan",

        "status":
            "FORWARD_VALIDATION",

        "model":
            C2_FORWARD_MODEL,

        "evaluated_date":
            evaluated_date,

        "universe_stock_count":
            len(V2_UNIVERSE),

        "evaluated_stock_count":
            len(results),

        "active_full_trend_count":
            len(
                active_full_trend
            ),

        "new_signal_count":
            len(new_signals),

        "error_count":
            len(errors),

        "new_signals":
            new_signals,

        "active_full_trend":
            active_full_trend,

        "errors":
            errors
    }


# ============================================================
# V4 RESEARCH - C2 + SPY MARKET FILTER CANDIDATES
# Research only. Does not change C2/V3 live rules.
# ============================================================

@app.get("/v4/c2-market-filter-study")
async def v4_c2_market_filter_study(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):
    spy_values = await get_spy_data()
    spy_dates = [x["datetime"] for x in spy_values]
    spy_closes = [float(x["close"]) for x in spy_values]
    spy_opens = [float(x["open"]) for x in spy_values]
    spy_index = {d: i for i, d in enumerate(spy_dates)}

    rows = []
    errors = []

    for symbol in V2_UNIVERSE:
        try:
            if symbol not in STOCK_CACHE:
                await backtest_stock(symbol, threshold=75)

            data = STOCK_CACHE.get(symbol)
            if not data or "values" not in data:
                errors.append({"symbol": symbol, "error": "Historical data unavailable"})
                continue

            values = list(reversed(data["values"]))
            dates = [x["datetime"] for x in values]
            closes = [float(x["close"]) for x in values]
            opens = [float(x["open"]) for x in values]

            for i in range(60, len(closes) - 20, 5):
                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit_date = dates[i + 20]

                if signal_date < start_date or exit_date > end_date:
                    continue
                if signal_date not in spy_index or entry_date not in spy_index or exit_date not in spy_index:
                    continue

                history = closes[:i + 1]
                price = history[-1]
                ema20 = calculate_ema(history, 20)
                ema50 = calculate_ema(history, 50)
                rsi14 = calculate_rsi(history, 14)
                _, _, macd_hist = calculate_macd(history)
                momentum20 = ((price - history[-21]) / history[-21]) * 100

                full_c2 = (
                    price > ema20
                    and ema20 > ema50
                    and 5 <= momentum20 <= 15
                    and 50 <= rsi14 <= 70
                    and macd_hist > 0
                )
                if not full_c2:
                    continue

                si = spy_index[signal_date]
                ei = spy_index[entry_date]
                xi = spy_index[exit_date]
                if si < 50:
                    continue

                spy_hist = spy_closes[:si + 1]
                spy_price = spy_hist[-1]
                spy_ema20 = calculate_ema(spy_hist, 20)
                spy_ema50 = calculate_ema(spy_hist, 50)
                spy_mom20 = ((spy_price - spy_hist[-21]) / spy_hist[-21]) * 100
                spy_dist20 = ((spy_price - spy_ema20) / spy_ema20) * 100
                spy_ema_gap = ((spy_ema20 - spy_ema50) / spy_ema50) * 100

                strong_uptrend = spy_price > spy_ema20 and spy_ema20 > spy_ema50 and spy_mom20 > 0
                weak_downtrend = spy_price <= spy_ema20 and spy_ema20 <= spy_ema50 and spy_mom20 <= 0

                entry_price = opens[i + 1]
                ret20 = ((closes[i + 20] - entry_price) / entry_price) * 100
                spy_entry = spy_opens[ei]
                spy_ret20 = ((spy_closes[xi] - spy_entry) / spy_entry) * 100

                rows.append({
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "return_20d": ret20,
                    "excess_return_20d": ret20 - spy_ret20,
                    "spy_momentum20": spy_mom20,
                    "spy_distance_ema20": spy_dist20,
                    "spy_ema20_vs_ema50": spy_ema_gap,
                    "strong_uptrend": strong_uptrend,
                    "weak_downtrend": weak_downtrend,
                })
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    def summarize(items):
        if not items:
            return {
                "count": 0, "average_return_20d": None, "median_return_20d": None,
                "win_rate_20d": None, "average_excess_return_20d": None,
                "median_excess_return_20d": None, "beat_spy_rate_20d": None,
                "trimmed_mean_excess_20d": None
            }
        rets = [x["return_20d"] for x in items]
        excs = [x["excess_return_20d"] for x in items]
        return {
            "count": len(items),
            "average_return_20d": round(sum(rets) / len(rets), 2),
            "median_return_20d": round(statistics.median(rets), 2),
            "win_rate_20d": round(sum(x > 0 for x in rets) / len(rets) * 100, 1),
            "average_excess_return_20d": round(sum(excs) / len(excs), 2),
            "median_excess_return_20d": round(statistics.median(excs), 2),
            "beat_spy_rate_20d": round(sum(x > 0 for x in excs) / len(excs) * 100, 1),
            "trimmed_mean_excess_20d": stage5_trimmed_mean(excs),
        }

    candidates = {
        "C2_BASELINE": lambda x: True,
        "BLOCK_WEAK_DOWNTREND": lambda x: not x["weak_downtrend"],
        "BLOCK_DEEP_BELOW_EMA20": lambda x: x["spy_distance_ema20"] >= -2,
        "REQUIRE_SPY_EMA20_ABOVE_EMA50": lambda x: x["spy_ema20_vs_ema50"] >= 0,
        "REQUIRE_SPY_MOMENTUM_NONNEGATIVE": lambda x: x["spy_momentum20"] >= 0,
        "SAFE_COMBO": lambda x: (not x["weak_downtrend"]) and x["spy_distance_ema20"] >= -2,
    }

    baseline = summarize(rows)
    result = {}
    for name, rule in candidates.items():
        selected = [x for x in rows if rule(x)]
        overall = summarize(selected)
        yearly = {}
        for year in ["2024", "2025", "2026"]:
            yr = [x for x in selected if x["signal_date"].startswith(year)]
            yearly[year] = summarize(yr)

        base_excess = baseline["average_excess_return_20d"] or 0
        cand_excess = overall["average_excess_return_20d"]
        result[name] = {
            "overall": overall,
            "signal_retention_pct": round(len(selected) / len(rows) * 100, 1) if rows else 0,
            "delta_average_excess_vs_c2": round(cand_excess - base_excess, 2) if cand_excess is not None else None,
            "yearly": yearly,
        }

    # --------------------------------------------------------
    # V4 momentum threshold sensitivity
    # Robustness check around the SPY momentum >= 0 candidate.
    # This does NOT change C2/V3 live rules.
    # --------------------------------------------------------
    momentum_thresholds = [-5.0, -2.0, 0.0, 2.0, 5.0]
    momentum_sensitivity = {}

    for threshold in momentum_thresholds:
        selected = [
            x for x in rows
            if x["spy_momentum20"] >= threshold
        ]

        overall = summarize(selected)
        yearly = {}
        for year in ["2024", "2025", "2026"]:
            yr = [
                x for x in selected
                if x["signal_date"].startswith(year)
            ]
            yearly[year] = summarize(yr)

        base_excess = baseline["average_excess_return_20d"] or 0
        cand_excess = overall["average_excess_return_20d"]
        key = f"SPY_MOMENTUM_GTE_{threshold:+.0f}"
        momentum_sensitivity[key] = {
            "threshold_pct": threshold,
            "overall": overall,
            "signal_retention_pct": round(len(selected) / len(rows) * 100, 1) if rows else 0,
            "delta_average_excess_vs_c2": round(cand_excess - base_excess, 2) if cand_excess is not None else None,
            "yearly": yearly,
        }

    return {
        "engine": "V4 C2 Market Filter Study",
        "model_status": "RESEARCH_ONLY",
        "purpose": "Test a small fixed set of SPY market filters on frozen C2 signals without changing C2/V3 live rules",
        "start_date": start_date,
        "end_date": end_date,
        "sampling": "Every 5 trading days",
        "entry": "Next trading day open",
        "benchmark": "SPY same entry and 20d exit",
        "universe_stock_count": len(V2_UNIVERSE),
        "c2_signal_count": len(rows),
        "error_count": len(errors),
        "errors": errors,
        "candidate_definitions": {
            "C2_BASELINE": "Frozen C2; no SPY filter",
            "BLOCK_WEAK_DOWNTREND": "Exclude only when SPY <= EMA20, EMA20 <= EMA50 and SPY 20d momentum <= 0",
            "BLOCK_DEEP_BELOW_EMA20": "Exclude when SPY is more than 2% below EMA20",
            "REQUIRE_SPY_EMA20_ABOVE_EMA50": "Keep only SPY EMA20 >= EMA50",
            "REQUIRE_SPY_MOMENTUM_NONNEGATIVE": "Keep only SPY 20d momentum >= 0",
            "SAFE_COMBO": "Block weak downtrend and block SPY more than 2% below EMA20",
        },
        "results": result,
        "momentum_sensitivity": {
            "purpose": "Check whether the SPY momentum filter is robust around the 0% threshold rather than a single-point result",
            "thresholds_pct": momentum_thresholds,
            "results": momentum_sensitivity,
        },
        "decision_note": "Do not promote a filter from this endpoint alone. Prefer neighboring-threshold consistency across 2024/2025/2026, adequate signal retention, and later forward validation."
    }


# ============================================================
# V4 EXACT C2 HISTORICAL EVENT BACKTEST
# Frozen C2 false->true transition semantics.
# Research only; does not change C2/V3 live rules.
# ============================================================

@app.get("/v4/c2-exact-event-backtest")
async def v4_c2_exact_event_backtest(
    start_date: str = "2024-01-01",
    end_date: str = "2026-08-15"
):
    spy_values = await get_spy_data()
    spy_dates = [x["datetime"] for x in spy_values]
    spy_closes = [float(x["close"]) for x in spy_values]
    spy_opens = [float(x["open"]) for x in spy_values]
    spy_index = {d: i for i, d in enumerate(spy_dates)}

    rows = []
    errors = []

    def full_c2_at(closes, i):
        if i < 60:
            return False
        history = closes[:i + 1]
        price = history[-1]
        ema20 = calculate_ema(history, 20)
        ema50 = calculate_ema(history, 50)
        rsi14 = calculate_rsi(history, 14)
        _, _, macd_hist = calculate_macd(history)
        momentum20 = ((price - history[-21]) / history[-21]) * 100
        return (
            price > ema20
            and ema20 > ema50
            and 5 <= momentum20 <= 15
            and 50 <= rsi14 <= 70
            and macd_hist > 0
        )

    for symbol in V2_UNIVERSE:
        try:
            if symbol not in STOCK_CACHE:
                await backtest_stock(symbol, threshold=75)

            data = STOCK_CACHE.get(symbol)
            if not data or "values" not in data:
                errors.append({"symbol": symbol, "error": "Historical data unavailable"})
                continue

            values = list(reversed(data["values"]))
            dates = [x["datetime"] for x in values]
            closes = [float(x["close"]) for x in values]
            opens = [float(x["open"]) for x in values]

            previous_full = False

            for i in range(60, len(closes) - 20):
                current_full = full_c2_at(closes, i)
                new_signal = current_full and not previous_full
                previous_full = current_full

                if not new_signal:
                    continue

                signal_date = dates[i]
                entry_date = dates[i + 1]
                exit5_date = dates[i + 5]
                exit10_date = dates[i + 10]
                exit20_date = dates[i + 20]

                if signal_date < start_date or exit20_date > end_date:
                    continue

                needed = [signal_date, entry_date, exit20_date]
                if any(d not in spy_index for d in needed):
                    continue

                si = spy_index[signal_date]
                ei = spy_index[entry_date]
                xi = spy_index[exit20_date]
                if si < 50:
                    continue

                spy_hist = spy_closes[:si + 1]
                spy_price = spy_hist[-1]
                spy_momentum20 = ((spy_price - spy_hist[-21]) / spy_hist[-21]) * 100

                entry_price = opens[i + 1]
                ret5 = ((closes[i + 5] - entry_price) / entry_price) * 100
                ret10 = ((closes[i + 10] - entry_price) / entry_price) * 100
                ret20 = ((closes[i + 20] - entry_price) / entry_price) * 100

                spy_entry = spy_opens[ei]
                spy_ret20 = ((spy_closes[xi] - spy_entry) / spy_entry) * 100

                rows.append({
                    "symbol": symbol,
                    "signal_date": signal_date,
                    "entry_date": entry_date,
                    "exit_5d_date": exit5_date,
                    "exit_10d_date": exit10_date,
                    "exit_20d_date": exit20_date,
                    "return_5d": ret5,
                    "return_10d": ret10,
                    "return_20d": ret20,
                    "spy_return_20d": spy_ret20,
                    "excess_return_20d": ret20 - spy_ret20,
                    "spy_momentum20": spy_momentum20,
                })

        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    def summarize(items):
        if not items:
            return {
                "count": 0,
                "average_return_5d": None,
                "average_return_10d": None,
                "average_return_20d": None,
                "median_return_20d": None,
                "win_rate_20d": None,
                "average_excess_return_20d": None,
                "median_excess_return_20d": None,
                "beat_spy_rate_20d": None,
                "trimmed_mean_excess_20d": None,
            }

        r5 = [x["return_5d"] for x in items]
        r10 = [x["return_10d"] for x in items]
        r20 = [x["return_20d"] for x in items]
        exc = [x["excess_return_20d"] for x in items]

        return {
            "count": len(items),
            "average_return_5d": round(sum(r5) / len(r5), 2),
            "average_return_10d": round(sum(r10) / len(r10), 2),
            "average_return_20d": round(sum(r20) / len(r20), 2),
            "median_return_20d": round(statistics.median(r20), 2),
            "win_rate_20d": round(sum(x > 0 for x in r20) / len(r20) * 100, 1),
            "average_excess_return_20d": round(sum(exc) / len(exc), 2),
            "median_excess_return_20d": round(statistics.median(exc), 2),
            "beat_spy_rate_20d": round(sum(x > 0 for x in exc) / len(exc) * 100, 1),
            "trimmed_mean_excess_20d": stage5_trimmed_mean(exc),
        }

    candidates = {
        "C2_BASELINE": lambda x: True,
        "SPY_MOMENTUM_GTE_-2": lambda x: x["spy_momentum20"] >= -2.0,
        "SPY_MOMENTUM_GTE_0": lambda x: x["spy_momentum20"] >= 0.0,
    }

    baseline = summarize(rows)
    results = {}

    for name, rule in candidates.items():
        selected = [x for x in rows if rule(x)]
        overall = summarize(selected)
        yearly = {}
        for year in ["2024", "2025", "2026"]:
            yearly[year] = summarize(
                [x for x in selected if x["signal_date"].startswith(year)]
            )

        base_excess = baseline["average_excess_return_20d"]
        cand_excess = overall["average_excess_return_20d"]

        results[name] = {
            "overall": overall,
            "signal_retention_pct": (
                round(len(selected) / len(rows) * 100, 1) if rows else 0
            ),
            "delta_average_excess_vs_c2": (
                round(cand_excess - base_excess, 2)
                if cand_excess is not None and base_excess is not None
                else None
            ),
            "yearly": yearly,
        }

    return {
        "engine": "V4 Exact C2 Historical Event Backtest",
        "model_status": "RESEARCH_ONLY",
        "purpose": (
            "Backtest frozen C2 with exact false-to-true event semantics, "
            "then compare only SPY momentum >= -2% and >= 0% filters"
        ),
        "start_date": start_date,
        "end_date": end_date,
        "signal_semantics": (
            "One signal only when C2 changes from FALSE to TRUE; "
            "no repeat signal while FULL remains TRUE"
        ),
        "entry": "Next trading day open",
        "targets": "5d / 10d / 20d closes",
        "benchmark": "SPY same next-day-open entry and 20d exit",
        "universe_stock_count": len(V2_UNIVERSE),
        "exact_c2_signal_count": len(rows),
        "error_count": len(errors),
        "errors": errors,
        "candidate_definitions": {
            "C2_BASELINE": "Frozen C2; no SPY filter",
            "SPY_MOMENTUM_GTE_-2": "Keep exact C2 events when SPY 20d momentum >= -2%",
            "SPY_MOMENTUM_GTE_0": "Keep exact C2 events when SPY 20d momentum >= 0%",
        },
        "results": results,
        "decision_note": (
            "Research only. Do not change frozen C2/V3 from this historical test alone; "
            "compare yearly consistency, signal retention, and forward validation."
        ),
    }

from app.strategy_v3 import make_router

app.include_router(
    make_router(lambda: STOCK_CACHE, lambda: SPY_CACHE, V2_UNIVERSE)
)

from app.tracker_v3 import make_tracker_router

app.include_router(
    make_tracker_router(lambda: STOCK_CACHE, lambda: SPY_CACHE, V2_UNIVERSE)
)
