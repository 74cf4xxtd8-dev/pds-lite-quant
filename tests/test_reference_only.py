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


def canon(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. before/after: 116 months of Core and monthly target weights
# ─────────────────────────────────────────────────────────────────────────────
def test_116_months_core_identical_before_after(market, base, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    _, h_old, _ = H.run(base, "reference_then_proxy", NOV1, tmp_path / "old", daily, un, f2r, wb())
    _, h_new, _ = H.run(NEW, "reference_only", NOV1, tmp_path / "new", daily, un, f2r, wb())
    assert [d["signal_month"] for d in h_old] == [d["signal_month"] for d in h_new]
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
    _, h_old, _ = H.run(base, "reference_then_proxy", NOV1, tmp_path / "old", daily, un, f2r, wb())
    p, h_new, files = H.run(NEW, "reference_then_proxy", NOV1, tmp_path / "new", daily, un, f2r, wb(), write=False)
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
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb(official=None))
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
    _, h, _ = H.run(NEW, "core_only", NOV1, tmp_path, daily, un, f2r, wb(("DEFENSIVE", 0.7)))
    assert all(d["risk_budget"] == 1.0 for d in h)
    assert h[-1]["sources"]["reference_seen"]["state"] == "DEFENSIVE"


def test_invalid_author_row_is_rejected(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    for bad in (("NORMAL", 0.8), ("DEFENSIVE", 0.3), ("DEFENSIVE", 1.0), ("WEIRD", 0.9)):
        p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path / str(bad), daily, un, f2r, wb(bad))
        assert h[-1]["risk_budget"] == 1.0, bad
        assert h[-1]["sources"]["adaptive"] == "CORE_DEFAULT_REFERENCE_REJECTED", bad


# ─────────────────────────────────────────────────────────────────────────────
# 2 + 3. reference ledger separate; later publications never rewrite a lock
# ─────────────────────────────────────────────────────────────────────────────
def test_late_publication_goes_to_ledger_not_lock(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    _, h1, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb(official=None))
    locked = canon(h1)
    later = datetime(2026, 11, 8, 8, 0, tzinfo=timezone.utc)
    p2, h2, _ = H.run(NEW, "reference_only", later, tmp_path, daily, un, f2r, wb(("DEFENSIVE", 0.71)))
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
    p, h, _ = H.run(NEW, "reference_only", DEC1, tmp_path, daily, un, f2r, wb(), lock_init=real)
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
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb())       # a good first run
    snap = _snapshot(out)
    assert set(snap) == {"official_history.json", "reference_adaptive.json", "portfolio_latest.json"}

    def boom():
        raise RuntimeError("price download failed: HTTP 503")
    bad_px = daily.copy()
    bad_px.iloc[-2, 2] = np.nan
    cases = {"api": boom, "nan": lambda: (NEW.check_price_integrity(bad_px), bad_px)[1]}
    for name, fn in cases.items():
        H.feed(NEW, daily, un, f2r, wb())
        NEW.fetch_prices = fn
        NEW.ADAPTIVE_MODE = "reference_only"
        with pytest.raises(RuntimeError):
            NEW.main([])
        assert _snapshot(out) == snap, name


def test_live_lock_waits_for_f2r_publication(market, tmp_path):
    daily, un, months = market
    f2r_late = H.f2r_state(months, official="2026-09", preview="2026-10")    # Oct F2R not official yet
    p, h, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r_late, wb(official=None))
    assert h[-1]["signal_month"] == "2026-09"                                 # Oct NOT locked
    assert p["official"]["signal_month"] == "2026-09"
    assert p["preview"]["signal_month"] == "2026-10" and p["preview"]["sources"]["f2r"] == "F2R_PREVIEW"
    assert any(f.startswith("OFFICIAL_PENDING 2026-10") for f in p["flags"])
    # F2R publishes on Nov 2 -> the next run locks October as a LIVE record
    f2r_ok = H.f2r_state(months, official="2026-10")
    p2, h2, _ = H.run(NEW, "reference_only", datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc), tmp_path,
                      daily, un, f2r_ok, wb(official=None))
    assert h2[-1]["signal_month"] == "2026-10" and h2[-1]["origin"] == "LIVE"
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


def test_diagnostic_and_mirror_modes_never_touch_operational_files(market, tmp_path):
    daily, un, months = market
    f2r = H.f2r_state(months, official="2026-10")
    out = H.point(NEW, tmp_path)
    H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb())
    snap = _snapshot(out)
    for mode in ("proxy", "reference_then_proxy"):
        H.feed(NEW, daily, un, f2r, wb())
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
    p, _, _ = H.run(NEW, "reference_only", NOV1, tmp_path, daily, un, f2r, wb())
    _shape(p)
    html = (H.ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert "data/portfolio_latest.json" in html                          # page reads the same file


def _commit_step_script():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Commit if changed")
    return step["run"]


@pytest.mark.parametrize("outcome,run_mode,adaptive,expect", [
    ("failure", "engine", "reference_only", {"docs/data/run_status.json"}),
    ("success", "engine", "proxy", {"docs/data/run_status.json"}),
    ("success", "reference", "reference_only", {"docs/data/run_status.json"}),
    ("success", "engine", "reference_only", {"docs/data/run_status.json", "docs/data/official_history.json",
                                             "docs/data/reference_adaptive.json"}),
])
def test_workflow_commit_step_is_fail_safe(tmp_path, outcome, run_mode, adaptive, expect):
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
    env = dict(os.environ, RUN_OUTCOME=outcome, RUN_MODE=run_mode, ADAPTIVE_MODE=adaptive,
               GITHUB_OUTPUT=str(tmp_path / "gh_out"), GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false")
    # same shell flags GitHub Actions uses for run steps: bash --noprofile --norc -eo pipefail
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _commit_step_script()],
                   cwd=work, env=env, check=True, capture_output=True)
    changed = set(g("show", "--name-only", "--format=", "HEAD").stdout.split())
    assert changed == expect
    if outcome == "failure":
        assert (d / "official_history.json").read_text() == "old\n"           # restored on disk too


def test_workflow_defaults_and_schedule():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    on = wf[True]
    assert on["push"]["branches"] == ["main"]                               # branches never run it
    assert on["workflow_dispatch"]["inputs"]["adaptive_mode"]["default"] == "reference_only"
    step = next(s for s in wf["jobs"]["run"]["steps"] if s.get("id") == "run")
    assert "'reference_only'" in step["env"]["ADAPTIVE_MODE"]
    assert NEW.ADAPTIVE_MODES[0] == "reference_only" and os.environ.get("ADAPTIVE_MODE") in (None, "", "reference_only")


# ─────────────────────────────────────────────────────────────────────────────
# live: real Yahoo / BLS / F2R / workbook, base vs new on identical inputs (network)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(os.environ.get("PDS_LIVE_TEST") != "1", reason="set PDS_LIVE_TEST=1 (network)")
def test_live_116_months_before_after(base, tmp_path):
    daily = NEW.fetch_prices()                       # includes the new integrity check on real data
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
