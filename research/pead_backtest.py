"""PEAD (bilanco sonrasi surukleme) backtest'i - arastirma, emir yok.

Veri: Yahoo bilanco tarihleri + EPS surprizi (get_earnings_dates), gunluk fiyatlar.
Tepki gunu: aciklama saat < 12:00 (ET) ise ayni gun, degilse ertesi islem gunu.
Sinyal: surpriz ve tepki gunu getirisi esikleri. Giris: tepki gununden sonraki gun ACILIS.
Cikis: N islem gunu sonra KAPANIS (stop yok). Maliyet her yon %0.10.
Olcumler:
  - Olay bazinda: ort. getiri, ayni donem SPY, fark (alfa), SPY'i gecme orani
  - Portfoy: 20 slot x %5; bos para SPY'da durur (adil kiyas). Gunde en fazla 5 yeni giris.
"""
import concurrent.futures as cf
from pathlib import Path

import numpy as np
import pandas as pd

from research import daily_picks_backtest as base

OUT = Path("research/pead")
START = "2018-01-01"
FEE = 0.001
SLOTS, MAX_NEW = 20, 5


def fetch_events(tickers):
    import yfinance as yf

    def one(t):
        try:
            d = yf.Ticker(t).get_earnings_dates(limit=40)
        except Exception:
            return []
        if d is None or d.empty:
            return []
        d = d.copy()
        cols = {c.lower(): c for c in d.columns}
        sc = next((cols[c] for c in cols if "surprise" in c), None)
        if sc is None:
            return []
        out = []
        for ts, row in d.iterrows():
            s = row[sc]
            if pd.isna(s):
                continue
            ts = pd.Timestamp(ts)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("America/New_York")
            out.append({"t": t, "ts": ts.tz_localize(None), "bmo": ts.hour < 12, "surprise": float(s)})
        return out
    ev = []
    with cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(one, tickers):
            ev.extend(r)
    return pd.DataFrame(ev)


def build_trades(ev, P, spy, rules):
    O, C = P["Open"], P["Close"]
    idx = C.index
    liq = ((C > 10) & ((C * P["Volume"]).rolling(20).mean() > 50e6))
    spy_v = spy.reindex(idx).ffill().values
    out = {name: [] for name in rules}
    for e in ev.itertuples():
        if e.t not in C.columns:
            continue
        day = e.ts.normalize()
        k = idx.searchsorted(day)
        if k >= len(idx):
            continue
        if not e.bmo:
            k += 1  # aksam aciklamasi -> tepki ertesi gun
        if k < 1 or k + 1 >= len(idx) or idx[k] < pd.Timestamp(START):
            continue
        j = C.columns.get_loc(e.t)
        c0, c1 = C.iat[k - 1, j], C.iat[k, j]
        if not (np.isfinite(c0) and np.isfinite(c1)) or not bool(liq.iat[k, j]):
            continue
        react = c1 / c0 - 1
        for name, (smin, smax, rmin, rmax, hold) in rules.items():
            if not (smin <= e.surprise <= smax and rmin <= react <= rmax):
                continue
            ke = k + 1
            kx = ke + hold - 1
            if kx >= len(idx):
                continue
            o, x = O.iat[ke, j], C.iat[kx, j]
            if not (np.isfinite(o) and np.isfinite(x)) or o <= 0:
                continue
            r = x / o * (1 - FEE) ** 2 - 1
            rs = spy_v[kx] / spy_v[ke - 1] - 1
            out[name].append({"t": e.t, "ke": ke, "kx": kx, "ret": r, "spy": rs,
                              "surprise": e.surprise, "react": react})
    return {n: pd.DataFrame(v) for n, v in out.items()}


def portfolio(tr, P, spy, dates):
    """Slotlu portfoy; bos para SPY'da. Islem getirisi gunluk olarak dagitilir."""
    idx = P["Close"].index
    C = P["Close"]
    spy_r = spy.reindex(idx).ffill().pct_change().fillna(0).values
    k_start = idx.searchsorted(dates[0])
    if tr.empty:
        eq = np.cumprod(1 + spy_r[k_start:])
        return pd.Series(eq, index=idx[k_start:])
    by_entry = {}
    for r in tr.sort_values("surprise", ascending=False).itertuples():
        by_entry.setdefault(r.ke, []).append(r)
    open_pos = []
    eq = 1.0
    vals = []
    Cv, Ov = C.values, P["Open"].values
    cols = {c: i for i, c in enumerate(C.columns)}
    for k in range(k_start, len(idx)):
        # bugun giris yapacaklar (acilista): gunluk getiri acilistan kapanisa
        new = []
        for r in by_entry.get(k, [])[:MAX_NEW]:
            if len(open_pos) + len(new) >= SLOTS:
                break
            new.append(r)
        w = 1.0 / SLOTS
        day_ret = 0.0
        still = []
        for p in open_pos:
            j = cols[p.t]
            a, b = Cv[k - 1, j], Cv[k, j]
            rr = b / a - 1 if np.isfinite(a) and np.isfinite(b) and a > 0 else 0.0
            if k == p.kx:
                rr = (1 + rr) * (1 - FEE) - 1
            day_ret += w * rr
            if k < p.kx:
                still.append(p)
        for p in new:
            j = cols[p.t]
            a, b = Ov[k, j], Cv[k, j]
            rr = (b / a) * (1 - FEE) - 1 if np.isfinite(a) and np.isfinite(b) and a > 0 else 0.0
            day_ret += w * rr
            if k < p.kx:
                still.append(p)
        n_used = len(open_pos) + len(new)
        day_ret += (1 - w * n_used) * spy_r[k]
        open_pos = still
        eq *= 1 + day_ret
        vals.append(eq)
    return pd.Series(vals, index=idx[k_start:])


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tick = base.universe()
    P = base.load(tick + ["SPY"])
    spy = P["Close"]["SPY"].copy()
    for k in P:
        P[k] = P[k].drop(columns=["SPY"], errors="ignore")
    ev = fetch_events(list(P["Close"].columns))
    print("olay:", len(ev), "hisse:", ev["t"].nunique() if len(ev) else 0, flush=True)
    inf = float("inf")
    rules = {
        "E1 surpriz>=%5, tepki>=+%2, 20 gun": (5, inf, 0.02, inf, 20),
        "E2 surpriz>=%5, tepki>=+%2, 40 gun": (5, inf, 0.02, inf, 40),
        "E3 surpriz>=%10, tepki>=+%4, 20 gun": (10, inf, 0.04, inf, 20),
        "E4 surpriz>=%5 (tepki sarti yok), 20 gun": (5, inf, -inf, inf, 20),
        "E5 tepki>=+%4 (surpriz sarti yok), 20 gun": (-inf, inf, 0.04, inf, 20),
        "KONTROL: surpriz<=-%5, tepki<=-%2, 20 gun": (-inf, -5, -inf, -0.02, 20),
    }
    T = build_trades(ev, P, spy, rules)
    dates = P["Close"].index[P["Close"].index >= START]
    rows, prow, curves = [], [], {}
    for name, tr in T.items():
        if tr.empty:
            rows.append({"kural": name, "olay": 0})
            continue
        diff = tr["ret"] - tr["spy"]
        rows.append({"kural": name, "olay": len(tr),
                     "ort_getiri_%": round(100 * tr["ret"].mean(), 2),
                     "ayni_donem_SPY_%": round(100 * tr["spy"].mean(), 2),
                     "fark_%": round(100 * diff.mean(), 2),
                     "fark_medyan_%": round(100 * diff.median(), 2),
                     "SPY'i_gecen_%": round(100 * (diff > 0).mean())})
        if not name.startswith("KONTROL"):
            eq = portfolio(tr, P, spy, dates)
            curves[name] = eq
            prow.append(base.perf(eq, name + " (bos para SPY'da)"))
    spy_eq = spy[spy.index >= dates[0]]
    spy_eq = spy_eq / spy_eq.iloc[0]
    curves["SPY al-tut"] = spy_eq
    prow.append(base.perf(spy_eq, "SPY al-tut"))
    # yil bazinda fark (E1)
    yr = []
    t1 = T[list(rules)[0]]
    if not t1.empty:
        t1 = t1.assign(yil=P["Close"].index[t1["ke"]].year, d=t1["ret"] - t1["spy"])
        for y, g in t1.groupby("yil"):
            yr.append({"yil": y, "olay": len(g), "fark_%": round(100 * g["d"].mean(), 2)})
    text = "\n".join([
        "# PEAD (bilanco sonrasi surukleme) backtest'i", "",
        f"Evren: {P['Close'].shape[1]} hisse (guncel S&P 500 + Nasdaq 100). Olay: {len(ev)}. Donem: {START} -> "
        f"{spy.index[-1].date()}. Giris tepki gunu sonrasi acilis, cikis N gun sonra kapanis, stop yok.", "",
        "## Olay bazinda (fark = islem getirisi - ayni donem SPY)", base.md(rows), "",
        "## Portfoy (20 slot x %5, bos para SPY'da)", base.md(prow), "",
        "## Yil yil (%)", base.md(base.yearly(curves).to_dict("records")), "",
        "## E1 yil bazinda fark", base.md(yr) if yr else "-", ""])
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
