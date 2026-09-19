import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


FORWARD_FILE = Path("data/forward_validation.json")

STAGE8_URL = os.environ.get(
    "STAGE8_URL",
    "https://abd-borsa-sinyal.onrender.com/v2/stage8-forward-scan"
)

FREEZE_DATE = "2026-09-19"


def load_forward_file():
    if not FORWARD_FILE.exists():
        raise RuntimeError(
            f"{FORWARD_FILE} not found"
        )

    with FORWARD_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:
        return json.load(f)


def save_forward_file(data):
    FORWARD_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

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


def fetch_stage8():
    print("Calling Stage 8 endpoint...")
    print(STAGE8_URL)

    request = urllib.request.Request(
        STAGE8_URL,
        headers={
            "User-Agent":
                "C2-Forward-Validation/1.0"
        }
    )

    # Render endpoint may take ~10-11 minutes.
    with urllib.request.urlopen(
        request,
        timeout=900
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                f"Stage 8 HTTP status: "
                f"{response.status}"
            )

        raw = response.read().decode(
            "utf-8"
        )

        return json.loads(raw)


def validate_scan(scan):
    if (
        scan.get("engine")
        != "Stage 8 C2 Forward Scan"
    ):
        raise RuntimeError(
            "Unexpected Stage 8 engine"
        )

    if (
        scan.get("status")
        != "FORWARD_VALIDATION"
    ):
        raise RuntimeError(
            "Unexpected Stage 8 status"
        )

    model = scan.get(
        "model",
        {}
    )

    if (
        model.get("name")
        != "C2-TREND-FROZEN"
    ):
        raise RuntimeError(
            "Unexpected model name"
        )

    if (
        model.get("version")
        != "1.0"
    ):
        raise RuntimeError(
            "Unexpected model version"
        )

    evaluated_count = scan.get(
        "evaluated_stock_count",
        0
    )

    error_count = scan.get(
        "error_count",
        0
    )

    if evaluated_count != 70:
        raise RuntimeError(
            f"Expected 70 evaluated stocks, "
            f"received {evaluated_count}"
        )

    if error_count != 0:
        raise RuntimeError(
            f"Stage 8 returned "
            f"{error_count} errors"
        )


def signal_key(signal):
    return (
        signal.get("symbol"),
        signal.get("signal_date")
    )


def main():
    forward = load_forward_file()

    scan = fetch_stage8()

    validate_scan(scan)

    evaluated_date = scan.get(
        "evaluated_date"
    )

    if not evaluated_date:
        raise RuntimeError(
            "evaluated_date missing"
        )

    print(
        f"Evaluated market date: "
        f"{evaluated_date}"
    )

    print(
        f"New signals returned: "
        f"{scan.get('new_signal_count', 0)}"
    )

    # --------------------------------------------------------
    # Keep an audit trail of successful daily scans.
    # One record per evaluated market date.
    # --------------------------------------------------------

    if "daily_scans" not in forward:
        forward["daily_scans"] = []

    existing_scan_dates = {
        row.get("evaluated_date")
        for row in forward["daily_scans"]
    }

    if evaluated_date not in existing_scan_dates:

        forward["daily_scans"].append({
            "evaluated_date":
                evaluated_date,

            "recorded_at_utc":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "evaluated_stock_count":
                scan.get(
                    "evaluated_stock_count"
                ),

            "active_full_trend_count":
                scan.get(
                    "active_full_trend_count"
                ),

            "new_signal_count":
                scan.get(
                    "new_signal_count"
                ),

            "new_signal_symbols": [
                row.get("symbol")
                for row in scan.get(
                    "new_signals",
                    []
                )
            ]
        })

    # --------------------------------------------------------
    # Existing signal keys prevent duplicates if the Action
    # runs more than once on the same day.
    # --------------------------------------------------------

    if "signals" not in forward:
        forward["signals"] = []

    existing_signal_keys = {
        signal_key(row)
        for row in forward["signals"]
    }

    added = 0

    for signal in scan.get(
        "new_signals",
        []
    ):

        key = signal_key(signal)

        if key in existing_signal_keys:
            print(
                f"Already recorded: "
                f"{key[0]} {key[1]}"
            )
            continue

        signal_date = signal.get(
            "signal_date"
        )

        # A signal from before/at the freeze boundary is kept
        # for audit purposes but is NOT part of the official
        # post-freeze forward sample.
        official_forward = (
            signal_date > FREEZE_DATE
        )

        metrics = signal.get(
            "metrics",
            {}
        )

        record = {
            "symbol":
                signal.get("symbol"),

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
                    else "PRE_FREEZE_BOOTSTRAP"
                ),

            "recorded_at_utc":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "signal_close":
                metrics.get("price"),

            "ema20":
                metrics.get("ema20"),

            "ema50":
                metrics.get("ema50"),

            "momentum20":
                metrics.get(
                    "momentum20"
                ),

            "rsi14":
                metrics.get("rsi14"),

            "macd_histogram":
                metrics.get(
                    "macd_histogram"
                ),

            "conditions":
                metrics.get(
                    "conditions",
                    {}
                ),

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

        forward["signals"].append(
            record
        )

        existing_signal_keys.add(
            key
        )

        added += 1

        print(
            f"Added signal: "
            f"{record['symbol']} "
            f"{record['signal_date']} "
            f"{record['sample_status']}"
        )

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
        for row in forward["signals"]
        if row.get(
            "official_forward_sample"
        )
    )

    forward[
        "bootstrap_signal_count"
    ] = sum(
        1
        for row in forward["signals"]
        if not row.get(
            "official_forward_sample",
            False
        )
    )

    save_forward_file(
        forward
    )

    print(
        f"New records added: {added}"
    )

    print(
        "Official forward signals: "
        f"{forward['official_forward_signal_count']}"
    )

    print(
        "Bootstrap signals: "
        f"{forward['bootstrap_signal_count']}"
    )

    print("Forward file updated.")


if __name__ == "__main__":
    try:
        main()

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr
        )
        sys.exit(1)
