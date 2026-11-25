#!/usr/bin/env python3
"""PDS-Lite: personal month-end rebalancing pipeline (Core + Adaptive) for 18 US ETFs.

Reverse-engineered from the public SlackQuant releases:
  * ADAA sleeves  : adaa-decision-diversification/03_Data_and_Code/02_Code/
                    01_EXPORT_G2_TARGET_WEIGHTS_ONLY_v0_11_1.R  (HAA, BAA Aggressive, ADM, legacy FAA)
                    04_EXPORT_G3_CANONICAL_FAA_ONLY_v0_16.R      (canonical FAA, optional variant)
                    build_independent_decision_panel_v0_11.py    (LAA carry_calendar)
  * ADAA mix      : 05_OPEN_G6_PERFORMANCE_EVIDENCE_v0_17.py  alpha_hist = 25/15/17.5/17.5/25
  * PDS Core      : 0.90 x ADAA + 0.10 x F2R(Top-4 equal weight)
                    (fitted exactly, max error 5.6e-17 on all 253 months of
                     PDS_Portfolio_History_Current_Definition.xlsx)
  * PDS Adaptive  : RB x Core + (1 - RB) x BIL  (PDS doc v1.5, Appendix D).  The author's
                    PPO ensemble and thresholds are private.  Operational RB policy
                    (ADAPTIVE_MODE=reference_only, default): RB = 1.0 (Core 100%) unless the
                    author's official Adaptive row for that month was already published and
                    passed sanity checks when the month was locked.  Values the author publishes
                    later are kept in a separate reference ledger and never rewrite a lock.

Usage
  python run_pipeline.py                      # engine mode (needs yfinance + network)
  python run_pipeline.py --mode reference     # mirror the author's published workbook only
  python run_pipeline.py --backfill-from 2017-03

Outputs (docs/data/):
  portfolio_latest.json   dashboard payload
  official_history.json   append-only lock of official decisions (never rewritten)
  reference_adaptive.json append-only ledger of the author's published Adaptive rows (reference only)
All outputs are written together at the very end, only after every check has passed.

Not investment advice. Weights only; no account data is stored.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ════════════════════════════════════════════════════════════════════════════
# 1. CONFIG
# ════════════════════════════════════════════════════════════════════════════
ENGINE_VERSION = "pds-lite-1.1.0"
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "docs" / "data"
LATEST_JSON = OUT_DIR / "portfolio_latest.json"
LOCK_JSON = OUT_DIR / "official_history.json"
REF_LEDGER_JSON = OUT_DIR / "reference_adaptive.json"
DIAG_DIR = OUT_DIR / "diagnostic"            # non-locking modes write here only (git-ignored)

UNIVERSE = ["QQQ", "SPY", "IWM", "EEM", "EFA", "EWY", "VGK", "AGG", "TIP",
            "DBC", "IEF", "TLT", "LQD", "VNQ", "GLD", "HYG", "SHY", "BIL"]
NAMES = {
    "QQQ": "Nasdaq-100", "SPY": "S&P 500", "IWM": "Russell 2000", "EEM": "MSCI EM",
    "EFA": "MSCI EAFE", "EWY": "MSCI Korea", "VGK": "FTSE Europe", "AGG": "US Agg Bond",
    "TIP": "US TIPS", "DBC": "Commodities", "IEF": "UST 7-10Y", "TLT": "UST 20Y+",
    "LQD": "IG Corp", "VNQ": "US REIT", "GLD": "Gold", "HYG": "High Yield",
    "SHY": "UST 1-3Y", "BIL": "T-Bill 1-3M",
}
# Verified against the workbook's "Core Asset Class" sheet (max error 2e-16).
ASSET_CLASS = {
    "Equity": ["QQQ", "SPY", "IWM", "EEM", "EFA", "EWY", "VGK"],
    "Fixed Income": ["AGG", "TIP", "IEF", "TLT", "LQD", "HYG", "SHY"],
    "Real Assets / Other": ["DBC", "VNQ", "GLD"],
    "Cash": ["BIL"],
}
SLEEVE_MIX = {"HAA": 0.25, "BAA": 0.15, "ADM": 0.175, "FAA": 0.175, "LAA": 0.25}
CORE_MIX = {"ADAA": 0.90, "F2R": 0.10}
F2R_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT", "EWY", "GLD"]

# Adaptive modes
#   reference_only        (default, operational) RB = 1.0 unless the author's official row for the
#                         month is already published AND sane at lock time; previews always RB = 1.0
#   core_only             (operational) RB = 1.0 always; author values are reference only
#   reference_then_proxy  (diagnostic, pre-1.1 behaviour) author value, else the transparent proxy
#   proxy                 (diagnostic) transparent proxy only
# Diagnostic modes never write the official lock, the ledger or portfolio_latest.json.
ADAPTIVE_MODES = ("reference_only", "core_only", "reference_then_proxy", "proxy")
LOCKING_MODES = ("reference_only", "core_only")
ADAPTIVE_MODE = os.environ.get("ADAPTIVE_MODE", "reference_only")
ADAPTIVE_FLOOR = 0.55            # lowest RB observed in the published history (2024-09, 2026-06)
REF_USABLE_STATUS = ("OFFICIAL", "FORWARD_MONITOR", "HISTORY")   # PREVIEW is never executed

# Fail-safe thresholds (a breach stops the run: nothing is written, the previous official stays)
INTEGRITY_WINDOW = 400           # trading days checked for gaps / jumps (covers the 13-month lookbacks)
MAX_DAILY_MOVE = 0.25            # |daily return| above this for a diversified ETF = data error
FROZEN_ALL_DAYS = 2              # all 18 closes unchanged this many days in a row = frozen feed
FROZEN_ONE_DAYS = 10             # one ticker unchanged this many days in a row = stale ticker
# The official lock waits until the author's F2R Top-4 for that month is published (2-8 days after
# month-end in 2026-08..10).  Locking a preview/proxy F2R would freeze a Core that differs from the
# author's.  Set to "0" only as an emergency override.
OFFICIAL_REQUIRES_F2R_PUBLISHED = os.environ.get("OFFICIAL_REQUIRES_F2R_PUBLISHED", "1") != "0"
# ... and until the author's own Core row for that month is published AND equals ours (<= 1e-6).
# The author's Core OFFICIAL row appeared the same day as F2R (2026-09: 10/02, 2026-10: 11/02).
OFFICIAL_REQUIRES_AUTHOR_CORE = os.environ.get("OFFICIAL_REQUIRES_AUTHOR_CORE", "1") != "0"
# Lock deadline in the holding month: the scheduled run of day LOCK_DEADLINE_DAY at >= 22:00 UTC is
# the final check.  Still incomplete then -> BLOCKED: no lock, keep current holdings, alert.  Data
# that arrives after the end of that day is never locked automatically; only a manual release
# (MANUAL_RELEASE_MONTH=YYYY-MM via workflow_dispatch) can lock it, and only with complete data.
LOCK_DEADLINE_DAY = 10
LOCK_FINAL_CHECK_UTC_HOUR = 22
MANUAL_RELEASE_MONTH = os.environ.get("MANUAL_RELEASE_MONTH", "").strip()
MANUAL_RELEASE_REASON = os.environ.get("MANUAL_RELEASE_REASON", "").strip()   # required with a release
RELEASE_MAX_PRICE_AGE_DAYS = 5   # a manual release needs prices no older than this (calendar days)
# Operator halt (repository variable PDS_HALT=1): no new month is locked at all, the current holdings
# stay, every run reports HALTED.  Step 1 of any rollback or incident; cleared by removing the variable.
OPERATOR_HALT = os.environ.get("PDS_HALT", "").strip().lower() in ("1", "true", "yes", "on")
EVENTS_JSON = OUT_DIR / "official_events.json"   # append-only audit: PENDING / BLOCKED / LOCKED / releases
RELEASES_JSON = OUT_DIR / "manual_releases.json"  # append-only: every manual release request and its checks
LIVE_LOCK_MAX_DAYS = 14          # a lock this many days after the cutoff counts as a live record
PROXY_TARGET_VOL = 0.10          # annualised; YOUR parameter, not the author's
PROXY_VOL_WINDOW = 63            # trading days
COST_PER_SIDE = 0.0010           # 10 bp per side, as in F2R / momentum-market-regularity

# IRP (Korean retirement account) conversion
IRP_RISK_CAP = 0.70
IRP_CAP_BUFFER = 0.02            # target <= 68% so price drift does not breach 70%
IRP_OVERFLOW = "329750"          # where the excess risk weight goes (safe asset)
# us ticker -> (KRX code, name, class RISK/SAFE, note)
IRP_MAP = {
    "QQQ": ("133690", "TIGER 미국나스닥100", "RISK", ""),
    "SPY": ("360750", "TIGER 미국S&P500", "RISK", ""),
    "IWM": ("360750", "TIGER 미국S&P500", "RISK", "대체: 퇴직연금 가능한 러셀2000 ETF 미확인"),
    "EEM": ("195980", "PLUS 신흥국MSCI(합성 H)", "RISK", ""),
    "EFA": ("251350", "KODEX MSCI선진국", "RISK", "대체: MSCI World(미국 포함)"),
    "EWY": ("069500", "KODEX 200", "RISK", ""),
    "VGK": ("195930", "TIGER 유로스탁스50(합성 H)", "RISK", ""),
    "AGG": ("0091C0", "KODEX 미국10년국채액티브(H)", "SAFE", "대체: 종합채권→10년 국채"),
    "TIP": ("0091C0", "KODEX 미국10년국채액티브(H)", "SAFE", "대체: 국내 TIPS ETF 없음"),
    "DBC": ("411060", "ACE KRX금현물", "RISK", "대체: 원자재 선물 ETF는 퇴직연금 매수 불가"),
    "IEF": ("0091C0", "KODEX 미국10년국채액티브(H)", "SAFE", ""),
    "TLT": ("458250", "TIGER 미국30년국채스트립액티브(합성H)", "SAFE", "듀레이션이 TLT보다 김"),
    "LQD": ("458260", "TIGER 미국투자등급회사채액티브(H)", "SAFE", ""),
    "VNQ": ("182480", "TIGER 미국MSCI리츠(합성 H)", "RISK", ""),
    "GLD": ("411060", "ACE KRX금현물", "RISK", ""),
    "HYG": ("182490", "TIGER 단기선진하이일드(합성 H)", "RISK", "대체: 단기 선진국 하이일드"),
    "SHY": ("329750", "TIGER 미국달러단기채권액티브", "SAFE", ""),
    "BIL": ("329750", "TIGER 미국달러단기채권액티브", "SAFE", ""),
}

PRICE_START = os.environ.get("PRICE_START", "2015-01-01")
REF_WORKBOOK_URL = ("https://raw.githubusercontent.com/slackquant80/slackquant-research/main/"
                    "public/assets/systems/pds/PDS_Portfolio_History_Current_Definition.xlsx")
F2R_STATE_URL = ("https://raw.githubusercontent.com/slackquant80/f2r-forecast-to-rank-allocation/main/"
                 "src/dashboard/public_data/f2r_public_state.json")
UNRATE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=UNRATE"
US_CLOSE_SAFE_UTC = 21 * 60 + 30   # treat a daily bar as final only after 21:30 UTC
KST = timezone(timedelta(hours=9))
TOL = 1e-9


# ════════════════════════════════════════════════════════════════════════════
# 2. DATA ACCESS (all network calls live here)
# ════════════════════════════════════════════════════════════════════════════
def _http_get(url: str, tries: int = 3, timeout: int = 60) -> bytes:
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pds-lite/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"GET failed: {url}: {last}")


def fetch_prices(start: str = PRICE_START) -> pd.DataFrame:
    """Daily adjusted closes (dividends/splits adjusted) for the 18 ETFs."""
    import yfinance as yf  # imported lazily so reference mode works without it
    last = None
    for k in range(3):
        try:
            raw = yf.download(UNIVERSE, start=start, auto_adjust=True, progress=False,
                              threads=False, group_by="column")
            px = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
            px = px[UNIVERSE].sort_index()
            px.index = pd.to_datetime(px.index).tz_localize(None)
            px = px.dropna(how="all")
            if px.iloc[-1].isna().any():          # a ticker missing today's bar -> drop the row
                px = px.iloc[:-1]
            if len(px) < 300:
                raise RuntimeError("too few rows")
            check_price_integrity(px)             # bad data -> retry, then fail closed
            return px
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (k + 1))
    raise RuntimeError(f"price download failed: {last}")


def check_price_integrity(px: pd.DataFrame) -> None:
    """Fail-safe on the price panel that every signal is computed from.
    Raises RuntimeError (-> no output is written) on gaps, implausible jumps or frozen feeds.
    month_end_frame() takes the last *available* close per month, so a silent gap would
    otherwise feed a stale price into the momentum ranks."""
    if list(px.columns) != UNIVERSE:
        raise RuntimeError(f"price columns {list(px.columns)} != universe")
    w = px.iloc[-INTEGRITY_WINDOW:]
    if w.isna().any().any():
        bad = {t: [str(d.date()) for d in w.index[w[t].isna()]][:5] for t in UNIVERSE if w[t].isna().any()}
        raise RuntimeError(f"price gaps in the last {len(w)} sessions: {bad}")
    if (w <= 0).any().any():
        raise RuntimeError("non-positive price")
    r = w.pct_change(fill_method=None).iloc[1:]
    jump = r.abs() > MAX_DAILY_MOVE
    if jump.any().any():
        bad = {t: [str(d.date()) for d in r.index[jump[t]]] for t in UNIVERSE if jump[t].any()}
        raise RuntimeError(f"daily move > {MAX_DAILY_MOVE:.0%}: {bad}")
    flat = (r == 0)
    all_flat = flat.all(axis=1).astype(int)
    if all_flat.groupby((all_flat == 0).cumsum()).cumsum().max() >= FROZEN_ALL_DAYS:
        raise RuntimeError(f"all closes unchanged for {FROZEN_ALL_DAYS}+ sessions (frozen feed)")
    for t in UNIVERSE:
        if t == "BIL":                                   # T-bill ETF can legitimately sit flat for days
            continue
        f = flat[t].astype(int)
        if f.groupby((f == 0).cumsum()).cumsum().max() >= FROZEN_ONE_DAYS:
            raise RuntimeError(f"{t} close unchanged for {FROZEN_ONE_DAYS}+ sessions (stale ticker)")


UNRATE_CACHE = OUT_DIR / "unrate_cache.csv"


def _unrate_fred() -> pd.Series:
    df = pd.read_csv(io.BytesIO(_http_get(UNRATE_URL, tries=2, timeout=30)))
    df.columns = ["date", "UNRATE"]
    s = pd.to_numeric(df["UNRATE"], errors="coerce")
    s.index = pd.to_datetime(df["date"]).dt.to_period("M")
    return s


def _unrate_bls() -> pd.Series:
    """BLS LNS14000000 = the series FRED republishes as UNRATE (seasonally adjusted)."""
    now = datetime.now(timezone.utc).year
    vals = {}
    for y0 in range(int(PRICE_START[:4]) - 2, now + 1, 10):        # v1 API: <=10 years per call
        body = json.dumps({"seriesid": ["LNS14000000"], "startyear": str(y0),
                           "endyear": str(min(y0 + 9, now))}).encode()
        req = urllib.request.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/", data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "pds-lite/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            js = json.loads(r.read())
        if js.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS: {js.get('message')}")
        for d in js["Results"]["series"][0]["data"]:
            if d["period"].startswith("M") and d["period"] != "M13":
                vals[pd.Period(f"{d['year']}-{d['period'][1:]}", "M")] = pd.to_numeric(d["value"], errors="coerce")
    s = pd.Series(vals).sort_index()
    return s.reindex(pd.period_range(s.index.min(), s.index.max(), freq="M"))


def fetch_unrate() -> tuple[pd.Series, str]:
    """FRED -> BLS -> committed cache. Returns (series, source)."""
    for name, fn in (("FRED", _unrate_fred), ("BLS", _unrate_bls)):
        try:
            s = fn()
            if s.dropna().size < 24:
                raise RuntimeError("too few observations")
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            s.rename("UNRATE").to_frame().to_csv(UNRATE_CACHE, index_label="month")
            return s, name
        except Exception as e:  # noqa: BLE001
            print(f"[warn] UNRATE via {name} failed: {e}")
    if UNRATE_CACHE.exists():
        c = pd.read_csv(UNRATE_CACHE)
        s = pd.Series(pd.to_numeric(c["UNRATE"], errors="coerce").values,
                      index=pd.PeriodIndex(c["month"], freq="M"))
        return s, "CACHE"
    raise RuntimeError("UNRATE unavailable from FRED, BLS and cache")


def fetch_f2r_state() -> dict | None:
    try:
        return json.loads(_http_get(F2R_STATE_URL))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] F2R state unavailable: {e}")
        return None


def fetch_reference_workbook() -> dict[str, pd.DataFrame] | None:
    try:
        return pd.read_excel(io.BytesIO(_http_get(REF_WORKBOOK_URL)), sheet_name=None)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] reference workbook unavailable: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 3. CLOCK: which month is OFFICIAL, which is PREVIEW
# ════════════════════════════════════════════════════════════════════════════
def _nyse_last_session(period: pd.Period) -> pd.Timestamp:
    from pandas.tseries.holiday import (AbstractHolidayCalendar, GoodFriday, Holiday,
                                        USLaborDay, USMartinLutherKingJr, USMemorialDay,
                                        USPresidentsDay, USThanksgivingDay, nearest_workday)

    class NYSE(AbstractHolidayCalendar):
        rules = [Holiday("NY", month=1, day=1, observance=nearest_workday),
                 USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
                 Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01",
                         observance=nearest_workday),
                 Holiday("July4", month=7, day=4, observance=nearest_workday),
                 USLaborDay, USThanksgivingDay,
                 Holiday("Xmas", month=12, day=25, observance=nearest_workday)]

    bday = pd.offsets.CustomBusinessDay(calendar=NYSE())
    end = period.to_timestamp(how="end").normalize()
    rng = pd.date_range(period.to_timestamp(how="start"), end, freq=bday)
    return rng[-1]


def split_clock(daily: pd.DataFrame, now_utc: datetime) -> tuple[pd.Period, pd.Period | None]:
    """Return (last complete signal month, partial preview month or None)."""
    last_day = daily.index[-1]
    last_p = last_day.to_period("M")
    bar_final = (now_utc.date() > last_day.date()) or (
        now_utc.hour * 60 + now_utc.minute >= US_CLOSE_SAFE_UTC)
    month_done = last_day >= _nyse_last_session(last_p) and bar_final
    if month_done:
        return last_p, None
    return last_p - 1, last_p


def month_end_frame(daily: pd.DataFrame) -> pd.DataFrame:
    """Last available close per calendar month (== xts::to.monthly(indexAt='lastof')).
    For the open month this is the latest close -> PREVIEW semantics."""
    m = daily.groupby(daily.index.to_period("M")).last()
    m.index.name = "signal_month"
    return m


# ════════════════════════════════════════════════════════════════════════════
# 4. ADAA SLEEVES — line-by-line ports of the author's frozen rules
#    m: month-end price frame (PeriodIndex), i: integer row = signal month
# ════════════════════════════════════════════════════════════════════════════
def _p(m, a):
    return m[a].to_numpy(dtype=float)


def _complete(m, i, assets, lags) -> bool:
    if i - max(lags) < 0:
        return False
    rows = [i - k for k in lags]
    return not np.isnan(m[assets].to_numpy(dtype=float)[rows]).any()


def _ratio_avg(m, a, i, lags):            # mean(P_t / P_{t-k})
    p = _p(m, a)
    return float(np.mean([p[i] / p[i - k] for k in lags]))


def _sma_ratio(m, a, i, k=13):            # P_t / mean(P_{t-12..t})
    p = _p(m, a)
    return float(p[i] / p[i - k + 1:i + 1].mean())


def _rank(d: dict) -> pd.Series:          # R rank(ties.method="average")
    return pd.Series(d, dtype=float).rank(method="average")


def _zero():
    return pd.Series(0.0, index=UNIVERSE)


def sleeve_haa(m, i):
    risk = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "DBC", "IEF", "TLT", "EWY", "GLD"]
    safe = ["IEF", "BIL"]
    if not _complete(m, i, sorted(set(risk + safe + ["TIP"])), (0, 1, 3, 6, 12)):
        return None
    tip = _p(m, "TIP")
    canary = sum(tip[i] / tip[i - k] - 1 for k in (1, 3, 6, 12))          # TIP 1/3/6/12 canary
    w = _zero()
    if canary > 0:                                                          # offensive: Top-6 x 1/6
        rr = _rank({a: _ratio_avg(m, a, i, (1, 3, 6, 12)) for a in risk})
        w[rr[rr >= 6].index] = 1 / 6
    else:                                                                   # defensive: best of IEF/BIL
        rr = _rank({a: _ratio_avg(m, a, i, (1, 3, 6, 12)) for a in safe})
        sel = rr[rr >= 2].index
        w[sel if len(sel) else ["BIL"]] = 1.0
    return w


def sleeve_baa(m, i):
    risk = ["QQQ", "EEM", "EFA", "AGG"]
    safe = ["TIP", "DBC", "IEF", "TLT", "LQD", "AGG", "BIL"]
    cana = ["SPY", "EEM", "EFA", "AGG"]
    if not _complete(m, i, sorted(set(risk + safe + cana)), (0, 1, 3, 6, 12)):
        return None

    def w13612(a):
        p = _p(m, a)
        return (12 * (p[i] / p[i - 1] - 1) + 4 * (p[i] / p[i - 3] - 1)
                + 2 * (p[i] / p[i - 6] - 1) + (p[i] / p[i - 12] - 1))

    w = _zero()
    if all(w13612(a) >= 0 for a in cana):                                   # all canaries >= 0
        rr = _rank({a: _sma_ratio(m, a, i) for a in risk})
        w[rr[rr == 4].index] = 1.0                                          # Top-1 by SMA13 ratio
    else:
        ss = {a: _sma_ratio(m, a, i) for a in safe}
        rr = _rank(ss)
        w[rr[rr >= 5].index] = 1 / 3                                        # Top-3 x 1/3
        for a in safe:
            if a != "BIL" and ss[a] <= ss["BIL"]:
                w[a] = 0.0                                                  # worse than cash -> cash
        w["BIL"] += 1.0 - w.sum()
    return w


def _safe_residual(m, i, w, risk):
    res = 1.0 - w[risk].sum()
    w["SHY" if _sma_ratio(m, "SHY", i) > _sma_ratio(m, "BIL", i) else "BIL"] += res
    return w


ADM_FAA_RISK = ["SPY", "QQQ", "VGK", "EWY", "EEM", "VNQ", "DBC", "GLD", "TLT", "HYG", "LQD", "TIP"]


def sleeve_adm(m, i):
    risk, safe = ADM_FAA_RISK, ["SHY", "BIL"]
    if not _complete(m, i, risk + safe, (0, 1, 3, 6, 12)):
        return None
    sc = {a: _ratio_avg(m, a, i, (1, 3, 6)) for a in risk}
    rr = _rank(sc)
    w = _zero()
    for a in rr[rr >= 7].index:                                             # Top-6 ...
        if sc[a] >= 1:                                                      # ... with absolute momentum
            w[a] = 1 / 6
    return _safe_residual(m, i, w, risk)


FAA_VARIANTS = {
    # name: (peer_only_correlation, exact_n)
    "canonical_v016": (True, True),     # peer-only corr, exact Top-6 stable    (04_EXPORT_G3_CANONICAL_FAA_ONLY_v0_16.R)
    "legacy_v011": (False, False),      # EW incl. self, rank<=6, 1/len(sel)    (01_EXPORT_G2_..._v0_11_1.R build_faa)
    "peer_rank": (True, False),         # peer-only corr, rank<=6, 1/len(sel)
}
# legacy_v011 reproduces the published PDS Core: 116/116 months on real Yahoo data (2017-03..2026-10);
# canonical_v016 (the paper's corrected FAA) matched only 77/116.
FAA_VARIANT = os.environ.get("FAA_VARIANT", "legacy_v011")


def sleeve_faa(m, i, variant: str | None = None):
    """FAA sleeve. Variants differ in correlation definition and tie handling."""
    peer_only, exact_n = FAA_VARIANTS[variant or FAA_VARIANT]
    risk, safe = ADM_FAA_RISK, ["SHY", "BIL"]
    if i < 12 or not _complete(m, i, risk + safe, (0, 3, 6, 12)):
        return None
    px = m[risk].to_numpy(dtype=float)
    rwin = px[i - 5:i + 1] / px[i - 6:i] - 1                                # last 6 monthly returns
    if np.isnan(rwin).any():
        return None
    mom = {a: _ratio_avg(m, a, i, (3, 6, 12)) for a in risk}
    vol, corv = {}, {}
    ew_all = rwin.mean(axis=1)
    for j, a in enumerate(risk):
        vol[a] = float(np.std(rwin[:, j], ddof=1))
        peer = np.delete(rwin, j, axis=1).mean(axis=1) if peer_only else ew_all
        corv[a] = float(np.corrcoef(rwin[:, j], peer)[0, 1])
    if not all(np.isfinite(list(mom.values()) + list(vol.values()) + list(corv.values()))):
        return None
    M = _rank({a: -mom[a] for a in risk})
    V = _rank(vol)
    C = _rank(corv)
    agg = M + 0.5 * V + 0.5 * C                                             # R + 0.5V + 0.5C
    if exact_n:
        sel = sorted(risk, key=lambda a: (agg[a], a))[:6]
    else:                                                                   # ties can select 5 or 7
        rr = agg.rank(method="average")
        sel = list(rr[rr <= 6].index)
    if not sel:
        return None
    w = _zero()
    for a in sel:
        if mom[a] >= 1:
            w[a] = 1 / len(sel)
    return _safe_residual(m, i, w, risk)


def unrate_carry_calendar(unrate: pd.Series, through: pd.Period) -> tuple[pd.Series, bool]:
    """Calendar-complete UNRATE with missing months carried forward (e.g. Oct-2025)."""
    u = unrate.sort_index()
    full = pd.period_range(u.index.min(), max(through, u.index.max()), freq="M")
    raw = u.reindex(full)
    carried = bool(pd.isna(raw.get(through, np.nan)))
    return raw.ffill(), carried


def sleeve_laa(m, i, unrate):
    assets = ["SPY", "QQQ", "EEM", "EWY", "IEF", "GLD", "SHY"]
    sm = m.index[i]
    om = sm - 1                                                             # obs t-1 -> signal t
    ua, carried = unrate_carry_calendar(unrate, om)
    if om not in ua.index or pd.isna(ua[om]):
        return None, carried
    hist = ua.loc[:om].tail(13)
    if len(hist) < 13 or i < 10:
        return None, carried
    spy = _p(m, "SPY")[i - 10:i + 1]
    if np.isnan(spy).any() or m.iloc[i][assets].isna().any():
        return None, carried
    sp_score = spy[-1] / spy.mean()                                         # SPY / 11-month SMA
    un_score = ua[om] / hist.mean()                                         # UNRATE / 13-month SMA
    w = _zero()
    w[["SPY", "EEM", "EWY", "IEF", "GLD"]] = [0.175, 0.05, 0.025, 0.25, 0.25]
    w["QQQ" if (sp_score > 1 or un_score < 1) else "SHY"] = 0.25
    return w, carried


def adaa_weights(m, i, unrate):
    sl = {"HAA": sleeve_haa(m, i), "BAA": sleeve_baa(m, i),
          "ADM": sleeve_adm(m, i), "FAA": sleeve_faa(m, i)}
    sl["LAA"], carried = sleeve_laa(m, i, unrate)
    if any(v is None for v in sl.values()):
        missing = [k for k, v in sl.items() if v is None]
        raise RuntimeError(f"ADAA incomplete for {m.index[i]}: {missing}")
    w = sum(SLEEVE_MIX[k] * sl[k] for k in SLEEVE_MIX)
    return w, sl, carried


# ════════════════════════════════════════════════════════════════════════════
# 5. F2R (10%): use the author's published Top-4; momentum proxy as fallback
# ════════════════════════════════════════════════════════════════════════════
def f2r_book(state: dict | None) -> dict[str, dict]:
    """signal_month -> {'w': {ticker: 0.25}, 'src': ...}"""
    out: dict[str, dict] = {}
    if not state:
        return out
    for r in state.get("historical_targets", {}).get("full_history", []):
        out[r["month"]] = {"w": {r[f"rank{k}"]: 0.25 for k in range(1, 5)}, "src": "F2R_PUBLISHED"}
    live = state.get("live_state", {})
    off = live.get("official", {})
    if off.get("signal_period"):
        out[off["signal_period"]] = {"w": {h["asset"]: float(h["target_weight"]) for h in off["holdings"]},
                                     "src": "F2R_PUBLISHED"}
    pv = live.get("preview", {})
    if pv.get("available") and pv.get("signal_period") and pv["signal_period"] not in out:
        out[pv["signal_period"]] = {"w": {h["asset"]: float(h["target_weight"]) for h in pv["holdings"]},
                                    "src": "F2R_PREVIEW"}
    return out


def f2r_proxy(m, i):
    """Fallback only. The real F2R is a Ridge/ElasticNet/XGBoost rank ensemble + 20% Chronos-2
    (f2r-model-adoption-replication: VARIANT_MAP.md C3_REX3_CHRONOS20); features are private.
    Tie-break follows TIE_BREAKING_AND_INTEGRATION_SPEC.md: score desc, then fixed asset order."""
    sc = {a: _ratio_avg(m, a, i, (1, 3, 6, 12)) for a in F2R_UNIVERSE}
    top = sorted(F2R_UNIVERSE, key=lambda a: (-sc[a], F2R_UNIVERSE.index(a)))[:4]
    return {a: 0.25 for a in top}


def f2r_weights(book, m, i):
    key = str(m.index[i])
    if key in book:
        w, src = book[key]["w"], book[key]["src"]
    else:
        w, src = f2r_proxy(m, i), "F2R_PROXY_MOMENTUM"
    s = _zero()
    for a, v in w.items():
        s[a] += v
    return s, src


# ════════════════════════════════════════════════════════════════════════════
# 6. CORE, ADAPTIVE, ASSET CLASS, IRP
# ════════════════════════════════════════════════════════════════════════════
def core_weights(adaa, f2r):
    return CORE_MIX["ADAA"] * adaa + CORE_MIX["F2R"] * f2r


def apply_risk_budget(core, rb):
    w = core * rb
    w["BIL"] += 1.0 - rb
    return w


def reference_adaptive(ref: dict | None) -> dict[str, dict]:
    if not ref or "Adaptive Portfolio" not in ref:
        return {}
    df = ref["Adaptive Portfolio"]
    out = {}
    pri = {"OFFICIAL": 0, "PREVIEW": 1, "HISTORY": 2, "FORWARD_MONITOR": 3}
    for _, r in df.sort_values("Status", key=lambda s: s.map(pri)).iterrows():
        key = str(r["Signal Month"]) + ("P" if r["Status"] == "PREVIEW" else "")
        out.setdefault(key, {"state": r["State"], "rb": float(r["Risk Budget"]), "status": r["Status"]})
    return out


def reference_row_problems(row: dict | None) -> list[str]:
    """Invariants every published Adaptive row has satisfied (2021-04..2026-11). A row that breaks
    one is never applied (RB falls back to 1.0) and is flagged."""
    if row is None:
        return ["missing"]
    p = []
    if row.get("status") not in REF_USABLE_STATUS:
        p.append(f"status {row.get('status')!r} not executable")
    st, rb = row.get("state"), row.get("rb")
    if st not in ("NORMAL", "DEFENSIVE"):
        p.append(f"state {st!r}")
    if not isinstance(rb, (int, float)) or not math.isfinite(rb):
        p.append(f"risk budget {rb!r}")
    elif st == "NORMAL" and abs(rb - 1.0) > 1e-9:
        p.append(f"NORMAL with RB {rb}")
    elif st == "DEFENSIVE" and not (ADAPTIVE_FLOOR - 1e-9 <= rb < 1.0):
        p.append(f"DEFENSIVE with RB {rb} outside [{ADAPTIVE_FLOOR}, 1)")
    return p


def adaptive_proxy(core, daily, asof, m, i):
    """Transparent stand-in for the private PPO+vol hybrid (YOUR rule, not the author's).
    Mirrors the documented hybrid shape: DEFENSIVE only if BOTH signals indicate caution.
      trend  : the author's fixed 12-month defensive trend rule
               (protection-patience-replication/src/build_strategy_panel.py:
                trailing 12m market wealth / trailing 12m cash wealth - 1 <= 0), SPY vs BIL;
      vol    : point-in-time volatility budget = target vol / 63-day realised Core vol < 1."""
    rets = daily.loc[:asof, UNIVERSE].pct_change(fill_method=None).tail(PROXY_VOL_WINDOW)
    sigma = float((rets.fillna(0.0) @ core).std(ddof=1) * math.sqrt(252))
    vol_budget = 1.0 if sigma <= 0 else min(1.0, PROXY_TARGET_VOL / sigma)
    spy, bil = _p(m, "SPY"), _p(m, "BIL")
    trailing_excess = float((spy[i] / spy[i - 12]) / (bil[i] / bil[i - 12]) - 1.0)
    caution = trailing_excess <= 0.0
    diag = {"sigma": sigma, "vol_budget": vol_budget, "trend12_excess": trailing_excess}
    if caution and vol_budget < 1:
        return "DEFENSIVE", max(ADAPTIVE_FLOOR, vol_budget), diag
    return "NORMAL", 1.0, diag


def asset_classes(w):
    return {k: float(w[v].sum()) for k, v in ASSET_CLASS.items()}


def to_irp(w_us: pd.Series) -> dict:
    rows: dict[str, dict] = {}
    for t, wt in w_us.items():
        if wt <= TOL:
            continue
        code, name, cls, note = IRP_MAP[t]
        r = rows.setdefault(code, {"code": code, "name": name, "class": cls, "weight": 0.0,
                                   "from": [], "notes": set()})
        r["weight"] += float(wt)
        r["from"].append(t)
        if note:
            r["notes"].add(f"{t}: {note}")
    risk = sum(r["weight"] for r in rows.values() if r["class"] == "RISK")
    cap = IRP_RISK_CAP - IRP_CAP_BUFFER
    moved = 0.0
    if risk > cap:
        scale = cap / risk
        for r in rows.values():
            if r["class"] == "RISK":
                moved += r["weight"] * (1 - scale)
                r["weight"] *= scale
        code = IRP_OVERFLOW
        name = next(v[1] for v in IRP_MAP.values() if v[0] == code)
        r = rows.setdefault(code, {"code": code, "name": name, "class": "SAFE", "weight": 0.0,
                                   "from": [], "notes": set()})
        r["weight"] += moved
        r["from"].append("CAP_OVERFLOW")
    out = sorted(rows.values(), key=lambda r: -r["weight"])
    for r in out:
        r["notes"] = sorted(r["notes"])
    total = sum(r["weight"] for r in out)
    if abs(total - 1) > 1e-8:
        raise RuntimeError(f"IRP weights sum {total}")
    risk_after = sum(r["weight"] for r in out if r["class"] == "RISK")
    return {"rows": out, "risk_before_cap": risk, "risk_after_cap": risk_after,
            "moved_to_safe": moved, "cap": IRP_RISK_CAP, "cap_target": cap}


def check_weights(w: pd.Series, label: str):
    if (w < -TOL).any():
        raise RuntimeError(f"{label}: negative weight {w[w < -TOL].to_dict()}")
    if abs(float(w.sum()) - 1.0) > 1e-8:
        raise RuntimeError(f"{label}: weights sum {w.sum()}")


# ════════════════════════════════════════════════════════════════════════════
# 7. DECISIONS
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class Decision:
    status: str              # OFFICIAL | PREVIEW | HISTORY
    signal_month: str
    holding_month: str
    core: pd.Series
    adaptive: pd.Series
    state: str
    rb: float
    sources: dict

    def to_json(self):
        return {
            "status": self.status, "signal_month": self.signal_month,
            "holding_month": self.holding_month, "state": self.state, "risk_budget": self.rb,
            "core": {k: round(float(v), 10) for k, v in self.core.items()},
            "adaptive": {k: round(float(v), 10) for k, v in self.adaptive.items()},
            "sources": self.sources,
        }


def adaptive_rule(mode: str, status: str, kind: str, sm: str, ref_adapt: dict,
                  proxy: tuple[str, float]) -> tuple[str, float, str, dict | None]:
    """Return (state, rb, source, reference_seen).  kind: LIVE | BACKFILL | PREVIEW.

    reference_only: only a LIVE official lock may use the author's row, and only if that row is
    already in the workbook fetched by this run (= published before the lock) and passes
    reference_row_problems().  Backfilled months and previews are RB = 1.0: the author's values
    for them were not available in real time, so they stay in the reference ledger."""
    if mode == "proxy":
        return proxy[0], proxy[1], "PROXY_VOL_TREND", None
    if mode == "reference_then_proxy":                        # pre-1.1 behaviour, diagnostic only
        key = sm + ("P" if status == "PREVIEW" else "")
        if key in ref_adapt:
            r = ref_adapt[key]
            return r["state"], r["rb"], f"REFERENCE_{r['status']}", r
        return proxy[0], proxy[1], "PROXY_VOL_TREND", None
    if mode == "core_only":
        return "NORMAL", 1.0, "CORE_ONLY", ref_adapt.get(sm)
    if mode != "reference_only":
        raise RuntimeError(f"unknown ADAPTIVE_MODE {mode!r}")
    row = ref_adapt.get(sm)                                   # never the 'P' (PREVIEW) key
    if kind == "PREVIEW":
        return "NORMAL", 1.0, "CORE_DEFAULT_PREVIEW", ref_adapt.get(sm + "P")
    if kind == "BACKFILL":
        return "NORMAL", 1.0, "CORE_DEFAULT_BACKFILL", row
    if row is None:
        return "NORMAL", 1.0, "CORE_DEFAULT_REFERENCE_NOT_PUBLISHED", None
    if reference_row_problems(row):
        return "NORMAL", 1.0, "CORE_DEFAULT_REFERENCE_REJECTED", row
    return row["state"], float(row["rb"]), f"REFERENCE_{row['status']}_AT_LOCK", row


def decide(status, m, i, daily, unrate, f2r, ref_adapt, asof, kind: str = "LIVE",
           mode: str | None = None) -> Decision:
    mode = mode or ADAPTIVE_MODE
    adaa, sleeves, carried = adaa_weights(m, i, unrate)
    f2w, f2src = f2r_weights(f2r, m, i)
    core = core_weights(adaa, f2w)
    sm = str(m.index[i])
    p_state, p_rb, diag = adaptive_proxy(core, daily, asof, m, i)
    state, rb, asrc, seen = adaptive_rule(mode, status, kind, sm, ref_adapt, (p_state, p_rb))
    if state == "NORMAL" and rb != 1.0 or state == "DEFENSIVE" and not (ADAPTIVE_FLOOR - 1e-9 <= rb < 1.0) \
            or state not in ("NORMAL", "DEFENSIVE"):
        raise RuntimeError(f"adaptive {sm}: inconsistent state/RB {state}/{rb}")
    adaptive = apply_risk_budget(core, rb)
    check_weights(core, f"core {sm}")
    check_weights(adaptive, f"adaptive {sm}")
    return Decision(status, sm, str(m.index[i] + 1), core, adaptive, state, rb, {
        "f2r": f2src, "adaptive": asrc, "adaptive_mode": mode, "unrate_carried": carried,
        "asof": str(asof.date()),
        "reference_seen": None if seen is None else {"status": seen["status"], "state": seen["state"],
                                                     "rb": seen["rb"], "problems": reference_row_problems(seen)
                                                     if seen.get("status") != "PREVIEW" else []},
        "proxy": {"state": p_state, "rb": p_rb, **{k: (round(v, 6) if isinstance(v, float) else v)
                                                   for k, v in diag.items()}},
        "sleeves": {k: {a: round(float(x), 6) for a, x in v.items() if x > TOL} for k, v in sleeves.items()},
    })


def load_lock() -> list[dict]:
    if LOCK_JSON.exists():
        return json.loads(LOCK_JSON.read_text(encoding="utf-8"))
    return []


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def assert_lock_append_only(before: list[dict], after: list[dict]) -> int:
    """Every previously locked month must survive byte-for-byte; only new months may be added.
    Returns the number of new entries.  Any violation aborts the run (nothing is written)."""
    b = {d["signal_month"]: _canon(d) for d in before}
    a = {d["signal_month"]: _canon(d) for d in after}
    if len(b) != len(before) or len(a) != len(after):
        raise RuntimeError("duplicate signal months in the official lock")
    lost = sorted(set(b) - set(a))
    changed = sorted(k for k in b if k in a and a[k] != b[k])
    if lost or changed:
        raise RuntimeError(f"official lock is append-only: removed {lost}, changed {changed}")
    if [d["signal_month"] for d in after] != sorted(a):
        raise RuntimeError("official lock not in signal-month order")
    return len(a) - len(b)


# ── reference ledger: what the author published, and when this pipeline first saw it ──────────
def load_ref_ledger() -> dict:
    if REF_LEDGER_JSON.exists():
        return json.loads(REF_LEDGER_JSON.read_text(encoding="utf-8"))
    return {"schema": "pds-lite/reference-adaptive/1", "source": REF_WORKBOOK_URL,
            "note": "Author-published Adaptive rows, reference only. Never used to rewrite official_history.json.",
            "rows": {}}


def update_ref_ledger(ledger: dict, ref, now_utc: datetime) -> tuple[dict, list[str]]:
    """Append-only: new rows are added with first_seen_utc; a changed value appends a revision;
    a row that disappears keeps its last value and gets missing_since_utc.  Nothing is deleted.
    Rows present when the ledger is first created are marked seeded=true (their real publication
    time is unknown)."""
    led = json.loads(json.dumps(ledger))                     # deep copy
    rows = led.setdefault("rows", {})
    seeding = not rows
    ts = now_utc.isoformat(timespec="seconds")
    notes: list[str] = []
    led["last_fetch_utc"] = ts
    if not ref or "Adaptive Portfolio" not in ref:
        led["last_fetch_ok"] = False
        return led, ["REFERENCE_UNAVAILABLE"]
    led["last_fetch_ok"] = True
    seen = set()
    for _, r in ref["Adaptive Portfolio"].iterrows():
        sm, status = str(r["Signal Month"]), str(r["Status"])
        key = f"{sm}|{status}"
        seen.add(key)
        cur = {"state": str(r["State"]), "risk_budget": float(r["Risk Budget"])}
        if key not in rows:
            rows[key] = {"signal_month": sm, "status": status, **cur, "first_seen_utc": ts,
                         "last_seen_utc": ts, "seeded": seeding, "revisions": []}
            if not seeding:
                notes.append(f"REFERENCE_NEW {key} {cur['state']} {cur['risk_budget']:.4f}")
            continue
        row = rows[key]
        row["last_seen_utc"] = ts
        row.pop("missing_since_utc", None)
        if row["state"] != cur["state"] or abs(row["risk_budget"] - cur["risk_budget"]) > 1e-9:
            row["revisions"].append({"seen_utc": ts, "from": {"state": row["state"],
                                                               "risk_budget": row["risk_budget"]}, "to": cur})
            row.update(cur)
            notes.append(f"REFERENCE_REVISED {key} -> {cur['state']} {cur['risk_budget']:.4f}")
    for key, row in rows.items():
        if key not in seen and "missing_since_utc" not in row:
            row["missing_since_utc"] = ts
    led["rows"] = dict(sorted(rows.items()))
    return led, notes


def reference_view(ledger: dict, sm: str | None, preview: bool = False) -> dict | None:
    """Best published row for a signal month from the ledger (for display only)."""
    if not sm:
        return None
    order = ("PREVIEW",) if preview else ("OFFICIAL", "FORWARD_MONITOR", "HISTORY")
    for st in order:
        r = ledger.get("rows", {}).get(f"{sm}|{st}")
        if r:
            return {k: r[k] for k in ("signal_month", "status", "state", "risk_budget", "first_seen_utc",
                                      "seeded") if k in r}
    return None


def series_from(d: dict) -> pd.Series:
    return pd.Series({a: float(d.get(a, 0.0)) for a in UNIVERSE})


# ════════════════════════════════════════════════════════════════════════════
# 8. VALIDATION against the author's published workbook
# ════════════════════════════════════════════════════════════════════════════
def validate_against_reference(decisions: list[dict], ref) -> dict:
    if not ref or "Core Portfolio" not in ref:
        return {"available": False}
    df = ref["Core Portfolio"]
    df = df[df["Status"] != "PREVIEW"].set_index("Signal Month")
    diffs, mism = [], []
    for d in decisions:
        sm = d["signal_month"]
        if sm not in df.index:
            continue
        ref_w = df.loc[sm, UNIVERSE].astype(float)
        diff = float((series_from(d["core"]) - ref_w).abs().max())
        diffs.append(diff)
        if diff > 1e-6:
            mism.append({"signal_month": sm, "max_abs_diff": diff})
    return {"available": True, "months_checked": len(diffs),
            "exact_matches": int(sum(x <= 1e-6 for x in diffs)),
            "max_abs_diff": max(diffs) if diffs else None, "mismatches": mism[-12:]}


# ════════════════════════════════════════════════════════════════════════════
# 9. PAYLOAD
# ════════════════════════════════════════════════════════════════════════════
def _rows(w_core: dict, w_adapt: dict, prev: dict | None):
    rows = []
    for t in UNIVERSE:
        c, a = float(w_core.get(t, 0)), float(w_adapt.get(t, 0))
        p = float(prev.get(t, 0)) if prev else None
        if c > TOL or a > TOL or (p and p > TOL):
            rows.append({"ticker": t, "name": NAMES[t], "core": c, "adaptive": a,
                         "delta": None if p is None else a - p,
                         "class": next(k for k, v in ASSET_CLASS.items() if t in v)})
    return sorted(rows, key=lambda r: -r["adaptive"])


def _block(d: dict | None, prev_adapt: dict | None):
    if not d:
        return None
    ad = series_from(d["adaptive"])
    co = series_from(d["core"])
    # target-to-target turnover (ignores intra-month drift, so it understates real turnover)
    l1 = float((ad - series_from(prev_adapt)).abs().sum()) if prev_adapt else None
    return {
        "status": d.get("status"),
        "signal_month": d["signal_month"], "holding_month": d["holding_month"],
        "state": d["state"], "risk_budget": d["risk_budget"], "sources": d.get("sources", {}),
        "turnover_one_way": None if l1 is None else l1 / 2,
        "est_cost": None if l1 is None else l1 * COST_PER_SIDE,
        "us": _rows(d["core"], d["adaptive"], prev_adapt),
        "asset_class": {"core": asset_classes(co), "adaptive": asset_classes(ad)},
        "irp": {"adaptive": to_irp(ad), "core": to_irp(co)},
    }


def record_class(d: dict) -> str:
    """LIVE = locked by the scheduled run within LIVE_LOCK_MAX_DAYS of the signal cutoff;
    RECONSTRUCTED = filled in later (backfill or a late first run).  Derived, never stored."""
    try:
        cutoff = datetime.fromisoformat(d["sources"]["asof"]).replace(tzinfo=timezone.utc)
        locked = datetime.fromisoformat(d["locked_at_utc"])
    except (KeyError, TypeError, ValueError):
        return "UNKNOWN"
    if d.get("origin") == "LIVE" and (locked - cutoff).days <= LIVE_LOCK_MAX_DAYS:
        return "LIVE"
    return "RECONSTRUCTED"


def build_payload(history: list[dict], preview: dict | None, validation: dict, mode: str,
                  asof: str, flags: list[str], reference: dict | None = None) -> dict:
    history = sorted(history, key=lambda d: d["signal_month"])
    official = history[-1]
    prev = history[-2]["adaptive"] if len(history) > 1 else None
    now = datetime.now(timezone.utc)
    trend = [{"signal_month": d["signal_month"], "holding_month": d["holding_month"],
              "state": d["state"], "risk_budget": d["risk_budget"],
              "adaptive_source": d.get("sources", {}).get("adaptive"), "record_class": record_class(d),
              "core": asset_classes(series_from(d["core"])),
              "adaptive": asset_classes(series_from(d["adaptive"]))} for d in history]
    out = {
        "schema": "pds-lite/1", "engine": ENGINE_VERSION, "mode": mode,
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
        "market_asof": asof, "flags": flags,
        "official": _block(official, prev),
        "preview": _block(preview, official["adaptive"]) if preview else None,
        "history": trend, "validation": validation,
        "config": {"sleeve_mix": SLEEVE_MIX, "core_mix": CORE_MIX, "adaptive_mode": ADAPTIVE_MODE,
                   "adaptive_floor": ADAPTIVE_FLOOR, "irp_cap": IRP_RISK_CAP,
                   "irp_cap_buffer": IRP_CAP_BUFFER,
                   "official_requires_f2r_published": OFFICIAL_REQUIRES_F2R_PUBLISHED,
                   "official_requires_author_core": OFFICIAL_REQUIRES_AUTHOR_CORE,
                   "lock_deadline_day": LOCK_DEADLINE_DAY,
                   "lock_final_check_utc_hour": LOCK_FINAL_CHECK_UTC_HOUR},
        "disclaimer": "Research reconstruction for personal monitoring. Not investment advice.",
    }
    if reference is not None:
        out["reference"] = reference
    return out


# ════════════════════════════════════════════════════════════════════════════
# 10. MODES
# ════════════════════════════════════════════════════════════════════════════
def run_reference(ref) -> dict:
    """Mirror the author's published workbook (no prices needed)."""
    if not ref:
        raise RuntimeError("reference workbook unavailable")
    core = ref["Core Portfolio"]
    adapt = ref["Adaptive Portfolio"]
    amap = reference_adaptive(ref)
    hist, preview = [], None
    for _, r in core.iterrows():
        sm = str(r["Signal Month"])
        key = sm + ("P" if r["Status"] == "PREVIEW" else "")
        cw = r[UNIVERSE].astype(float)
        a = amap.get(key)
        state, rb = (a["state"], a["rb"]) if a else ("NORMAL", 1.0)
        aw = apply_risk_budget(cw, rb)
        d = {"status": r["Status"], "signal_month": sm, "holding_month": str(r["Holding Month"]),
             "state": state, "risk_budget": rb,
             "core": cw.to_dict(), "adaptive": aw.to_dict(),
             "sources": {"core": "REFERENCE_WORKBOOK",
                         "adaptive": f"REFERENCE_{a['status']}" if a else "NOT_PUBLISHED_DEFAULT_NORMAL"}}
        if r["Status"] == "PREVIEW":
            preview = d
        else:
            hist.append(d)
    asof = str(adapt["Signal Month"].iloc[0])
    return build_payload(hist, preview, {"available": True, "note": "reference mode = published values"},
                         "reference", asof, ["REFERENCE_MODE"])


def diagnose_faa_variants(m, unrate, f2r, ref, backfill_from, last_off) -> dict:
    """Core match rate vs the published workbook for each FAA variant (other sleeves fixed)."""
    if not ref or "Core Portfolio" not in ref:
        return {}
    df = ref["Core Portfolio"]
    df = df[df["Status"] != "PREVIEW"].set_index("Signal Month")
    out = {v: {"checked": 0, "exact": 0, "miss": []} for v in FAA_VARIANTS}
    for p in m.index:
        sm = str(p)
        if sm < backfill_from or p > last_off or sm not in df.index:
            continue
        i = m.index.get_loc(p)
        base = {"HAA": sleeve_haa(m, i), "BAA": sleeve_baa(m, i), "ADM": sleeve_adm(m, i),
                "LAA": sleeve_laa(m, i, unrate)[0]}
        if any(v is None for v in base.values()):
            continue
        f2w = f2r_weights(f2r, m, i)[0]
        refw = df.loc[sm, UNIVERSE].astype(float)
        for v in FAA_VARIANTS:
            faa = sleeve_faa(m, i, v)
            if faa is None:
                continue
            sl = dict(base, FAA=faa)
            core = core_weights(sum(SLEEVE_MIX[k] * sl[k] for k in SLEEVE_MIX), f2w)
            ok = float((core - refw).abs().max()) <= 1e-6
            out[v]["checked"] += 1
            out[v]["exact"] += int(ok)
            if not ok:
                out[v]["miss"].append(sm)
    for v in out:
        print(f"[diag] FAA {v}: {out[v]['exact']}/{out[v]['checked']} exact")
    return out


def _author_core(ref, sm: str) -> pd.Series | None:
    if not ref or "Core Portfolio" not in ref:
        return None
    df = ref["Core Portfolio"]
    df = df[(df["Status"] != "PREVIEW") & (df["Signal Month"].astype(str) == sm)]
    return None if df.empty else df.iloc[0][UNIVERSE].astype(float)


def lock_deadlines(sm: str) -> tuple[datetime, datetime]:
    """(final_check, hard_deadline) in UTC for signal month sm, both on LOCK_DEADLINE_DAY of the
    holding month: from final_check an incomplete month is BLOCKED; after hard_deadline even
    complete data is never locked automatically."""
    h = pd.Period(sm, "M") + 1
    final = datetime(h.year, h.month, LOCK_DEADLINE_DAY, LOCK_FINAL_CHECK_UTC_HOUR, 0, tzinfo=timezone.utc)
    hard = datetime(h.year, h.month, LOCK_DEADLINE_DAY, 23, 59, 59, tzinfo=timezone.utc)
    return final, hard


def load_events() -> list[dict]:
    if EVENTS_JSON.exists():
        return json.loads(EVENTS_JSON.read_text(encoding="utf-8"))
    return []


def load_releases() -> list[dict]:
    if RELEASES_JSON.exists():
        return json.loads(RELEASES_JSON.read_text(encoding="utf-8"))
    return []


def release_input_problems(daily, now_utc: datetime, unrate_src: str, f2r_state, ref,
                           lock_before: list[dict]) -> list[str]:
    """Checks a manual release needs on top of official F2R + matching author Core: the approval
    reason, every public input fetched live, fresh prices (integrity was already checked in
    fetch_prices) and the author's published Core history still equal to our locked Core."""
    probs = []
    if not MANUAL_RELEASE_REASON:
        probs.append("approval reason missing (workflow input release_reason)")
    if OPERATOR_HALT:
        probs.append("operator halt is active (PDS_HALT)")
    if f2r_state is None:
        probs.append("F2R endpoint unavailable")
    if not ref:
        probs.append("author workbook unavailable")
    if unrate_src == "CACHE":
        probs.append("UNRATE from cache (FRED and BLS both failed)")
    age = (now_utc.date() - daily.index[-1].date()).days
    if age > RELEASE_MAX_PRICE_AGE_DAYS:
        probs.append(f"prices stale: last close {daily.index[-1].date()} ({age} days old)")
    if ref:
        v = validate_against_reference(lock_before, ref)
        if v.get("months_checked") and v["exact_matches"] != v["months_checked"]:
            probs.append(f"author Core history mismatch: {v['exact_matches']}/{v['months_checked']} months")
    return probs


def _event(events: list[dict], sm: str, kind: str, now_utc: datetime, detail: str, once: bool = True):
    if once and any(e["signal_month"] == sm and e["event"] == kind for e in events):
        return
    events.append({"signal_month": sm, "event": kind, "at_utc": now_utc.isoformat(timespec="seconds"),
                   "detail": detail})


def rebalance_action(live, official: dict) -> dict:
    """What the operator should do now.  FOLLOW_OFFICIAL: the newest month is locked; WAIT: inside
    days 1-10, official data incomplete, do not trade; HALTED: deadline passed incomplete, keep the
    current holdings and stop automatic rebalancing until a manual release."""
    off = official["signal_month"]
    if live is not None and live[0] == "OPERATOR_HALT":
        sm = live[1]
        return {"code": "HALTED", "month": sm, "in_force": off, "reason": "operator halt (PDS_HALT)",
                "message": f"⛔ 운영자 중지(PDS_HALT): 신규 잠금·자동 리밸런싱 없음. 현재 보유 자산을 그대로 "
                           f"유지하세요(마지막 공식 {off}). 저장소 변수 PDS_HALT를 지우면 다음 실행부터 정상 동작."}
    if live is None:
        return {"code": "FOLLOW_OFFICIAL", "month": off, "in_force": off, "reason": None,
                "message": f"공식 목표 {off}→{official['holding_month']} 유효. 신규 잠금 대상 월 없음."}
    code, d, why, final_check = live
    sm = d["signal_month"]
    out = {"code": code, "month": sm, "in_force": off, "reason": why,
           "final_check_utc": final_check.isoformat(timespec="minutes")}
    if code == "FOLLOW_OFFICIAL":
        out["message"] = f"{sm} 공식 목표 확정({d['holding_month']} 보유). 이 목표로만 리밸런싱."
    elif code == "WAIT":
        out["message"] = (f"{sm} 잠금 대기: {why}. 매월 {LOCK_DEADLINE_DAY}일까지 매일 재확인. "
                          f"리밸런싱하지 말고 현재 보유를 유지하세요(직전 공식 {off}).")
    else:
        out["message"] = (f"⛔ {sm} 자동 리밸런싱 중지: {why}. 현재 보유 자산을 그대로 유지하세요. "
                          f"직접 확인한 뒤 release_month={sm}, release_reason을 넣어 실행해야만 해제되며, "
                          f"공식 F2R·원작자 Core 일치·입력 데이터 정상일 때만 잠깁니다.")
    return out


def official_hold_reason(d: dict, ref, sm: str, live: bool) -> str | None:
    """Why this month may not be locked yet (None = complete and verified)."""
    if OFFICIAL_REQUIRES_F2R_PUBLISHED and d["sources"]["f2r"] != "F2R_PUBLISHED":
        return f"F2R official not published ({d['sources']['f2r']})"
    if not live:
        return None
    acore = _author_core(ref, sm)
    if acore is None:
        return "author's Core for this month not published" if OFFICIAL_REQUIRES_AUTHOR_CORE else None
    diff = float((series_from(d["core"]) - acore).abs().max())
    if diff > 1e-6:
        return f"Core differs from the author's published Core by {diff:.4f}"
    return None


def run_engine(backfill_from: str, now_utc: datetime, mode: str | None = None) -> tuple[dict, dict]:
    """Compute everything in memory.  Returns (payload, files) where files maps a path to the
    object to write; main() writes them only after this function has returned without error."""
    mode = mode or ADAPTIVE_MODE
    if mode not in ADAPTIVE_MODES:
        raise RuntimeError(f"unknown ADAPTIVE_MODE {mode!r}")
    daily = fetch_prices()                                  # integrity-checked, retried, fail closed
    unrate, unrate_src = fetch_unrate()
    print(f"[data] prices {daily.index[0].date()}..{daily.index[-1].date()}, UNRATE via {unrate_src} "
          f"(last {unrate.dropna().index.max()})")
    f2r_state = fetch_f2r_state()
    f2r = f2r_book(f2r_state)
    ref = fetch_reference_workbook()
    ref_adapt = reference_adaptive(ref)
    m = month_end_frame(daily)
    last_off, prev_p = split_clock(daily, now_utc)

    lock_before = load_lock()
    lock = {d["signal_month"]: d for d in lock_before}
    oev_before = load_events()
    oev = json.loads(json.dumps(oev_before))
    flags: list[str] = [] if unrate_src != "CACHE" else ["UNRATE_FROM_CACHE"]
    if f2r_state is None:
        flags.append("F2R_UNAVAILABLE")
    earliest = lock_before[0]["signal_month"] if lock_before else None
    latest = lock_before[-1]["signal_month"] if lock_before else None
    rel_before = load_releases()
    releases = json.loads(json.dumps(rel_before))
    release = MANUAL_RELEASE_MONTH or None
    rel = None                                          # this run's manual-release record
    if release:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", release):
            raise RuntimeError(f"MANUAL_RELEASE_MONTH must be YYYY-MM, got {release!r}")
        probs = release_input_problems(daily, now_utc, unrate_src, f2r_state, ref, lock_before)
        if latest and release <= latest:
            probs.insert(0, f"not newer than the latest official {latest}")
        run_url = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
                   f"{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else None
        rel = {"signal_month": release, "requested_at_utc": now_utc.isoformat(timespec="seconds"),
               "actor": os.environ.get("GITHUB_ACTOR") or "local", "run_url": run_url,
               "reason": MANUAL_RELEASE_REASON, "outcome": None, "problems": probs,
               "checks": {"price_integrity": "passed", "prices_last_close": str(daily.index[-1].date()),
                          "unrate_source": unrate_src, "f2r_endpoint": f2r_state is not None,
                          "author_workbook": bool(ref), "author_core_history": None}}
        if ref:
            v = validate_against_reference(lock_before, ref)
            rel["checks"]["author_core_history"] = f"{v.get('exact_matches')}/{v.get('months_checked')}"
        if probs:
            release = None
    months = [p for p in m.index if str(p) >= backfill_from and p <= last_off]
    live = None                                         # outcome of the newest live month
    gaps: list[str] = []
    if OPERATOR_HALT:
        months = []                                     # nothing is locked while the operator halt is on
        live = ("OPERATOR_HALT", str(last_off))
        flags.append("OPERATOR_HALT: PDS_HALT is set; no new lock, keep current holdings")
        _event(oev, str(last_off), "OPERATOR_HALT", now_utc, "PDS_HALT set; no new locks")
    for p in months:
        sm = str(p)
        if sm in lock:
            continue                                    # official decisions are immutable
        final_check, hard_deadline = lock_deadlines(sm)
        if latest is None:                              # bootstrap: reconstruct the history
            kind = "LIVE" if (p == last_off and now_utc <= hard_deadline) else "BACKFILL"
        elif sm < earliest:
            kind = "BACKFILL"                           # extending the history backwards
        elif sm < latest:
            gaps.append(sm)                             # blocked and never released: stays unlocked
            continue
        else:
            kind = "LIVE"
        i = m.index.get_loc(p)
        asof = daily.index[daily.index.to_period("M") == p][-1]
        try:
            d = decide("OFFICIAL", m, i, daily, unrate, f2r, ref_adapt, asof, kind=kind, mode=mode).to_json()
        except RuntimeError as e:
            if kind == "LIVE":
                raise                                   # live month must succeed (fail closed)
            print(f"[skip] backfill {sm}: {e}")
            continue
        hold = official_hold_reason(d, ref, sm, live=(kind == "LIVE"))
        if hold and hold.startswith("Core differs"):
            _event(oev, sm, "CORE_MISMATCH", now_utc, hold)
        if rel is not None and rel["signal_month"] == sm:
            acore = _author_core(ref, sm)
            rel["checks"].update({"f2r_source": d["sources"]["f2r"], "author_core_published": acore is not None,
                                  "author_core_max_abs_diff": None if acore is None else
                                  float((series_from(d["core"]) - acore).abs().max())})
        if kind == "BACKFILL":
            if hold:
                flags.append(f"BACKFILL_SKIPPED {sm}: {hold}")
                continue
        else:
            released = release == sm
            if hold and released:
                rel["problems"].append(hold)
                released = False
            if hold and now_utc < final_check:
                d["status"] = "PENDING"
                live = ("WAIT", d, hold, final_check)
                flags.append(f"OFFICIAL_PENDING {sm}: {hold}; previous official stays in force "
                             f"(final check {final_check:%Y-%m-%d %H:%M} UTC)")
                _event(oev, sm, "PENDING", now_utc, hold)
                continue
            if hold or (now_utc > hard_deadline and not released):
                why = hold or "official data complete only after the lock deadline"
                d["status"] = "BLOCKED"
                live = ("HALTED", d, why, final_check)
                flags.append(f"REBALANCE_HALTED {sm}: {why}. Keep current holdings; no automatic lock after "
                             f"day {LOCK_DEADLINE_DAY}. Manual release (workflow input release_month={sm}) "
                             f"locks it only once official F2R and the author's Core are complete and match.")
                _event(oev, sm, "BLOCKED", now_utc, why)
                continue
        d["locked_at_utc"] = now_utc.isoformat(timespec="seconds")
        if kind == "BACKFILL":
            d["origin"] = "BACKFILL_RECOMPUTED"
        elif release == sm:
            d["origin"] = "MANUAL_RELEASE"
            d["manual_release"] = {k: rel[k] for k in ("requested_at_utc", "actor", "run_url", "reason")}
            rel["outcome"] = "LOCKED"
            rel["locked"] = {"state": d["state"], "risk_budget": d["risk_budget"],
                             "adaptive_source": d["sources"]["adaptive"]}
            _event(oev, sm, "MANUAL_RELEASE", now_utc,
                   f"by {rel['actor']}: {rel['reason']}; official F2R and author Core verified", once=False)
        else:
            d["origin"] = "LIVE"
        if kind == "LIVE":
            live = ("FOLLOW_OFFICIAL", d, None, final_check)
            _event(oev, sm, "LOCKED", now_utc, f"{d['origin']} {d['state']} RB {d['risk_budget']:.4f} "
                   f"({d['sources']['adaptive']})")
        lock[sm] = d
        print(f"[lock] {sm}: state={d['state']} rb={d['risk_budget']:.4f} adaptive={d['sources']['adaptive']} "
              f"f2r={d['sources']['f2r']} origin={d['origin']}")
    if rel is not None:
        if rel["outcome"] is None:
            if not rel["problems"]:
                rel["problems"].append("signal month not complete yet, or not a month awaiting its lock")
            rel["outcome"] = "REJECTED"
            why = "; ".join(rel["problems"])
            flags.append(f"MANUAL_RELEASE_REJECTED {rel['signal_month']}: {why}")
            _event(oev, rel["signal_month"], "MANUAL_RELEASE_REJECTED", now_utc,
                   f"by {rel['actor']}: {why}", once=False)
        releases.append(rel)
    history = [lock[k] for k in sorted(lock)]
    if not history:
        raise RuntimeError("no official decision available")
    n_new = assert_lock_append_only(lock_before, history)
    if oev[:len(oev_before)] != oev_before:
        raise RuntimeError("official_events.json is append-only")
    if releases[:len(rel_before)] != rel_before:
        raise RuntimeError("manual_releases.json is append-only")

    preview = None
    if live is not None and live[0] in ("WAIT", "HALTED"):
        preview = live[1]                               # the month awaiting/blocked: 'do not execute'
    elif prev_p is not None and prev_p in m.index:
        i = m.index.get_loc(prev_p)
        try:
            preview = decide("PREVIEW", m, i, daily, unrate, f2r, ref_adapt, daily.index[-1],
                             kind="PREVIEW", mode=mode).to_json()
        except RuntimeError as e:
            flags.append(f"PREVIEW_UNAVAILABLE: {e}")
    for d in [history[-1]] + ([preview] if preview else []):
        if d["sources"]["f2r"] != "F2R_PUBLISHED":
            flags.append(f"{d['signal_month']}: {d['sources']['f2r']}")
        if d["sources"]["adaptive"].startswith("PROXY"):
            flags.append(f"{d['signal_month']}: ADAPTIVE_PROXY")
        if d["sources"]["unrate_carried"]:
            flags.append(f"{d['signal_month']}: UNRATE_CARRIED")

    ledger, events = update_ref_ledger(load_ref_ledger(), ref, now_utc)
    flags += [e for e in events if not e.startswith("REFERENCE_NEW")]
    off = history[-1]
    rv = reference_view(ledger, off["signal_month"])
    if rv and not off["sources"]["adaptive"].startswith("REFERENCE") and \
            (rv["state"] != off["state"] or abs(rv["risk_budget"] - off["risk_budget"]) > 1e-9):
        flags.append(f"{off['signal_month']}: AUTHOR_REFERENCE {rv['state']} RB {rv['risk_budget']:.2f} "
                     f"(published after lock, not applied)")
    reference = {
        "note": "Author-published Adaptive values for reference only. They are applied to a month only if "
                "already published when that month was locked (see official.sources.adaptive).",
        "official": rv,
        "applied_to_official": off["sources"]["adaptive"].startswith("REFERENCE"),
        "preview": reference_view(ledger, preview["signal_month"], preview=True)
        if preview and preview["status"] == "PREVIEW" else None,
        "pending": reference_view(ledger, preview["signal_month"])
        if preview and preview["status"] in ("PENDING", "BLOCKED") else None,
        "ledger_rows": len(ledger.get("rows", {})), "last_fetch_ok": ledger.get("last_fetch_ok"),
        "events": events,
    }
    validation = validate_against_reference(history, ref)
    validation["faa_variant"] = FAA_VARIANT
    validation["faa_variant_match"] = diagnose_faa_variants(m, unrate, f2r, ref, backfill_from, last_off)
    validation["new_official_locks"] = n_new
    payload = build_payload(history, preview, validation, "engine", str(daily.index[-1].date()), flags,
                            reference=reference)
    payload["config"]["adaptive_mode"] = mode
    payload["action"] = rebalance_action(live, history[-1])
    payload["unlocked_gaps"] = gaps
    payload["official_events"] = oev[-12:]
    payload["notify"] = oev[len(oev_before):]           # state changes of this run -> Telegram
    payload["manual_release"] = rel
    payload["config"]["operator_halt"] = OPERATOR_HALT
    if mode in LOCKING_MODES:
        files = {LOCK_JSON: history, REF_LEDGER_JSON: ledger, EVENTS_JSON: oev, RELEASES_JSON: releases,
                 LATEST_JSON: payload}
    else:                                               # diagnostic: never touch operational files
        files = {DIAG_DIR / f"portfolio_{mode}.json": payload}
    return payload, files


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["engine", "reference"], default="engine")
    ap.add_argument("--backfill-from", default="2017-03")
    ap.add_argument("--reference-file", help="local copy of the reference workbook (offline)")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.mode == "reference":
        ref = (pd.read_excel(args.reference_file, sheet_name=None) if args.reference_file
               else fetch_reference_workbook())
        payload = run_reference(ref)
        files = {DIAG_DIR / "portfolio_reference_mirror.json": payload}   # mirror is never operational
    else:
        payload, files = run_engine(args.backfill_from, now)
    for path, obj in files.items():                     # only reached if every check passed
        save_json(path, obj)
    o = payload["official"]
    print(f"OK {payload['mode']} ({payload['config']['adaptive_mode']}): official {o['signal_month']}->"
          f"{o['holding_month']} {o['state']} RB={o['risk_budget']:.2%}; wrote {[p.name for p in files]}; "
          f"flags={payload['flags']}")
    if payload.get("action"):
        print(f"ACTION {payload['action']['code']} {payload['action']['month']}: {payload['action']['message']}")
    v = payload["validation"]
    if v.get("months_checked"):
        print(f"validation: {v['exact_matches']}/{v['months_checked']} months exact, max diff {v['max_abs_diff']:.2e}")


if __name__ == "__main__":
    sys.exit(main())
