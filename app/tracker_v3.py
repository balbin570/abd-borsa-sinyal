"""
tracker_v3.py  -  İleriye dönük (paper) takip + izleme paneli
=============================================================

Amaç: strategy_v3 kurallarını DONDURUP, gerçek zamanlı olarak seçimleri kaydetmek
ve birkaç ay sonra "bu seçimlerde gerçekten bir bilgi var mı?" sorusunu
cevaplamak. Para riski yoktur.

Nasıl çalışır
-------------
1. /v3/track/run (dışarıdan cron ile her iş günü ABD kapanışından sonra çağrılır):
   a) 71 sembol için son ~200 günlük barı Twelve Data'dan çeker ve snapshot'a ekler
      (dakikada 7 çağrı -> yaklaşık 10 dakika, arka planda çalışır).
   b) Son kohort üzerinden >= 5 işlem günü geçtiyse YENİ KOHORT kaydeder:
      o günün top_n seçimi (dondurulmuş kurallarla).
   c) Tüm kohortların 5/10/20 günlük sonuçlarını hesaplar; SPY, tüm evren ve
      rastgele seçimle kıyaslar.
   d) İsteğe bağlı: Telegram'a özet gönderir (kalıcı, zaman damgalı bir kayıt olur).
2. /v3/dashboard: piyasa filtresi, güncel seçimler, takip özeti, 70 hissenin durumu.

Kalıcılık (ÖNEMLİ)
------------------
Kohortlar TRACK_DIR (varsayılan /code/track) içindeki cohorts.jsonl dosyasına yazılır.
Render'ın ücretsiz planında dosya sistemi her deploy/yeniden başlatmada SIFIRLANIR.
Bu yüzden ya (a) Render Disk bağlayıp TRACK_DIR'i oraya ayarlayın, ya da (b)
Telegram bildirimini açık tutun: her kohort Telegram'a da yazılır ve kayıp
olursa oradan geri yüklenebilir.

Ortam değişkenleri (zaten olanlar): TWELVE_DATA_API_KEY, WEBHOOK_SECRET,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. İsteğe bağlı: TRACK_DIR.

Kullanım (main.py en altı):

    from app.tracker_v3 import make_tracker_router
    app.include_router(
        make_tracker_router(lambda: STOCK_CACHE, lambda: SPY_CACHE, V2_UNIVERSE)
    )

Bu bir alım tavsiyesi değildir; araştırma ve paper takip aracıdır.
"""

import asyncio
import hashlib
import html
import json
import os
import secrets
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

try:
    from app import strategy_v3 as sv
except ImportError:  # yerel test
    import strategy_v3 as sv


# ----------------------------------------------------------------------
# DONDURULMUŞ KURALLAR
# ----------------------------------------------------------------------
RULES = sv.Params()   # varsayılan değerler = dondurulmuş kurallar
RULES_HASH = hashlib.sha1(
    json.dumps(asdict(RULES), sort_keys=True).encode()
).hexdigest()[:10]

COHORT_SPACING_DAYS = 5      # yeni kohort için asgari işlem günü aralığı
MIN_COHORTS_FOR_VERDICT = 26  # ~6 ay haftalık kohort

TRACK_DIR = Path(os.getenv("TRACK_DIR", "/code/track"))
COHORT_FILE = TRACK_DIR / "cohorts.jsonl"
LIVE_FILE = TRACK_DIR / "live_prices.json"

STATE = {
    "running": False,
    "started_utc": None,
    "finished_utc": None,
    "progress": "",
    "last_result": None,
    "last_error": None,
}
LIVE = {}            # sembol -> {tarih: bar}
_LIVE_LOADED = False
_LIVE_VERSION = 0
_PANEL_CACHE = {"key": None, "panel": None}


# ----------------------------------------------------------------------
# DOSYA G/Ç
# ----------------------------------------------------------------------
def _ensure_dir():
    try:
        TRACK_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


def load_cohorts():
    if not COHORT_FILE.exists():
        return []
    out = []
    with COHORT_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return sorted(out, key=lambda c: c["signal_date"])


def append_cohort(cohort):
    if not _ensure_dir():
        return False
    with COHORT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(cohort, ensure_ascii=False) + "\n")
    return True


def _load_live():
    global _LIVE_LOADED, LIVE, _LIVE_VERSION
    if _LIVE_LOADED:
        return
    _LIVE_LOADED = True
    try:
        if LIVE_FILE.exists():
            LIVE = json.loads(LIVE_FILE.read_text(encoding="utf-8"))
            _LIVE_VERSION += 1
    except Exception:
        LIVE = {}


def _save_live():
    if not _ensure_dir():
        return
    try:
        LIVE_FILE.write_text(json.dumps(LIVE), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------
# VERİ ÇEKME
# ----------------------------------------------------------------------
def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # tzdata yoksa sabit UTC-5 (yaz saatinde daha temkinli davranır)
        return datetime.now(timezone(timedelta(hours=-5)))


def _bar_is_incomplete(bar_date: str) -> bool:
    """Bugüne ait bar, ABD kapanışından (16:30 ET) önce tamamlanmamıştır."""
    now = _now_et()
    if bar_date[:10] != now.strftime("%Y-%m-%d"):
        return False
    return now.hour * 60 + now.minute < 16 * 60 + 30


async def fetch_recent(symbols, api_key, outputsize=200, per_minute=7, progress=None):
    """Twelve Data'dan son barları çeker. Ücretsiz plan limiti için yavaştır."""
    out, errors = {}, {}
    url = "https://api.twelvedata.com/time_series"
    async with httpx.AsyncClient(timeout=30) as client:
        for n, sym in enumerate(symbols, 1):
            for attempt in range(3):
                try:
                    r = await client.get(url, params={
                        "symbol": sym, "interval": "1day",
                        "outputsize": outputsize, "apikey": api_key,
                    })
                    data = r.json()
                except Exception as e:
                    errors[sym] = str(e)[:200]
                    break
                if data.get("code") == 429 and attempt < 2:
                    await asyncio.sleep(61)
                    continue
                vals = data.get("values")
                if not vals:
                    errors[sym] = str(data)[:200]
                else:
                    bars = {}
                    for it in vals:
                        if _bar_is_incomplete(it["datetime"]):
                            continue
                        bars[it["datetime"]] = it
                    out[sym] = bars
                break
            if progress:
                progress(f"{n}/{len(symbols)} {sym}")
            if n % per_minute == 0 and n < len(symbols):
                await asyncio.sleep(61)
    return out, errors


def _merge_live(new_bars):
    global LIVE, _LIVE_VERSION
    _load_live()
    for sym, bars in new_bars.items():
        cur = LIVE.setdefault(sym, {})
        cur.update(bars)
        for d in sorted(cur.keys())[:-300]:   # son 300 barı tut
            del cur[d]
    _LIVE_VERSION += 1
    _save_live()


def merged_panel(stock_cache, spy_values, universe=None):
    """Snapshot (tarihsel) + canlı barlar -> panel. STOCK_CACHE'e dokunmaz."""
    _load_live()
    key = (_LIVE_VERSION, len(stock_cache or {}), len(spy_values or []))
    if _PANEL_CACHE["key"] == key and _PANEL_CACHE["panel"] is not None:
        return _PANEL_CACHE["panel"]

    def merge(base_values, extra):
        d = {it["datetime"]: it for it in base_values}
        if extra:
            d.update(extra)
        return list(d.values())

    stocks = {
        s: {"values": merge(v["values"], LIVE.get(s))}
        for s, v in (stock_cache or {}).items()
        if isinstance(v, dict) and "values" in v
    }
    spy = merge(spy_values or [], LIVE.get("SPY"))
    panel = sv.build_panel(stocks, spy, universe)
    _PANEL_CACHE["key"], _PANEL_CACHE["panel"] = key, panel
    return panel


# ----------------------------------------------------------------------
# KOHORT KAYDI
# ----------------------------------------------------------------------
def create_cohort_if_due(panel, force=False):
    dates = panel["dates"]
    t = len(dates) - 1
    date_str = str(dates[t].date())
    cohorts = load_cohorts()

    if any(c["signal_date"] == date_str for c in cohorts):
        return None, "bu tarih zaten kayıtlı"
    if cohorts and not force:
        last_idx = int(dates.searchsorted(pd.Timestamp(cohorts[-1]["signal_date"])))
        if t - last_idx < COHORT_SPACING_DAYS:
            return None, f"yeni kohort için henüz erken (aralık {COHORT_SPACING_DAYS} işlem günü)"

    info = sv.current_picks(panel, RULES, limit=RULES.top_n)
    last_bar = panel["close"].iloc[-1]
    stale = [s for s in panel["symbols"] if not np.isfinite(last_bar[s])]

    cohort = {
        "cohort_id": date_str,
        "signal_date": date_str,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rules_hash": RULES_HASH,
        "rules": asdict(RULES),
        "market_filter_ok": info["market_filter_ok"],
        "spy_close": round(float(panel["spy_close"].iloc[-1]), 2),
        "picks": [
            {k: p[k] for k in (
                "rank", "symbol", "close", "momentum_score_pct",
                "rsi14", "atr_pct", "initial_stop")}
            for p in info.get("picks", [])
        ],
        "stale_symbols": stale,
    }
    saved = append_cohort(cohort)
    return cohort, "kaydedildi" if saved else "KAYDEDİLEMEDİ (TRACK_DIR yazılamıyor)"


# ----------------------------------------------------------------------
# DEĞERLENDİRME
# ----------------------------------------------------------------------
def evaluate_cohorts(panel, cohorts, horizons=(5, 10, 20), n_random=500, seed=42):
    dates = panel["dates"]
    T = len(dates)
    syms = panel["symbols"]
    sidx = {s: j for j, s in enumerate(syms)}
    O = panel["open"].values
    C = panel["close"].values
    SO = panel["spy_open"].reindex(dates).values
    SC = panel["spy_close"].reindex(dates).values
    rng = np.random.default_rng(seed)

    rows = {h: [] for h in horizons}
    pending = {h: 0 for h in horizons}
    usable = 0

    for c in cohorts:
        if not c.get("picks"):
            continue
        ts = pd.Timestamp(c["signal_date"])
        i = int(dates.searchsorted(ts))
        if i >= T or dates[i] != ts:
            continue
        picks = [sidx[x["symbol"]] for x in c["picks"] if x["symbol"] in sidx]
        if not picks:
            continue
        usable += 1
        for h in horizons:
            if i + h > T - 1:
                pending[h] += 1
                continue
            with np.errstate(divide="ignore", invalid="ignore"):
                ret = (C[i + h] / O[i + 1] - 1) * 100
            pr = ret[picks]
            pr = pr[np.isfinite(pr)]
            u = ret[np.isfinite(ret)]
            spy_r = (SC[i + h] / SO[i + 1] - 1) * 100
            if len(pr) == 0 or len(u) < 5 or not np.isfinite(spy_r):
                continue
            pick_mean, u_mean = float(pr.mean()), float(u.mean())
            k = min(len(pr), len(u))
            idx = np.argsort(rng.random((n_random, len(u))), axis=1)[:, :k]
            pct = float((u[idx].mean(axis=1) < pick_mean).mean())
            rows[h].append({
                "signal_date": c["signal_date"],
                "pick_ret": pick_mean,
                "univ_ret": u_mean,
                "spy_ret": float(spy_r),
                "pct": pct,
            })

    def agg(r):
        n = len(r)
        if n == 0:
            return {"cohorts": 0}
        pick = np.array([x["pick_ret"] for x in r])
        univ = np.array([x["univ_ret"] for x in r])
        spy = np.array([x["spy_ret"] for x in r])
        pct = np.array([x["pct"] for x in r])
        ex_u = pick - univ
        out = {
            "cohorts": n,
            "avg_pick_return_pct": round(float(pick.mean()), 2),
            "avg_universe_return_pct": round(float(univ.mean()), 2),
            "avg_spy_return_pct": round(float(spy.mean()), 2),
            "avg_excess_vs_universe_pct": round(float(ex_u.mean()), 2),
            "avg_excess_vs_spy_pct": round(float((pick - spy).mean()), 2),
            "cohorts_beating_universe_pct": round(float((ex_u > 0).mean() * 100), 1),
            "cohorts_beating_spy_pct": round(float((pick > spy).mean() * 100), 1),
            "avg_percentile_vs_random": round(float(pct.mean()), 2),
        }
        if n >= 3 and ex_u.std(ddof=1) > 0:
            out["t_stat_excess_vs_universe"] = round(
                float(ex_u.mean() / (ex_u.std(ddof=1) / np.sqrt(n))), 2)
        return out

    by_h = {f"{h}d": agg(rows[h]) for h in horizons}
    by_h_pending = {f"{h}d": pending[h] for h in horizons}

    n20 = by_h.get("20d", {}).get("cohorts", 0)
    a20 = by_h.get("20d", {})
    if n20 < MIN_COHORTS_FOR_VERDICT:
        verdict = (
            f"YETERSİZ VERİ: 20 günlük sonucu olan {n20} kohort var; "
            f"herhangi bir yargı için en az {MIN_COHORTS_FOR_VERDICT} gerekir."
        )
    else:
        t = a20.get("t_stat_excess_vs_universe", 0)
        p_ = a20.get("avg_percentile_vs_random", 0.5)
        if p_ >= 0.60 and t >= 2 and a20["avg_excess_vs_universe_pct"] > 0:
            verdict = ("İŞARET VAR (temkinli): seçimler rastgeleyi tutarlı biçimde geçti. "
                       "Kohortlar üst üste bindiği için gerçek belirsizlik daha büyüktür.")
        else:
            verdict = "AVANTAJ KANITLANMADI: seçimler rastgele seçimden ayırt edilemiyor."

    return {
        "rules_hash": RULES_HASH,
        "usable_cohorts": usable,
        "horizons": by_h,
        "cohorts_waiting_for_horizon": by_h_pending,
        "verdict": verdict,
        "note": ("avg_percentile_vs_random: 0.50 = rastgele seçimle aynı; "
                 "0.60+ tutarlı ise işaret; kohortlar üst üste bindiği için "
                 "t değeri iyimserdir."),
    }


# ----------------------------------------------------------------------
# İZLEME TABLOSU
# ----------------------------------------------------------------------
def monitor_table(panel, p=RULES):
    ind = sv.prepare(panel, p)
    dates = panel["dates"]
    t = len(dates) - 1
    syms = panel["symbols"]
    close = panel["close"]
    C = close.values
    sma_f = close.rolling(p.sma_fast).mean().values[t]
    sma_s = close.rolling(p.sma_slow).mean().values[t]
    hi = close.rolling(252, min_periods=60).max().values[t]

    score = ind["score"][t]
    finite = np.where(np.isfinite(score))[0]
    order = finite[np.argsort(-score[finite])]
    rank = {int(j): r + 1 for r, j in enumerate(order)}

    spy = panel["spy_close"]
    spy_sma = float(spy.rolling(p.market_sma).mean().iloc[-1])
    market = {
        "spy_close": round(float(spy.iloc[-1]), 2),
        "spy_sma": round(spy_sma, 2),
        "filter_ok": bool(ind["market_ok"][t]),
    }

    rows = []
    for j, s in enumerate(syms):
        c = C[t, j]
        if not np.isfinite(c):
            rows.append({"symbol": s, "stale": True})
            continue
        a = ind["atr"][t, j]
        rsi = sv.rsi_wilder(close[s].dropna(), 14).iloc[-1]
        eligible = bool(ind["eligible"][t, j])
        if eligible:
            state = "TREND"
        elif c < sma_f[j] or sma_f[j] < sma_s[j]:
            state = "ZAYIF"
        else:
            state = "BEKLE"
        rows.append({
            "symbol": s,
            "stale": False,
            "state": state,
            "rank": rank.get(j),
            "close": round(float(c), 2),
            "momentum_score_pct": round(float(score[j]) * 100, 1) if np.isfinite(score[j]) else None,
            "rsi14": round(float(rsi), 1) if np.isfinite(rsi) else None,
            "atr_pct": round(float(a / c) * 100, 2) if np.isfinite(a) else None,
            "dist_sma50_pct": round(float(c / sma_f[j] - 1) * 100, 1) if np.isfinite(sma_f[j]) else None,
            "from_52w_high_pct": round(float(c / hi[j] - 1) * 100, 1) if np.isfinite(hi[j]) else None,
        })
    rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0))
    return {"data_date": str(dates[t].date()), "market": market, "stocks": rows}


def _fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def render_dashboard(panel, evaluation, cohorts):
    mon = monitor_table(panel)
    picks = sv.current_picks(panel, RULES, limit=RULES.top_n)
    e = html.escape
    m = mon["market"]

    parts = [
        "<!doctype html><html lang='tr'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>ABD Borsa İzleme</title><style>",
        "body{font-family:system-ui,sans-serif;margin:16px;max-width:1100px}",
        "table{border-collapse:collapse;width:100%;margin:8px 0 20px;font-size:14px}",
        "th,td{border-bottom:1px solid #ccc;padding:5px 8px;text-align:right}",
        "th:first-child,td:first-child{text-align:left}",
        ".ok{color:#0a7d32;font-weight:600}.bad{color:#b3261e;font-weight:600}",
        ".box{background:#f2f2f2;padding:10px 14px;border-radius:8px;margin:10px 0}",
        "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}",
        ".box{background:#222}th,td{border-color:#444}}",
        "</style></head><body>",
        "<h2>ABD Borsa: izleme ve paper takip</h2>",
        "<p>Bu sayfa araştırma ve takip amaçlıdır, alım tavsiyesi değildir.</p>",
        f"<div class='box'>Veri tarihi: <b>{e(mon['data_date'])}</b> &nbsp;|&nbsp; ",
        f"SPY {m['spy_close']} / SMA{RULES.market_sma} {m['spy_sma']} &nbsp;|&nbsp; ",
        "Piyasa filtresi: ",
        ("<span class='ok'>AÇIK (yatırım serbest)</span>" if m["filter_ok"]
         else "<span class='bad'>KAPALI (nakit)</span>"),
        f"<br>Kayıtlı kohort: {len(cohorts)} &nbsp;|&nbsp; Kural sürümü: {RULES_HASH}</div>",
        "<h3>Takip sonucu</h3>",
        f"<div class='box'><b>{e(evaluation['verdict'])}</b></div>",
        "<table><tr><th>Ufuk</th><th>Kohort</th><th>Seçim %</th><th>Evren %</th>"
        "<th>SPY %</th><th>Fark (evren)</th><th>Rastgele yüzdelik</th></tr>",
    ]
    for hz, a in evaluation["horizons"].items():
        if a.get("cohorts", 0) == 0:
            parts.append(f"<tr><td>{hz}</td><td>0</td><td colspan='5'>henüz sonuç yok</td></tr>")
        else:
            parts.append(
                f"<tr><td>{hz}</td><td>{a['cohorts']}</td>"
                f"<td>{a['avg_pick_return_pct']}</td><td>{a['avg_universe_return_pct']}</td>"
                f"<td>{a['avg_spy_return_pct']}</td><td>{a['avg_excess_vs_universe_pct']}</td>"
                f"<td>{a['avg_percentile_vs_random']}</td></tr>")
    parts.append("</table>")

    parts.append("<h3>Güncel seçimler (dondurulmuş kurallar)</h3>")
    if not picks["market_filter_ok"]:
        parts.append("<p>Piyasa filtresi kapalı: kurallar nakitte kalmayı söylüyor.</p>")
    else:
        parts.append("<table><tr><th>Sıra</th><th>Hisse</th><th>Kapanış</th><th>6a getiri %</th>"
                     "<th>RSI</th><th>ATR %</th><th>İlk stop</th></tr>")
        for p in picks["picks"]:
            parts.append(
                f"<tr><td>{p['rank']}</td><td>{e(p['symbol'])}</td><td>{p['close']}</td>"
                f"<td>{p['return_6m_pct']}</td><td>{p['rsi14']}</td>"
                f"<td>{p['atr_pct']}</td><td>{p['initial_stop']}</td></tr>")
        parts.append("</table>")

    parts.append("<h3>70 hissenin durumu</h3>")
    parts.append("<table><tr><th>Hisse</th><th>Durum</th><th>Sıra</th><th>Kapanış</th>"
                 "<th>Skor %</th><th>RSI</th><th>ATR %</th><th>SMA50 uzaklık %</th>"
                 "<th>52h zirveden %</th></tr>")
    for r in mon["stocks"]:
        if r.get("stale"):
            parts.append(f"<tr><td>{e(r['symbol'])}</td><td colspan='8'>veri güncel değil</td></tr>")
            continue
        parts.append(
            f"<tr><td>{e(r['symbol'])}</td><td>{r['state']}</td><td>{_fmt(r['rank'])}</td>"
            f"<td>{r['close']}</td><td>{_fmt(r['momentum_score_pct'])}</td>"
            f"<td>{_fmt(r['rsi14'])}</td><td>{_fmt(r['atr_pct'])}</td>"
            f"<td>{_fmt(r['dist_sma50_pct'])}</td><td>{_fmt(r['from_52w_high_pct'])}</td></tr>")
    parts.append("</table></body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------
# TELEGRAM (kalıcı kayıt)
# ----------------------------------------------------------------------
async def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:3900]},
            )
            return r.status_code == 200
    except Exception:
        return False


def _telegram_text(cohort, evaluation, status):
    lines = ["ABD PAPER TAKIP (alim tavsiyesi degil)", ""]
    if cohort:
        lines.append(f"Yeni kohort: {cohort['signal_date']} | kural {cohort['rules_hash']}")
        if not cohort["market_filter_ok"]:
            lines.append("Piyasa filtresi KAPALI: nakit.")
        for p in cohort["picks"]:
            lines.append(f"{p['rank']}. {p['symbol']}  kapanis {p['close']}  stop {p['initial_stop']}")
    else:
        lines.append(f"Kohort: {status}")
    lines.append("")
    a = evaluation["horizons"].get("20d", {})
    lines.append(f"20g sonucu olan kohort: {a.get('cohorts', 0)}")
    lines.append(evaluation["verdict"])
    return "\n".join(lines)


# ----------------------------------------------------------------------
# GÜNLÜK İŞ
# ----------------------------------------------------------------------
async def run_daily(get_stock_cache, get_spy_cache, universe, api_key,
                    force=False, notify=True):
    STATE.update(running=True, started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 progress="başladı", last_error=None)
    try:
        symbols = list(dict.fromkeys(list(universe or []) + ["SPY"]))
        new_bars, errors = await fetch_recent(
            symbols, api_key,
            progress=lambda s: STATE.update(progress=s))
        if new_bars:
            _merge_live(new_bars)

        panel = merged_panel(get_stock_cache(), get_spy_cache(), universe)
        cohort, status = create_cohort_if_due(panel, force=force)
        cohorts = load_cohorts()
        evaluation = evaluate_cohorts(panel, cohorts)

        if notify and (cohort or evaluation["usable_cohorts"]):
            await send_telegram(_telegram_text(cohort, evaluation, status))

        STATE["last_result"] = {
            "data_date": str(panel["dates"][-1].date()),
            "symbols_fetched": len(new_bars),
            "fetch_errors": errors,
            "cohort": status,
            "cohort_count": len(cohorts),
            "verdict": evaluation["verdict"],
        }
    except Exception as exc:
        STATE["last_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        STATE.update(running=False, progress="",
                     finished_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))


# ----------------------------------------------------------------------
# FASTAPI ROUTER
# ----------------------------------------------------------------------
def make_tracker_router(get_stock_cache, get_spy_cache, universe=None):
    from fastapi import APIRouter, BackgroundTasks, HTTPException
    from fastapi.responses import HTMLResponse

    router = APIRouter(prefix="/v3", tags=["tracker-v3"])

    def _panel():
        stocks, spy = get_stock_cache(), get_spy_cache()
        if not stocks or not spy:
            raise HTTPException(status_code=400, detail="Veri yok: snapshot yüklenmemiş")
        try:
            return merged_panel(stocks, spy, universe)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @router.get("/track/run")
    async def track_run(background: BackgroundTasks, key: str = "", force: bool = False):
        secret = os.getenv("WEBHOOK_SECRET", "")
        if not secret or not secrets.compare_digest(key, secret):
            raise HTTPException(status_code=401, detail="Unauthorized")
        api_key = os.getenv("TWELVE_DATA_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=500, detail="Twelve Data API key missing")
        if STATE["running"]:
            return {"status": "already_running", "progress": STATE["progress"]}
        background.add_task(run_daily, get_stock_cache, get_spy_cache,
                            universe, api_key, force, True)
        return {"status": "started",
                "note": "Yaklaşık 10 dakika sürer; /v3/track/status ile izleyin."}

    @router.get("/track/status")
    def track_status():
        return {**STATE, "rules_hash": RULES_HASH, "track_dir": str(TRACK_DIR),
                "cohort_file_exists": COHORT_FILE.exists()}

    @router.get("/track/cohorts")
    def track_cohorts(limit: int = 30):
        c = load_cohorts()
        return {"count": len(c), "cohorts": c[-limit:]}

    @router.get("/track/evaluation")
    def track_evaluation():
        return evaluate_cohorts(_panel(), load_cohorts())

    @router.get("/monitor")
    def monitor():
        return monitor_table(_panel())

    @router.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        panel = _panel()
        cohorts = load_cohorts()
        return render_dashboard(panel, evaluate_cohorts(panel, cohorts), cohorts)

    return router
