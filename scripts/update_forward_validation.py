import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


# ============================================================
# C2-TREND-FROZEN
# FROZEN: 2026-09-19
# DO NOT MODIFY DURING FORWARD VALIDATION
# ============================================================

FREEZE_DATE = "2026-09-19"

FORWARD_FILE = Path(
    "data/forward_validation.json"
)

API_KEY = os.environ.get(
    "TWELVE_DATA_API_KEY"
)

API_URL = (
    "https://api.twelvedata.com/time_series"
)


V2_UNIVERSE = [
    # Original 20
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "AVGO", "TSLA", "AMD", "NFLX",
    "PLTR", "ORCL", "CRM", "QCOM", "MU",
    "AMAT", "PANW", "CRWD", "LRCX", "KLAC",

    # General validation 25
    "JPM", "BAC", "GS", "V", "MA",
    "LLY", "JNJ", "ABBV", "MRK", "UNH",
    "WMT", "COST", "HD", "MCD", "NKE",
    "XOM", "CVX", "CAT", "GE", "RTX",
    "KO", "PEP", "DIS", "UPS", "LOW",

    # Growth / tech validation 25
    "NOW", "ADBE", "INTU", "SNOW", "DDOG",
    "FTNT", "ZS", "NET", "OKTA", "MDB",
    "MRVL", "ADI", "NXPI", "MCHP", "ON",
    "SHOP", "UBER", "ABNB", "DASH", "RBLX",
    "APP", "TTD", "TEAM", "HUBS", "WDAY"
]


# ============================================================
# INDICATORS
# These intentionally match the frozen research implementation.
# ============================================================

def calculate_ema(prices, period):
    multiplier = 2 / (period + 1)

    ema = (
        sum(prices[:period])
        / period
    )

    for price in prices[period:]:
        ema = (
            (price - ema)
            * multiplier
            + ema
        )

    return ema


def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(prices)
    ):
        change = (
            prices[i]
            - prices[i - 1]
        )

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[-period:])
        / period
    )

    avg_loss = (
        sum(losses[-period:])
        / period
    )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return (
        100
        - (
            100
            / (1 + rs)
        )
    )


def calculate_ema_series(
    prices,
    period
):
    if len(prices) < period:
        return []

    multiplier = (
        2 / (period + 1)
    )

    ema = (
        sum(prices[:period])
        / period
    )

    ema_values = [ema]

    for price in prices[period:]:
        ema = (
            (price - ema)
            * multiplier
            + ema
        )

        ema_values.append(ema)

    return ema_values


def calculate_macd(prices):
    ema12_series = (
        calculate_ema_series(
            prices,
            12
        )
    )

    ema26_series = (
        calculate_ema_series(
            prices,
            26
        )
    )

    if (
        len(ema12_series) == 0
        or len(ema26_series) == 0
    ):
        return 0, 0, 0

    ema12_aligned = (
        ema12_series[
            -len(ema26_series):
        ]
    )

    macd_series = [
        ema12 - ema26
        for ema12, ema26
        in zip(
            ema12_aligned,
            ema26_series
        )
    ]

    if len(macd_series) < 9:
        return 0, 0, 0

    signal_series = (
        calculate_ema_series(
            macd_series,
            9
        )
    )

    macd = macd_series[-1]
    signal = signal_series[-1]

    histogram = (
        macd - signal
    )

    return (
        macd,
        signal,
        histogram
    )


# ============================================================
# C2 FROZEN CONDITION
# ============================================================

def calculate_c2(closes):

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
            all(
                conditions.values()
            )
    }


# ============================================================
# TWELVE DATA
# ============================================================

def fetch_symbol(symbol):

    params = {
        "symbol":
            symbol,

        "interval":
            "1day",

        "outputsize":
            100,

        "apikey":
            API_KEY
    }

    for attempt in range(1, 4):

        try:

            response = httpx.get(
                API_URL,
                params=params,
                timeout=30.0
            )

            response.raise_for_status()

            data = response.json()

            if (
                "values" in data
                and data["values"]
            ):
                values = list(
                    reversed(
                        data["values"]
                    )
                )

                return values

            print(
                f"{symbol}: "
                f"API response without values: "
                f"{data}"
            )

        except Exception as exc:

            print(
                f"{symbol}: "
                f"attempt {attempt} failed: "
                f"{exc}"
            )

        if attempt < 3:
            time.sleep(10)

    raise RuntimeError(
        f"Could not download {symbol}"
    )


# ============================================================
# FILE HELPERS
# ============================================================

def load_forward_file():

    if not FORWARD_FILE.exists():
        raise RuntimeError(
            "forward_validation.json "
            "not found"
        )

    with FORWARD_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)


def save_forward_file(data):

    with FORWARD_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )

        f.write("\n")

# ============================================================
# FORWARD RESULT TRACKING
# ============================================================

def percent_return(
    entry_price,
    exit_price
):
    return (
        (
            exit_price
            / entry_price
        )
        - 1
    ) * 100


def update_signal_results(
    forward,
    downloaded,
    spy_values
):

    print("")
    print(
        "Updating forward signal results..."
    )

    spy_by_date = {
        row["datetime"]: row
        for row in spy_values
    }

    horizons = {
        "5d": 5,
        "10d": 10,
        "20d": 20
    }

    for signal in forward.get(
        "signals",
        []
    ):

        symbol = signal.get(
            "symbol"
        )

        signal_date = signal.get(
            "signal_date"
        )

        if (
            not symbol
            or not signal_date
        ):
            continue

        if symbol not in downloaded:
            continue

        values = downloaded[
            symbol
        ]

        # --------------------------------------------
        # Locate the original signal date.
        # --------------------------------------------

        signal_index = None

        for i, row in enumerate(
            values
        ):
            if (
                row["datetime"]
                == signal_date
            ):
                signal_index = i
                break

        if signal_index is None:
            print(
                f"{symbol} {signal_date}: "
                "signal date not found "
                "in current history"
            )
            continue

        # --------------------------------------------
        # NEXT TRADING DAY OPEN
        #
        # Signal = close of day i
        # Entry  = open of day i+1
        # --------------------------------------------

        entry_index = (
            signal_index + 1
        )

        if entry_index >= len(values):

            signal[
                "result_status"
            ] = "WAITING_FOR_ENTRY"

            continue

        entry_row = values[
            entry_index
        ]

        entry_date = entry_row[
            "datetime"
        ]

        entry_price = float(
            entry_row["open"]
        )

        spy_entry_row = (
            spy_by_date.get(
                entry_date
            )
        )

        if spy_entry_row is None:

            print(
                f"{symbol}: "
                f"SPY entry date "
                f"{entry_date} missing"
            )

            continue

        spy_entry_price = float(
            spy_entry_row["open"]
        )

        signal[
            "entry_date"
        ] = entry_date

        signal[
            "entry_price"
        ] = round(
            entry_price,
            4
        )

        signal[
            "spy_entry_price"
        ] = round(
            spy_entry_price,
            4
        )

        # --------------------------------------------
        # Evaluate 5 / 10 / 20 trading-day horizons.
        #
        # This deliberately matches our historical
        # backtest:
        #
        # signal i
        # entry  i+1 open
        # 5d exit  i+5 close
        # 10d exit i+10 close
        # 20d exit i+20 close
        # --------------------------------------------

        completed_horizons = 0

        for (
            horizon_name,
            trading_days
        ) in horizons.items():

            exit_index = (
                signal_index
                + trading_days
            )

            if (
                exit_index
                >= len(values)
            ):
                continue

            exit_row = values[
                exit_index
            ]

            exit_date = exit_row[
                "datetime"
            ]

            exit_price = float(
                exit_row["close"]
            )

            spy_exit_row = (
                spy_by_date.get(
                    exit_date
                )
            )

            if spy_exit_row is None:

                print(
                    f"{symbol}: "
                    f"SPY exit date "
                    f"{exit_date} missing"
                )

                continue

            spy_exit_price = float(
                spy_exit_row["close"]
            )

            stock_return = (
                percent_return(
                    entry_price,
                    exit_price
                )
            )

            spy_return = (
                percent_return(
                    spy_entry_price,
                    spy_exit_price
                )
            )

            excess_return = (
                stock_return
                - spy_return
            )

            signal.setdefault(
                "results",
                {}
            )

            signal.setdefault(
                "benchmark_results",
                {}
            )

            signal.setdefault(
                "excess_results",
                {}
            )

            signal.setdefault(
                "exit_dates",
                {}
            )

            signal.setdefault(
                "exit_prices",
                {}
            )

            signal.setdefault(
                "spy_exit_prices",
                {}
            )

            signal[
                "results"
            ][
                horizon_name
            ] = round(
                stock_return,
                4
            )

            signal[
                "benchmark_results"
            ][
                horizon_name
            ] = round(
                spy_return,
                4
            )

            signal[
                "excess_results"
            ][
                horizon_name
            ] = round(
                excess_return,
                4
            )

            signal[
                "exit_dates"
            ][
                horizon_name
            ] = exit_date

            signal[
                "exit_prices"
            ][
                horizon_name
            ] = round(
                exit_price,
                4
            )

            signal[
                "spy_exit_prices"
            ][
                horizon_name
            ] = round(
                spy_exit_price,
                4
            )

            completed_horizons += 1

        # --------------------------------------------
        # Signal status
        # --------------------------------------------

        if (
            signal.get(
                "results",
                {}
            ).get(
                "20d"
            )
            is not None
        ):

            signal[
                "result_status"
            ] = "COMPLETE_20D"

        elif (
            signal.get(
                "results",
                {}
            ).get(
                "10d"
            )
            is not None
        ):

            signal[
                "result_status"
            ] = "COMPLETE_10D"

        elif (
            signal.get(
                "results",
                {}
            ).get(
                "5d"
            )
            is not None
        ):

            signal[
                "result_status"
            ] = "COMPLETE_5D"

        else:

            signal[
                "result_status"
            ] = "ENTRY_RECORDED"

        print(
            f"{symbol} "
            f"{signal_date}: "
            f"{signal['result_status']}"
        )

# ============================================================
# MAIN
# ============================================================

def main():

    if not API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY "
            "secret is missing"
        )

    forward = (
        load_forward_file()
    )

    if (
        forward.get("model")
        != "C2-TREND-FROZEN"
    ):
        raise RuntimeError(
            "Unexpected model in "
            "forward_validation.json"
        )

    existing_keys = {
        (
            row.get("symbol"),
            row.get("signal_date")
        )
        for row
        in forward.get(
            "signals",
            []
        )
    }

    downloaded = {}

    errors = []

    print(
        "Starting C2 forward scan..."
    )

    print(
        f"Universe: "
        f"{len(V2_UNIVERSE)} stocks"
    )

    # --------------------------------------------------------
    # Download 70 stocks.
    # 7 per batch + 65 second pause.
    # --------------------------------------------------------

    for index, symbol in enumerate(
        V2_UNIVERSE,
        start=1
    ):

        try:

            values = (
                fetch_symbol(
                    symbol
                )
            )

            downloaded[
                symbol
            ] = values

            print(
                f"[{index}/"
                f"{len(V2_UNIVERSE)}] "
                f"{symbol}: OK"
            )

        except Exception as exc:

            errors.append({
                "symbol":
                    symbol,

                "error":
                    str(exc)
            })

            print(
                f"{symbol}: ERROR "
                f"{exc}"
            )

        if (
            index % 7 == 0
            and index
            < len(V2_UNIVERSE)
        ):
            print(
                "API limit pause: "
                "65 seconds..."
            )

            time.sleep(65)

    if errors:
        raise RuntimeError(
            f"Forward scan stopped. "
            f"{len(errors)} symbol(s) "
            f"failed: {errors}"
        )

    if (
        len(downloaded)
        != len(V2_UNIVERSE)
    ):
        raise RuntimeError(
            "Not all 70 stocks "
            "were downloaded"
        )

    # ========================================================
    # DOWNLOAD SPY BENCHMARK
    # ========================================================

    print("")
    print(
        "Downloading SPY benchmark..."
    )

    try:

        spy_values = (
            fetch_symbol(
                "SPY"
            )
        )

        print(
            "SPY: OK"
        )

    except Exception as exc:

        raise RuntimeError(
            f"SPY download failed: "
            f"{exc}"
        )
    
    # --------------------------------------------------------
    # Latest common market date.
    # --------------------------------------------------------

    latest_dates = []

    for values in downloaded.values():

        latest_dates.append(
            values[-1]["datetime"]
        )

    evaluated_date = min(
        latest_dates
    )

    print(
        f"Evaluated market date: "
        f"{evaluated_date}"
    )

    # --------------------------------------------------------
    # Evaluate current and previous completed bars.
    # --------------------------------------------------------

    active_symbols = []

    new_signals = []

    for symbol in V2_UNIVERSE:

        values = downloaded[
            symbol
        ]

        # If a symbol somehow has a newer bar
        # than the common market date, trim it.
        values = [
            row
            for row in values
            if row["datetime"]
            <= evaluated_date
        ]

        closes = [
            float(row["close"])
            for row in values
        ]

        if len(closes) < 61:
            raise RuntimeError(
                f"Not enough data "
                f"for {symbol}"
            )

        current = calculate_c2(
            closes
        )

        previous = calculate_c2(
            closes[:-1]
        )

        if (
            current is None
            or previous is None
        ):
            raise RuntimeError(
                f"C2 calculation failed "
                f"for {symbol}"
            )

        if current["full_trend"]:

            active_symbols.append(
                symbol
            )

        new_signal = (
            current["full_trend"]
            and not previous[
                "full_trend"
            ]
        )

        if not new_signal:
            continue

        signal_date = (
            values[-1][
                "datetime"
            ]
        )

        key = (
            symbol,
            signal_date
        )

        if key in existing_keys:
            continue

        official_forward = (
            signal_date
            > FREEZE_DATE
        )

        record = {
            "symbol":
                symbol,

            "signal_date":
                signal_date,

            "model":
                "C2-TREND-FROZEN",

            "model_version":
                "1.0",

            "official_forward_sample":
                official_forward,

            "sample_status":
                (
                    "OFFICIAL_FORWARD"
                    if official_forward
                    else
                    "PRE_FREEZE_BOOTSTRAP"
                ),

            "recorded_at_utc":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "signal_close":
                current["price"],

            "ema20":
                current["ema20"],

            "ema50":
                current["ema50"],

            "momentum20":
                current[
                    "momentum20"
                ],

            "rsi14":
                current["rsi14"],

            "macd_histogram":
                current[
                    "macd_histogram"
                ],

            "conditions":
                current[
                    "conditions"
                ],

            "entry_definition":
                "Next US trading day open",

            "entry_date":
                None,

            "entry_price":
                None,

            "results": {
                "5d": None,
                "10d": None,
                "20d": None
            },

            "benchmark_results": {
                "5d": None,
                "10d": None,
                "20d": None
            },

            "excess_results": {
                "5d": None,
                "10d": None,
                "20d": None
            },

            "result_status":
                "WAITING_FOR_ENTRY"
        }

        forward.setdefault(
            "signals",
            []
        ).append(
            record
        )

        existing_keys.add(
            key
        )

        new_signals.append(
            symbol
        )

        print(
            f"NEW SIGNAL: "
            f"{symbol} "
            f"{signal_date} "
            f"{record['sample_status']}"
        )

    # ========================================================
    # UPDATE ENTRY + 5D / 10D / 20D + SPY RESULTS
    # ========================================================

    update_signal_results(
        forward,
        downloaded,
        spy_values
    )
    
    # --------------------------------------------------------
    # Daily audit record.
    # --------------------------------------------------------

    forward.setdefault(
        "daily_scans",
        []
    )

    existing_scan_dates = {
        row.get(
            "evaluated_date"
        )
        for row
        in forward[
            "daily_scans"
        ]
    }

    if (
        evaluated_date
        not in existing_scan_dates
    ):

        forward[
            "daily_scans"
        ].append({
            "evaluated_date":
                evaluated_date,

            "recorded_at_utc":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "evaluated_stock_count":
                len(downloaded),

            "active_full_trend_count":
                len(
                    active_symbols
                ),

            "active_full_trend_symbols":
                active_symbols,

            "new_signal_count":
                len(
                    new_signals
                ),

            "new_signal_symbols":
                new_signals
        })

    forward[
        "last_successful_scan_market_date"
    ] = evaluated_date

    forward[
        "last_updated_at_utc"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    forward[
        "official_forward_signal_count"
    ] = sum(
        1
        for row
        in forward.get(
            "signals",
            []
        )
        if row.get(
            "official_forward_sample"
        )
    )

    forward[
        "bootstrap_signal_count"
    ] = sum(
        1
        for row
        in forward.get(
            "signals",
            []
        )
        if not row.get(
            "official_forward_sample",
            False
        )
    )

    save_forward_file(
        forward
    )

    print("")
    print(
        "=============================="
    )
    print(
        "C2 FORWARD SCAN COMPLETE"
    )
    print(
        "=============================="
    )

    print(
        f"Market date: "
        f"{evaluated_date}"
    )

    print(
        f"Stocks evaluated: "
        f"{len(downloaded)}"
    )

    print(
        f"Active FULL: "
        f"{len(active_symbols)}"
    )

    print(
        f"New signals: "
        f"{len(new_signals)}"
    )

    print(
        f"Official forward total: "
        f"{forward['official_forward_signal_count']}"
    )

    print(
        f"Bootstrap total: "
        f"{forward['bootstrap_signal_count']}"
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            f"ERROR: {exc}",
            file=sys.stderr
        )

        sys.exit(1)
