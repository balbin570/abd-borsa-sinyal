"""GUNLUK SECIM backtest'i (arastirma, emir yok).

Evren: guncel S&P 500 + Nasdaq 100 (survivorship bias: listeden cikan/batan sirketler yok).
Ortak filtreler: SPY > SMA200; C > SMA50 > SMA200; 63g getiri en iyi %20; fiyat > 10 $;
20g ort. islem hacmi > 50 M $; (varsa) 10 gun icinde bilanco yok.
Kurulumlar (herhangi biri):
  BB   : kapanis < alt Bollinger (20, 2)
  RSI  : RSI(14) < 40
  KIRIL: kapanis = 20g en yuksek kapanis ve hacim > 1.5 x 20g ort.
Her gun en fazla 4 yeni hisse (63g getiriye gore), en fazla 10 acik pozisyon, her biri ozsermayenin %10'u.
Giris: ertesi gun ACILIS. Maliyet: her yon %0.10.
Cikis varyantlari:
  A_SABIT    : stop -2ATR, hedef +3ATR, 15 gun
  B_BASABAS  : A + fiyat +1ATR olunca stop girise
  C_IZLEYEN  : stop -2ATR; +1ATR sonrasi en yuksek - 2ATR izleyen stop; hedef yok; 15 gun
  D_ORTALAMA : stop -2ATR; kapanis < SMA20 ise ertesi acilista cik; 15 gun
  Hepsinde: (bilanco verisi varsa) bilancodan onceki gun kapanista cik.
Gecmis filtresi (walk-forward, ileriye bakmadan): ayni hisse + ayni kurulumun daha once KAPANMIS
  sinyallerinin (A_SABIT ile) ortalamasi <= 0 ve en az 3 ornek varsa sinyal elenir.
"""
import concurrent.futures as cf
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

START = "2018-01-01"
MAX_NEW, SLOTS, FEE = 4, 10, 0.001
OUT = Path("research/daily_picks")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ResearchScanner/1.0)"}


# ------------------------------------------------------------------ veri
def universe():
    def wiki(url, col, minlen):
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        t = next(x for x in pd.read_html(io.StringIO(r.text)) if col in x.columns and len(x) > minlen)
        return {str(s).strip().replace(".", "-") for s in t[col].dropna()}
    sp = wiki("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", 450)
    try:
        nd = wiki("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", 90)
    except Exception:
        nd = set()
    return sorted(sp | nd)


def load(tickers):
    import yfinance as yf
    fr = {k: {} for k in ("Open", "High", "Low", "Close", "Volume")}
    for i in range(0, len(tickers), 100):
        ch = tickers[i:i + 100]
        df = yf.download(ch, start="2016-06-01", auto_adjust=True, progress=False, group_by="ticker", threads=True)
        for t in ch:
            try:
                sub = df[t]
            except KeyError:
                continue
            if sub["Close"].notna().sum() < 300:
                continue
            for k in fr:
                fr[k][t] = sub[k]
    return {k: pd.DataFrame(v).sort_index() for k, v in fr.items()}


def earnings_dates(tickers):
    import yfinance as yf

    def one(t):
        try:
            d = yf.Ticker(t).get_earnings_dates(limit=40)
            if d is None or d.empty:
                return t, []
            return t, sorted({ts.tz_localize(None).normalize() if ts.tzinfo else ts.normalize() for ts in d.index})
        except Exception:
            return t, []
    out = {}
    with cf.ThreadPoolExecutor(6) as ex:
        for t, ds in ex.map(one, tickers):
            out[t] = ds
    return out


# ------------------------------------------------------------------ gostergeler
def indicators(P, spy):
    C, H, L, V = P["Close"], P["High"], P["Low"], P["Volume"]
    I = {"sma20": C.rolling(20).mean(), "sma50": C.rolling(50).mean(), "sma200": C.rolling(200).mean()}
    d = C.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    I["rsi"] = 100 - 100 / (1 + up / dn)
    pc = C.shift(1)
    tr = np.maximum(np.maximum((H - L).values, (H - pc).abs().values), (L - pc).abs().values)
    I["atr"] = pd.DataFrame(tr, index=C.index, columns=C.columns).ewm(alpha=1 / 14, adjust=False).mean()
    mid, sd = C.rolling(20).mean(), C.rolling(20).std()
    I["bb_low"] = mid - 2 * sd
    I["mom63"] = C / C.shift(63) - 1
    I["mom_rank"] = I["mom63"].rank(axis=1, pct=True)
    I["hi20"] = C.rolling(20).max()
    I["vol_ratio"] = V / V.rolling(20).mean()
    I["liquid"] = (C > 10) & ((C * V).rolling(20).mean() > 50e6)
    I["spy_ok"] = (spy > spy.rolling(200).mean()).reindex(C.index).fillna(False)
    base = (C > I["sma50"]) & (I["sma50"] > I["sma200"]) & (I["mom_rank"] >= 0.8) & I["liquid"]
    setups = {
        "BB": (C < I["bb_low"]),
        "RSI": (I["rsi"] < 40),
        "KIRIL": (C >= I["hi20"]) & (I["vol_ratio"] > 1.5),
    }
    # BB ve RSI geri cekilme oldugu icin SMA50 sartini gevset: C > SMA200 ve SMA50 > SMA200 yeterli
    pull_base = (C > I["sma200"]) & (I["sma50"] > I["sma200"]) & (I["mom_rank"] >= 0.8) & I["liquid"]
    I["sig"] = {k: ((pull_base if k in ("BB", "RSI") else base) & v).fillna(False) for k, v in setups.items()}
    return I


# ------------------------------------------------------------------ tek islem simulasyonu
def run_trade(k0, j, exit_rule, A, earn_next):
    """k0: giris gunu indeksi (acilis). Donus: (cikis_k, cikis_fiyati, sebep)"""
    O, H, L, C, ATR, S20 = A
    e = O[k0, j]
    atr = ATR[k0 - 1, j]
    if not (np.isfinite(e) and np.isfinite(atr) and atr > 0):
        return None
    stop, tgt, peak = e - 2 * atr, e + 3 * atr, e
    be = False
    n = O.shape[0]
    pending_exit = False
    for k in range(k0, min(k0 + 15, n)):
        o, h, l, c = O[k, j], H[k, j], L[k, j], C[k, j]
        if not np.isfinite(c):
            continue
        if pending_exit and k > k0:
            return k, o, "ortalama"
        first = k == k0
        if not first and o <= stop:
            return k, o, "stop"
        if l <= stop:
            return k, stop, "stop"
        if exit_rule in ("A_SABIT", "B_BASABAS"):
            if not first and o >= tgt:
                return k, o, "hedef"
            if h >= tgt:
                return k, tgt, "hedef"
        peak = max(peak, h)
        if exit_rule == "B_BASABAS" and not be and peak >= e + atr:
            be, stop = True, max(stop, e)
        if exit_rule == "C_IZLEYEN" and peak >= e + atr:
            stop = max(stop, peak - 2 * atr)
        if exit_rule == "D_ORTALAMA" and np.isfinite(S20[k, j]) and c < S20[k, j]:
            pending_exit = True
        if earn_next is not None and earn_next[k] is not None and earn_next[k] <= 1:
            return k, c, "bilanco"
        if k == k0 + 14:
            return k, c, "sure"
    k = min(k0 + 14, n - 1)
    return (k, C[k, j], "acik") if np.isfinite(C[k, j]) else None


# ------------------------------------------------------------------ portfoy
def simulate(P, I, exit_rule, hist_filter, earn, dates, setups=("BB", "RSI", "KIRIL")):
    cols = list(P["Close"].columns)
    A = tuple(x.values for x in (P["Open"], P["High"], P["Low"], P["Close"], I["atr"], I["sma20"]))
    idx = P["Close"].index
    di = {d: k for k, d in enumerate(idx)}
    Mv = I["mom63"].values
    # bilanco: her gun icin "sonraki bilancoya kac islem gunu" dizisi (hisse bazinda, ihtiyac aninda)
    earn_cache = {}

    def earn_next_arr(j):
        if j in earn_cache:
            return earn_cache[j]
        ds = earn.get(cols[j]) if earn else None
        if not ds:
            earn_cache[j] = None
            return None
        pset = np.unique(np.searchsorted(idx.values, np.array(ds, dtype="datetime64[ns]")))
        ks = np.arange(len(idx))
        nx = np.searchsorted(pset, ks)  # ilk bilanco indeksi >= k
        arr = [int(pset[n] - k) if n < len(pset) else None for k, n in zip(ks, nx)]
        earn_cache[j] = arr
        return arr

    sig = {s: I["sig"][s].values for s in setups}
    history = {}  # (j, setup) -> list of (exit_k, ret) A_SABIT
    cash, pos, trades, eq = 1.0, [], [], []
    pending = []
    for d in dates:
        k = di[d]
        # yeni girisler
        if pending:
            eq_prev = eq[-1][1] if eq else 1.0
            for (j, s) in pending:
                if len(pos) >= SLOTS:
                    break
                if any(p["j"] == j for p in pos):
                    continue
                res = run_trade(k, j, exit_rule, A, earn_next_arr(j))
                if res is None:
                    continue
                alloc = min(eq_prev / SLOTS, cash)
                if alloc <= 0:
                    break
                cash -= alloc
                pos.append({"j": j, "s": s, "k0": k, "entry": A[0][k, j], "alloc": alloc,
                            "xk": res[0], "xp": res[1], "why": res[2]})
            pending = []
        # cikislar (bugun kapananlar)
        keep = []
        for p in pos:
            if p["xk"] <= k:
                r = p["xp"] / p["entry"] * (1 - FEE) ** 2 - 1
                cash += p["alloc"] * (1 + r)
                trades.append({"t": cols[p["j"]], "kurulum": p["s"], "giris": idx[p["k0"]], "cikis": idx[p["xk"]],
                               "ret": r, "sebep": p["why"], "gun": p["xk"] - p["k0"] + 1})
            else:
                keep.append(p)
        pos = keep
        val = cash + sum(p["alloc"] * (A[3][k, p["j"]] / p["entry"] if np.isfinite(A[3][k, p["j"]]) else 1) for p in pos)
        eq.append((d, val))
        # sinyaller
        if not I["spy_ok"].iloc[k] or k + 1 >= len(idx):
            continue
        cands = []
        for s in setups:
            js = np.nonzero(sig[s][k])[0]
            for j in js:
                en = earn_next_arr(j)
                if en is not None and en[k] is not None and en[k] <= 10:
                    continue
                if hist_filter:
                    h = [r for (xk, r) in history.get((j, s), []) if xk < k]
                    if len(h) >= 3 and np.mean(h) <= 0:
                        continue
                cands.append((Mv[k, j], j, s))
            # gecmis kaydi (A_SABIT sonucu) - filtre icin, portfoyden bagimsiz
            for j in js:
                res = run_trade(k + 1, j, "A_SABIT", A, None)
                if res:
                    history.setdefault((j, s), []).append((res[0], res[1] / A[0][k + 1, j] * (1 - FEE) ** 2 - 1))
        cands.sort(key=lambda x: -x[0])
        seen, pending = set(), []
        for _, j, s in cands:
            if j in seen:
                continue
            seen.add(j)
            pending.append((j, s))
            if len(pending) >= MAX_NEW:
                break
    return pd.Series(dict(eq)), pd.DataFrame(trades)


def perf(eq, label, tr=None):
    r = eq.pct_change().dropna()
    yrs = len(r) / 252
    row = {"strateji": label,
           "yillik_%": round(100 * ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1), 1),
           "max_dusus_%": round(100 * (eq / eq.cummax() - 1).min(), 1),
           "Sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() else 0}
    if tr is not None and len(tr):
        row.update({"islem": len(tr), "kazanma_%": round(100 * (tr["ret"] > 0).mean()),
                    "ort_islem_%": round(100 * tr["ret"].mean(), 2),
                    "cikislar": ", ".join(f"{k} {v}" for k, v in tr["sebep"].value_counts().items())})
    return row


def md(rows):
    df = pd.DataFrame(rows).astype(object).fillna("")
    f = lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    out = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    out += ["| " + " | ".join(f(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(out)


def yearly(curves):
    rows = {}
    for n, e in curves.items():
        y = e.groupby(e.index.year).last()
        prev = pd.Series([e.iloc[0]] + list(y.values[:-1]), index=y.index)
        rows[n] = (100 * (y / prev - 1)).round(1)
    return pd.DataFrame(rows).rename_axis("yil").reset_index()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tick = universe()
    P = load(tick + ["SPY"])
    spy = P["Close"]["SPY"].copy()
    for k in P:
        P[k] = P[k].drop(columns=["SPY"], errors="ignore")
    I = indicators(P, spy)
    earn = earnings_dates(list(P["Close"].columns))
    cov = sum(1 for v in earn.values() if v)
    dates = P["Close"].index[P["Close"].index >= START]
    rows, curves = [], {}
    best = None
    for hf in (False, True):
        for ex in ("A_SABIT", "B_BASABAS", "C_IZLEYEN", "D_ORTALAMA"):
            name = ex + (" + gecmis filtresi" if hf else "")
            eq, tr = simulate(P, I, ex, hf, earn, dates)
            curves[name] = eq
            rows.append(perf(eq, name, tr))
            print(rows[-1], flush=True)
            if best is None or rows[-1]["Sharpe"] > best[0]["Sharpe"]:
                best = (rows[-1], tr, name)
    setup_rows = []
    for s in ("BB", "RSI", "KIRIL"):
        eq, tr = simulate(P, I, "A_SABIT", False, earn, dates, setups=(s,))
        setup_rows.append(perf(eq, f"Sadece {s} (A_SABIT)", tr))
    spy_eq = spy[spy.index >= dates[0]]
    spy_eq = spy_eq / spy_eq.iloc[0]
    curves["SPY al-tut"] = spy_eq
    rows.append(perf(spy_eq, "SPY al-tut"))
    tr = best[1]
    lines = ["# GUNLUK SECIM backtest'i", "",
             f"Evren: {P['Close'].shape[1]} hisse (S&P 500 + Nasdaq 100, guncel liste - survivorship bias). "
             f"Donem: {START} -> {spy.index[-1].date()}. Bilanco tarihi bulunan hisse: {cov}. "
             f"Maliyet her yon %{FEE * 100:.2f}. Haber filtresi gecmise donuk test edilemedi.", "",
             "## Cikis varyantlari", md(rows), "", "## Kurulumlar ayri ayri", md(setup_rows), "",
             "## Yil yil getiri (%)", md(yearly(curves).to_dict("records")), "",
             f"## En iyi (Sharpe): {best[2]}", ""]
    if len(tr):
        by = tr.groupby("kurulum")["ret"].agg(["count", "mean", lambda x: (x > 0).mean()])
        by.columns = ["islem", "ort", "kazanma"]
        lines += ["Kuruluma gore:", md([{"kurulum": i, "islem": int(r.islem), "ort_%": round(100 * r.ort, 2),
                                         "kazanma_%": round(100 * r.kazanma)} for i, r in by.iterrows()]), ""]
        last = tr.sort_values("cikis").tail(10)
        lines += ["Son 10 islem:", md([{"hisse": r.t, "kurulum": r.kurulum, "giris": r.giris.date(),
                                        "cikis": r.cikis.date(), "sebep": r.sebep, "getiri_%": round(100 * r.ret, 1)}
                                       for r in last.itertuples()]), ""]
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
