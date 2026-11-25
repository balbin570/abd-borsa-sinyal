"""SWING-15 backtest: haftalık hisse seçimi, en fazla 15 işlem günü tutma (araştırma, emir yok).

Evren: güncel S&P 500 (survivorship bias: bugün listede olmayan/batan şirketler yok).
Ortak filtreler (hepsinde):
  - SPY kapanış > SPY SMA200 (değilse o hafta alım yok)
  - 63 günlük getiri, evrenin en iyi %20'si
  - Fiyat > 10 $, 20 günlük ort. işlem hacmi > 50 M $
Giriş versiyonları:
  V1 PULLBACK : C > SMA50 > SMA200 ve 5 günlük getiri < 0
  V2 RSI+EMA  : C > EMA50 > EMA200 ve RSI(14) < 45
  V3 BOLLINGER: C > SMA200, SMA50 > SMA200 ve C < alt Bollinger (20, 2)
  V4 KIRILIM  : C > SMA50 > SMA200, C = 20 günlük en yüksek kapanış, hacim > 1.2 × 20g ort.
İşlem: Haftanın son işlem günü sinyal -> sonraki işlem günü AÇILIŞ'ta al.
  5 slot (her biri özsermayenin %20'si); dolu slot varsa yeni alım yapılmaz.
  Sıralama: 63 günlük getiri (yüksekten düşüğe).
Çıkış: stop = giriş - 2×ATR14, hedef = giriş + 3×ATR14, en fazla 15 işlem günü (kapanışta).
  Aynı gün hem stop hem hedef görülürse STOP varsayılır (temkinli). Boşluklu açılışta açılış fiyatı.
Maliyet: her yön %0.10.
"""
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

START = "2018-01-01"
SLOTS = 5
FEE = 0.001
STOP_ATR, TGT_ATR, MAX_HOLD = 2.0, 3.0, 15
OUT = Path("research/swing15")


def universe():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchScanner/1.0)"}, timeout=30)
    r.raise_for_status()
    t = next(x for x in pd.read_html(io.StringIO(r.text)) if "Symbol" in x.columns and len(x) > 450)
    return sorted({str(s).strip().replace(".", "-") for s in t["Symbol"].dropna()})


def load(tickers):
    import yfinance as yf
    frames = {k: {} for k in ("Open", "High", "Low", "Close", "Volume")}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        df = yf.download(chunk, start="2016-06-01", auto_adjust=True, progress=False,
                         group_by="ticker", threads=True)
        for t in chunk:
            try:
                sub = df[t]
            except KeyError:
                continue
            if sub["Close"].notna().sum() < 300:
                continue
            for k in frames:
                frames[k][t] = sub[k]
    return {k: pd.DataFrame(v).sort_index() for k, v in frames.items()}


def indicators(P, spy_close):
    C, H, L, V = P["Close"], P["High"], P["Low"], P["Volume"]
    I = {}
    I["sma50"], I["sma200"] = C.rolling(50).mean(), C.rolling(200).mean()
    I["ema50"], I["ema200"] = C.ewm(span=50, adjust=False).mean(), C.ewm(span=200, adjust=False).mean()
    d = C.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    I["rsi"] = 100 - 100 / (1 + up / dn)
    pc = C.shift(1)
    tr = np.maximum(np.maximum((H - L).values, (H - pc).abs().values), (L - pc).abs().values)
    I["atr"] = pd.DataFrame(tr, index=C.index, columns=C.columns).ewm(alpha=1 / 14, adjust=False).mean()
    mid, sd = C.rolling(20).mean(), C.rolling(20).std()
    I["bb_low"] = mid - 2 * sd
    I["ret5"] = C / C.shift(5) - 1
    I["mom63"] = C / C.shift(63) - 1
    I["mom_rank"] = I["mom63"].rank(axis=1, pct=True)
    I["hi20"] = C.rolling(20).max()
    I["vol_ratio"] = V / V.rolling(20).mean()
    I["liquid"] = (C > 10) & ((C * V).rolling(20).mean() > 50e6)
    spy_ok = spy_close > spy_close.rolling(200).mean()
    I["spy_ok"] = spy_ok.reindex(C.index).fillna(False)
    return I


def entry_mask(name, P, I):
    C = P["Close"]
    common = (I["mom_rank"] >= 0.8) & I["liquid"]
    if name == "V1 Geri çekilme":
        m = (C > I["sma50"]) & (I["sma50"] > I["sma200"]) & (I["ret5"] < 0)
    elif name == "V2 RSI+EMA":
        m = (C > I["ema50"]) & (I["ema50"] > I["ema200"]) & (I["rsi"] < 45)
    elif name == "V3 Bollinger":
        m = (C > I["sma200"]) & (I["sma50"] > I["sma200"]) & (C < I["bb_low"])
    else:
        m = (C > I["sma50"]) & (I["sma50"] > I["sma200"]) & (C >= I["hi20"]) & (I["vol_ratio"] > 1.2)
    return (m & common).fillna(False)


def simulate(name, P, I):
    O, H, L, C = (P[k] for k in ("Open", "High", "Low", "Close"))
    dates = C.index[C.index >= START]
    iso = dates.isocalendar()
    week = pd.Series(iso["year"].values * 100 + iso["week"].values, index=dates)
    signal_days = set(dates[:-1][week.values[:-1] != week.values[1:]])
    mask = entry_mask(name, P, I)
    cols = {t: j for j, t in enumerate(C.columns)}
    Ov, Hv, Lv, Cv, Av, Mv = O.values, H.values, L.values, C.values, I["atr"].values, I["mom63"].values
    di = {d: k for k, d in enumerate(C.index)}
    cash, pos, trades, equity = 1.0, [], [], []
    pending = []
    last_px = {}
    for d in dates:
        k = di[d]
        # --- yeni girişler (önceki günün sinyali)
        if pending:
            eq_prev = equity[-1][1] if equity else 1.0
            free = SLOTS - len(pos)
            held = {p["t"] for p in pos}
            for t, atr in pending:
                if free <= 0:
                    break
                j = cols[t]
                o = Ov[k, j]
                if t in held or not np.isfinite(o) or o <= 0 or not np.isfinite(atr):
                    continue
                alloc = min(eq_prev / SLOTS, cash)
                if alloc <= 0:
                    break
                sh = alloc * (1 - FEE) / o
                cash -= alloc
                pos.append({"t": t, "j": j, "sh": sh, "entry": o, "k0": k, "date": d,
                            "stop": o - STOP_ATR * atr, "tgt": o + TGT_ATR * atr})
                free -= 1
            pending = []
        # --- çıkışlar
        keep = []
        for p in pos:
            j = p["j"]
            o, h, l, c = Ov[k, j], Hv[k, j], Lv[k, j], Cv[k, j]
            if not np.isfinite(c):
                keep.append(p)
                continue
            first = k == p["k0"]
            px, why = None, None
            if not first and o <= p["stop"]:
                px, why = o, "stop"
            elif l <= p["stop"]:
                px, why = p["stop"], "stop"
            elif not first and o >= p["tgt"]:
                px, why = o, "hedef"
            elif h >= p["tgt"]:
                px, why = p["tgt"], "hedef"
            elif k - p["k0"] + 1 >= MAX_HOLD:
                px, why = c, "süre"
            if px is None:
                keep.append(p)
                last_px[j] = c
                continue
            cash += p["sh"] * px * (1 - FEE)
            trades.append({"t": p["t"], "giriş": p["date"], "çıkış": d, "ret": px / p["entry"] * (1 - FEE) ** 2 - 1,
                           "sebep": why, "gün": k - p["k0"] + 1})
        pos = keep
        val = cash + sum(p["sh"] * (Cv[k, p["j"]] if np.isfinite(Cv[k, p["j"]]) else last_px.get(p["j"], p["entry"]))
                         for p in pos)
        equity.append((d, val))
        # --- sinyal (haftanın son günü)
        if d in signal_days and I["spy_ok"].iloc[k] and len(pos) < SLOTS:
            row = mask.iloc[k]
            cands = [t for t in row.index[row.values]]
            cands.sort(key=lambda t: -Mv[k, cols[t]])
            pending = [(t, Av[k, cols[t]]) for t in cands[:SLOTS * 2]]
    eq = pd.Series(dict(equity))
    return eq, pd.DataFrame(trades)


def perf(eq, label, trades=None):
    r = eq.pct_change().dropna()
    yrs = len(r) / 252
    row = {"strateji": label,
           "yıllık_%": round(100 * ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1), 1),
           "toplam_%": round(100 * (eq.iloc[-1] / eq.iloc[0] - 1), 0),
           "max_düşüş_%": round(100 * (eq / eq.cummax() - 1).min(), 1),
           "Sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else 0}
    if trades is not None and len(trades):
        row.update({"işlem": len(trades),
                    "kazanma_%": round(100 * (trades["ret"] > 0).mean(), 0),
                    "ort_işlem_%": round(100 * trades["ret"].mean(), 2),
                    "hedef/stop/süre": "/".join(str(int((trades["sebep"] == s).sum()))
                                                for s in ("hedef", "stop", "süre"))})
    return row


def md(rows):
    df = pd.DataFrame(rows).astype(object).fillna("")
    out = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    f = lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    out += ["| " + " | ".join(f(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(out)


def yearly(series_map):
    rows = {}
    for name, eq in series_map.items():
        y = eq.groupby(eq.index.year).last()
        first = eq.iloc[0]
        prev = pd.Series([first] + list(y.values[:-1]), index=y.index)
        rows[name] = (100 * (y / prev - 1)).round(1)
    df = pd.DataFrame(rows)
    df.index.name = "yıl"
    return df.reset_index()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tick = universe()
    P = load(tick + ["SPY"])
    spy = P["Close"]["SPY"].copy()
    for k in P:
        P[k] = P[k].drop(columns=["SPY"], errors="ignore")
    I = indicators(P, spy)
    names = ["V1 Geri çekilme", "V2 RSI+EMA", "V3 Bollinger", "V4 Kırılım"]
    rows, curves, all_trades = [], {}, {}
    for n in names:
        eq, tr = simulate(n, P, I)
        curves[n], all_trades[n] = eq, tr
        rows.append(perf(eq, n, tr))
    spy_eq = spy[spy.index >= curves[names[0]].index[0]]
    spy_eq = spy_eq / spy_eq.iloc[0]
    curves["SPY al-tut"] = spy_eq
    rows.append(perf(spy_eq, "SPY al-tut"))
    lines = ["# SWING-15 backtest (haftalık seçim, en fazla 15 gün)", "",
             f"Evren: {P['Close'].shape[1]} S&P 500 hissesi (güncel liste - survivorship bias). "
             f"Dönem: {START} → {spy.index[-1].date()}. Maliyet her yön %{FEE * 100:.2f}.", "",
             md(rows), "", "## Yıl yıl getiri (%)", "", md(yearly(curves).to_dict("records")), ""]
    best = max(names, key=lambda n: curves[n].iloc[-1])
    tr = all_trades[best]
    if len(tr):
        tr = tr.sort_values("çıkış")
        lines += [f"## Son 10 işlem — {best}", "",
                  md([{"hisse": r.t, "giriş": r.giriş.date(), "çıkış": r.çıkış.date(),
                       "gün": r.gün, "sebep": r.sebep, "getiri_%": round(100 * r.ret, 1)}
                      for r in tr.tail(10).itertuples()]), ""]
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
