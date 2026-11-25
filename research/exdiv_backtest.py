"""TEMETTU (ex-date) dususu sonrasi alim backtest'i - arastirma, emir yok.

Fiyatlar: temettuye gore DUZELTILMEMIS (bolunmeye gore duzeltilmis) Yahoo fiyatlari; boylece ex-date
dususu gorunur. Olay: Dividends > 0 olan gun (ex-date).
  dusus   = ex-date kapanis / onceki kapanis - 1
  verim   = temettu / onceki kapanis
  asiri   = dusus + verim  (< 0 ise fiyat temettuden FAZLA dusmus)
Giris: ex-date'ten sonraki gun ACILIS. Cikis: N islem gunu sonra KAPANIS. Stop yok. Maliyet %0.10/yon.
Kiyas: ayni donem SPY (toplam getiri). Portfoy: 20 slot x %5, bos para SPY'da.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from research import daily_picks_backtest as base
from research import pead_backtest as pead

OUT = Path("research/exdiv")
START = "2018-01-01"
FEE = 0.001


def load_raw(tickers):
    import yfinance as yf
    fr = {k: {} for k in ("Open", "Close", "Volume", "Dividends")}
    for i in range(0, len(tickers), 100):
        ch = tickers[i:i + 100]
        df = yf.download(ch, start="2016-06-01", auto_adjust=False, actions=True, progress=False,
                         group_by="ticker", threads=True)
        for t in ch:
            try:
                sub = df[t]
            except KeyError:
                continue
            if sub["Close"].notna().sum() < 300:
                continue
            for k in fr:
                fr[k][t] = sub[k] if k in sub else 0.0
    return {k: pd.DataFrame(v).sort_index() for k, v in fr.items()}


def events(P):
    C, D = P["Close"], P["Dividends"].fillna(0)
    liq = (C > 10) & ((C * P["Volume"]).rolling(20).mean() > 50e6)
    idx = C.index
    rows = []
    ks, js = np.nonzero((D.values > 0))
    for k, j in zip(ks, js):
        if k < 1 or idx[k] < pd.Timestamp(START) or not bool(liq.iat[k, j]):
            continue
        c0, c1, dv = C.iat[k - 1, j], C.iat[k, j], D.iat[k, j]
        if not (np.isfinite(c0) and np.isfinite(c1)) or c0 <= 0:
            continue
        drop = c1 / c0 - 1
        yld = dv / c0
        rows.append({"t": C.columns[j], "k": k, "drop": drop, "yield": yld, "excess": drop + yld})
    return pd.DataFrame(rows)


def trades(ev, P, spy_tr, rule, hold):
    O, C = P["Open"], P["Close"]
    idx = C.index
    s = spy_tr.reindex(idx).ffill().values
    sel = ev[rule(ev)] if len(ev) else ev
    out = []
    for e in sel.itertuples():
        j = C.columns.get_loc(e.t)
        ke, kx = e.k + 1, e.k + hold
        if kx >= len(idx):
            continue
        o, x = O.iat[ke, j], C.iat[kx, j]
        if not (np.isfinite(o) and np.isfinite(x)) or o <= 0:
            continue
        out.append({"t": e.t, "ke": ke, "kx": kx, "ret": x / o * (1 - FEE) ** 2 - 1,
                    "spy": s[kx] / s[ke - 1] - 1, "surprise": e[4]})
    return pd.DataFrame(out)


def main():
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    tick = base.universe()
    P = load_raw(tick)
    spy_tr = yf.download("SPY", start="2016-06-01", auto_adjust=True, progress=False)["Close"].squeeze()
    ev = events(P)
    print("ex-date olayi:", len(ev), "hisse:", ev["t"].nunique(), flush=True)
    R = {
        "D1 tum ex-date'ler, 5 gun": (lambda e: e["drop"] > -1, 5),
        "D2 tum ex-date'ler, 10 gun": (lambda e: e["drop"] > -1, 10),
        "D3 tum ex-date'ler, 20 gun": (lambda e: e["drop"] > -1, 20),
        "D4 temettuden FAZLA dusenler, 10 gun": (lambda e: e["excess"] < 0, 10),
        "D5 temettuden en az %1 FAZLA dusenler, 10 gun": (lambda e: e["excess"] < -0.01, 10),
        "D6 yuksek temettu (odeme >= %1), 10 gun": (lambda e: e["yield"] >= 0.01, 10),
    }
    dates = P["Close"].index[P["Close"].index >= START]
    Pp = {"Open": P["Open"], "Close": P["Close"]}
    rows, prow, curves = [], [], {}
    stats = {"ort_dusus_%": round(100 * ev["drop"].mean(), 2), "ort_temettu_%": round(100 * ev["yield"].mean(), 2),
             "temettuden_fazla_dusme_orani_%": round(100 * (ev["excess"] < 0).mean())}
    for name, (rule, hold) in R.items():
        tr = trades(ev, P, spy_tr, rule, hold)
        if tr.empty:
            continue
        d = tr["ret"] - tr["spy"]
        rows.append({"kural": name, "olay": len(tr), "ort_getiri_%": round(100 * tr["ret"].mean(), 2),
                     "ayni_donem_SPY_%": round(100 * tr["spy"].mean(), 2), "fark_%": round(100 * d.mean(), 2),
                     "fark_medyan_%": round(100 * d.median(), 2), "SPY'i_gecen_%": round(100 * (d > 0).mean())})
        eq = pead.portfolio(tr, Pp, spy_tr, dates)
        curves[name] = eq
        prow.append(base.perf(eq, name + " (bos para SPY'da)"))
        print(rows[-1], flush=True)
    spy_eq = spy_tr[spy_tr.index >= dates[0]]
    spy_eq = spy_eq / spy_eq.iloc[0]
    curves["SPY al-tut"] = spy_eq
    prow.append(base.perf(spy_eq, "SPY al-tut"))
    text = "\n".join([
        "# Temettu (ex-date) dususu sonrasi alim backtest'i", "",
        f"Evren: {P['Close'].shape[1]} hisse (guncel S&P 500 + Nasdaq 100). Ex-date olayi: {len(ev)}. "
        f"Donem: {START} -> {dates[-1].date()}.", "",
        "Ex-date genel: " + ", ".join(f"{k} {v}" for k, v in stats.items()), "",
        "## Olay bazinda (fark = islem getirisi - ayni donem SPY)", base.md(rows), "",
        "## Portfoy (20 slot x %5, bos para SPY'da)", base.md(prow), "",
        "## Yil yil (%)", base.md(base.yearly(curves).to_dict("records")), ""])
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
