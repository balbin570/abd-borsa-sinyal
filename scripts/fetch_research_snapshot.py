import os
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")

if not API_KEY:
    raise RuntimeError(
        "TWELVE_DATA_API_KEY GitHub Secret bulunamadi."
    )


BACKTEST_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "AVGO", "TSLA", "AMD", "NFLX",
    "PLTR", "ORCL", "CRM", "QCOM", "MU",
    "AMAT", "PANW", "CRWD", "LRCX", "KLAC"
]

VALIDATION_WATCHLIST = [
    "JPM", "BAC", "GS", "V", "MA",
    "LLY", "JNJ", "ABBV", "MRK", "UNH",
    "WMT", "COST", "HD", "MCD", "NKE",
    "XOM", "CVX", "CAT", "GE", "RTX",
    "KO", "PEP", "DIS", "UPS", "LOW"
]

GROWTH_VALIDATION_WATCHLIST = [
    "NOW", "ADBE", "INTU", "SNOW", "DDOG",
    "FTNT", "ZS", "NET", "OKTA", "MDB",
    "MRVL", "ADI", "NXPI", "MCHP", "ON",
    "SHOP", "UBER", "ABNB", "DASH", "RBLX",
    "APP", "TTD", "TEAM", "HUBS", "WDAY"
]


V2_UNIVERSE = list(dict.fromkeys(
    BACKTEST_WATCHLIST
    + VALIDATION_WATCHLIST
    + GROWTH_VALIDATION_WATCHLIST
))

SYMBOLS = ["SPY"] + V2_UNIVERSE

OUTPUT_FILE = Path("data/research_snapshot.json")

BASE_URL = "https://api.twelvedata.com/time_series"


def fetch_symbol(symbol):
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": 1000,
        "apikey": API_KEY
    }

    url = (
        BASE_URL
        + "?"
        + urllib.parse.urlencode(params)
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(
                url,
                timeout=30
            ) as response:
                data = json.loads(
                    response.read().decode("utf-8")
                )

            if "values" in data:
                return data

            print(
                f"{symbol}: API cevabi hatali "
                f"(deneme {attempt}/3)"
            )
            print(data)

        except Exception as exc:
            print(
                f"{symbol}: istek hatasi "
                f"(deneme {attempt}/3): {exc}"
            )

        if attempt < 3:
            time.sleep(65)

    raise RuntimeError(
        f"{symbol} verisi 3 denemede alinamadi."
    )


def main():
    print(
        f"Toplam {len(SYMBOLS)} sembol indirilecek."
    )

    snapshot = {
        "snapshot_version": 1,
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "provider": "Twelve Data",
        "interval": "1day",
        "outputsize": 1000,
        "symbol_count": len(SYMBOLS),
        "research_stock_count": len(V2_UNIVERSE),
        "symbols": {}
    }

    # Ücretsiz API limitini güvenli tarafta
    # tutmak için 7 çağrı / grup.
    batch_size = 7

    for start in range(
        0,
        len(SYMBOLS),
        batch_size
    ):
        group = SYMBOLS[
            start:start + batch_size
        ]

        print()
        print(
            f"Grup "
            f"{start // batch_size + 1}: "
            f"{', '.join(group)}"
        )

        for symbol in group:
            print(f"{symbol} indiriliyor...")

            data = fetch_symbol(symbol)

            snapshot["symbols"][symbol] = data

            value_count = len(
                data.get("values", [])
            )

            print(
                f"{symbol}: {value_count} bar alindi."
            )

        # Son gruptan sonra bekleme.
        if start + batch_size < len(SYMBOLS):
            print(
                "API dakika limiti icin "
                "65 saniye bekleniyor..."
            )
            time.sleep(65)

    missing = [
        symbol
        for symbol in SYMBOLS
        if symbol not in snapshot["symbols"]
    ]

    if missing:
        raise RuntimeError(
            "Eksik semboller: "
            + ", ".join(missing)
        )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            snapshot,
            file,
            ensure_ascii=False,
            separators=(",", ":")
        )

    file_size_mb = (
        OUTPUT_FILE.stat().st_size
        / 1024
        / 1024
    )

    print()
    print("=" * 60)
    print("SNAPSHOT TAMAMLANDI")
    print(f"Dosya: {OUTPUT_FILE}")
    print(
        f"Sembol sayisi: "
        f"{len(snapshot['symbols'])}"
    )
    print(
        f"Arastirma hissesi: "
        f"{len(V2_UNIVERSE)}"
    )
    print(
        f"Dosya boyutu: "
        f"{file_size_mb:.2f} MB"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
