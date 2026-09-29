"""
strategy_v3.py  -  Cross-sectional momentum + trend + market filter
===================================================================

Mevcut ABD Borsa Sinyal projesindeki analizlerin (Stage 5-7) sonucuna ve
literatürde en çok desteklenen etkiye dayanır: "güçlü olan güçlü kalmaya
meyillidir" (cross-sectional momentum) + trend filtresi + piyasa filtresi.

V2'den farkları
---------------
* Sinyal başına getiri yerine PORTFÖY düzeyinde simülasyon (özkaynak eğrisi,
  max drawdown, Sharpe, işlem sayısı).
* İşlem maliyeti / slipaj dahil (varsayılan taraf başına %0.10).
* ATR takip eden stop (gap durumunda açılıştan çıkış), risk bazlı pozisyon
  boyutu (işlem başına özkaynağın %1.5'i riske edilir).
* Piyasa filtresi: SPY < SMA100 ise nakde geç.
* Hisse seçimi mutlak eşik değil, evrendeki GÖRECELİ sıralama.
* Wilder RSI/ATR, vektörel hesap (O(n^2) yok).
* Walk-forward bölünme: parametreler sabit, dönem ikiye bölünüp ayrı raporlanır.
* Duyarlılık (sensitivity) testi: parametreler biraz değişince sonuç çöküyor mu?
* Canlı öneri listesi (/v3/picks): stop seviyesi ve önerilen pozisyon büyüklüğü.

Kullanım
--------
main.py içinde:

    from strategy_v3 import make_router
    app.include_router(
        make_router(lambda: STOCK_CACHE, lambda: SPY_CACHE, V2_UNIVERSE)
    )

veya komut satırından:

    python strategy_v3.py /code/data/research_snapshot.json

Gereksinim: pandas, numpy  (pip install pandas numpy)

UYARI: Geçmiş performans gelecek getiriyi garanti etmez. Evren bugünün
başarılı hisselerinden seçildiği için sonuçlar İYİMSER olur (survivorship
bias). Gerçek para öncesi mutlaka ileriye dönük (paper) takip yapın.
"""

import json
import math
import sys
from dataclasses import dataclass, asdict, replace

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------
# PARAMETRELER  (bilerek az sayıda; çok parametre = aşırı uyum)
# ----------------------------------------------------------------------
@dataclass
class Params:
    top_n: int = 5                  # aynı anda en fazla pozisyon
    hold_rank: int = 10             # pozisyon, sıralamada bu sıraya kadar tutulur
    rebalance_days: int = 5         # karar sıklığı (işlem günü)
    mom_long: int = 126             # ~6 ay
    mom_short: int = 63             # ~3 ay
    skip: int = 5                   # son 5 günü atla (kısa vadeli geri dönüş gürültüsü)
    sma_fast: int = 50
    sma_slow: int = 200
    market_sma: int = 100           # SPY filtresi
    atr_period: int = 14
    atr_mult: float = 3.0           # takip eden stop mesafesi (ATR katı)
    risk_per_trade: float = 0.015   # işlem başına riske edilen özkaynak oranı
    max_weight: float = 0.25        # tek hissede max özkaynak oranı
    cost_per_side: float = 0.0010   # komisyon + slipaj (taraf başına)
    initial_capital: float = 10000.0


# ----------------------------------------------------------------------
# VERİ
# ----------------------------------------------------------------------
def _to_df(values):
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return (
        df.sort_values("datetime")
        .drop_duplicates("datetime")
        .set_index("datetime")
    )


def build_panel(stock_data: dict, spy_values: list, universe=None, min_bars=300):
    """
    stock_data : {"AAPL": {"values": [ {datetime, open, high, low, close, volume}, ... ]}, ...}
                 (Twelve Data formatı; sıralama fark etmez)
    spy_values : SPY için aynı formatta liste
    """
    spy = _to_df(spy_values)
    dates = spy.index

    cols = {"open": {}, "high": {}, "low": {}, "close": {}}
    for sym in (universe or list(stock_data.keys())):
        d = stock_data.get(sym)
        if not d or "values" not in d:
            continue
        df = _to_df(d["values"])
        if len(df) < min_bars:
            continue
        df = df.reindex(dates)
        if (df["close"].dropna() <= 0).any():
            continue
        for k in cols:
            cols[k][sym] = df[k]

    if not cols["close"]:
        raise ValueError("Kullanılabilir hisse verisi yok")

    panel = {k: pd.DataFrame(v) for k, v in cols.items()}
    panel["dates"] = dates
    panel["symbols"] = list(panel["close"].columns)
    panel["spy_close"] = spy["close"]
    return panel


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    symbols = snap["symbols"]
    spy = symbols["SPY"]["values"]
    stocks = {k: v for k, v in symbols.items() if k != "SPY"}
    return build_panel(stocks, spy)


# ----------------------------------------------------------------------
# GÖSTERGELER (vektörel)
# ----------------------------------------------------------------------
def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad
    return 100 - 100 / (1 + rs)


def prepare(panel, p: Params):
    close, high, low = panel["close"], panel["high"], panel["low"]

    base = close.shift(p.skip)
    ret_l = base / close.shift(p.skip + p.mom_long) - 1
    ret_s = base / close.shift(p.skip + p.mom_short) - 1
    score = 0.5 * ret_l + 0.5 * ret_s

    sma_f = close.rolling(p.sma_fast).mean()
    sma_s = close.rolling(p.sma_slow).mean()

    prev_c = close.shift(1)
    tr = np.maximum(
        np.maximum(high - low, (high - prev_c).abs()),
        (low - prev_c).abs(),
    )
    atr = tr.ewm(alpha=1 / p.atr_period, adjust=False, min_periods=p.atr_period).mean()

    eligible = (close > sma_f) & (sma_f > sma_s) & (score > 0)

    spy = panel["spy_close"]
    market_ok = (spy > spy.rolling(p.market_sma).mean()).fillna(False)

    return {
        "score": score.values,
        "ret_l": ret_l.values,
        "atr": atr.values,
        "eligible": eligible.values,
        "market_ok": market_ok.values,
    }


# ----------------------------------------------------------------------
# SİMÜLASYON
#   Karar: gün t kapanışında  ->  emir: gün t+1 açılışında
#   Stop : ertesi günlerin en düşüğü stop'a değerse, min(açılış, stop)'ta çıkış
# ----------------------------------------------------------------------
def run(panel, p: Params, start_date=None, end_date=None):
    ind = prepare(panel, p)
    dates = panel["dates"]
    syms = panel["symbols"]

    O = panel["open"].values
    L = panel["low"].values
    C = panel["close"].values
    Cff = pd.DataFrame(C).ffill().values
    T = len(dates)

    warm = max(p.mom_long + p.skip, p.sma_slow, p.market_sma, p.atr_period) + 2
    t0 = warm
    if start_date:
        t0 = max(t0, int(dates.searchsorted(pd.Timestamp(start_date))))
    t_end = T - 1
    if end_date:
        t_end = min(t_end, int(dates.searchsorted(pd.Timestamp(end_date), side="right")) - 1)
    if t_end - t0 < 30:
        raise ValueError("Test dönemi çok kısa")

    score, atr, elig, mok = ind["score"], ind["atr"], ind["eligible"], ind["market_ok"]

    cash = p.initial_capital
    pos = {}                 # j -> dict
    pending_sell = set()
    pending_buy = {}         # j -> dolar
    equity, eq_dates = [], []
    trades = []
    invested_days = 0

    def close_position(j, px, t, reason):
        nonlocal cash
        q = pos.pop(j)
        cash += q["shares"] * px
        trades.append({
            "symbol": syms[j],
            "entry_date": str(dates[q["entry_t"]].date()),
            "exit_date": str(dates[t].date()),
            "entry_price": round(q["entry_px"], 2),
            "exit_price": round(px, 2),
            "return_pct": round((px / q["entry_px"] - 1) * 100, 2),
            "days_held": int(t - q["entry_t"]),
            "reason": reason,
        })

    for t in range(t0, t_end + 1):
        o = O[t]

        # 1) Bekleyen emirleri açılışta uygula (önce satış, sonra alış)
        for j in list(pending_sell):
            if j in pos and np.isfinite(o[j]):
                close_position(j, o[j] * (1 - p.cost_per_side), t, "signal")
        pending_sell.clear()

        for j, dollars in pending_buy.items():
            if j in pos or not np.isfinite(o[j]):
                continue
            px = o[j] * (1 + p.cost_per_side)
            dollars = min(dollars, cash)
            if dollars < 50:
                continue
            shares = dollars / px
            cash -= shares * px
            a = atr[t - 1, j]
            stop = o[j] - p.atr_mult * a if np.isfinite(a) else o[j] * 0.85
            pos[j] = {"shares": shares, "entry_px": px, "entry_t": t,
                      "hc": o[j], "stop": stop}
        pending_buy.clear()

        # 2) Stop kontrolü (gün içi düşük)
        for j in list(pos):
            lo = L[t, j]
            if np.isfinite(lo) and lo <= pos[j]["stop"]:
                base_px = o[j] if np.isfinite(o[j]) else pos[j]["stop"]
                px = min(base_px, pos[j]["stop"]) * (1 - p.cost_per_side)
                close_position(j, px, t, "stop")

        # 3) Kapanışta değerle ve takip eden stopları yukarı çek
        eq = cash + sum(q["shares"] * Cff[t, j] for j, q in pos.items())
        equity.append(eq)
        eq_dates.append(dates[t])
        if pos:
            invested_days += 1
        for j, q in pos.items():
            c = C[t, j]
            if np.isfinite(c):
                q["hc"] = max(q["hc"], c)
                a = atr[t, j]
                if np.isfinite(a):
                    q["stop"] = max(q["stop"], q["hc"] - p.atr_mult * a)

        # 4) Karar günü mü?
        if t < t_end and (t - t0) % p.rebalance_days == 0:
            if not mok[t]:
                pending_sell = set(pos.keys())        # piyasa zayıf -> nakit
            else:
                cand = np.where(elig[t])[0]
                order = cand[np.argsort(-score[t, cand])]
                rank = {int(j): r + 1 for r, j in enumerate(order)}
                keep = [j for j in pos if rank.get(j, 10 ** 9) <= p.hold_rank]
                pending_sell = set(pos.keys()) - set(keep)

                slots = p.top_n - len(keep)
                for j in order:
                    if slots <= 0:
                        break
                    j = int(j)
                    if j in pos:
                        continue
                    a, c = atr[t, j], C[t, j]
                    if not (np.isfinite(a) and np.isfinite(c) and a > 0):
                        continue
                    atrp = a / c
                    dollars = min(
                        p.max_weight * eq,
                        p.risk_per_trade * eq / (p.atr_mult * atrp),
                    )
                    pending_buy[j] = dollars
                    slots -= 1

    open_positions = [
        {
            "symbol": syms[j],
            "entry_date": str(dates[q["entry_t"]].date()),
            "entry_price": round(q["entry_px"], 2),
            "last_close": round(float(Cff[t_end, j]), 2),
            "stop": round(q["stop"], 2),
            "unrealized_pct": round((Cff[t_end, j] / q["entry_px"] - 1) * 100, 2),
        }
        for j, q in pos.items()
    ]

    return {
        "equity": np.array(equity),
        "dates": pd.DatetimeIndex(eq_dates),
        "trades": trades,
        "open_positions": open_positions,
        "exposure_pct": round(invested_days / len(equity) * 100, 1),
    }


# ----------------------------------------------------------------------
# METRİKLER
# ----------------------------------------------------------------------
def perf_metrics(eq, ppy=252):
    eq = np.asarray(eq, dtype=float)
    if len(eq) < 3 or eq[0] <= 0:
        return {}
    r = eq[1:] / eq[:-1] - 1
    years = len(r) / ppy
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 else 0.0
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1).min())
    sd = r.std(ddof=1)
    sharpe = float(r.mean() / sd * math.sqrt(ppy)) if sd > 0 else 0.0
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0
    return {
        "total_return_pct": round(float(eq[-1] / eq[0] - 1) * 100, 2),
        "cagr_pct": round(float(cagr) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(float(calmar), 2),
    }


def trade_stats(trades):
    if not trades:
        return {"trade_count": 0}
    r = np.array([t["return_pct"] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() < 0 else None
    return {
        "trade_count": int(len(r)),
        "win_rate_pct": round(float((r > 0).mean() * 100), 1),
        "avg_trade_pct": round(float(r.mean()), 2),
        "median_trade_pct": round(float(np.median(r)), 2),
        "avg_win_pct": round(float(wins.mean()), 2) if len(wins) else 0,
        "avg_loss_pct": round(float(losses.mean()), 2) if len(losses) else 0,
        "profit_factor": round(float(pf), 2) if pf else None,
        "stop_exits": int(sum(1 for t in trades if t["reason"] == "stop")),
    }


def _benchmarks(panel, dates):
    spy = panel["spy_close"].reindex(dates).ffill().values
    rets = panel["close"].reindex(dates).pct_change().mean(axis=1, skipna=True).fillna(0)
    ew = (1 + rets).cumprod().values
    return spy, ew


def backtest_report(panel, p: Params, start_date=None, end_date=None, split_date=None):
    res = run(panel, p, start_date, end_date)
    eq, d = res["equity"], res["dates"]
    spy, ew = _benchmarks(panel, d)

    if split_date:
        k = int(d.searchsorted(pd.Timestamp(split_date)))
    else:
        k = len(d) // 2
    k = min(max(k, 5), len(d) - 5)

    strat = perf_metrics(eq)
    spy_m = perf_metrics(spy)
    ins = perf_metrics(eq[: k + 1])
    oos = perf_metrics(eq[k:])
    ins_spy = perf_metrics(spy[: k + 1])
    oos_spy = perf_metrics(spy[k:])

    ts = trade_stats(res["trades"])
    notes = []
    if ts.get("trade_count", 0) < 30:
        notes.append("İşlem sayısı 30'un altında: istatistiksel olarak zayıf örnek.")
    if oos and oos_spy and oos["cagr_pct"] <= oos_spy["cagr_pct"]:
        notes.append("Out-of-sample dönemde strateji SPY al-tut'u geçemedi.")
    if oos and ins and oos["sharpe"] < 0.5 * ins["sharpe"]:
        notes.append("Out-of-sample Sharpe, in-sample'ın yarısından düşük: aşırı uyum şüphesi.")
    notes.append("Evren hindsight ile seçildi (survivorship bias): sonuçlar iyimser.")

    return {
        "params": asdict(p),
        "period": {
            "start": str(d[0].date()),
            "end": str(d[-1].date()),
            "split_date": str(d[k].date()),
            "trading_days": int(len(d)),
        },
        "strategy": strat,
        "benchmark_spy_buy_hold": spy_m,
        "benchmark_equal_weight_universe": perf_metrics(ew),
        "in_sample_half": {"strategy": ins, "spy": ins_spy},
        "out_of_sample_half": {"strategy": oos, "spy": oos_spy},
        "exposure_pct": res["exposure_pct"],
        "trades": ts,
        "open_positions": res["open_positions"],
        "last_trades": res["trades"][-25:],
        "notes": notes,
    }


def sensitivity(panel, base: Params, start_date=None, end_date=None):
    """Parametreler etrafında küçük bir ızgara: sonuç kararlı mı?"""
    rows = []
    for top_n in (3, 5, 8):
        for mom_long in (90, 126, 189):
            p = replace(base, top_n=top_n, mom_long=mom_long)
            try:
                res = run(panel, p, start_date, end_date)
            except ValueError:
                continue
            m = perf_metrics(res["equity"])
            spy, _ = _benchmarks(panel, res["dates"])
            m_spy = perf_metrics(spy)
            rows.append({
                "top_n": top_n,
                "mom_long": mom_long,
                "cagr_pct": m["cagr_pct"],
                "max_drawdown_pct": m["max_drawdown_pct"],
                "sharpe": m["sharpe"],
                "beats_spy_cagr": m["cagr_pct"] > m_spy["cagr_pct"],
                "trades": len(res["trades"]),
            })
    if not rows:
        return {"configs": []}
    beat = sum(r["beats_spy_cagr"] for r in rows)
    return {
        "configs": rows,
        "configs_beating_spy": f"{beat}/{len(rows)}",
        "median_sharpe": round(float(np.median([r["sharpe"] for r in rows])), 2),
        "worst_drawdown_pct": min(r["max_drawdown_pct"] for r in rows),
        "reading": (
            "Çoğu konfigürasyon SPY'ı geçiyor ve Sharpe benzerse sonuç kararlıdır. "
            "Sadece 1-2 hücre iyiyse bu büyük olasılıkla şanstır."
        ),
    }


# ----------------------------------------------------------------------
# CANLI ÖNERİLER (son tamamlanmış günlük bar üzerinden)
# ----------------------------------------------------------------------
def current_picks(panel, p: Params, limit=15):
    ind = prepare(panel, p)
    dates, syms = panel["dates"], panel["symbols"]
    t = len(dates) - 1
    C = panel["close"].values

    market_ok = bool(ind["market_ok"][t])
    out = {
        "data_date": str(dates[t].date()),
        "market_filter_ok": market_ok,
        "warning": "Son bar piyasa kapandıktan sonraki tamamlanmış bar olmalı.",
        "picks": [],
    }
    if not market_ok:
        out["message"] = "SPY piyasa filtresinin altında: strateji nakitte kalır."
        return out

    cand = np.where(ind["eligible"][t])[0]
    order = cand[np.argsort(-ind["score"][t, cand])][:limit]
    for rank, j in enumerate(order, 1):
        c, a = C[t, j], ind["atr"][t, j]
        if not (np.isfinite(c) and np.isfinite(a) and a > 0):
            continue
        atrp = a / c
        weight = min(p.max_weight, p.risk_per_trade / (p.atr_mult * atrp))
        rsi = rsi_wilder(panel["close"][syms[j]].dropna(), 14).iloc[-1]
        out["picks"].append({
            "rank": rank,
            "symbol": syms[j],
            "close": round(float(c), 2),
            "momentum_score_pct": round(float(ind["score"][t, j]) * 100, 1),
            "return_6m_pct": round(float(ind["ret_l"][t, j]) * 100, 1),
            "rsi14": round(float(rsi), 1),
            "atr_pct": round(float(atrp) * 100, 2),
            "initial_stop": round(float(c - p.atr_mult * a), 2),
            "suggested_weight_pct": round(float(weight) * 100, 1),
            "buy_candidate": rank <= p.top_n,
        })
    return out


# ----------------------------------------------------------------------
# FASTAPI ROUTER
# ----------------------------------------------------------------------
def make_router(get_stock_cache, get_spy_cache, universe=None):
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/v3", tags=["strategy-v3"])

    def _panel():
        stocks, spy = get_stock_cache(), get_spy_cache()
        if not stocks or not spy:
            raise HTTPException(
                status_code=400,
                detail="Veri yok: snapshot yüklü olmalı veya /v2/prepare-data çalıştırılmalı",
            )
        try:
            return build_panel(stocks, spy, universe)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # NOT: def (async değil) -> FastAPI bunları thread havuzunda çalıştırır,
    # CPU işi event loop'u bloklamaz.
    @router.get("/backtest")
    def v3_backtest(
        top_n: int = 5,
        mom_long: int = 126,
        atr_mult: float = 3.0,
        risk_per_trade: float = 0.015,
        cost_bps: float = 10.0,
        start_date: str = None,
        end_date: str = None,
        split_date: str = None,
    ):
        p = Params(top_n=top_n, mom_long=mom_long, atr_mult=atr_mult,
                   risk_per_trade=risk_per_trade, cost_per_side=cost_bps / 10000)
        try:
            return backtest_report(_panel(), p, start_date, end_date, split_date)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/sensitivity")
    def v3_sensitivity(start_date: str = None, end_date: str = None):
        return sensitivity(_panel(), Params(), start_date, end_date)

    @router.get("/picks")
    def v3_picks(top_n: int = 5, limit: int = 15):
        return current_picks(_panel(), Params(top_n=top_n), limit)

    return router


# ----------------------------------------------------------------------
# KOMUT SATIRI
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Kullanım: python strategy_v3.py research_snapshot.json")
        sys.exit(1)
    panel = load_snapshot(sys.argv[1])
    params = Params()
    print(json.dumps(backtest_report(panel, params), indent=2, ensure_ascii=False))
    print(json.dumps(sensitivity(panel, params), indent=2, ensure_ascii=False))
    print(json.dumps(current_picks(panel, params), indent=2, ensure_ascii=False))
