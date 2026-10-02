"""
tracker_v3.py  -  Ä°leriye dÃ¶nÃ¼k (paper) takip + izleme paneli
=============================================================

AmaÃ§: strategy_v3 kurallarÄ±nÄ± DONDURUP, gerÃ§ek zamanlÄ± olarak seÃ§imleri kaydetmek
ve birkaÃ§ ay sonra "bu seÃ§imlerde gerÃ§ekten bir bilgi var mÄ±?" sorusunu
cevaplamak. Para riski yoktur.

NasÄ±l Ã§alÄ±ÅŸÄ±r
-------------
1. /v3/track/run (dÄ±ÅŸarÄ±dan cron ile her iÅŸ gÃ¼nÃ¼ ABD kapanÄ±ÅŸÄ±ndan sonra Ã§aÄŸrÄ±lÄ±r):
   a) 71 sembol iÃ§in son ~200 gÃ¼nlÃ¼k barÄ± Twelve Data'dan Ã§eker ve snapshot'a ekler
      (dakikada 7 Ã§aÄŸrÄ± -> yaklaÅŸÄ±k 10 dakika, arka planda Ã§alÄ±ÅŸÄ±r).
   b) Son kohort Ã¼zerinden >= 5 iÅŸlem gÃ¼nÃ¼ geÃ§tiyse YENÄ° KOHORT kaydeder:
      o gÃ¼nÃ¼n top_n seÃ§imi (dondurulmuÅŸ kurallarla).
   c) TÃ¼m kohortlarÄ±n 5/10/20 gÃ¼nlÃ¼k sonuÃ§larÄ±nÄ± hesaplar; SPY, tÃ¼m evren ve
      rastgele seÃ§imle kÄ±yaslar.
   d) Ä°steÄŸe baÄŸlÄ±: Telegram'a Ã¶zet gÃ¶nderir (kalÄ±cÄ±, zaman damgalÄ± bir kayÄ±t olur).
2. /v3/dashboard: piyasa filtresi, gÃ¼ncel seÃ§imler, takip Ã¶zeti, 70 hissenin durumu.

KalÄ±cÄ±lÄ±k (Ã–NEMLÄ°)
------------------
Kohortlar TRACK_DIR (varsayÄ±lan /code/track) iÃ§indeki cohorts.jsonl dosyasÄ±na yazÄ±lÄ±r.
Render'Ä±n Ã¼cretsiz planÄ±nda dosya sistemi her deploy/yeniden baÅŸlatmada SIFIRLANIR.
Bu yÃ¼zden ya (a) Render Disk baÄŸlayÄ±p TRACK_DIR'i oraya ayarlayÄ±n, ya da (b)
Telegram bildirimini aÃ§Ä±k tutun: her kohort Telegram'a da yazÄ±lÄ±r ve kayÄ±p
olursa oradan geri yÃ¼klenebilir.

Ortam deÄŸiÅŸkenleri (zaten olanlar): TWELVE_DATA_API_KEY, WEBHOOK_SECRET,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Ä°steÄŸe baÄŸlÄ±: TRACK_DIR.

KullanÄ±m (main.py en altÄ±):

    from app.tracker_v3 import make_tracker_router
    app.include_router(
        make_tracker_router(lambda: STOCK_CACHE, lambda: SPY_CACHE, V2_UNIVERSE)
    )

Bu bir alÄ±m tavsiyesi deÄŸildir; araÅŸtÄ±rma ve paper takip aracÄ±dÄ±r.
"""

import asyncio
import base64
import gzip
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
# DONDURULMUÅ KURALLAR
# ----------------------------------------------------------------------
RULES = sv.Params()   # varsayÄ±lan deÄŸerler = dondurulmuÅŸ kurallar
RULES_HASH = hashlib.sha1(
    json.dumps(asdict(RULES), sort_keys=True).encode()
).hexdigest()[:10]

COHORT_SPACING_DAYS = 5      # yeni kohort iÃ§in asgari iÅŸlem gÃ¼nÃ¼ aralÄ±ÄŸÄ±
MIN_COHORTS_FOR_VERDICT = 26  # ~6 ay haftalÄ±k kohort

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
# DOSYA G/Ã‡
# ----------------------------------------------------------------------
def _ensure_dir():
    try:
        TRACK_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------------
# GITHUB KALICI KAYIT
# ----------------------------------------------------------------------
# Render Environment:
#   GITHUB_TOKEN = fine-grained token (Contents: Read and write)
#   GITHUB_REPO  = owner/repo
# Opsiyonel:
#   GITHUB_BRANCH = main
#   GITHUB_COHORT_PATH = track/cohorts.jsonl
#   GITHUB_LIVE_PATH = track/live_prices.json.gz
#
# Kohortlar JSONL, canlÄ± fiyat geÃ§miÅŸi ise boyutu kÃ¼Ã§Ã¼ltmek iÃ§in gzip olarak
# GitHub'da tutulur. Token hiÃ§bir endpoint Ã§Ä±ktÄ±sÄ±nda gÃ¶sterilmez.
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_COHORT_PATH = os.getenv(
    "GITHUB_COHORT_PATH", "track/cohorts.jsonl"
).strip().lstrip("/")
GITHUB_LIVE_PATH = os.getenv(
    "GITHUB_LIVE_PATH", "track/live_prices.json.gz"
).strip().lstrip("/")
GITHUB_SCAN_PATH = os.getenv(
    "GITHUB_SCAN_PATH", "track/scan_state.json"
).strip().lstrip("/")
SCAN_BATCH_SIZE = int(os.getenv("SCAN_BATCH_SIZE", "7"))


def _github_enabled():
    return bool(GITHUB_REPO and GITHUB_TOKEN)


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "abd-borsa-sinyal-tracker",
    }


def _github_get_bytes(path):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    with httpx.Client(timeout=30) as client:
        r = client.get(
            url, headers=_github_headers(),
            params={"ref": GITHUB_BRANCH},
        )
    if r.status_code == 404:
        return None, None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub read failed ({path}): HTTP {r.status_code}")
    data = r.json()
    encoded = (data.get("content") or "").replace("\n", "")
    try:
        raw = base64.b64decode(encoded) if encoded else b""
    except Exception as exc:
        raise RuntimeError(
            f"GitHub decode failed ({path}): {type(exc).__name__}"
        ) from exc
    return raw, data.get("sha")


def _github_put_bytes(path, raw, message, sha=None):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    with httpx.Client(timeout=45) as client:
        r = client.put(url, headers=_github_headers(), json=payload)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub write failed ({path}): HTTP {r.status_code}")
    return True


def _parse_cohort_text(raw_text):
    out = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("signal_date"):
                out.append(item)
        except Exception:
            continue
    by_date = {x["signal_date"]: x for x in out}
    return sorted(by_date.values(), key=lambda x: x["signal_date"])


def _cohorts_to_text(cohorts):
    return "".join(
        json.dumps(x, ensure_ascii=False) + "\n"
        for x in sorted(cohorts, key=lambda c: c["signal_date"])
    )


def _write_local_cohorts(cohorts):
    if not _ensure_dir():
        return False
    try:
        COHORT_FILE.write_text(_cohorts_to_text(cohorts), encoding="utf-8")
        return True
    except Exception:
        return False


def _github_read_cohorts():
    raw, sha = _github_get_bytes(GITHUB_COHORT_PATH)
    if raw is None:
        return [], None
    return _parse_cohort_text(raw.decode("utf-8")), sha


def _github_write_cohorts(cohorts, sha=None):
    raw = _cohorts_to_text(cohorts).encode("utf-8")
    return _github_put_bytes(
        GITHUB_COHORT_PATH,
        raw,
        "tracker: update cohorts "
        + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        sha=sha,
    )


def load_cohorts():
    if _github_enabled():
        cohorts, _ = _github_read_cohorts()
        _write_local_cohorts(cohorts)
        return cohorts

    if not COHORT_FILE.exists():
        return []
    try:
        return _parse_cohort_text(COHORT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_cohort(cohort):
    if _github_enabled():
        cohorts, sha = _github_read_cohorts()
        if any(
            c.get("signal_date") == cohort.get("signal_date")
            for c in cohorts
        ):
            _write_local_cohorts(cohorts)
            return True
        cohorts.append(cohort)
        cohorts = sorted(cohorts, key=lambda c: c["signal_date"])
        _github_write_cohorts(cohorts, sha=sha)
        _write_local_cohorts(cohorts)
        return True

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

    # GitHub etkinse kalÄ±cÄ± canlÄ± fiyat geÃ§miÅŸini Ã¶nce oradan yÃ¼kle.
    if _github_enabled():
        try:
            raw, _ = _github_get_bytes(GITHUB_LIVE_PATH)
            if raw is not None:
                LIVE = json.loads(gzip.decompress(raw).decode("utf-8"))
                _LIVE_VERSION += 1
                if _ensure_dir():
                    LIVE_FILE.write_text(
                        json.dumps(LIVE), encoding="utf-8"
                    )
                return
        except Exception:
            # GitHub dosyasÄ± henÃ¼z yoksa/okunamazsa gÃ¼nlÃ¼k tarama yine
            # snapshot + yeni Twelve Data verisiyle baÅŸlayabilsin.
            LIVE = {}

    # Geriye uyumluluk / geÃ§ici yerel ayna.
    try:
        if LIVE_FILE.exists():
            LIVE = json.loads(LIVE_FILE.read_text(encoding="utf-8"))
            _LIVE_VERSION += 1
    except Exception:
        LIVE = {}


def _save_live():
    # Ã–nce yerel geÃ§ici aynayÄ± yaz.
    if _ensure_dir():
        try:
            LIVE_FILE.write_text(json.dumps(LIVE), encoding="utf-8")
        except Exception:
            pass

    # Sonra sÄ±kÄ±ÅŸtÄ±rÄ±lmÄ±ÅŸ canlÄ± fiyat geÃ§miÅŸini GitHub'a kalÄ±cÄ± yaz.
    if _github_enabled():
        raw_json = json.dumps(
            LIVE, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        compressed = gzip.compress(raw_json, compresslevel=9)

        _, sha = _github_get_bytes(GITHUB_LIVE_PATH)
        _github_put_bytes(
            GITHUB_LIVE_PATH,
            compressed,
            "tracker: update live prices "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            sha=sha,
        )


# ----------------------------------------------------------------------
# VERÄ° Ã‡EKME
# ----------------------------------------------------------------------
def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # tzdata yoksa sabit UTC-5 (yaz saatinde daha temkinli davranÄ±r)
        return datetime.now(timezone(timedelta(hours=-5)))


def _bar_is_incomplete(bar_date: str) -> bool:
    """BugÃ¼ne ait bar, ABD kapanÄ±ÅŸÄ±ndan (16:30 ET) Ã¶nce tamamlanmamÄ±ÅŸtÄ±r."""
    now = _now_et()
    if bar_date[:10] != now.strftime("%Y-%m-%d"):
        return False
    return now.hour * 60 + now.minute < 16 * 60 + 30


# ----------------------------------------------------------------------
# KESINTIYE DAYANIKLI TARAMA DURUMU
# ----------------------------------------------------------------------
def _read_scan_state():
    if not _github_enabled():
        return {}
    try:
        raw, _ = _github_get_bytes(GITHUB_SCAN_PATH)
        return json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return {}


def _write_scan_state(state):
    if not _github_enabled():
        return False
    _, sha = _github_get_bytes(GITHUB_SCAN_PATH)
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _github_put_bytes(
        GITHUB_SCAN_PATH, raw,
        "tracker: update scan state " + datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        sha=sha,
    )
    return True


def _clear_scan_state(data_date=None):
    state = {
        "active": False,
        "completed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "completed_data_date": data_date,
        "next_index": 0,
        "errors": {},
    }
    _write_scan_state(state)
    return state


async def fetch_recent(symbols, api_key, outputsize=200, per_minute=7, progress=None):
    """Twelve Data'dan son barlarÄ± Ã§eker. Ãœcretsiz plan limiti iÃ§in yavaÅŸtÄ±r."""
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


async def fetch_batch(symbols, api_key, start_index=0, batch_size=7, progress=None):
    """En fazla batch_size sembol ceker; uzun bekleme yapmaz."""
    subset = symbols[start_index:start_index + batch_size]
    out, errors = {}, {}
    url = "https://api.twelvedata.com/time_series"
    async with httpx.AsyncClient(timeout=30) as client:
        for offset, sym in enumerate(subset, start_index + 1):
            try:
                r = await client.get(url, params={
                    "symbol": sym, "interval": "1day",
                    "outputsize": 200, "apikey": api_key,
                })
                data = r.json()
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
            except Exception as exc:
                errors[sym] = f"{type(exc).__name__}: {exc}"[:200]
            if progress:
                progress(f"{offset}/{len(symbols)} {sym}")
    return out, errors, start_index + len(subset)


def _merge_live(new_bars):
    global LIVE, _LIVE_VERSION
    _load_live()
    for sym, bars in new_bars.items():
        cur = LIVE.setdefault(sym, {})
        cur.update(bars)
        for d in sorted(cur.keys())[:-300]:   # son 300 barÄ± tut
            del cur[d]
    _LIVE_VERSION += 1
    _save_live()


def merged_panel(stock_cache, spy_values, universe=None):
    """Snapshot (tarihsel) + canlÄ± barlar -> panel. STOCK_CACHE'e dokunmaz."""
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
        return None, "bu tarih zaten kayÄ±tlÄ±"
    if cohorts and not force:
        last_idx = int(dates.searchsorted(pd.Timestamp(cohorts[-1]["signal_date"])))
        if t - last_idx < COHORT_SPACING_DAYS:
            return None, f"yeni kohort iÃ§in henÃ¼z erken (aralÄ±k {COHORT_SPACING_DAYS} iÅŸlem gÃ¼nÃ¼)"

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
    return cohort, "kaydedildi" if saved else "KAYDEDÄ°LEMEDÄ° (TRACK_DIR yazÄ±lamÄ±yor)"


# ----------------------------------------------------------------------
# DEÄERLENDÄ°RME
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
            f"YETERSÄ°Z VERÄ°: 20 gÃ¼nlÃ¼k sonucu olan {n20} kohort var; "
            f"herhangi bir yargÄ± iÃ§in en az {MIN_COHORTS_FOR_VERDICT} gerekir."
        )
    else:
        t = a20.get("t_stat_excess_vs_universe", 0)
        p_ = a20.get("avg_percentile_vs_random", 0.5)
        if p_ >= 0.60 and t >= 2 and a20["avg_excess_vs_universe_pct"] > 0:
            verdict = ("Ä°ÅARET VAR (temkinli): seÃ§imler rastgeleyi tutarlÄ± biÃ§imde geÃ§ti. "
                       "Kohortlar Ã¼st Ã¼ste bindiÄŸi iÃ§in gerÃ§ek belirsizlik daha bÃ¼yÃ¼ktÃ¼r.")
        else:
            verdict = "AVANTAJ KANITLANMADI: seÃ§imler rastgele seÃ§imden ayÄ±rt edilemiyor."

    return {
        "rules_hash": RULES_HASH,
        "usable_cohorts": usable,
        "horizons": by_h,
        "cohorts_waiting_for_horizon": by_h_pending,
        "verdict": verdict,
        "note": ("avg_percentile_vs_random: 0.50 = rastgele seÃ§imle aynÄ±; "
                 "0.60+ tutarlÄ± ise iÅŸaret; kohortlar Ã¼st Ã¼ste bindiÄŸi iÃ§in "
                 "t deÄŸeri iyimserdir."),
    }


# ----------------------------------------------------------------------
# Ä°ZLEME TABLOSU
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
        "<title>ABD Borsa Ä°zleme</title><style>",
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
        "<p>Bu sayfa araÅŸtÄ±rma ve takip amaÃ§lÄ±dÄ±r, alÄ±m tavsiyesi deÄŸildir.</p>",
        f"<div class='box'>Veri tarihi: <b>{e(mon['data_date'])}</b> &nbsp;|&nbsp; ",
        f"SPY {m['spy_close']} / SMA{RULES.market_sma} {m['spy_sma']} &nbsp;|&nbsp; ",
        "Piyasa filtresi: ",
        ("<span class='ok'>AÃ‡IK (yatÄ±rÄ±m serbest)</span>" if m["filter_ok"]
         else "<span class='bad'>KAPALI (nakit)</span>"),
        f"<br>KayÄ±tlÄ± kohort: {len(cohorts)} &nbsp;|&nbsp; Kural sÃ¼rÃ¼mÃ¼: {RULES_HASH}</div>",
        "<h3>Takip sonucu</h3>",
        f"<div class='box'><b>{e(evaluation['verdict'])}</b></div>",
        "<table><tr><th>Ufuk</th><th>Kohort</th><th>SeÃ§im %</th><th>Evren %</th>"
        "<th>SPY %</th><th>Fark (evren)</th><th>Rastgele yÃ¼zdelik</th></tr>",
    ]
    for hz, a in evaluation["horizons"].items():
        if a.get("cohorts", 0) == 0:
            parts.append(f"<tr><td>{hz}</td><td>0</td><td colspan='5'>henÃ¼z sonuÃ§ yok</td></tr>")
        else:
            parts.append(
                f"<tr><td>{hz}</td><td>{a['cohorts']}</td>"
                f"<td>{a['avg_pick_return_pct']}</td><td>{a['avg_universe_return_pct']}</td>"
                f"<td>{a['avg_spy_return_pct']}</td><td>{a['avg_excess_vs_universe_pct']}</td>"
                f"<td>{a['avg_percentile_vs_random']}</td></tr>")
    parts.append("</table>")

    parts.append("<h3>GÃ¼ncel seÃ§imler (dondurulmuÅŸ kurallar)</h3>")
    if not picks["market_filter_ok"]:
        parts.append("<p>Piyasa filtresi kapalÄ±: kurallar nakitte kalmayÄ± sÃ¶ylÃ¼yor.</p>")
    else:
        parts.append("<table><tr><th>SÄ±ra</th><th>Hisse</th><th>KapanÄ±ÅŸ</th><th>6a getiri %</th>"
                     "<th>RSI</th><th>ATR %</th><th>Ä°lk stop</th></tr>")
        for p in picks["picks"]:
            parts.append(
                f"<tr><td>{p['rank']}</td><td>{e(p['symbol'])}</td><td>{p['close']}</td>"
                f"<td>{p['return_6m_pct']}</td><td>{p['rsi14']}</td>"
                f"<td>{p['atr_pct']}</td><td>{p['initial_stop']}</td></tr>")
        parts.append("</table>")

    parts.append("<h3>70 hissenin durumu</h3>")
    parts.append("<table><tr><th>Hisse</th><th>Durum</th><th>SÄ±ra</th><th>KapanÄ±ÅŸ</th>"
                 "<th>Skor %</th><th>RSI</th><th>ATR %</th><th>SMA50 uzaklÄ±k %</th>"
                 "<th>52h zirveden %</th></tr>")
    for r in mon["stocks"]:
        if r.get("stale"):
            parts.append(f"<tr><td>{e(r['symbol'])}</td><td colspan='8'>veri gÃ¼ncel deÄŸil</td></tr>")
            continue
        parts.append(
            f"<tr><td>{e(r['symbol'])}</td><td>{r['state']}</td><td>{_fmt(r['rank'])}</td>"
            f"<td>{r['close']}</td><td>{_fmt(r['momentum_score_pct'])}</td>"
            f"<td>{_fmt(r['rsi14'])}</td><td>{_fmt(r['atr_pct'])}</td>"
            f"<td>{_fmt(r['dist_sma50_pct'])}</td><td>{_fmt(r['from_52w_high_pct'])}</td></tr>")
    parts.append("</table></body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------
# TELEGRAM (kalÄ±cÄ± kayÄ±t)
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
# GÃœNLÃœK Ä°Å
# ----------------------------------------------------------------------
async def run_daily(get_stock_cache, get_spy_cache, universe, api_key,
                    force=False, notify=True):
    """Tek cron cagrisinda tum taramayi 7'li paketlerle tamamlar."""
    STATE.update(
        running=True,
        started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        progress="basladi", last_error=None,
    )
    try:
        symbols = list(dict.fromkeys(list(universe or []) + ["SPY"]))
        scan = _read_scan_state()
        if not scan.get("active"):
            scan = {
                "active": True,
                "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "next_index": 0,
                "total": len(symbols),
                "errors": {},
            }
            _write_scan_state(scan)

        start_index = int(scan.get("next_index", 0) or 0)
        if start_index < 0 or start_index >= len(symbols):
            start_index = 0
        all_errors = dict(scan.get("errors") or {})

        while start_index < len(symbols):
            new_bars, errors, proposed_next = await fetch_batch(
                symbols, api_key, start_index=start_index,
                batch_size=SCAN_BATCH_SIZE,
                progress=lambda x: STATE.update(progress=x),
            )
            if new_bars:
                _merge_live(new_bars)

            if errors:
                all_errors.update(errors)
                scan.update(
                    active=True, next_index=start_index, total=len(symbols),
                    errors=all_errors,
                    updated_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                _write_scan_state(scan)
                STATE["last_result"] = {
                    "status": "retrying", "processed": start_index,
                    "total": len(symbols), "batch_fetched": len(new_bars),
                    "batch_errors": errors,
                }
                await asyncio.sleep(61)
                continue

            for sym in symbols[start_index:proposed_next]:
                all_errors.pop(sym, None)
            start_index = proposed_next
            scan.update(
                active=True, next_index=start_index, total=len(symbols),
                errors=all_errors,
                updated_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            _write_scan_state(scan)
            STATE["last_result"] = {
                "status": "partial" if start_index < len(symbols) else "finishing",
                "processed": start_index, "total": len(symbols),
                "next_symbol": symbols[start_index] if start_index < len(symbols) else None,
                "batch_fetched": len(new_bars), "batch_errors": {},
            }
            if start_index < len(symbols):
                await asyncio.sleep(61)

        panel = merged_panel(get_stock_cache(), get_spy_cache(), universe)
        cohort, status = create_cohort_if_due(panel, force=force)
        cohorts = load_cohorts()
        evaluation = evaluate_cohorts(panel, cohorts)
        if notify and (cohort or evaluation["usable_cohorts"]):
            await send_telegram(_telegram_text(cohort, evaluation, status))
        completed_data_date = str(panel["dates"][-1].date())
        _clear_scan_state(completed_data_date)
        STATE["last_result"] = {
            "status": "complete",
            "data_date": completed_data_date,
            "symbols_fetched": len(symbols),
            "fetch_errors": all_errors,
            "cohort": status,
            "cohort_count": len(cohorts),
            "verdict": evaluation["verdict"],
        }
    except Exception as exc:
        STATE["last_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        STATE.update(
            running=False, progress="",
            finished_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

def make_tracker_router(get_stock_cache, get_spy_cache, universe=None):
    from fastapi import APIRouter, BackgroundTasks, HTTPException
    from fastapi.responses import HTMLResponse

    router = APIRouter(prefix="/v3", tags=["tracker-v3"])

    def _panel():
        stocks, spy = get_stock_cache(), get_spy_cache()
        if not stocks or not spy:
            raise HTTPException(status_code=400, detail="Veri yok: snapshot yÃ¼klenmemiÅŸ")
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

        # Yedek cron korumasi: ayni ABD islem gunu tamamlandiysa tekrar tarama yapma.
        if not force and _github_enabled():
            scan = _read_scan_state()
            completed_data_date = scan.get("completed_data_date")
            if completed_data_date:
                now_et = _now_et()
                expected = now_et.date()

                # Son tamamlanmis ABD islem gununu bul.
                # Hafta ici 16:30 ET'den once bugunun gunluk bari tamamlanmis sayilmaz.
                if expected.weekday() < 5 and (now_et.hour * 60 + now_et.minute) < (16 * 60 + 30):
                    expected -= timedelta(days=1)

                # Cumartesi/Pazar veya cuma oncesine dusen tarihi son islem gunune cek.
                while expected.weekday() >= 5:
                    expected -= timedelta(days=1)

                expected_data_date = expected.isoformat()
                if completed_data_date == expected_data_date:
                    return {
                        "status": "already_complete",
                        "data_date": completed_data_date,
                        "completed_utc": scan.get("completed_utc"),
                        "note": "Bu ABD islem gunu zaten basariyla tarandi; tekrar tarama yapilmadi."
                    }

        background.add_task(run_daily, get_stock_cache, get_spy_cache,
                            universe, api_key, force, True)
        return {
            "status": "started",
            "note": f"Tek cagri tum taramayi tamamlar; {SCAN_BATCH_SIZE} sembolluk paketler arasinda API limiti icin bekler."
        }

    @router.get("/track/status")
    def track_status():
        return {
            **STATE,
            "rules_hash": RULES_HASH,
            "track_dir": str(TRACK_DIR),
            "cohort_file_exists": COHORT_FILE.exists(),
            "live_file_exists": LIVE_FILE.exists(),
            "persistence": "github" if _github_enabled() else "local",
            "github_repo": GITHUB_REPO if _github_enabled() else None,
            "github_cohort_path": GITHUB_COHORT_PATH if _github_enabled() else None,
            "github_live_path": GITHUB_LIVE_PATH if _github_enabled() else None,
            "github_scan_path": GITHUB_SCAN_PATH if _github_enabled() else None,
            "scan_batch_size": SCAN_BATCH_SIZE,
            "scan_state": _read_scan_state() if _github_enabled() else {},
        }

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
