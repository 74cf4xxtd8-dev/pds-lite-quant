"""reference_only + fail-safe tests (offline; run: python -m pytest tests -q).

Before/after comparisons load run_pipeline.py from the base ref (PDS_BASE_REF, default main)
and run both versions on identical inputs.  The live real-data comparison at the bottom needs
network and runs only with PDS_LIVE_TEST=1 (see .github/workflows/tests.yml).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H  # noqa: E402

NEW = H.NEW
sys.path.insert(0, str(H.ROOT / "tools"))
import notify_message as NOTIFY  # noqa: E402
import record_run_status as RUNSTATUS  # noqa: E402
import rollback_check as ROLLBACK  # noqa: E402
import send_alert  # noqa: E402,F401
NOV1 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)      # Oct-2026 complete -> last_off = 2026-10
DEC1 = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def market():
    daily, un = H.synthetic_market()
    return daily, un, sorted(set(daily.index.to_period("M")))


@pytest.fixture(scope="module")
def base():
    b = H.load_base()
    if b is None:
        pytest.skip("base ref not available (set PDS_BASE_REF or fetch main)")
    return b


def wb(official=("DEFENSIVE", 0.7), core_rows=None):
    return H.author_workbook(H.default_adaptive_rows(official), core_rows)


def wbc(daily, un, f2r, official=("DEFENSIVE", 0.7), months=("2026-10",)):
    """Author workbook whose OFFICIAL Core row for `months` equals the engine's Core (as published
    by the author the same day as F2R) - required before a live month may lock."""
    return wb(official, H.author_core_rows(daily, un, f2r, list(months)))


def canon(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. before/after: 116 months of Core and monthly target weights
# ─────────────────────────────────────────────────────────────────────────────
def test_116_months_core_identical_before_after(market, base, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    ref = wbc(daily, un, f2r)
    _, h_old, _ = H.run(base, "reference_then_proxy", NOV1, tmp_path / "old", daily, un, f2r, ref)
    p_new, h_new, _ = H.run(NEW, "reference_only", NOV1, tmp_path / "new", daily, un, f2r, ref)
    assert [d["signal_month"] for d in h_old] == [d["signal_month"] for d in h_new]
    assert p_new["validation"]["exact_matches"] == p_new["validation"]["months_checked"] == 1
    assert len(h_new) == 116 and h_new[0]["signal_month"] == "2017-03" and h_new[-1]["signal_month"] == "2026-10"
    for a, b in zip(h_old, h_new):
        assert a["core"] == b["core"], a["signal_month"]                 # bit-identical Core
        assert a["sources"]["sleeves"] == b["sources"]["sleeves"]
        assert a["sources"]["f2r"] == b["sources"]["f2r"]
        assert b["sources"]["proxy"] == a["sources"]["proxy"]            # diagnostic kept
    # new monthly target weights: Core 100% everywhere except a LIVE month whose author row was
    # already published at lock time (here 2026-10 OFFICIAL DEFENSIVE 0.7)
    for d in h_new[:-1]:
        assert d["risk_budget"] == 1.0 and d["adaptive"] == d["core"], d["signal_month"]
        assert d["sources"]["adaptive"] == "CORE_DEFAULT_BACKFILL"
    last = h_new[-1]
    assert last["sources"]["adaptive"] == "REFERENCE_OFFICIAL_AT_LOCK" and last["risk_budget"] == 0.7
    adaptive = NEW.apply_risk_budget(NEW.series_from(last["core"]), 0.7)
    assert np.allclose(NEW.series_from(last["adaptive"]).values, adaptive.values, atol=1e-10)
    report = os.environ.get("PDS_TEST_REPORT_DIR")
    if report:
        _write_weight_report(Path(report) / "synthetic_before_after.csv", h_old, h_new)


def test_diagnostic_mode_reproduces_base_exactly(market, base, tmp_path):
    """reference_then_proxy in the new code == the old default, except bookkeeping fields."""
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    ref = wbc(daily, un, f2r)
    _, h_old, _ = H.run(base, "reference_then_proxy", NOV1, tmp_path / "old", daily, un, f2r, ref)
    seen, guard = {}, NEW.assert_lock_append_only

    def capture(before, after):                      # diagnostic runs never write the lock: grab it here
        seen["h"] = after
        return guard(before, after)
    NEW.assert_lock_append_only = capture
    try:
        p, _, files = H.run(NEW, "reference_then_proxy", NOV1, tmp_path / "new", daily, un, f2r, ref, write=False)
    finally:
        NEW.assert_lock_append_only = guard
    h_new = seen["h"]
    assert len(h_old) == len(h_new) == 116
    strip = lambda d: {k: v for k, v in d.items() if k not in ("sources", "locked_at_utc")}  # noqa: E731
    for a, b in zip(h_old, h_new):
        assert strip(a) == strip(b), a["signal_month"]
        assert a["sources"]["adaptive"] == b["sources"]["adaptive"]
    assert list(files) == [NEW.DIAG_DIR / "portfolio_reference_then_proxy.json"]   # never operational


# ─────────────────────────────────────────────────────────────────────────────
# 1. RB = 1.0 until the author's official row is published
# ─────────────────────────────────────────────────────────────────────────────
def test_rb_is_one_before_publication(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r, official=None))
    d = h[-1]
    assert d["signal_month"] == "2026-10" and d["origin"] == "LIVE"
    assert (d["state"], d["risk_budget"]) == ("NORMAL", 1.0)
    assert d["sources"]["adaptive"] == "CORE_DEFAULT_REFERENCE_NOT_PUBLISHED"
    assert d["adaptive"] == d["core"]
    assert p["official"]["risk_budget"] == 1.0


def test_preview_is_core_and_author_preview_is_reference_only(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10", preview="2026-11")
    d2, _ = H.synthetic_market(end="2026-11-20")
    rows = H.default_adaptive_rows(("NORMAL", 1.0))
    rows = [r for r in rows if r[0] != "PREVIEW"] + [("PREVIEW", "2026-11", "DEFENSIVE", 0.6)]
    p, h, _ = H.run(NEW, "reference_only", datetime(2026, 11, 21, tzinfo=timezone.utc), tmp_path,
                    d2, un, f2r, H.author_workbook(rows))
    assert p["preview"]["signal_month"] == "2026-11"
    assert (p["preview"]["state"], p["preview"]["risk_budget"]) == ("NORMAL", 1.0)
    assert p["preview"]["sources"]["adaptive"] == "CORE_DEFAULT_PREVIEW"
    assert p["reference"]["preview"]["state"] == "DEFENSIVE"            # shown, not applied


def test_core_only_never_applies_author(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    _, h, _ = H.run(NEW, "core_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r, ("DEFENSIVE", 0.7)))
    assert h[-1]["signal_month"] == "2026-10" and all(d["risk_budget"] == 1.0 for d in h)
    assert h[-1]["sources"]["reference_seen"]["state"] == "DEFENSIVE"


def test_invalid_author_row_is_rejected(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    for bad in (("NORMAL", 0.8), ("DEFENSIVE", 0.3), ("DEFENSIVE", 1.0), ("WEIRD", 0.9)):
        p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path / str(bad), daily, un, f2r, wbc(daily, un, f2r, bad))
        assert h[-1]["signal_month"] == "2026-10" and h[-1]["risk_budget"] == 1.0, bad
        assert h[-1]["sources"]["adaptive"] == "CORE_DEFAULT_REFERENCE_REJECTED", bad


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3. reference ledger separate; later publications never rewrite a lock
# ─────────────────────────────────────────────────────────────────────────────
def test_late_publication_goes_to_ledger_not_lock(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    _, h1, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r, official=None))
    assert h1[-1]["signal_month"] == "2026-10"
    locked = canon(h1)
    later = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
    p2, h2, _ = H.run(NEW, "reference_only", later, tmp_path, daily, un, f2r, wbc(daily, un, f2r, ("DEFENSIVE", 0.71)))
    assert canon(h2) == locked                                          # byte-identical lock
    led = json.loads(NEW.REF_LEDGER_JSON.read_text())
    row = led["rows"]["2026-10|OFFICIAL"]
    assert row["state"] == "DEFENSIVE" and row["first_seen_utc"].startswith("2026-11-08")
    assert row["seeded"] is False
    assert p2["official"]["risk_budget"] == 1.0
    assert p2["reference"]["official"]["state"] == "DEFENSIVE" and p2["reference"]["applied_to_official"] is False
    assert any("AUTHOR_REFERENCE DEFENSIVE" in f and "not applied" in f for f in p2["flags"])


def test_ledger_is_append_only(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb(("NORMAL", 1.0)))
    first = json.loads(NEW.REF_LEDGER_JSON.read_text())
    assert first["rows"]["2026-09|FORWARD_MONITOR"]["seeded"] is True
    # author revises 2026-09 and drops 2024-09 from the sheet
    rows = [r for r in H.default_adaptive_rows(("NORMAL", 1.0)) if r[1] != "2024-09"]
    rows = [(s, sm, "NORMAL", 1.0) if sm == "2026-09" else (s, sm, st, rb) for s, sm, st, rb in rows]
    p, _, _ = H.run(NEW, "reference_only", datetime(2026, 11, 9, tzinfo=timezone.utc), tmp_path,
                    daily, un, f2r, H.author_workbook(rows))
    led = json.loads(NEW.REF_LEDGER_JSON.read_text())
    r9 = led["rows"]["2026-09|FORWARD_MONITOR"]
    assert r9["state"] == "NORMAL" and r9["revisions"][0]["from"]["state"] == "DEFENSIVE"
    assert "missing_since_utc" in led["rows"]["2024-09|HISTORY"]            # kept, not deleted
    assert set(first["rows"]) <= set(led["rows"])
    assert any(f.startswith("REFERENCE_REVISED 2026-09|FORWARD_MONITOR") for f in p["flags"])


def test_real_committed_lock_is_preserved(market, tmp_path):
    """The 116 months currently in docs/data/official_history.json survive byte-for-byte."""
    real = json.loads((H.ROOT / "docs" / "data" / "official_history.json").read_text(encoding="utf-8"))
    daily, un = H.synthetic_market(end="2026-11-30")
    months = sorted(set(daily.index.to_period("M")))
    f2r = H.f2r_state(months, official="2026-11")
    p, h, _ = H.run(NEW, "reference_only", DEC1, tmp_path, daily, un, f2r, wbc(daily, un, f2r, months=("2026-11",)),
                    lock_init=real)
    assert len(h) == len(real) + 1 and h[-1]["signal_month"] == "2026-11"
    for a, b in zip(real, h):
        assert canon(a) == canon(b), a["signal_month"]
    assert h[-1]["sources"]["adaptive"] == "CORE_DEFAULT_REFERENCE_NOT_PUBLISHED"
    classes = {r["record_class"] for r in p["history"][:-1]}
    assert classes == {"RECONSTRUCTED"}                                   # all 116 were filled in later
    assert p["history"][-1]["record_class"] == "LIVE"


def test_rollback_base_code_reads_new_outputs(market, base, tmp_path):
    """After a git revert, the old pipeline must keep working on files written by the new one."""
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-09")
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb(official=None))   # Oct pending
    new_lock = json.loads((tmp_path / "docs" / "data" / "official_history.json").read_text())
    f2r_ok = H.f2r_state(months, official="2026-10")
    p, h, _ = H.run(base, "reference_then_proxy", NOV1, tmp_path, daily, un, f2r_ok, wb(official=None))
    for a, b in zip(new_lock, h):
        assert canon(a) == canon(b)                                       # old code keeps new locks
    assert h[-1]["signal_month"] == "2026-10" and p["official"]["signal_month"] == "2026-10"


def test_lock_guard_rejects_any_rewrite():
    a = [{"signal_month": "2026-09", "x": 1}, {"signal_month": "2026-10", "x": 2}]
    assert NEW.assert_lock_append_only(a, a + [{"signal_month": "2026-11", "x": 3}]) == 1
    with pytest.raises(RuntimeError):
        NEW.assert_lock_append_only(a, [a[0], {"signal_month": "2026-10", "x": 9}])
    with pytest.raises(RuntimeError):
        NEW.assert_lock_append_only(a, [a[1]])
    with pytest.raises(RuntimeError):
        NEW.assert_lock_append_only(a, [a[1], a[0]])


# ─────────────────────────────────────────────────────────────────────────────
# 4. fail-safe
# ─────────────────────────────────────────────────────────────────────────────
def test_price_integrity_checks(market):
    daily, _, _ = market
    NEW.check_price_integrity(daily)
    gap = daily.copy()
    gap.iloc[-30, 3] = np.nan
    jump = daily.copy()
    jump.iloc[-5, 0] *= 1.4
    frozen_all = daily.copy()
    frozen_all.iloc[-3:] = frozen_all.iloc[-4].values
    stale = daily.copy()
    stale.iloc[-12:, 5] = stale.iloc[-13, 5]
    for bad in (gap, jump, frozen_all, stale, daily.iloc[:, :-1]):
        with pytest.raises(RuntimeError):
            NEW.check_price_integrity(bad)


def _snapshot(out: Path):
    return {p.name: p.read_bytes() for p in out.glob("*.json")}


def test_failures_write_nothing(market, tmp_path):
    """API failure / bad prices / bad UNRATE: main() raises before any file is touched."""
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    out = H.point(NEW, tmp_path)
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r))   # a good first run
    snap = _snapshot(out)
    assert set(snap) == {"official_history.json", "reference_adaptive.json", "official_events.json",
                         "manual_releases.json", "portfolio_latest.json"}

    def boom():
        raise RuntimeError("price download failed: HTTP 503")
    bad_px = daily.copy()
    bad_px.iloc[-2, 2] = np.nan
    cases = {"api": boom, "nan": lambda: (NEW.check_price_integrity(bad_px), bad_px)[1]}
    for name, fn in cases.items():
        H.feed(NEW, daily, un, f2r, wbc(daily, un, f2r))
        NEW.fetch_prices = fn
        NEW.ADAPTIVE_MODE = "reference_only"
        with pytest.raises(RuntimeError):
            NEW.main([])
        assert _snapshot(out) == snap, name


def test_month_lock_waits_for_f2r_publication(market, tmp_path):
    daily, un, months = market
    f2r_late = H.f2r_state(months, official="2026-09", preview="2026-10")    # Oct F2R not official yet
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r_late, wb(official=None))
    assert h[-1]["signal_month"] == "2026-09"                                 # Oct NOT locked
    assert p["official"]["signal_month"] == "2026-09"
    assert p["preview"]["signal_month"] == "2026-10" and p["preview"]["sources"]["f2r"] == "F2R_PREVIEW"
    assert any(f.startswith("OFFICIAL_PENDING 2026-10") for f in p["flags"])
    assert p["action"]["code"] == "WAIT" and p["action"]["month"] == "2026-10"
    # F2R and the author's Core publish on Nov 2 -> the next run locks October as a LIVE record
    f2r_ok = H.f2r_state(months, official="2026-10")
    p2, h2, _ = H.run(NEW, "reference_only", datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc), tmp_path,
                      daily, un, f2r_ok, wbc(daily, un, f2r_ok, official=None))
    assert h2[-1]["signal_month"] == "2026-10" and h2[-1]["origin"] == "LIVE"
    assert p2["action"]["code"] == "FOLLOW_OFFICIAL" and p2["preview"] is None   # no partial month yet
    assert h2[-1]["sources"]["f2r"] == "F2R_PUBLISHED"


def test_pending_month_takes_the_preview_slot_early_next_month(tmp_path):
    """Dec 1-8: November is complete but its F2R is not published; December is a partial month.
    The dashboard must show the pending November (not December) as the preview."""
    daily, un = H.synthetic_market(end="2026-12-02")
    months = sorted(set(daily.index.to_period("M")))
    p, h, _ = H.run(NEW, "reference_only", datetime(2026, 12, 3, tzinfo=timezone.utc), tmp_path,
                    daily, un, H.f2r_state(months, official="2026-10", preview="2026-11"), wb(official=None))
    assert h[-1]["signal_month"] == "2026-10"
    assert p["preview"]["signal_month"] == "2026-11" and p["preview"]["status"] == "PENDING"
    assert any(f.startswith("OFFICIAL_PENDING 2026-11") for f in p["flags"])


def test_f2r_api_failure_does_not_lock_with_proxy(market, tmp_path):
    daily, un, months = market
    with pytest.raises(RuntimeError):                                     # fresh start: nothing lockable
        H.run(NEW, "reference_only", NOV1, tmp_path / "fresh", daily, un, None, wb(official=None))
    assert not (tmp_path / "fresh" / "docs" / "data" / "official_history.json").exists()
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, H.f2r_state(months, official="2026-09"), wb(official=None))
    before = NEW.LOCK_JSON.read_text()
    p, h, _ = H.run(NEW, "reference_only", datetime(2026, 11, 3, tzinfo=timezone.utc), tmp_path,
                    daily, un, None, wb(official=None))                   # F2R endpoint down
    assert NEW.LOCK_JSON.read_text() == before and h[-1]["signal_month"] == "2026-09"
    assert "F2R_UNAVAILABLE" in p["flags"]
    assert any(f.startswith("OFFICIAL_PENDING 2026-10") and "F2R_PROXY_MOMENTUM" in f for f in p["flags"])


def test_core_mismatch_with_author_holds_the_lock(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    wrong = {("OFFICIAL", "2026-10"): {"BIL": 1.0}}
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb(official=None, core_rows=wrong))
    assert h[-1]["signal_month"] == "2026-09"
    assert any(f.startswith("OFFICIAL_PENDING 2026-10") and "Core differs" in f for f in p["flags"])


def test_reference_workbook_unavailable_defaults_to_core(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, None)
    assert h[-1]["risk_budget"] == 1.0 and "REFERENCE_UNAVAILABLE" in p["flags"]
    # without the workbook the live month cannot be Core-verified -> it waits, it is not locked
    assert h[-1]["signal_month"] == "2026-09" and p["action"]["code"] == "WAIT"
    assert any(f.startswith("OFFICIAL_PENDING 2026-10") and "author's Core" in f for f in p["flags"])


def test_diagnostic_and_mirror_modes_never_touch_operational_files(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    out = H.point(NEW, tmp_path)
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r))
    snap = _snapshot(out)
    for mode in ("proxy", "reference_then_proxy"):
        H.feed(NEW, daily, un, f2r, wbc(daily, un, f2r))
        NEW.ADAPTIVE_MODE = mode
        NEW.main([])
        assert _snapshot(out) == snap, mode
    H.feed(NEW, daily, un, f2r, wb(core_rows={("HISTORY", "2026-09"): {"SPY": 1.0},
                                              ("OFFICIAL", "2026-10"): {"SPY": 1.0}}))
    NEW.main(["--mode", "reference"])                                     # mirror of the author's workbook
    assert _snapshot(out) == snap
    assert (out / "diagnostic" / "portfolio_reference_mirror.json").exists()
    assert (out / "diagnostic" / "portfolio_proxy.json").exists()
    NEW.ADAPTIVE_MODE = "reference_only"


# ─────────────────────────────────────────────────────────────────────────────
# 7. lock deadline: days 1-10 daily check, BLOCKED after day 10, manual release only
# ─────────────────────────────────────────────────────────────────────────────
def _utc(m, d, h=22, mi=40, y=2026):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def dl_market():
    """One market through 2026-12-02; each run sees only the closes up to its own date."""
    daily, un = H.synthetic_market(end="2026-12-02")
    months = sorted(set(daily.index.to_period("M")))
    return {"daily": daily, "un": un,
            "sep": H.f2r_state(months, official="2026-09", preview="2026-10"),
            "oct": H.f2r_state(months, official="2026-10", preview="2026-11"),
            "nov": H.f2r_state(months, official="2026-11")}


OK_REASON = "공식 F2R·원작자 Core 일치 직접 확인"


def _run_at(dl, tmp, now, f2r, ref, release=None, reason=OK_REASON, px_until=None, after_feed=None, auth="ok"):
    daily = dl["daily"].loc[:pd.Timestamp(px_until or now.date())]
    auth_file = Path(tmp) / "release_auth.json"
    auth_file.unlink(missing_ok=True)
    if release and auth is not None:
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(json.dumps(H.release_auth(release) if auth == "ok" else auth), encoding="utf-8")
    NEW.MANUAL_RELEASE_MONTH = release or ""
    NEW.MANUAL_RELEASE_REASON = reason if release else ""
    try:
        return H.run(NEW, "reference_only", now, tmp, daily, dl["un"], f2r, ref, after_feed=after_feed)
    finally:
        NEW.MANUAL_RELEASE_MONTH = NEW.MANUAL_RELEASE_REASON = ""


def _releases():
    return json.loads(NEW.RELEASES_JSON.read_text(encoding="utf-8"))


def _core_wb(dl, f2r, months, official=None):
    return wbc(dl["daily"], dl["un"], f2r, official=official, months=months)


def _events():
    return json.loads(NEW.EVENTS_JSON.read_text(encoding="utf-8"))


def _start_with_october_pending(dl, tmp):
    """Nov 1: history locked through 2026-09; October's F2R is not official yet."""
    p, h, _ = _run_at(dl, tmp, datetime(2026, 11, 1, tzinfo=timezone.utc), dl["sep"], wb(official=None))
    assert h[-1]["signal_month"] == "2026-09" and p["action"]["code"] == "WAIT"
    return NEW.LOCK_JSON.read_bytes()


def test_lock_deadlines_are_day_10_of_the_holding_month():
    assert NEW.lock_deadlines("2026-10") == (_utc(11, 10, 22, 0), datetime(2026, 11, 10, 23, 59, 59, tzinfo=timezone.utc))
    assert NEW.lock_deadlines("2026-12")[0] == _utc(1, 10, 22, 0, y=2027)          # year rollover
    assert NEW.LOCK_DEADLINE_DAY == 10


def test_daily_check_until_day_10_then_block(dl_market, tmp_path):
    dl = dl_market
    lock0 = _start_with_october_pending(dl, tmp_path)
    log = [_events()]
    for now in (_utc(11, 3), _utc(11, 7), _utc(11, 10, 21, 0)):                     # still inside the window
        p, h, _ = _run_at(dl, tmp_path, now, dl["sep"], wb(official=None))
        assert p["action"]["code"] == "WAIT" and p["preview"]["status"] == "PENDING", now
        assert p["official"]["signal_month"] == "2026-09" and NEW.LOCK_JSON.read_bytes() == lock0
        log.append(_events())
    p, h, _ = _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))   # day-10 final check
    assert NEW.LOCK_JSON.read_bytes() == lock0                                      # nothing locked
    assert p["action"]["code"] == "HALTED" and p["action"]["month"] == "2026-10"
    assert "현재 보유" in p["action"]["message"] and "release_month=2026-10" in p["action"]["message"]
    assert p["preview"]["status"] == "BLOCKED" and p["preview"]["sources"]["f2r"] == "F2R_PREVIEW"
    assert p["official"]["signal_month"] == "2026-09"                               # no preview substitution
    assert any(f.startswith("REBALANCE_HALTED 2026-10") for f in p["flags"])
    log.append(_events())
    for a, b in zip(log, log[1:]):
        assert b[:len(a)] == a                                                      # events append-only
    assert [(e["signal_month"], e["event"]) for e in log[-1]] == [("2026-10", "PENDING"), ("2026-10", "BLOCKED")]


def test_no_automatic_lock_after_day_10_even_with_complete_data(dl_market, tmp_path):
    dl = dl_market
    lock0 = _start_with_october_pending(dl, tmp_path)
    _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))              # BLOCKED
    p, h, _ = _run_at(dl, tmp_path, _utc(11, 13), dl["oct"], _core_wb(dl, dl["oct"], ["2026-10"]))
    assert NEW.LOCK_JSON.read_bytes() == lock0 and p["action"]["code"] == "HALTED"
    assert "only after the lock deadline" in p["action"]["reason"]
    # next month runs normally: November locks, October stays an unlocked gap
    ref = _core_wb(dl, dl["nov"], ["2026-10", "2026-11"])
    p, h, _ = _run_at(dl, tmp_path, _utc(12, 2), dl["nov"], ref)
    assert [d["signal_month"] for d in h[-2:]] == ["2026-09", "2026-11"] and h[-1]["origin"] == "LIVE"
    assert p["action"]["code"] == "FOLLOW_OFFICIAL" and p["action"]["month"] == "2026-11"
    p, h2, _ = _run_at(dl, tmp_path, _utc(12, 2, 23, 0), dl["nov"], ref)
    assert p["unlocked_gaps"] == ["2026-10"] and canon(h2) == canon(h)
    assert [e["event"] for e in _events() if e["signal_month"] == "2026-10"] == ["PENDING", "BLOCKED"]


def test_manual_release_needs_complete_official_data(dl_market, tmp_path, monkeypatch):
    dl = dl_market
    monkeypatch.setenv("GITHUB_ACTOR", "owner")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    lock0 = _start_with_october_pending(dl, tmp_path)
    _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))              # BLOCKED
    # release requested but F2R still not official -> rejected, still blocked
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 11, 3, 0), dl["sep"], wb(official=None), release="2026-10")
    assert NEW.LOCK_JSON.read_bytes() == lock0 and p["action"]["code"] == "HALTED"
    assert any(f.startswith("MANUAL_RELEASE_REJECTED 2026-10") and "F2R official not published" in f
               for f in p["flags"])
    # release with official F2R but the author's Core disagrees -> rejected
    bad = wb(official=None, core_rows={("OFFICIAL", "2026-10"): {"BIL": 1.0}})
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 12, 3, 0), dl["oct"], bad, release="2026-10")
    assert NEW.LOCK_JSON.read_bytes() == lock0
    assert any(f.startswith("MANUAL_RELEASE_REJECTED 2026-10") and "Core differs" in f for f in p["flags"])
    # complete data but no approval reason -> rejected
    ref = _core_wb(dl, dl["oct"], ["2026-10"], official=("DEFENSIVE", 0.7122398))
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 12, 4, 0), dl["oct"], ref, release="2026-10", reason="")
    assert NEW.LOCK_JSON.read_bytes() == lock0
    assert any("approval reason missing" in f for f in p["flags"])
    # complete, matching, inputs healthy, reason given -> locked as a manual release; the author's
    # Adaptive row already published at this moment is applied (D3), later ones are not
    p, h, _ = _run_at(dl, tmp_path, _utc(11, 13), dl["oct"], ref, release="2026-10")
    assert h[-1]["signal_month"] == "2026-10" and h[-1]["origin"] == "MANUAL_RELEASE"
    assert h[-1]["manual_release"] == {"requested_at_utc": "2026-11-13T22:40:00+00:00", "actor": "owner",
                                       "run_url": "https://github.com/o/r/actions/runs/42", "reason": OK_REASON,
                                       "approved_by": ["owner"], "approval_type": "self-confirmation"}
    assert h[-1]["sources"]["adaptive"] == "REFERENCE_OFFICIAL_AT_LOCK" and h[-1]["risk_budget"] == 0.7122398
    assert p["action"]["code"] == "FOLLOW_OFFICIAL" and p["history"][-1]["record_class"] == "RECONSTRUCTED"
    ev = [(e["signal_month"], e["event"]) for e in _events()]
    assert ev[-2:] == [("2026-10", "MANUAL_RELEASE"), ("2026-10", "LOCKED")]
    assert ev.count(("2026-10", "MANUAL_RELEASE_REJECTED")) == 3
    rel = _releases()                                                               # separate approval record
    assert [r["outcome"] for r in rel] == ["REJECTED", "REJECTED", "REJECTED", "LOCKED"]
    last = rel[-1]
    assert last["actor"] == "owner" and last["reason"] == OK_REASON and last["problems"] == []
    assert last["checks"]["f2r_source"] == "F2R_PUBLISHED" and last["checks"]["author_core_max_abs_diff"] <= 1e-12
    assert last["checks"]["unrate_source"] == "TEST" and last["checks"]["price_integrity"] == "passed"
    assert last["checks"]["author_core_history"] == "0/0" and last["locked"]["risk_budget"] == 0.7122398
    assert rel[1]["checks"]["author_core_max_abs_diff"] > 0.5
    assert last["authorization"]["actor_permission"] == "admin" and last["authorization"]["ref"] == "refs/heads/main"
    assert last["authorization"]["approvals"][0]["user"] == "owner"
    assert last["authorization"]["approval_type"] == "self-confirmation"              # 1-person repo
    assert "본인 재확인(1인 승인)" in [e for e in _events() if e["event"] == "MANUAL_RELEASE"][-1]["detail"]
    # releasing a month that is not newer than the latest official is refused
    locked = NEW.LOCK_JSON.read_bytes()
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 14), dl["oct"], ref, release="2026-09")
    assert NEW.LOCK_JSON.read_bytes() == locked
    assert any(f.startswith("MANUAL_RELEASE_REJECTED 2026-09: not newer") for f in p["flags"])
    assert _releases()[:4] == rel and _releases()[-1]["outcome"] == "REJECTED"   # append-only


@pytest.mark.parametrize("case", ["unrate_cache", "stale_prices", "f2r_endpoint_down", "author_history_mismatch"])
def test_manual_release_requires_healthy_inputs(dl_market, tmp_path, case):
    dl = dl_market
    lock0 = _start_with_october_pending(dl, tmp_path)
    _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))              # BLOCKED
    f2r, ref, kw = dl["oct"], _core_wb(dl, dl["oct"], ["2026-10"]), {}
    if case == "unrate_cache":
        kw["after_feed"] = lambda mod: setattr(mod, "fetch_unrate", lambda: (dl["un"].copy(), "CACHE"))
        expect = "UNRATE from cache"
    elif case == "stale_prices":
        kw["px_until"] = "2026-11-06"                                               # 9 days old on 11/15
        expect = "prices stale"
    elif case == "f2r_endpoint_down":
        f2r = None
        expect = "F2R endpoint unavailable"
    else:
        rows = H.author_core_rows(dl["daily"], dl["un"], dl["oct"], ["2026-10"])
        rows[("HISTORY", "2026-09")] = {"BIL": 1.0}                                 # published history moved
        ref = wb(official=None, core_rows=rows)
        expect = "author Core history mismatch"
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 15), f2r, ref, release="2026-10", **kw)
    assert NEW.LOCK_JSON.read_bytes() == lock0 and p["action"]["code"] == "HALTED"
    rec = _releases()[-1]
    assert rec["outcome"] == "REJECTED" and any(expect in x for x in rec["problems"]), rec["problems"]


def test_operator_halt_stops_every_new_lock(dl_market, tmp_path):
    dl = dl_market
    lock0 = _start_with_october_pending(dl, tmp_path)
    ref = _core_wb(dl, dl["oct"], ["2026-10"])
    NEW.OPERATOR_HALT = True
    p, _, files = _run_at(dl, tmp_path, _utc(11, 3), dl["oct"], ref)                # complete, but halted
    assert files == {} and NEW.LOCK_JSON.read_bytes() == lock0                     # read-only run
    assert p["action"]["code"] == "HALTED" and "PDS_HALT" in p["action"]["message"]
    assert [e["event"] for e in p["notify"]] == ["OPERATOR_HALT"]                    # de-duplicated in run_status
    rel0 = NEW.RELEASES_JSON.read_bytes()
    p, _, files = _run_at(dl, tmp_path, _utc(11, 4, 23, 0), dl["oct"], ref, release="2026-10")
    assert files == {} and NEW.LOCK_JSON.read_bytes() == lock0 and NEW.RELEASES_JSON.read_bytes() == rel0
    assert p["manual_release"]["outcome"] == "REJECTED"
    assert "operator halt is active (PDS_HALT)" in p["manual_release"]["problems"]
    NEW.OPERATOR_HALT = False                                                       # variable removed
    p, h, _ = _run_at(dl, tmp_path, _utc(11, 5), dl["oct"], ref)
    assert h[-1]["signal_month"] == "2026-10" and h[-1]["origin"] == "LIVE"
    assert [e["event"] for e in p["notify"]] == ["LOCKED"]


@pytest.mark.parametrize("value,halted", [("1", True), ("true", True), ("Y", True), ("0", False), ("", False),
                                          ("off", False)])
def test_pds_halt_values(monkeypatch, value, halted):
    import importlib.util
    monkeypatch.setenv("PDS_HALT", value)
    spec = importlib.util.spec_from_file_location("rp_halt_probe", H.ROOT / "run_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rp_halt_probe"] = mod
    spec.loader.exec_module(mod)
    assert mod.OPERATOR_HALT is halted                                              # a typo halts, never runs


def test_operator_halt_changes_no_operational_data(dl_market, tmp_path):
    """Halted run through main(): complete official data, a new author publication and a release
    request are all present, yet no byte under docs/data changes; only run_result.json (outside)."""
    dl = dl_market
    _start_with_october_pending(dl, tmp_path)
    out = tmp_path / "docs" / "data"
    (out / "run_status.json").write_text(json.dumps({"recent": [
        {"at_utc": "2026-11-02T22:44:00+00:00", "ok": True, "action": "FOLLOW_OFFICIAL", "new_locks": 1,
         "operator_halt": False}]}), encoding="utf-8")
    before = {f.name: f.read_bytes() for f in out.rglob("*") if f.is_file()}
    rows = H.default_adaptive_rows(("DEFENSIVE", 0.8))                                # newly published by the author
    ref = wb(None, H.author_core_rows(dl["daily"], dl["un"], dl["oct"], ["2026-10"]))
    ref["Adaptive Portfolio"] = H.author_workbook(rows)["Adaptive Portfolio"]
    H.point(NEW, tmp_path)
    H.feed(NEW, dl["daily"].loc[:"2026-11-03"], dl["un"], dl["oct"], ref)
    (tmp_path / "release_auth.json").write_text(json.dumps(H.release_auth("2026-10")), encoding="utf-8")
    NEW.ADAPTIVE_MODE, NEW.OPERATOR_HALT = "reference_only", True
    NEW.MANUAL_RELEASE_MONTH, NEW.MANUAL_RELEASE_REASON = "2026-10", OK_REASON
    NEW.main([])
    after = {f.name: f.read_bytes() for f in out.rglob("*") if f.is_file()}
    assert after == before                                                          # nothing under docs/data
    res = json.loads((tmp_path / "run_result.json").read_text(encoding="utf-8"))
    assert res["operator_halt"] is True and res["files_written"] == [] and res["new_locks"] == 0
    assert res["action"]["code"] == "HALTED" and res["official"]["signal_month"] == "2026-09"
    assert res["manual_release"]["outcome"] == "REJECTED"
    assert res["halt_report"]["previous_run"]["new_locks"] == 1                     # what ran just before
    assert {e["event"] for e in res["notify"]} == {"OPERATOR_HALT", "MANUAL_RELEASE_REJECTED"}


def test_halt_notice_once_and_active_run_report():
    now = datetime(2026, 11, 3, 22, 45, tzinfo=timezone.utc)
    res = {"generated_at_utc": now.isoformat(), "operator_halt": True, "new_locks": 0,
           "action": {"code": "HALTED", "month": "2026-10", "reason": "operator halt (PDS_HALT)"},
           "notify": [{"signal_month": "2026-10", "event": "OPERATOR_HALT", "detail": "x"}],
           "halt_report": {"previous_run": {"at_utc": "2026-11-02T22:44:00+00:00", "action": "FOLLOW_OFFICIAL",
                                            "new_locks": 1}, "locks_last_72h": [
                               {"signal_month": "2026-10", "locked_at_utc": "2026-11-02T22:44:00+00:00"}]}}
    first = RUNSTATUS.build({"recent": [{"ok": True, "operator_halt": False}]}, True, res, [], "workflow_dispatch",
                            "u", now, active_runs=[])
    assert [e["event"] for e in first["notify"]] == ["OPERATOR_HALT"] and first["operator_halt"] is True
    assert first["halt_report"]["other_active_runs"] == []
    msg = NOTIFY.message(dict(first, official={}))
    assert "직전 실행: 2026-11-02T22:44:00+00:00 FOLLOW_OFFICIAL, 신규 잠금 1" in msg
    assert "최근 72시간 잠금: 2026-10(2026-11-02T22:44:00+00:00)" in msg and "다른 update-portfolio 실행: 0건" in msg
    again = RUNSTATUS.build(first, True, res, [], "schedule", "u", now)              # still halted
    assert again["notify"] == [] and again["recent"][-1]["operator_halt"] is True
    runs = [{"id": 1, "status": "in_progress"}, {"id": 2, "status": "completed"}, {"id": 3, "status": "queued"}]
    assert [r["id"] for r in RUNSTATUS.other_active_runs(runs, "3")] == [1]


def test_ops_audit_flags_settings_against_the_readme():
    sys.path.insert(0, str(H.ROOT / "tools"))
    import ops_audit as OA
    perms = OA.workflow_permissions(H.ROOT / ".github" / "workflows")
    assert set(perms) >= {"update.yml", "tests.yml", "rollback-check.yml", "ops-audit.yml"}
    for name, p in perms.items():                    # only update.yml writes, and only contents
        assert OA.write_scopes(p) <= OA.WRITE_SCOPES_ALLOWED.get(name, set()), (name, p)
    owner = [{"login": "owner", "role_name": "admin", "permissions": {"admin": True}}]
    good_env = {"name": "pds-manual-release", "can_admins_bypass": False,
                "protection_rules": [{"type": "required_reviewers", "reviewers": [{"reviewer": {"login": "owner"}}]}],
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
    rows = OA.evaluate(owner, {"protected": True}, [{"type": "deletion"}, {"type": "non_fast_forward"}],
                       good_env, perms, "")
    assert all(ok is not False for _, ok, _ in rows), [r for r in rows if r[1] is False]
    today = OA.evaluate(owner, {"protected": False}, [], {"_http_error": 404}, perms, "")   # as found today
    bad = {item for item, ok, _ in today if ok is False}
    assert {"main: 삭제 금지(deletion)", "main: 강제 푸시 금지(non_fast_forward)",
            "환경 pds-manual-release 존재"} <= bad
    pr_rule = OA.evaluate(owner, {"protected": True}, [{"type": "pull_request"}], good_env, perms, "")
    assert ("main: 봇 데이터 커밋을 막는 규칙 없음", False) in {(i, ok) for i, ok, _ in pr_rule}
    assert "| 항목 | 상태 | 내용 |" in OA.report(rows)


@pytest.mark.parametrize("change,expect", [
    (None, None),
    ("missing", "release authorization not available"),
    ({"ref": "refs/heads/feature"}, "must run from main"),
    ({"actor_permission": "write"}, "admin required"),
    ({"environment": {"name": "pds-manual-release", "required_reviewers": []}}, "no required reviewers"),
    ({"environment": None}, "no required reviewers"),
    ({"approvals": []}, "no approval recorded"),
    ({"approvals": [{"user": "o", "state": "rejected", "environments": ["pds-manual-release"]}]}, "no approval"),
    ({"approvals": [{"user": "o", "state": "approved", "environments": ["other"]}]}, "no approval"),
    ({"release_month": "2026-09"}, "authorization is for"),
])
def test_release_authorization_rules(change, expect):
    auth = None if change == "missing" else H.release_auth("2026-10", **(change or {}))
    probs = NEW.release_auth_problems(auth, "2026-10")
    assert (probs == []) if expect is None else any(expect in x for x in probs), probs


def test_self_confirmation_vs_independent_approval():
    own = H.release_auth("2026-10")                                                 # owner asks and approves
    assert NEW.approval_type(own) == "self-confirmation" and NEW.release_auth_problems(own, "2026-10") == []
    env_psr = {"name": NEW.RELEASE_ENVIRONMENT, "required_reviewers": ["owner", "reviewer"],
               "prevent_self_review": True}
    strict_self = H.release_auth("2026-10", environment=env_psr)
    assert any("requester's own" in x for x in NEW.release_auth_problems(strict_self, "2026-10"))
    other = H.release_auth("2026-10", environment=env_psr, approvals=[
        {"user": "reviewer", "state": "approved", "comment": "ok", "environments": [NEW.RELEASE_ENVIRONMENT]}])
    assert NEW.approval_type(other) == "independent" and NEW.release_auth_problems(other, "2026-10") == []
    assert NEW.approval_type(H.release_auth("2026-10", approvals=[])) is None


def _alert_server(responses):
    """Local stand-in for api.telegram.org: replies with the given (code, body) in turn."""
    import http.server
    import threading
    seen, queue = [], list(responses)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
            code, body = queue.pop(0) if queue else (200, {"ok": True})
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _status_with_lock(tmp_path):
    data = tmp_path / "docs" / "data"
    data.mkdir(parents=True)
    (data / "portfolio_latest.json").write_text(json.dumps({"official": {
        "signal_month": "2026-11", "holding_month": "2026-12", "state": "NORMAL", "risk_budget": 1.0,
        "us": [{"ticker": "SPY", "adaptive": 0.3}]}}), encoding="utf-8")
    st = data / "run_status.json"
    st.write_text(json.dumps({"ok": True, "action": {"code": "FOLLOW_OFFICIAL", "month": "2026-11"},
                              "notify": [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}],
                              "log_tail": ["a", "b", "boom"]}), encoding="utf-8")
    return st


@pytest.mark.parametrize("case", ["sent", "telegram_says_no", "http_500_twice", "unreachable", "no_secret"])
def test_send_alert_checks_delivery_and_records_failures(tmp_path, monkeypatch, case):
    import send_alert as SA
    st = _status_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALERT_RETRY_SECONDS", "0")
    monkeypatch.setenv("TG_TOKEN", "t")
    monkeypatch.setenv("TG_CHAT", "c")
    srv = None
    if case == "unreachable":
        monkeypatch.setenv("TELEGRAM_API", "http://127.0.0.1:9")                     # nothing listens there
    elif case == "no_secret":
        monkeypatch.delenv("TG_TOKEN")
    else:
        replies = {"sent": [(200, {"ok": True})],
                   "telegram_says_no": [(200, {"ok": False, "description": "chat not found"})] * 2,
                   "http_500_twice": [(500, {"ok": False, "description": "oops"})] * 2}[case]
        srv, seen = _alert_server(replies)
        monkeypatch.setenv("TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    try:
        code = SA.main(["state", "--status", str(st), "--pages", "https://p/"])
    finally:
        if srv:
            srv.shutdown()
    rec = json.loads(st.read_text(encoding="utf-8"))
    if case == "sent":
        assert code == 0 and rec["delivery"]["status"] == "sent" and "notify_pending" not in rec
        assert "%E2%9C%85" in seen[0] and len(seen) == 1                                 # ✅ line, one call
    elif case == "no_secret":
        assert code == 0 and rec["delivery"]["status"] == "skipped_no_telegram_secret"
    else:
        assert code == 3 and rec["delivery"]["status"] == "failed" and rec["delivery"]["error"]
        assert rec["notify_pending"] == rec["notify"]                                    # resent next run
        if case != "unreachable":
            assert len(seen) == 2                                                        # one retry
    assert {"telegram_says_no": "chat not found", "http_500_twice": "HTTP 500"}.get(case, "") in \
        (rec["delivery"].get("error") or "")


def test_commit_failure_alert_says_nothing_was_saved(tmp_path):
    import send_alert as SA
    st = json.loads(_status_with_lock(tmp_path).read_text(encoding="utf-8"))
    t = SA.failure_text(st, "commit", "https://run/1")
    assert "커밋 실패" in t and "저장되지 않았습니다" in t and "FOLLOW_OFFICIAL 2026-11" in t and "boom" in t
    assert "https://run/1" in t and "pds-lite-run-" in t
    assert "실행 실패" in SA.failure_text(st, "pipeline", "u")


def test_undelivered_alerts_are_resent_and_survive_failed_runs():
    now = datetime(2026, 12, 3, 22, 45, tzinfo=timezone.utc)
    lost = [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}]
    prev = {"recent": [{"ok": True}], "notify_pending": lost}
    failed = RUNSTATUS.build(prev, False, None, ["err"], "schedule", "u", now)
    assert failed["notify"] == [] and failed["notify_pending"][0]["event"] == "LOCKED"   # kept
    res = {"generated_at_utc": now.isoformat(), "action": {"code": "FOLLOW_OFFICIAL"}, "notify": [], "new_locks": 0}
    ok = RUNSTATUS.build(failed, True, res, [], "schedule", "u", now)
    assert ok["notify"][0]["resend"] is True and ok["notify_pending"] == []
    assert NOTIFY.message(dict(ok, official={})).startswith("[PDS-Lite]\n(재전송: 이전 실행에서 전달 실패) ✅ 2026-11")


def _git_repo_with_remote(tmp_path):
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work.mkdir()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"), g("config", "user.name", "t"), g("config", "commit.gpgsign", "false")
    d = work / "docs" / "data"
    d.mkdir(parents=True)
    for f in ("official_history.json", "run_status.json"):
        (d / f).write_text("old\n")
    (work / "run_pipeline.py").write_text("code\n")
    g("add", "."), g("commit", "-q", "-m", "init"), g("remote", "add", "origin", str(remote)), g("push", "-q", "-u", "origin", "main")
    env = dict(os.environ, RUN_OUTCOME="success", RUN_MODE="engine", ADAPTIVE_MODE="reference_only", HALT="false",
               GITHUB_OUTPUT=str(tmp_path / "gh_out"), GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false")
    return remote, work, d, g, env


def _run_commit_step(work, env):
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _commit_step_script()],
                          cwd=work, env=env, capture_output=True, text=True)


def test_commit_step_push_rejected_fails_and_keeps_the_record(tmp_path):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash unavailable")
    remote, work, d, g, env = _git_repo_with_remote(tmp_path)
    other = tmp_path / "other"                                                     # someone else pushed first
    subprocess.run(["git", "clone", "-q", "-b", "main", str(remote), str(other)], check=True)
    (other / "README").write_text("x")
    for a in (["config", "user.email", "o@o"], ["config", "user.name", "o"], ["add", "."], ["commit", "-q", "-m", "x"],
              ["push", "-q"]):
        subprocess.run(["git", "-c", "commit.gpgsign=false", *a], cwd=other, check=True, capture_output=True)
    (d / "official_history.json").write_text("new\n")
    (d / "run_status.json").write_text('{"ok": true}\n')
    r = _run_commit_step(work, env)
    assert r.returncode != 0                                                        # -> failure alert + red run
    assert (d / "run_status.json").read_text() == '{"ok": true}\n'                   # still there for the artifact
    remote_files = subprocess.run(["git", "--git-dir", str(remote), "show", "--name-only", "--format=", "main"],
                                  capture_output=True, text=True).stdout.split()
    assert remote_files == ["README"]                                               # nothing of ours landed


def test_commit_step_refuses_files_outside_docs_data(tmp_path):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash unavailable")
    remote, work, d, g, env = _git_repo_with_remote(tmp_path)
    (work / "run_pipeline.py").write_text("tampered\n")
    g("add", "run_pipeline.py")                                                     # staged by something else
    (d / "official_history.json").write_text("new\n")
    r = _run_commit_step(work, env)
    assert r.returncode == 1 and "outside docs/data" in r.stdout
    head = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                          capture_output=True, text=True).stdout.split("\n")
    assert head[0] == "init"                                                        # nothing pushed


def test_alert_delivery_failure_end_to_end(tmp_path, monkeypatch):
    """Workflow order: commit -> state alert (Telegram down) -> 'Record alert delivery failure'.
    The failure lands in the repository's run_status.json, the run ends red, and the next run
    resends the lost alert."""
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash unavailable")
    import send_alert as SA
    remote, work, d, g, env = _git_repo_with_remote(tmp_path)
    (d / "portfolio_latest.json").write_text(json.dumps({"official": {
        "signal_month": "2026-11", "holding_month": "2026-12", "state": "NORMAL", "risk_budget": 1.0, "us": []}}))
    (d / "run_status.json").write_text(json.dumps({"ok": True, "recent": [{"ok": True}],
                                                   "notify": [{"signal_month": "2026-11", "event": "LOCKED",
                                                               "detail": "x"}]}))
    assert _run_commit_step(work, env).returncode == 0                                  # data committed
    srv, seen = _alert_server([(502, {"ok": False, "description": "bad gateway"})] * 2)
    monkeypatch.chdir(work)
    for k, v in {"ALERT_RETRY_SECONDS": "0", "TG_TOKEN": "t", "TG_CHAT": "c",
                 "TELEGRAM_API": f"http://127.0.0.1:{srv.server_port}"}.items():
        monkeypatch.setenv(k, v)
    try:
        assert SA.main(["state"]) == 3
    finally:
        srv.shutdown()
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    step = next(x for x in wf["jobs"]["run"]["steps"] if x.get("name") == "Record alert delivery failure")
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], cwd=work,
                       env=dict(env, COMMIT_OUTCOME="success"), capture_output=True, text=True)
    assert r.returncode == 1 and "not delivered" in r.stdout                           # run marked failed
    pushed = json.loads(subprocess.run(["git", "--git-dir", str(remote), "show", "main:docs/data/run_status.json"],
                                       capture_output=True, text=True, check=True).stdout)
    assert pushed["delivery"]["status"] == "failed" and "HTTP 502" in pushed["delivery"]["error"]
    assert pushed["notify_pending"][0]["event"] == "LOCKED"
    nxt = RUNSTATUS.build(pushed, True, {"generated_at_utc": "x", "notify": [], "action": None}, [], "schedule", "u",
                          datetime(2026, 12, 4, tzinfo=timezone.utc))
    assert [(e["event"], e.get("resend")) for e in nxt["notify"]] == [("LOCKED", True)]


def test_preview_notes_are_scoped_to_the_preview(dl_market, tmp_path):
    """'2026-11: F2R_PREVIEW' belongs to the in-month preview only: the official block and the lock
    are built from the published F2R, and the dashboard gets the note scoped to the Preview tab."""
    dl = dl_market
    ref = _core_wb(dl, dl["oct"], ["2026-10"])
    p, h, _ = _run_at(dl, tmp_path, _utc(11, 13, 3, 0), dl["oct"], ref, px_until="2026-11-12")
    assert h[-1]["signal_month"] == "2026-10" and h[-1]["sources"]["f2r"] == "F2R_PUBLISHED"
    assert p["official"]["signal_month"] == "2026-10" and p["preview"]["signal_month"] == "2026-11"
    assert p["preview"]["sources"]["f2r"] == "F2R_PREVIEW"
    assert "2026-11: F2R_PREVIEW" in p["flags"]                                      # legacy list unchanged
    assert p["flags_scoped"] == {"official": [], "preview": ["F2R_PREVIEW"], "run": []}
    p2, _, _ = _run_at(dl, tmp_path / "noref", _utc(11, 13, 3, 0), dl["oct"], None, px_until="2026-11-12")
    assert "REFERENCE_UNAVAILABLE" in p2["flags_scoped"]["run"]                       # run-level: both tabs
    assert p2["flags_scoped"]["preview"] == ["F2R_PREVIEW"]


def test_dashboard_flag_view_per_tab():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    html = (H.ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    js = html.split("// <flag-view>")[1].split("// </flag-view>")[0]
    cases = {
        "preview_note": {"flags": ["2026-11: F2R_PREVIEW"],
                         "flags_scoped": {"official": [], "preview": ["F2R_PREVIEW"], "run": []}},
        "run_warning": {"flags": ["REFERENCE_UNAVAILABLE", "2026-11: F2R_PREVIEW"],
                        "flags_scoped": {"official": [], "preview": ["F2R_PREVIEW"], "run": ["REFERENCE_UNAVAILABLE"]}},
        "official_note": {"flags": ["2026-10: UNRATE_CARRIED"],
                          "flags_scoped": {"official": ["UNRATE_CARRIED"], "preview": [], "run": []}},
        "old_data": {"flags": ["2026-11: F2R_PREVIEW", "REFERENCE_MODE"]},
    }
    script = js + "\nconst C = " + json.dumps(cases, ensure_ascii=False) + ";\n" + \
        "const out = {}; for (const k in C) out[k] = {official: flagView(C[k], 'official'), preview: flagView(C[k], 'preview')};\n" + \
        "console.log(JSON.stringify(out));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    out = json.loads(r.stdout)
    note = "F2R 공식 Top-4 미공개 → 원작자 Preview 순위로 추정"
    assert out["preview_note"]["official"] == {"warn": [], "info": []}                  # no warning on Official
    assert out["preview_note"]["preview"] == {"warn": [], "info": [note]}               # info on Preview
    assert out["run_warning"]["official"]["warn"] == ["REFERENCE_UNAVAILABLE"]
    assert out["run_warning"]["preview"] == {"warn": ["REFERENCE_UNAVAILABLE"], "info": [note]}
    assert out["official_note"]["official"]["warn"] == ["실업률 최신 월 미발표 → 직전 값 사용"]
    assert out["old_data"]["official"]["warn"] == ["2026-11: F2R_PREVIEW"]              # old JSON: as before


def test_ops_audit_commit_provenance():
    sys.path.insert(0, str(H.ROOT / "tools"))
    import ops_audit as OA
    bot = {"login": OA.BOT}
    commits = [{"sha": "a" * 40, "author": bot, "commit": {"message": "data: x"}},
               {"sha": "b" * 40, "author": bot, "commit": {"message": "data: y"}},
               {"sha": "c" * 40, "author": {"login": "owner"}, "commit": {"message": "Merge pull request #1"}},
               {"sha": "d" * 40, "author": {"login": "owner"}, "commit": {"message": "hotfix on main"}}]
    files = {"a" * 40: ["docs/data/run_status.json"], "b" * 40: ["docs/data/x.json", "run_pipeline.py"]}
    pulls = {"c" * 40: [{"merged_at": "2026-12-01T00:00:00Z"}], "d" * 40: []}
    rows = {i: (ok, detail) for i, ok, detail in OA.main_commit_provenance(commits, files, pulls)}
    bot_row = rows[f"main 최근 {OA.PROVENANCE_DAYS}일: 봇 커밋은 docs/data만 변경"]
    pr_row = rows[f"main 최근 {OA.PROVENANCE_DAYS}일: 봇 외 변경은 모두 PR 병합 경유"]
    assert bot_row[0] is False and "bbbbbbb" in bot_row[1] and "run_pipeline.py" in bot_row[1]
    assert pr_row[0] is False and "ddddddd owner: hotfix on main" in pr_row[1] and "ccccccc" not in pr_row[1]
    clean = OA.main_commit_provenance(commits[:1] + commits[2:3], files, pulls)
    assert all(ok for _, ok, _ in clean)


def test_release_auth_reads_environment_protection():
    sys.path.insert(0, str(H.ROOT / "tools"))
    import release_auth as RA
    env = {"name": "pds-manual-release", "can_admins_bypass": False,
           "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False,
                                 "reviewers": [{"type": "User", "reviewer": {"login": "owner"}}]},
                                {"type": "branch_policy"}],
           "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
    assert RA.environment_summary(env) == {"name": "pds-manual-release", "required_reviewers": ["owner"],
                                           "prevent_self_review": False, "can_admins_bypass": False,
                                           "branch_policy": "custom"}
    assert RA.environment_summary(None) is None


def test_notifications_only_on_state_changes(dl_market, tmp_path):
    dl = dl_market
    seen = []

    def step(now, f2r, ref):
        p, _, _ = _run_at(dl, tmp_path, now, f2r, ref)
        seen.append([e["event"] for e in p["notify"]])
        return p
    step(datetime(2026, 11, 1, tzinfo=timezone.utc), dl["sep"], wb(official=None))  # waiting starts
    step(_utc(11, 2), dl["sep"], wb(official=None))
    step(_utc(11, 3), dl["sep"], wb(official=None))
    bad = wb(official=None, core_rows={("OFFICIAL", "2026-10"): {"BIL": 1.0}})
    step(_utc(11, 4), dl["oct"], bad)                                                # F2R out, Core differs
    step(_utc(11, 5), dl["oct"], bad)
    p = step(_utc(11, 6), dl["oct"], _core_wb(dl, dl["oct"], ["2026-10"]))           # fixed -> locked
    step(_utc(11, 7), dl["oct"], _core_wb(dl, dl["oct"], ["2026-10"]))
    assert seen == [["PENDING"], [], [], ["CORE_MISMATCH"], [], ["LOCKED"], []]
    msg = NOTIFY.message(p, "https://x/")
    assert msg.startswith("[PDS-Lite]\n✅ 2026-10 공식 확정 → 2026-11 보유") and msg.endswith("https://x/")
    assert NOTIFY.message({"notify": []}) == ""


def test_blocked_month_alerts_once(dl_market, tmp_path):
    dl = dl_market
    _start_with_october_pending(dl, tmp_path)
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))
    assert [e["event"] for e in p["notify"]] == ["BLOCKED"]
    msg = NOTIFY.message(p)
    assert "⛔ 2026-10 자동 리밸런싱 중지(10일 마감)" in msg and "현재 보유 자산을 그대로 유지" in msg
    p, _, _ = _run_at(dl, tmp_path, _utc(11, 13), dl["sep"], wb(official=None))     # Friday re-run
    assert p["action"]["code"] == "HALTED" and p["notify"] == []


def test_run_status_keeps_a_rolling_log_and_ignores_stale_payloads(tmp_path):
    now = datetime(2026, 12, 3, 22, 45, tzinfo=timezone.utc)
    payload = {"generated_at_utc": now.isoformat(), "action": {"code": "WAIT", "month": "2026-11", "reason": "x"},
               "notify": [], "new_locks": 0}
    prev = {"recent": [{"i": k} for k in range(RUNSTATUS.RECENT_KEEP)]}
    out = RUNSTATUS.build(prev, True, payload, ["[lock] a", "b"], "schedule", "u", now)
    assert len(out["recent"]) == RUNSTATUS.RECENT_KEEP and out["recent"][-1]["action"] == "WAIT"
    assert out["recent"][0] == {"i": 1} and out["log_tail"] == ["b"] and out["notify"] == []
    fail = RUNSTATUS.build(out, False, payload, [], "schedule", "u", now)        # failed run: no payload used
    assert fail["action"] is None and fail["recent"][-1]["ok"] is False
    f = tmp_path / "p.json"
    f.write_text(json.dumps(payload))
    assert RUNSTATUS.this_runs_result(f, now) is not None
    assert RUNSTATUS.this_runs_result(f, datetime(2026, 12, 3, 23, 0, tzinfo=timezone.utc)) is None


def test_rollback_dry_run_flags_a_gap_fill(dl_market, base, tmp_path):
    """The 1.0 code would back-fill a BLOCKED month: the dry run must say 'manual review'."""
    dl = dl_market
    _start_with_october_pending(dl, tmp_path)
    _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))              # Oct BLOCKED
    ref = _core_wb(dl, dl["nov"], ["2026-10", "2026-11"])
    _run_at(dl, tmp_path, _utc(12, 2), dl["nov"], ref)                               # Nov locked, Oct gap
    data = tmp_path / "docs" / "data"
    snap = {f.name: f.read_bytes() for f in data.glob("*.json")}
    H.feed(base, dl["daily"].loc[:"2026-12-02"], dl["un"], dl["nov"], ref)
    base.ADAPTIVE_MODE = "reference_then_proxy"
    res = ROLLBACK.check(base, data, _utc(12, 2, 23, 0))
    assert res["gap_fills"] == ["2026-10"] and res["needs_review"] is True
    assert res["changed"] == [] and res["removed"] == []
    assert "수동 검토 필요" in ROLLBACK.report(res, "main", _utc(12, 2, 23, 0))
    assert {f.name: f.read_bytes() for f in data.glob("*.json")} == snap             # dry run wrote nothing


def test_rollback_dry_run_clean_case(market, base, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    ref = wbc(daily, un, f2r)
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, ref)
    H.feed(base, daily, un, f2r, ref)
    base.ADAPTIVE_MODE = "reference_then_proxy"
    res = ROLLBACK.check(base, tmp_path / "docs" / "data", datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert res["needs_review"] is False and res["appended"] == [] and res["gap_fills"] == []
    assert ROLLBACK.compare([{"signal_month": "2026-09", "x": 1}], [{"signal_month": "2026-09", "x": 2}])["changed"] \
        == ["2026-09"]


def test_month_lock_requires_the_authors_core(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path / "a", daily, un, f2r, wb(official=None))
    assert h[-1]["signal_month"] == "2026-09" and p["action"]["code"] == "WAIT"
    assert any("author's Core for this month not published" in f for f in p["flags"])
    NEW.OFFICIAL_REQUIRES_AUTHOR_CORE = False                                      # documented override only
    try:
        _, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path / "b", daily, un, f2r, wb(official=None))
    finally:
        NEW.OFFICIAL_REQUIRES_AUTHOR_CORE = True
    assert h[-1]["signal_month"] == "2026-10"


def test_rollback_base_code_after_a_blocked_month(dl_market, base, tmp_path):
    """Files written by the new code after a BLOCKED month (gap + events file) stay readable by the
    old code; it keeps every new lock byte-for-byte (it would back-fill the gap month itself)."""
    dl = dl_market
    _start_with_october_pending(dl, tmp_path)
    _run_at(dl, tmp_path, _utc(11, 10), dl["sep"], wb(official=None))
    _, h_new, _ = _run_at(dl, tmp_path, _utc(12, 2), dl["nov"], _core_wb(dl, dl["nov"], ["2026-10", "2026-11"]))
    daily = dl["daily"].loc[:"2026-12-02"]
    p, h_old, _ = H.run(base, "reference_then_proxy", _utc(12, 2, 23, 0), tmp_path, daily, dl["un"], dl["nov"],
                        _core_wb(dl, dl["nov"], ["2026-10", "2026-11"]))
    old = {d["signal_month"]: d for d in h_old}
    for d in h_new:
        assert canon(old[d["signal_month"]]) == canon(d), d["signal_month"]
    assert p["official"]["signal_month"] == "2026-11"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Pages / Actions compatibility
# ─────────────────────────────────────────────────────────────────────────────
def _shape(payload):
    blk = ("signal_month", "holding_month", "state", "risk_budget", "sources", "turnover_one_way",
           "est_cost", "us", "asset_class", "irp")
    top = ("generated_at_kst", "market_asof", "mode", "flags", "official", "preview", "history",
           "validation", "disclaimer", "config")
    for k in top:
        assert k in payload, k
    for b in [payload["official"]] + ([payload["preview"]] if payload["preview"] else []):
        for k in blk:
            assert k in b, k
        for side in ("core", "adaptive"):
            assert set(b["asset_class"][side]) == set(NEW.ASSET_CLASS)
            irp = b["irp"][side]
            assert {"risk_after_cap", "moved_to_safe", "rows", "cap_target"} <= set(irp)
        for r in b["us"]:
            assert {"ticker", "name", "core", "adaptive", "delta"} <= set(r)
    for r in payload["history"]:
        assert {"holding_month", "state", "risk_budget", "core", "adaptive"} <= set(r)
    assert isinstance(payload["flags"], list)


def test_payload_keeps_every_field_the_dashboard_reads(market, tmp_path):
    committed = json.loads((H.ROOT / "docs" / "data" / "portfolio_latest.json").read_text(encoding="utf-8"))
    _shape(committed)
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    p, _, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wbc(daily, un, f2r))
    _shape(p)
    assert {"code", "month", "in_force", "message"} <= set(p["action"])
    html = (H.ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert "data/portfolio_latest.json" in html                          # page reads the same file


def _commit_step_script():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Commit if changed")
    return step["run"]


@pytest.mark.parametrize("outcome,run_mode,adaptive,halt,expect", [
    ("failure", "engine", "reference_only", "false", {"docs/data/run_status.json"}),
    ("success", "engine", "proxy", "false", {"docs/data/run_status.json"}),
    ("success", "reference", "reference_only", "false", {"docs/data/run_status.json"}),
    ("success", "engine", "reference_only", "true", {"docs/data/run_status.json"}),      # operator halt
    ("success", "engine", "reference_only", "false", {"docs/data/run_status.json", "docs/data/official_history.json",
                                                      "docs/data/reference_adaptive.json"}),
])
def test_workflow_commit_step_is_fail_safe(tmp_path, outcome, run_mode, adaptive, halt, expect):
    if shutil.which("git") is None or shutil.which("bash") is None:
        pytest.skip("git/bash unavailable")
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    work.mkdir()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"), g("config", "user.name", "t"), g("config", "commit.gpgsign", "false")
    d = work / "docs" / "data"
    d.mkdir(parents=True)
    for f in ("official_history.json", "run_status.json", "portfolio_latest.json"):
        (d / f).write_text("old\n")
    g("add", "."), g("commit", "-q", "-m", "init"), g("remote", "add", "origin", str(remote)), g("push", "-q", "-u", "origin", "main")
    for f in ("official_history.json", "run_status.json"):
        (d / f).write_text("new\n")
    (d / "reference_adaptive.json").write_text("new\n")
    env = dict(os.environ, RUN_OUTCOME=outcome, RUN_MODE=run_mode, ADAPTIVE_MODE=adaptive, HALT=halt,
               GITHUB_OUTPUT=str(tmp_path / "gh_out"), GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false")
    # same shell flags GitHub Actions uses for run steps: bash --noprofile --norc -eo pipefail
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _commit_step_script()],
                   cwd=work, env=env, check=True, capture_output=True)
    changed = set(g("show", "--name-only", "--format=", "HEAD").stdout.split())
    assert changed == expect
    if outcome == "failure" or halt == "true":
        assert (d / "official_history.json").read_text() == "old\n"           # restored on disk too


def test_workflow_defaults_and_schedule():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    on = wf[True]
    assert on["push"]["branches"] == ["main"]                               # branches never run it
    assert on["workflow_dispatch"]["inputs"]["adaptive_mode"]["default"] == "reference_only"
    step = next(s for s in wf["jobs"]["run"]["steps"] if s.get("id") == "run")
    assert "'reference_only'" in step["env"]["ADAPTIVE_MODE"]
    assert NEW.ADAPTIVE_MODES[0] == "reference_only" and os.environ.get("ADAPTIVE_MODE") in (None, "", "reference_only")
    assert step["env"]["OFFICIAL_REQUIRES_F2R_PUBLISHED"] == "1" and step["env"]["OFFICIAL_REQUIRES_AUTHOR_CORE"] == "1"
    assert "release_month" in step["env"]["MANUAL_RELEASE_MONTH"]
    assert "release_reason" in step["env"]["MANUAL_RELEASE_REASON"] and "vars.PDS_HALT" in step["env"]["PDS_HALT"]
    assert on["workflow_dispatch"]["inputs"]["release_month"]["default"] == ""
    assert on["workflow_dispatch"]["inputs"]["release_reason"]["default"] == ""
    # a daily run on days 1-10 at or after the final-check hour
    daily = [c["cron"].split() for c in on["schedule"] if c["cron"].split()[2] == "1-10"]
    assert daily and int(daily[0][1]) >= NEW.LOCK_FINAL_CHECK_UTC_HOUR and daily[0][4] == "*"
    steps = {s.get("name", ""): s for s in wf["jobs"]["run"]["steps"]}
    tg = [n for n in steps if n.startswith("Telegram")]
    assert tg == ["Telegram alert (state changes, after a successful commit)",
                  "Telegram alert on failure (pipeline or commit/push)"]
    assert "steps.status.outputs.notify == 'true'" in steps[tg[0]]["if"]
    assert "steps.commit.outcome == 'success'" in steps[tg[0]]["if"]                 # never announce unsaved
    assert "send_alert.py state" in steps[tg[0]]["run"] and "send_alert.py failure" in steps[tg[1]]["run"]
    assert "steps.commit.outcome == 'failure'" in steps[tg[1]]["if"]
    rec = steps["Record alert delivery failure"]
    assert "steps.tg.outcome == 'failure'" in rec["if"] and rec["run"].rstrip().endswith("exit 1")
    names = [s.get("name") or s.get("uses") for s in wf["jobs"]["run"]["steps"]]
    order = [names.index(n) for n in ("Record run status (readable without Actions UI; keeps the last 60 runs)",
                                      "Commit if changed", tg[0], tg[1], "Record alert delivery failure",
                                      "actions/upload-artifact@v4")]
    assert order == sorted(order) and names[-1] == "actions/upload-artifact@v4"
    art = wf["jobs"]["run"]["steps"][-1]
    assert art["if"] == "always()" and art["with"]["retention-days"] == 90
    assert {"docs/data/", "run_result.json", "run.log"} <= set(art["with"]["path"].split())
    assert "record_run_status.py" in steps["Record run status (readable without Actions UI; keeps the last 60 runs)"]["run"]
    # job-level concurrency: runs are serialized (halt barrier) and a pending approval blocks nothing
    assert "concurrency" not in wf and wf["jobs"]["run"]["concurrency"] == {"group": "update-portfolio",
                                                                          "cancel-in-progress": False}
    assert wf["permissions"] == {} and wf["jobs"]["run"]["permissions"] == {"contents": "write", "actions": "read"}
    gate = wf["jobs"]["release-gate"]
    assert gate["environment"] == NEW.RELEASE_ENVIRONMENT and gate["permissions"] == {}
    assert "release_month != ''" in gate["if"] and wf["jobs"]["run"]["needs"] == "release-gate"
    assert "!cancelled()" in wf["jobs"]["run"]["if"] and "skipped" in wf["jobs"]["run"]["if"]
    auth = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name", "").startswith("Release authorization"))
    assert "release_auth.py" in auth["run"] and auth["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert steps["Commit if changed"]["env"]["HALT"] == "${{ steps.status.outputs.halt }}"
    for job in wf["jobs"].values():                      # no workflow input is pasted into a shell script
        for st in job["steps"]:
            assert "github.event.inputs" not in st.get("run", ""), st.get("name")
    tw = yaml.safe_load((H.ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    live_cmd = next(x["run"] for x in tw["jobs"]["live"]["steps"] if "python -m pytest" in x.get("run", ""))
    assert "-k test_live_" in live_cmd             # the live job runs exactly the real-data tests
    assert sorted(n for n in dir(sys.modules[__name__]) if n.startswith("test_live_")) == [
        "test_live_116_months_before_after", "test_live_new_code_on_the_committed_lock"]
    rb = yaml.safe_load((H.ROOT / ".github" / "workflows" / "rollback-check.yml").read_text(encoding="utf-8"))
    assert rb["permissions"] == {"contents": "read"} and list(rb[True]) == ["workflow_dispatch"]


# ─────────────────────────────────────────────────────────────────────────────
# live: real Yahoo / BLS / F2R / workbook, base vs new on identical inputs (network)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_into_tmp(tmp_path: Path):
    """The real fetchers write the UNRATE cache next to the outputs: keep it out of docs/data
    (a copy of the committed cache stays available as the last fallback)."""
    out = H.point(NEW, tmp_path / "fetch")
    cache = H.ROOT / "docs" / "data" / "unrate_cache.csv"
    if cache.exists():
        shutil.copy2(cache, out / "unrate_cache.csv")


@pytest.mark.skipif(os.environ.get("PDS_LIVE_TEST") != "1", reason="set PDS_LIVE_TEST=1 (network)")
def test_live_116_months_before_after(base, tmp_path):
    assert NEW.fetch_prices.__module__ == "run_pipeline"   # the real fetchers, not a test stub
    _fetch_into_tmp(tmp_path)
    daily = NEW.fetch_prices()                       # includes the new integrity check on real data
    assert daily.index[-1] >= pd.Timestamp.now().normalize() - pd.Timedelta(days=7)
    un, src = NEW.fetch_unrate()
    f2r = NEW.fetch_f2r_state()
    ref = NEW.fetch_reference_workbook()
    assert f2r is not None and ref is not None
    now = datetime.now(timezone.utc)
    _, h_old, _ = H.run(base, "reference_then_proxy", now, tmp_path / "old", daily, un, f2r, ref)
    p_new, h_new, _ = H.run(NEW, "reference_only", now, tmp_path / "new", daily, un, f2r, ref)
    old = {d["signal_month"]: d for d in h_old}
    new = {d["signal_month"]: d for d in h_new}
    span = [m for m in sorted(old) if "2017-03" <= m <= "2026-10"]
    assert len(span) == 116 and all(m in new for m in span)
    for m in span:
        assert old[m]["core"] == new[m]["core"], m
    v = p_new["validation"]
    assert v["months_checked"] >= 116 and v["exact_matches"] == v["months_checked"], v.get("mismatches")
    committed = {d["signal_month"]: d for d in json.loads((H.ROOT / "docs" / "data" / "official_history.json").read_text())}
    for m in span:
        assert committed[m]["core"] == new[m]["core"], m
    report = os.environ.get("PDS_TEST_REPORT_DIR")
    if report:
        _write_weight_report(Path(report) / "live_before_after.csv", [old[m] for m in span], [new[m] for m in span])


@pytest.mark.skipif(os.environ.get("PDS_LIVE_TEST") != "1", reason="set PDS_LIVE_TEST=1 (network)")
def test_live_new_code_on_the_committed_lock(tmp_path):
    """Today's real run of the new code on top of the committed 116-month lock (what the first
    scheduled run after a merge would do), written to a temp dir."""
    real = json.loads((H.ROOT / "docs" / "data" / "official_history.json").read_text(encoding="utf-8"))
    assert NEW.fetch_prices.__module__ == "run_pipeline"
    _fetch_into_tmp(tmp_path)
    daily = NEW.fetch_prices()
    assert daily.index[-1] >= pd.Timestamp.now().normalize() - pd.Timedelta(days=7)
    un, _ = NEW.fetch_unrate()
    f2r = NEW.fetch_f2r_state()
    ref = NEW.fetch_reference_workbook()
    now = datetime.now(timezone.utc)
    p, h, files = H.run(NEW, "reference_only", now, tmp_path, daily, un, f2r, ref, lock_init=real)
    for a, b in zip(real, h):
        assert canon(a) == canon(b), a["signal_month"]                    # committed lock untouched
    v = p["validation"]
    assert v["exact_matches"] == v["months_checked"] >= 116, v.get("mismatches")
    assert p["action"]["code"] in ("FOLLOW_OFFICIAL", "WAIT", "HALTED")
    # an official target is locked only with the published F2R, so no F2R estimate note can be scoped to it
    assert not [c for c in p["flags_scoped"]["official"] if c.startswith("F2R")], p["flags_scoped"]
    report = os.environ.get("PDS_TEST_REPORT_DIR")
    if report:
        out = Path(report)
        out.mkdir(parents=True, exist_ok=True)
        (out / "live_new_on_committed_lock.json").write_text(json.dumps(
            {"now_utc": now.isoformat(timespec="seconds"), "market_asof": p["market_asof"],
             "new_locks": len(h) - len(real), "action": p["action"], "flags": p["flags"],
             "flags_scoped": p["flags_scoped"],
             "official": {k: p["official"][k] for k in ("signal_month", "holding_month", "state", "risk_budget")},
             "preview": None if not p["preview"] else
             {k: p["preview"].get(k) for k in ("signal_month", "state", "risk_budget", "status")},
             "validation": {k: v.get(k) for k in ("months_checked", "exact_matches", "max_abs_diff")},
             "reference": {k: p["reference"].get(k) for k in ("official", "applied_to_official", "ledger_rows")},
             "events": json.loads(NEW.EVENTS_JSON.read_text()) if NEW.EVENTS_JSON.exists() else []},
            ensure_ascii=False, indent=1), encoding="utf-8")


def _write_weight_report(path: Path, before: list, after: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for a, b in zip(before, after):
        ca, cb = NEW.series_from(a["core"]), NEW.series_from(b["core"])
        aa, ab = NEW.series_from(a["adaptive"]), NEW.series_from(b["adaptive"])
        rows.append({"signal_month": a["signal_month"],
                     "core_max_abs_diff": float((ca - cb).abs().max()),
                     "before_state": a["state"], "before_rb": a["risk_budget"], "before_source": a["sources"]["adaptive"],
                     "after_state": b["state"], "after_rb": b["risk_budget"], "after_source": b["sources"]["adaptive"],
                     "adaptive_max_abs_diff": float((aa - ab).abs().max()),
                     "adaptive_one_way_diff": float((aa - ab).abs().sum() / 2),
                     **{f"before_{t}": aa[t] for t in NEW.UNIVERSE}, **{f"after_{t}": ab[t] for t in NEW.UNIVERSE}})
    pd.DataFrame(rows).to_csv(path, index=False, float_format="%.10g")
