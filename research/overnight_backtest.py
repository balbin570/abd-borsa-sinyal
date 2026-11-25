"""Overnight strateji backtest'i (sadece araştırma, emir yok).

Karşılaştırılan stratejiler (her biri SMA200 filtreli ve filtresiz):
  A  KLASİK      : kapanışta al -> ertesi gün açılışta sat
  B  KURAL       : kapanışta al -> açılış < alış ise açılışta sat;
                   değilse %2 takip eden stop, en geç kapanışa 30 dk kala sat
                   (1 saatlik barlarla, son ~2 yıl; Yahoo 60m limiti)
  B' KURAL-GÜNLÜK: aynı kural ama stop yok (açılış kârdaysa 15:30 yerine kapanışta çık)
                   -> uzun dönem (günlük barlar) için yaklaşık karşılaştırma
  AL-TUT         : karşılaştırma için

Maliyet: işlem başına her yön COST_BPS (komisyon + kayma).
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COST_BPS = 2.0          # her yön 0.02%
TRAIL = 0.02
SMA = 200
OUT = Path("research/overnight_backtest")


# ------------------------------------------------------------------ metrikler
def stats(rets: pd.Series, n_days: int, label: str) -> dict:
    rets = rets.dropna()
    if rets.empty:
        return {"strateji": label, "işlem": 0}
    eq = (1 + rets).cumprod()
    years = n_days / 252
    total = eq.iloc[-1] - 1
    cagr = (eq.iloc[-1]) ** (1 / years) - 1 if years > 0 else float("nan")
    dd = (eq / eq.cummax() - 1).min()
    return {
        "strateji": label,
        "işlem": int(len(rets)),
        "kazanma_%": round(100 * (rets > 0).mean(), 1),
        "ort_işlem_%": round(100 * rets.mean(), 3),
        "toplam_%": round(100 * total, 1),
        "yıllık_%": round(100 * cagr, 1),
        "max_düşüş_%": round(100 * dd, 1),
    }


def cost():
    return 2 * COST_BPS / 10000


# ------------------------------------------------------------------ günlük stratejiler
def daily_strategies(d: pd.DataFrame):
    """d: Open, Close (düzeltilmiş), index=tarih. Getiri serileri döndürür (giriş günü indeksli)."""
    d = d.copy()
    d["sma"] = d["Close"].rolling(SMA).mean()
    d["filt"] = d["Close"] > d["sma"]
    nxt_open = d["Open"].shift(-1)
    nxt_close = d["Close"].shift(-1)

    classic = nxt_open / d["Close"] - 1 - cost()
    # B': açılış zarardaysa açılışta çık, değilse kapanışta çık
    rule_d = np.where(nxt_open < d["Close"], nxt_open / d["Close"] - 1,
                      nxt_close / d["Close"] - 1) - cost()
    rule_d = pd.Series(rule_d, index=d.index)
    valid = d["sma"].notna() & nxt_open.notna()
    out = {
        "A klasik": classic[valid],
        "A klasik + SMA200": classic[valid & d["filt"]],
        "B' kural (stop yok)": rule_d[valid],
        "B' kural (stop yok) + SMA200": rule_d[valid & d["filt"]],
    }
    bh = d["Close"][valid].pct_change().dropna()
    out["AL-TUT"] = bh
    return out, int(valid.sum())


# ------------------------------------------------------------------ saatlik kural
def hourly_rule(daily: pd.DataFrame, hourly: pd.DataFrame, use_filter: bool):
    """daily: Close + sma (alış fiyatı & filtre); hourly: Open/High/Low, ET indeksli.
    Saatlik barlar 9:30, 10:30 ... 15:30. Son bar (15:30) açılışında çıkılır."""
    hourly = hourly.copy()
    hourly["day"] = hourly.index.date
    groups = {k: g for k, g in hourly.groupby("day")}
    days = sorted(groups)
    rets, reasons = {}, []
    for i in range(len(days) - 1):
        d0, d1 = days[i], days[i + 1]
        ts0 = pd.Timestamp(d0)
        if ts0 not in daily.index:
            continue
        row = daily.loc[ts0]
        if math.isnan(row["sma"]):
            continue
        if use_filter and not row["Close"] > row["sma"]:
            continue
        buy = row["Close"]
        g = groups[d1].sort_index()
        if len(g) < 2:
            continue
        o = g["Open"].iloc[0]
        if o < buy:
            px, why = o, "açılış zararda"
        else:
            peak, px, why = o, None, None
            bars = g.iloc[:-1]            # 15:30 barından önceki barlar
            for _, b in bars.iterrows():
                stop = peak * (1 - TRAIL)
                if b["Open"] <= stop:
                    px, why = b["Open"], "stop"
                    break
                if b["Low"] <= stop:
                    px, why = stop, "stop"
                    break
                peak = max(peak, b["High"])
                stop = peak * (1 - TRAIL)
                if b["Low"] <= stop:      # temkinli: tepe sonrası düşüş aynı barda
                    px, why = stop, "stop"
                    break
            if px is None:
                px, why = g["Open"].iloc[-1], "süre doldu"
        rets[ts0] = px / buy - 1 - cost()
        reasons.append(why)
    return pd.Series(rets).sort_index(), reasons


def hourly_classic(daily, hourly, use_filter):
    hourly = hourly.copy()
    hourly["day"] = hourly.index.date
    opens = hourly.groupby("day")["Open"].first()
    days = list(opens.index)
    rets = {}
    for i in range(len(days) - 1):
        ts0 = pd.Timestamp(days[i])
        if ts0 not in daily.index or math.isnan(daily.loc[ts0, "sma"]):
            continue
        r = daily.loc[ts0]
        if use_filter and not r["Close"] > r["sma"]:
            continue
        rets[ts0] = opens.iloc[i + 1] / r["Close"] - 1 - cost()
    return pd.Series(rets).sort_index()


# ------------------------------------------------------------------ ana
def table(rows):
    keys = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    lines = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    return "\n".join(lines)


def run(symbol):
    import yfinance as yf
    t = yf.Ticker(symbol)
    d = t.history(period="max", interval="1d", auto_adjust=True)[["Open", "High", "Low", "Close"]].dropna()
    d.index = pd.to_datetime(d.index.date)
    d = d[d.index >= "2000-01-01"]
    md = [f"## {symbol}"]

    # --- uzun dönem (günlük)
    for start, label in (("2000-01-01", "2000-bugün"), ("2016-01-01", "son 10 yıl")):
        sub = d[d.index >= pd.Timestamp(start) - pd.Timedelta(days=400)]
        strat, n = daily_strategies(sub)
        rows = []
        for k, s in strat.items():
            s = s[s.index >= start]
            rows.append(stats(s, len(sub[sub.index >= start]), k))
        md += [f"### Günlük barlar, {label}", table(rows), ""]

    # --- son ~2 yıl, saatlik (tam kural)
    h = t.history(period="720d", interval="60m", auto_adjust=True)[["Open", "High", "Low", "Close"]].dropna()
    h.index = h.index.tz_convert("America/New_York")
    h = h[(h.index.time >= pd.Timestamp("09:30").time()) & (h.index.time < pd.Timestamp("16:00").time())]
    # saatlik verideki fiyatlar günlük düzeltilmiş fiyatlarla uyumlu olsun: aynı gün kapanış oranıyla ölçekle
    dd = d.copy()
    dd["sma"] = dd["Close"].rolling(SMA).mean()
    hday_close = h.groupby(h.index.date)["Close"].last()
    scale = (dd["Close"].reindex(pd.to_datetime(hday_close.index)).values / hday_close.values)
    scale = pd.Series(scale, index=hday_close.index)
    for c in ("Open", "High", "Low", "Close"):
        h[c] = h[c] * pd.Series(h.index.date, index=h.index).map(scale).values
    h = h.dropna()
    n_days = h.index.normalize().nunique()
    rows, reason_md = [], []
    for filt in (False, True):
        tag = " + SMA200" if filt else ""
        rc = hourly_classic(dd, h, filt)
        rows.append(stats(rc, n_days, "A klasik" + tag))
        rr, why = hourly_rule(dd, h, filt)
        rows.append(stats(rr, n_days, "B SENİN KURALIN" + tag))
        if why:
            vc = pd.Series(why).value_counts()
            reason_md.append(f"- B{tag}: " + ", ".join(f"{k} {v}" for k, v in vc.items()))
    bh = dd["Close"][dd.index >= pd.Timestamp(h.index[0].date())].pct_change().dropna()
    rows.append(stats(bh, n_days, "AL-TUT"))
    start, end = h.index[0].date(), h.index[-1].date()
    md += [f"### Saatlik barlar (tam kural, %2 stop), {start} → {end}", table(rows), "",
           "Çıkış sebepleri:", *reason_md, ""]
    return "\n".join(md)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    parts = ["# Overnight backtest", "",
             f"Maliyet: her yön %{COST_BPS / 100:.2f}. SMA200 filtresi: alış günü kapanışı > 200 günlük ortalama.",
             "Saatlik testte stop sırası temkinli varsayılır (aynı barda önce tepe sonra düşüş).", ""]
    for sym in sys.argv[1:] or ["QQQ", "SPY"]:
        try:
            parts.append(run(sym))
        except Exception as e:
            parts.append(f"## {sym}\nHATA: {type(e).__name__}: {e}")
    text = "\n".join(parts)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
