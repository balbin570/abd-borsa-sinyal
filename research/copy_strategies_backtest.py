"""Kopyalama stratejileri backtest'i (araştırma, emir yok).

Veri: Jakeschincariol/trader-skill araçları (SEC EDGAR 13F, House Clerk PTR) + Yahoo fiyatları.

1) PELOSI: Nancy Pelosi'nin PTR bildirimlerindeki her ALIM (hisse veya opsiyon -> dayanak hisse)
   bildirimin yayınlandığı günden sonraki ilk işlem günü açılışta alınır, 6 ve 12 ay tutulur.
   Karşılaştırma: aynı dönem SPY. Ayrıca "işlem tarihinde alsaydık" (gecikme maliyeti).
   Portföy: açık pozisyonlar eşit ağırlıklı; pozisyon yoksa nakit.
2) BUFFETT: Berkshire 13F'teki en büyük 5 / 10 hisse, her 13F yayınlandıktan sonraki
   işlem günü kapanışında yeniden dengelenir. Karşılaştırma: SPY ve BRK-B.

Kullanım: python -m research.copy_strategies_backtest  (TRADER_SKILL dizini env ile)
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

TS = Path(os.environ.get("TRADER_SKILL", "external/trader-skill"))
OUT = Path("research/copy_strategies")
UA = os.environ.get("SEC_USER_AGENT", "")
BERKSHIRE = "1067983"


def sh(args, timeout=180):
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, SEC_USER_AGENT=UA))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-400:])
    return json.loads(r.stdout)


# ------------------------------------------------------------------ fiyatlar
_PX = {}


def prices(tickers, start="2012-01-01"):
    import yfinance as yf
    need = [t for t in tickers if t not in _PX]
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        df = yf.download(chunk, start=start, auto_adjust=True, progress=False,
                         group_by="ticker", threads=True)
        for t in chunk:
            try:
                sub = df[t] if len(chunk) > 1 else df
                sub = sub[["Open", "Close"]].dropna()
                _PX[t] = sub if len(sub) else None
            except Exception:
                _PX[t] = None
    return {t: _PX.get(t) for t in tickers}


def yahoo(t):
    return t.replace(".", "-").replace("/", "-")


# ------------------------------------------------------------------ metrik
def perf(daily: pd.Series, label: str) -> dict:
    daily = daily.dropna()
    eq = (1 + daily).cumprod()
    yrs = len(daily) / 252
    return {"strateji": label,
            "dönem": f"{daily.index[0].date()} → {daily.index[-1].date()}",
            "toplam_%": round(100 * (eq.iloc[-1] - 1), 1),
            "yıllık_%": round(100 * (eq.iloc[-1] ** (1 / yrs) - 1), 1),
            "max_düşüş_%": round(100 * (eq / eq.cummax() - 1).min(), 1),
            "yıllık_oynaklık_%": round(100 * daily.std() * np.sqrt(252), 1)}


def md(rows):
    df = pd.DataFrame(rows).astype(object)
    out = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    out += ["| " + " | ".join(str(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(out)


# ------------------------------------------------------------------ PELOSI
def pelosi_signals(years):
    sigs, log = [], []
    pdf_dir = OUT / "ptr-pdfs"
    for y in years:
        try:
            idx = sh([str(TS / "trader-pelosi/ptr.py"), "index", "--year", str(y),
                      "--member", "pelosi", "--json"])
        except Exception as e:
            log.append(f"{y} index hatası: {e}")
            continue
        for f in idx:
            if f.get("first", "").lower() != "nancy":
                continue
            try:
                doc = sh([str(TS / "trader-pelosi/ptr.py"), "fetch", f["doc_id"], "--year", str(y),
                          "--filed", f["filed"], "--pdf-dir", str(pdf_dir), "--json"])
            except Exception as e:
                log.append(f"{f['doc_id']} okunamadı: {str(e)[:120]}")
                continue
            time.sleep(0.3)
            for r in doc.get("rows", []):
                if not r.get("ticker") or not str(r.get("type", "")).startswith("P"):
                    continue
                sigs.append({"filed": f["filed"], "trade_date": r.get("date"),
                             "ticker": r["ticker"].upper(), "asset_type": r.get("asset_type"),
                             "amount_low": r.get("amount_low") or 0, "doc": f["doc_id"]})
    df = pd.DataFrame(sigs)
    if len(df):
        df = df.drop_duplicates(["filed", "ticker"])
    return df, log


def next_bar(px, day):
    i = px.index.searchsorted(pd.Timestamp(day), side="right")
    return i if i < len(px) else None


def pelosi_backtest():
    years = list(range(2019, pd.Timestamp.today().year + 1))
    sig, log = pelosi_signals(years)
    lines = ["## 1) Pelosi kopyalama", ""]
    if sig.empty:
        return "\n".join(lines + ["Sinyal bulunamadı.", *log]), None
    tick = sorted(set(map(yahoo, sig["ticker"]))) + ["SPY"]
    px = prices(tick, start="2018-06-01")
    spy = px["SPY"]
    rows, positions = [], []
    for s in sig.itertuples():
        p = px.get(yahoo(s.ticker))
        if p is None or len(p) < 10:
            continue
        i = next_bar(p, s.filed)
        if i is None:
            continue
        entry_day, entry = p.index[i], p["Open"].iloc[i]
        for hold in (126, 252):
            j = min(i + hold, len(p) - 1)
            if j <= i:
                continue
            r = p["Close"].iloc[j] / entry - 1
            si = spy.index.searchsorted(entry_day)
            sj = spy.index.searchsorted(p.index[j])
            if sj >= len(spy):
                continue
            rs = spy["Close"].iloc[sj] / spy["Open"].iloc[si] - 1
            complete = (i + hold) < len(p)
            rows.append({"hold": hold, "ticker": s.ticker, "filed": s.filed, "ret": r,
                         "spy": rs, "complete": complete, "asset": s.asset_type})
        # işlem tarihinde alsaydık (gecikme etkisi), 126 gün
        if s.trade_date:
            k = p.index.searchsorted(pd.Timestamp(s.trade_date))
            if k < len(p) and k + 126 < len(p):
                rows.append({"hold": "trade126", "ticker": s.ticker, "filed": s.filed,
                             "ret": p["Close"].iloc[k + 126] / p["Open"].iloc[k] - 1,
                             "spy": spy["Close"].iloc[min(spy.index.searchsorted(p.index[k + 126]), len(spy) - 1)]
                             / spy["Open"].iloc[spy.index.searchsorted(p.index[k])] - 1,
                             "complete": True, "asset": s.asset_type})
        positions.append((s.ticker, entry_day, i))
    r = pd.DataFrame(rows)
    summ = []
    for key, label in ((126, "Bildirimden sonra al, 6 ay tut"), (252, "Bildirimden sonra al, 12 ay tut"),
                       ("trade126", "(Hayali) işlem gününde al, 6 ay tut")):
        g = r[(r["hold"] == key) & r["complete"]]
        if g.empty:
            continue
        summ.append({"senaryo": label, "sinyal": len(g),
                     "ort_getiri_%": round(100 * g["ret"].mean(), 1),
                     "medyan_%": round(100 * g["ret"].median(), 1),
                     "SPY_aynı_dönem_%": round(100 * g["spy"].mean(), 1),
                     "SPY'ı_geçen_%": round(100 * (g["ret"] > g["spy"]).mean(), 0),
                     "fark_%": round(100 * (g["ret"] - g["spy"]).mean(), 1)})
    lines += [f"{len(sig)} alım sinyali ({sig['filed'].min()} → {sig['filed'].max()}), "
              f"{sig['ticker'].nunique()} farklı hisse. Opsiyon alımları dayanak hisse olarak sayıldı.", "",
              md(summ), ""]

    # portföy: her sinyal 126 gün, açık pozisyonlar eşit ağırlık
    rets = {t: px[yahoo(t)]["Close"].pct_change() for t in set(sig["ticker"]) if px.get(yahoo(t)) is not None}
    days = spy.index[spy.index >= pd.Timestamp(sig["filed"].min())]
    port = pd.Series(0.0, index=days)
    count = pd.Series(0, index=days)
    contrib = pd.Series(0.0, index=days)
    for t, entry_day, i in positions:
        rr = rets.get(t)
        if rr is None:
            continue
        p = px[yahoo(t)]
        window = p.index[i + 1:i + 127]
        w = rr.reindex(window).dropna()
        w = w[w.index.isin(days)]
        contrib.loc[w.index] += w.values
        count.loc[w.index] += 1
    port = (contrib / count.replace(0, np.nan)).fillna(0.0)
    spy_r = spy["Close"].pct_change().reindex(days).fillna(0.0)
    prow = [perf(port, "Pelosi kopya portföyü (6 ay tut)"), perf(spy_r, "SPY al-tut")]
    exposure = round(100 * (count > 0).mean(), 0)
    lines += ["### Portföy olarak (açık pozisyonlar eşit ağırlıklı)", md(prow), "",
              f"Portföyün yatırımda olduğu gün oranı: %{exposure:.0f}. "
              f"Aynı anda en fazla {int(count.max())} pozisyon.", ""]
    best = r[(r["hold"] == 126) & r["complete"]].sort_values("ret")
    if len(best) >= 6:
        lines += ["En iyi 3 / en kötü 3 sinyal (6 ay):", ""]
        for x in list(best.tail(3).itertuples())[::-1] + list(best.head(3).itertuples()):
            lines.append(f"- {x.ticker} (bildirim {x.filed}): %{100 * x.ret:+.0f}  (SPY %{100 * x.spy:+.0f})")
        lines.append("")
    if log:
        lines += ["Notlar: " + "; ".join(log[:8]), ""]
    return "\n".join(lines), r


# ------------------------------------------------------------------ BUFFETT
def buffett_backtest():
    lines = ["## 2) Buffett (Berkshire 13F) kopyalama", ""]
    fl = sh([str(TS / "trader-copycat/edgar13f.py"), "filings", BERKSHIRE, "--limit", "200", "--json"])
    filings = [f for f in fl["filings"] if f["form"] == "13F-HR"]
    filings.sort(key=lambda f: f["filed"])
    books = {5: [], 10: []}
    for f in filings:
        for top in (5, 10):
            try:
                tg = sh([str(TS / "trader-copycat/edgar13f.py"), "copy", BERKSHIRE, "--top", str(top),
                         "--accession", f["accession"], "--dry-run", "--json"])
                books[top].append((f["filed"], {w["ticker"]: w["weight"] / 100 for w in tg["weights"]
                                                 if w.get("ticker")}))
            except Exception as e:
                lines.append(f"- {f['filed']} top{top} okunamadı: {str(e)[:100]}")
            time.sleep(0.2)
    if not books[5]:
        return "\n".join(lines + ["13F verisi alınamadı."])
    allt = sorted({yahoo(t) for top in books for _, w in books[top] for t in w} | {"SPY", "BRK-B"})
    px = prices(allt, start="2012-01-01")
    close = pd.DataFrame({t: p["Close"] for t, p in px.items() if p is not None})
    rets = close.pct_change()
    rows = []
    hist_md = []
    for top in (5, 10):
        b = books[top]
        start = close.index[close.index.searchsorted(pd.Timestamp(b[0][0]), side="right")]
        days = close.index[close.index >= start]
        daily = pd.Series(0.0, index=days)
        for k, (filed, w) in enumerate(b):
            d0 = close.index.searchsorted(pd.Timestamp(filed), side="right")
            d1 = (close.index.searchsorted(pd.Timestamp(b[k + 1][0]), side="right")
                  if k + 1 < len(b) else len(close.index))
            seg = close.index[d0 + 1:d1 + 1]   # rebalans kapanışta, getiri ertesi günden
            ww = {yahoo(t): v for t, v in w.items() if yahoo(t) in rets.columns}
            if not ww or seg.empty:
                continue
            tot = sum(ww.values())
            ww = {t: v / tot for t, v in ww.items()}  # bulunamayan hisseler -> yeniden ölçekle
            # dönem içi ağırlık kayması: al-tut (dönem başı ağırlıkla bileşik)
            g = (1 + rets.loc[seg, list(ww)].fillna(0)).cumprod()
            val = (g * pd.Series(ww)).sum(axis=1)
            seg_r = val.pct_change()
            seg_r.iloc[0] = val.iloc[0] - 1
            daily.loc[seg] = seg_r.values
            if top == 5:
                hist_md.append(f"- {filed}: " + ", ".join(f"{t} %{100 * v:.0f}" for t, v in w.items()))
        rows.append(perf(daily, f"Buffett ilk {top} kopya (13F sonrası)"))
    s = rows[0]["dönem"].split(" → ")[0]
    for t, lbl in (("SPY", "SPY al-tut"), ("BRK-B", "Berkshire hissesi (BRK-B) al-tut")):
        rows.append(perf(rets[t][rets.index >= pd.Timestamp(s)], lbl))
    lines += [f"{len(books[5])} adet 13F ({books[5][0][0]} → {books[5][-1][0]}). "
              "Her 13F yayınlandıktan sonraki gün kapanışında yeniden dengelendi.", "",
              md(rows), "", "<details><summary>İlk 5 portföy geçmişi</summary>", "", *hist_md,
              "", "</details>", ""]
    return "\n".join(lines)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    parts = ["# Kopyalama stratejileri backtest'i", "",
             "Komisyon ve vergi dahil değil. Geçmiş sonuç gelecek garantisi değildir.", ""]
    for fn in (pelosi_backtest, buffett_backtest):
        try:
            res = fn()
            parts.append(res[0] if isinstance(res, tuple) else res)
        except Exception as e:
            import traceback
            parts.append(f"## {fn.__name__} HATA\n```\n{traceback.format_exc()[-1500:]}\n```")
    text = "\n".join(parts)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
