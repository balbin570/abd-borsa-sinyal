"""GIRIS testi: hacmi artan ve o gun %1-2 yukselmis hisse (arastirma, emir yok).

Sinyal gunu: kapanis / onceki kapanis - 1 belirli aralikta VE hacim / 20g ort. hacim >= esik.
Giris ertesi gun ACILIS. Cikis: A_SABIT (stop -2ATR, hedef +3ATR, 15 gun, bilancodan once cik).
Portfoy: gunde en fazla 4 yeni, en fazla 10 pozisyon, her biri %10. Maliyet her yon %0.10.
"Fark" sutunu: her islemin getirisi - ayni donemde SPY getirisi (girisin gercek katkisi).
"""
from pathlib import Path

import numpy as np
import pandas as pd

from research import daily_picks_backtest as base

OUT = Path("research/momentum_entry")


def variants(P, I):
    C, V = P["Close"], P["Volume"]
    day_ret = C / C.shift(1) - 1
    vol_ratio = V / V.rolling(20).mean().shift(1)  # bugunku hacim / onceki 20 gun ortalamasi
    trend = (C > I["sma50"]) & (I["sma50"] > I["sma200"]) & (I["mom_rank"] >= 0.8)
    liq = I["liquid"]

    def m(lo, hi, vr, use_trend):
        x = (day_ret >= lo) & (day_ret <= hi) & (vol_ratio >= vr) & liq
        return (x & trend if use_trend else x).fillna(False)
    return {
        "G1 +%1-2, hacim 1.5x, trend": m(0.01, 0.02, 1.5, True),
        "G2 +%1-2, hacim 2x, trend": m(0.01, 0.02, 2.0, True),
        "G3 +%1-2, hacim 1.5x, trend YOK": m(0.01, 0.02, 1.5, False),
        "G4 +%2-4, hacim 1.5x, trend": m(0.02, 0.04, 1.5, True),
        "REF geri cekilme (RSI+BB)": (I["sig"]["RSI"] | I["sig"]["BB"]),
    }


def alpha_stats(tr, spy):
    if tr.empty:
        return {}
    s = spy[~spy.index.duplicated()]
    pos = {d: i for i, d in enumerate(s.index)}
    v = s.values
    spy_ret = np.array([v[pos[pd.Timestamp(c)]] / v[pos[pd.Timestamp(g)] - 1] - 1
                        for g, c in zip(tr["giris"], tr["cikis"])], dtype=float)
    diff = tr["ret"].values - spy_ret
    return {"ort_islem_%": round(100 * tr["ret"].mean(), 2),
            "ayni_donem_SPY_%": round(100 * spy_ret.mean(), 2),
            "fark_%": round(100 * diff.mean(), 2),
            "SPY'i_gecen_%": round(100 * (diff > 0).mean())}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tick = base.universe()
    P = base.load(tick + ["SPY"])
    spy = P["Close"]["SPY"].copy()
    for k in P:
        P[k] = P[k].drop(columns=["SPY"], errors="ignore")
    I = base.indicators(P, spy)
    earn = base.earnings_dates(list(P["Close"].columns))
    dates = P["Close"].index[P["Close"].index >= base.START]
    V = variants(P, I)
    rows, curves = [], {}
    for name, mask in V.items():
        I["sig"] = {name: mask}
        eq, tr = base.simulate(P, I, "A_SABIT", False, earn, dates, setups=(name,))
        n_sig = int(mask.loc[dates].values.sum())
        row = base.perf(eq, name, tr)
        row.pop("cikislar", None)
        row.update(alpha_stats(tr, spy))
        row["gunluk_sinyal"] = round(n_sig / len(dates), 1)
        rows.append(row)
        curves[name] = eq
        print(row, flush=True)
    spy_eq = spy[spy.index >= dates[0]]
    spy_eq = spy_eq / spy_eq.iloc[0]
    curves["SPY al-tut"] = spy_eq
    rows.append(base.perf(spy_eq, "SPY al-tut"))
    text = "\n".join(["# GIRIS testi: hacim artisi + gunluk %1-2 yukselis", "",
                      f"Evren: {P['Close'].shape[1]} hisse (guncel S&P 500 + Nasdaq 100). Donem: {base.START} -> "
                      f"{spy.index[-1].date()}. Cikis: 2ATR stop / 3ATR hedef / 15 gun / bilanco oncesi.", "",
                      base.md(rows), "", "## Yil yil (%)", base.md(base.yearly(curves).to_dict("records")), ""])
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
