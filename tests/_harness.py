"""Shared fixtures for the reference_only / fail-safe tests.

* synthetic_market(): 18-ETF daily closes 2015-01-02 .. end, UNRATE, all deterministic.
* f2r_state(): a dict shaped like f2r_public_state.json.
* author_workbook(): a dict shaped like the PDS workbook (Core / Adaptive sheets).
* load_base(): the pipeline as it is on the base ref (default: main) - the 'before' side.
* run(): run_engine() of either version on identical inputs inside a temp directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_pipeline as NEW  # noqa: E402

U = NEW.UNIVERSE


def synthetic_market(seed: int = 7, end: str = "2026-10-30"):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2015-01-02", end)
    vol = rng.uniform(0.004, 0.015, len(U))
    vol[U.index("BIL")] = 0.0003
    vol[U.index("SHY")] = 0.001
    drift = rng.normal(0.0002, 0.0004, (len(days), len(U)))
    k = len(days) // 3
    drift[k:k + 180] -= 0.002                               # bear phase -> defensive sleeve branches
    px = 100 * np.exp(np.cumsum(drift + rng.standard_normal(drift.shape) * vol, axis=0))
    daily = pd.DataFrame(px, index=days, columns=U)
    months = pd.period_range("2013-01", pd.Timestamp(end).to_period("M"), freq="M")
    un = pd.Series(4 + np.cumsum(rng.normal(0, 0.1, len(months))), index=months)
    return daily, un


def f2r_state(months, official: str | None, preview: str | None = None, seed: int = 3) -> dict:
    """full_history for every month up to `official` (inclusive); optional preview month."""
    rng = np.random.default_rng(seed)
    hist = []
    for p in months:
        if official is None or str(p) > official:
            continue
        pick = list(rng.choice(NEW.F2R_UNIVERSE, size=4, replace=False))
        hist.append({"month": str(p), **{f"rank{k + 1}": a for k, a in enumerate(pick)}})
    st = {"historical_targets": {"full_history": hist}, "live_state": {}}
    if preview:
        pick = list(rng.choice(NEW.F2R_UNIVERSE, size=4, replace=False))
        st["live_state"]["preview"] = {"available": True, "signal_period": preview,
                                       "holdings": [{"asset": a, "target_weight": 0.25} for a in pick]}
    return st


def author_workbook(adaptive_rows: list[tuple], core_rows: dict | None = None) -> dict:
    """adaptive_rows: (status, signal_month, state, rb). core_rows: {(status, sm): weights dict}."""
    a = pd.DataFrame([{"Status": s, "Signal Month": sm, "Holding Month": str(pd.Period(sm, "M") + 1),
                       "State": st, "Risk Budget": rb} for s, sm, st, rb in adaptive_rows])
    rows = []
    for (s, sm), w in (core_rows or {}).items():
        rows.append({"Status": s, "Signal Month": sm, "Holding Month": str(pd.Period(sm, "M") + 1),
                     **{t: float(w.get(t, 0.0)) for t in U}})
    c = pd.DataFrame(rows, columns=["Status", "Signal Month", "Holding Month"] + U)
    return {"Core Portfolio": c, "Adaptive Portfolio": a}


def release_auth(month: str, **over) -> dict:
    """Authorization as tools/release_auth.py gathers it for an approved admin release on main."""
    auth = {"release_month": month, "ref": "refs/heads/main", "actor": "owner", "actor_permission": "admin",
            "environment": {"name": NEW.RELEASE_ENVIRONMENT, "required_reviewers": ["owner"],
                            "can_admins_bypass": False, "branch_policy": "protected_or_custom"},
            "approvals": [{"user": "owner", "state": "approved", "comment": "checked",
                           "environments": [NEW.RELEASE_ENVIRONMENT], "at_utc": "2026-11-13T22:41:00Z"}]}
    auth.update(over)
    return auth


def engine_core(daily, un, f2r: dict | None, sm: str) -> dict:
    """Core weights the engine computes for signal month sm (what the author publishes when the
    reconstruction is exact) - used to build an author workbook whose Core row matches."""
    m = NEW.month_end_frame(daily)
    i = m.index.get_loc(pd.Period(sm, "M"))
    adaa, _, _ = NEW.adaa_weights(m, i, un)
    f2w, _ = NEW.f2r_weights(NEW.f2r_book(f2r), m, i)
    return NEW.core_weights(adaa, f2w).to_dict()


def author_core_rows(daily, un, f2r: dict | None, months: list[str], status: str = "OFFICIAL") -> dict:
    return {(status, sm): engine_core(daily, un, f2r, sm) for sm in months}


def default_adaptive_rows(official_2026_10: tuple | None = None) -> list[tuple]:
    rows = []
    for p in pd.period_range("2021-04", "2026-07", freq="M"):
        sm = str(p)
        st, rb = {"2024-09": ("DEFENSIVE", 0.55), "2026-05": ("DEFENSIVE", 0.5812205),
                  "2026-06": ("DEFENSIVE", 0.55)}.get(sm, ("NORMAL", 1.0))
        rows.append(("HISTORY", sm, st, rb))
    rows += [("FORWARD_MONITOR", "2026-08", "NORMAL", 1.0), ("FORWARD_MONITOR", "2026-09", "DEFENSIVE", 0.7122398)]
    if official_2026_10:
        rows.append(("OFFICIAL", "2026-10") + tuple(official_2026_10))
    rows.append(("PREVIEW", "2026-11", "NORMAL", 1.0))
    return rows


# The v1.0 pipeline as it ran before PR #1 (main 88decb7).  Only the opt-in v1.0 migration tests
# (marker v10_migration, PDS_V10_MIGRATION=1) compare against it; they are PR #1's evidence.
V10_BASELINE = "88decb746f5776209724e1c6aeef1be1b8910675"
# Paths whose change can alter what the pipeline locks or writes; rollback tests only run if one
# of them differs from the PR's previous code.
PIPELINE_PATHS = ("run_pipeline.py", "requirements.txt", "tools", ".github/workflows/update.yml")


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def _sha(ref: str) -> str | None:
    r = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def pr_target_sha() -> str | None:
    """The target branch commit this PR is checked against: pinned by CI (PDS_PR_TARGET_SHA) so a
    main that moves during the run cannot change the comparison; locally main/origin/main."""
    if os.environ.get("PDS_PR_TARGET_SHA"):
        return _sha(os.environ["PDS_PR_TARGET_SHA"])
    for ref in ("origin/main", "main"):
        if _sha(ref):
            return _sha(ref)
    return None


def pr_merge_base_sha() -> str | None:
    """Where this PR branched off the target (PDS_PR_MERGE_BASE pinned by CI, else computed)."""
    if os.environ.get("PDS_PR_MERGE_BASE"):
        return _sha(os.environ["PDS_PR_MERGE_BASE"])
    target = pr_target_sha()
    if not target:
        return None
    r = _git("merge-base", target, "HEAD")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def pr_compare_sha() -> str | None:
    """The code this run's code is compared with.  CI pins it: the target commit for pull_request
    runs (they test the PR merged into the target) and the merge-base for branch pushes (they test
    the branch head).  Locally: the merge-base."""
    if os.environ.get("PDS_PR_COMPARE_SHA"):
        return _sha(os.environ["PDS_PR_COMPARE_SHA"])
    return pr_merge_base_sha()


def pipeline_changed_since(ref: str) -> bool:
    """True if any pipeline path differs between ref and the working tree."""
    return _git("diff", "--quiet", ref, "--", *PIPELINE_PATHS).returncode != 0


def load_ref(ref: str | None, name: str):
    """run_pipeline.py as it was at ref, imported as module `name` (None if unavailable)."""
    if not ref:
        return None
    r = _git("show", f"{ref}:run_pipeline.py")
    if r.returncode != 0 or "def run_engine" not in r.stdout:
        return None
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"{name}_{ref[:12]}.py"
    tmp.write_text(r.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                              # dataclasses need the module registered
    spec.loader.exec_module(mod)
    mod.__base_ref__ = ref
    return mod


def load_base():
    """v1.0 baseline (opt-in migration tests only; PDS_BASE_REF may point elsewhere)."""
    return load_ref(os.environ.get("PDS_BASE_REF") or V10_BASELINE, "run_pipeline_base")


def point(mod, tmp: Path, lock_init: list | None = None):
    """Redirect every output path of a pipeline module into tmp."""
    out = tmp / "docs" / "data"
    out.mkdir(parents=True, exist_ok=True)
    mod.OUT_DIR = out
    mod.LATEST_JSON = out / "portfolio_latest.json"
    mod.LOCK_JSON = out / "official_history.json"
    mod.UNRATE_CACHE = out / "unrate_cache.csv"
    if hasattr(mod, "REF_LEDGER_JSON"):
        mod.REF_LEDGER_JSON = out / "reference_adaptive.json"
        mod.DIAG_DIR = out / "diagnostic"
    if hasattr(mod, "EVENTS_JSON"):
        mod.EVENTS_JSON = out / "official_events.json"
    if hasattr(mod, "RELEASES_JSON"):
        mod.RELEASES_JSON = out / "manual_releases.json"
    if hasattr(mod, "STATUS_JSON"):
        mod.STATUS_JSON = out / "run_status.json"
        mod.RUN_RESULT_PATH = tmp / "run_result.json"
        mod.RELEASE_AUTH_PATH = tmp / "release_auth.json"
    if lock_init is not None:
        mod.LOCK_JSON.write_text(json.dumps(lock_init, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def feed(mod, daily, un, f2r, ref, check_prices: bool = True):
    """Replace every network call with the given inputs.  NEW keeps its price integrity check."""
    def prices():
        if check_prices and hasattr(mod, "check_price_integrity"):
            mod.check_price_integrity(daily)
        return daily.copy()
    mod.fetch_prices = prices
    mod.fetch_unrate = lambda: (un.copy(), "TEST")
    mod.fetch_f2r_state = lambda: (json.loads(json.dumps(f2r)) if f2r is not None else None)
    mod.fetch_reference_workbook = lambda: ({k: v.copy() for k, v in ref.items()} if ref is not None else None)


def run(mod, mode: str, now: datetime, tmp: Path, daily, un, f2r, ref, lock_init=None, write=True,
        after_feed=None):
    """Run one engine pass.  Returns (payload, history list).  For NEW, files are written only
    when write=True (what main() does after a successful run).  after_feed(mod) may override an
    input (e.g. the UNRATE source) after the standard feed."""
    point(mod, tmp, lock_init)
    feed(mod, daily, un, f2r, ref)
    if after_feed:
        after_feed(mod)
    mod.ADAPTIVE_MODE = mode
    res = mod.run_engine("2017-03", now)
    if isinstance(res, tuple):                       # NEW: (payload, files)
        payload, files = res
        if write:
            for p, obj in files.items():
                mod.save_json(p, obj)
        hist = files.get(mod.LOCK_JSON)
        if hist is None:
            hist = json.loads(mod.LOCK_JSON.read_text()) if mod.LOCK_JSON.exists() else []
        return payload, hist, files
    payload = res                                    # BASE: lock already saved inside run_engine
    return payload, json.loads(mod.LOCK_JSON.read_text()), None
