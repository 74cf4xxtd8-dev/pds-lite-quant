"""Equivalence tests (run locally: python tests/test_equivalence.py).

1) Sleeve engine vs the author's independent Python implementation on synthetic prices.
   Opt-in: set ADAA_REPO to YOUR clone of slackquant80/adaa-decision-diversification.
   This imports third-party code, so only enable it on a machine where you trust that clone.
2) Core / Adaptive / asset-class identities vs the published PDS workbook (PDS_XLSX).
3) IRP conversion invariants.
"""
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_pipeline as rp  # noqa: E402


def synthetic(seed, n_months=60):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-01-01", periods=n_months * 21)
    vol = rng.uniform(0.002, 0.02, len(rp.UNIVERSE))
    vol[rp.UNIVERSE.index("BIL")] = 0.0002
    vol[rp.UNIVERSE.index("SHY")] = 0.001
    drift = rng.normal(0.0002, 0.0006, (len(days), len(rp.UNIVERSE)))
    k = len(days) // 3
    drift[k:k + 200] -= 0.0025                      # bear phase -> defensive branches execute
    px = 100 * np.exp(np.cumsum(drift + rng.standard_normal(drift.shape) * vol, axis=0))
    daily = pd.DataFrame(px, index=days, columns=rp.UNIVERSE)
    months = pd.period_range("2017-01", days[-1].to_period("M"), freq="M")
    un = pd.Series(4 + np.cumsum(rng.normal(0, 0.15, len(months))), index=months)
    un.iloc[len(un) // 2] = np.nan                  # missing release -> carry_calendar path
    return daily, un


def test_sleeves_match_author_python():
    repo = os.environ.get("ADAA_REPO")
    if not repo:
        print("skip sleeve equivalence: ADAA_REPO not set")
        return
    p = Path(repo) / "03_Data_and_Code" / "02_Code" / "build_independent_decision_panel_v0_11.py"
    spec = importlib.util.spec_from_file_location("author_panel", p)
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    counts = {k: 0 for k in rp.SLEEVE_MIX}
    for seed in range(12):
        daily, un = synthetic(seed)
        m = rp.month_end_frame(daily)
        ref = {"HAA": a.build_haa(m)[0], "BAA": a.build_baa(m)[0], "ADM": a.build_adm(m)[0],
               "FAA": a.build_faa(m, self_exclude=True, exact_n=True)[0]}
        u = un.to_frame("UNRATE")
        u.index = u.index.to_timestamp()
        ref["LAA"] = a.build_laa_missing_observation_controls(m, u)["carry_calendar"][0]
        for i in range(len(m)):
            mine = {"HAA": rp.sleeve_haa(m, i), "BAA": rp.sleeve_baa(m, i), "ADM": rp.sleeve_adm(m, i),
                    "FAA": rp.sleeve_faa(m, i), "LAA": rp.sleeve_laa(m, i, un)[0]}
            for k, w in mine.items():
                r = ref[k].iloc[i].reindex(rp.UNIVERSE).fillna(0.0)
                if w is None:
                    assert r.abs().sum() < 1e-12, (seed, k, m.index[i])
                    continue
                assert np.allclose(w.values, r.values, atol=1e-12), (seed, k, m.index[i])
                counts[k] += 1
    print("sleeve-months matched:", counts)


def test_core_and_adaptive_identities():
    x = os.environ.get("PDS_XLSX")
    if not x:
        print("skip workbook identities: PDS_XLSX not set")
        return
    wb = pd.read_excel(x, sheet_name=None)
    core, ad, ac = wb["Core Portfolio"], wb["Adaptive Portfolio"], wb["Core Asset Class"]
    C = {(r["Status"] == "PREVIEW", str(r["Signal Month"])): r[rp.UNIVERSE].astype(float)
         for _, r in core.iterrows()}
    for _, r in ad.iterrows():
        k = (r["Status"] == "PREVIEW", str(r["Signal Month"]))
        got = rp.apply_risk_budget(C[k].copy(), float(r["Risk Budget"]))
        assert np.allclose(got.values, r[rp.UNIVERSE].astype(float).values, atol=1e-12), k
    for (_, r), (_, q) in zip(core.iterrows(), ac.iterrows()):
        got = rp.asset_classes(r[rp.UNIVERSE].astype(float))
        assert all(abs(got[g] - float(q[g])) < 1e-12 for g in rp.ASSET_CLASS)
    print(f"Adaptive = RB*Core + (1-RB)*BIL: {len(ad)} rows exact; asset classes: {len(core)} rows exact")


def test_engine_invariants_on_synthetic():
    for seed in range(6):
        daily, un = synthetic(seed)
        m = rp.month_end_frame(daily)
        for i in range(14, len(m)):
            w, sleeves, _ = rp.adaa_weights(m, i, un)
            rp.check_weights(w, f"adaa {m.index[i]}")
            for k, s in sleeves.items():
                rp.check_weights(s, f"{k} {m.index[i]}")
            grid = (w.values * 2400)          # ADAA lattice: every weight is a multiple of 1/2400
            assert np.allclose(grid, np.round(grid), atol=1e-7), (m.index[i], w[w > 0])
            core = rp.core_weights(w, rp.f2r_weights({}, m, i)[0])
            st, rb, _ = rp.adaptive_proxy(core, daily, daily.index[daily.index.to_period("M") == m.index[i]][-1], m, i)
            rp.check_weights(rp.apply_risk_budget(core, rb), "adaptive")
            assert rp.ADAPTIVE_FLOOR <= rb <= 1
    print("engine invariants OK (sum=1, non-negative, 1/2400 lattice, RB bounds)")


def test_irp_cap():
    w = pd.Series(0.0, index=rp.UNIVERSE)
    w[["QQQ", "SPY", "EEM", "GLD", "IEF"]] = [0.3, 0.3, 0.2, 0.1, 0.1]
    out = rp.to_irp(w)
    assert abs(sum(r["weight"] for r in out["rows"]) - 1) < 1e-12
    assert out["risk_after_cap"] <= rp.IRP_RISK_CAP - rp.IRP_CAP_BUFFER + 1e-12
    print(f"IRP cap: risk {out['risk_before_cap']:.4f} -> {out['risk_after_cap']:.4f}")


if __name__ == "__main__":
    test_sleeves_match_author_python()
    test_core_and_adaptive_identities()
    test_engine_invariants_on_synthetic()
    test_irp_cap()
