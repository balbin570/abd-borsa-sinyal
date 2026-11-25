"""Klasik overnight + SMA200 filtresi - ayrıntılı backtest (araştırma, emir yok).

Kapanışta al (sadece kapanış > 200 günlük ortalama ise), ertesi gün açılışta sat.
Her gece sabit ALLOC USD. Komisyon senaryoları: IBKR sabit $1/yön, 0 komisyon.
Çıktı: yıl yıl tablo, dolar K/Z, son 12 ay ay ay.
"""
import sys
from pathlib import Path

import pandas as pd

ALLOC = 5000.0
SMA = 200
OUT = Path("research/overnight_backtest")


def build(d: pd.DataFrame, commission_per_side: float) -> pd.DataFrame:
    d = d.copy()
    d["sma"] = d["Close"].rolling(SMA).mean()
    d["next_open"] = d["Open"].shift(-1)
    t = d[(d["Close"] > d["sma"]) & d["next_open"].notna() & d["sma"].notna()].copy()
    t["gross_pct"] = t["next_open"] / t["Close"] - 1
    t["qty"] = (ALLOC // t["Close"]).clip(lower=1)
    t["pnl_usd"] = t["qty"] * (t["next_open"] - t["Close"]) - 2 * commission_per_side
    t["net_pct"] = t["pnl_usd"] / (t["qty"] * t["Close"])
    return t


def yearly(t: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for y, g in t.groupby(t.index.year):
        eq = (1 + g["net_pct"]).cumprod()
        dy = d[d.index.year == y]["Close"]
        rows.append({
            "yıl": y,
            "işlem": len(g),
            "kazanma_%": round(100 * (g["pnl_usd"] > 0).mean(), 1),
            "strateji_%": round(100 * (eq.iloc[-1] - 1), 1),
            "K/Z_USD": round(g["pnl_usd"].sum(), 0),
            "max_düşüş_%": round(100 * (eq / eq.cummax() - 1).min(), 1),
            "al-tut_%": round(100 * (dy.iloc[-1] / dy.iloc[0] - 1), 1) if len(dy) > 1 else "",
        })
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for r in df.astype(object).itertuples(index=False):
        out.append("| " + " | ".join(str(v) for v in r) + " |")
    return "\n".join(out)


def summary(t: pd.DataFrame, label: str) -> dict:
    eq = (1 + t["net_pct"]).cumprod()
    years = (t.index[-1] - t.index[0]).days / 365.25
    return {
        "senaryo": label,
        "işlem": len(t),
        "kazanma_%": round(float(100 * (t["pnl_usd"] > 0).mean()), 1),
        "toplam_K/Z_USD": round(t["pnl_usd"].sum(), 0),
        "ort_işlem_USD": round(t["pnl_usd"].mean(), 2),
        "yıllık_%": round(100 * (eq.iloc[-1] ** (1 / years) - 1), 1),
        "max_düşüş_%": round(100 * (eq / eq.cummax() - 1).min(), 1),
        "en_kötü_gece_USD": round(t["pnl_usd"].min(), 0),
        "en_iyi_gece_USD": round(t["pnl_usd"].max(), 0),
    }


def run(symbol: str) -> str:
    import yfinance as yf
    d = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=True)[["Open", "Close"]].dropna()
    d.index = pd.to_datetime(d.index.date)
    md = [f"## {symbol} — klasik overnight + SMA200, her gece {ALLOC:.0f} USD"]
    for start, lbl in (("2016-01-01", "Son 10 yıl"), ("2024-01-01", "2024'ten beri")):
        rows = []
        for comm, name in ((1.0, "IBKR $1/yön"), (0.0, "komisyonsuz")):
            t = build(d, comm)
            t = t[t.index >= start]
            rows.append(summary(t, name))
        md += [f"### {lbl}", md_table(pd.DataFrame(rows)), ""]
    t = build(d, 1.0)
    t = t[t.index >= "2016-01-01"]
    md += ["### Yıl yıl (IBKR $1/yön)", md_table(yearly(t, d[d.index >= "2016-01-01"])), ""]
    last = t[t.index >= t.index[-1] - pd.Timedelta(days=365)]
    m = last.groupby(last.index.to_period("M")).agg(işlem=("pnl_usd", "size"), K_Z_USD=("pnl_usd", "sum"))
    m["K_Z_USD"] = m["K_Z_USD"].round(0)
    m = m.reset_index().rename(columns={"index": "ay"})
    md += ["### Son 12 ay, ay ay", md_table(m), ""]
    return "\n".join(md)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    parts = ["# Klasik overnight + SMA200 — ayrıntılı backtest", ""]
    for s in sys.argv[1:] or ["QQQ"]:
        try:
            parts.append(run(s))
        except Exception as e:
            parts.append(f"## {s}\nHATA: {type(e).__name__}: {e}")
    text = "\n".join(parts)
    (OUT / "classic_detail.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
