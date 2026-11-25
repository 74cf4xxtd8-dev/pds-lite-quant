"""Dry run before a rollback: what would the pipeline at another git ref lock today?

Runs run_pipeline.py from --ref on today's public data against a COPY of the current docs/data
and compares the resulting official lock with the current one.  Nothing under docs/data is
written.  Use it with the update-portfolio workflow disabled (see README, 롤백 절차).

exit 0  no change to the official lock other than appending months newer than the latest one
exit 2  manual review required: the target code would fill a skipped (BLOCKED) month, change or
        remove a locked month - i.e. it would change investment targets nobody approved
exit 1  the dry run itself failed

    python tools/rollback_check.py --ref <commit-before-the-merge> [--report out.md]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ATTRS = {"OUT_DIR": "", "LATEST_JSON": "portfolio_latest.json", "LOCK_JSON": "official_history.json",
                "UNRATE_CACHE": "unrate_cache.csv", "REF_LEDGER_JSON": "reference_adaptive.json",
                "DIAG_DIR": "diagnostic", "EVENTS_JSON": "official_events.json",
                "RELEASES_JSON": "manual_releases.json"}


def load_ref(ref: str, name: str = "pds_rollback_target"):
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:run_pipeline.py"],
                         capture_output=True, text=True, check=True).stdout
    tmp = Path(tempfile.mkdtemp(prefix="rollback_ref_")) / f"{name}.py"
    tmp.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                            # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def point(mod, out: Path) -> None:
    for attr, rel in OUTPUT_ATTRS.items():
        if hasattr(mod, attr):
            setattr(mod, attr, out / rel if rel else out)


def compare(before: list[dict], after: list[dict]) -> dict:
    b = {d["signal_month"]: d for d in before}
    a = {d["signal_month"]: d for d in after}
    latest = max(b) if b else None
    canon = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)  # noqa: E731
    added = sorted(set(a) - set(b))
    out = {"latest_before": latest,
           "appended": [m for m in added if latest is None or m > latest],
           "gap_fills": [m for m in added if latest is not None and m < latest],
           "changed": sorted(m for m in b if m in a and canon(a[m]) != canon(b[m])),
           "removed": sorted(set(b) - set(a))}
    out["needs_review"] = bool(out["gap_fills"] or out["changed"] or out["removed"])
    out["targets"] = {m: {"state": a[m].get("state"), "risk_budget": a[m].get("risk_budget"),
                          "adaptive_source": a[m].get("sources", {}).get("adaptive")}
                      for m in out["appended"] + out["gap_fills"] + out["changed"]}
    return out


def check(mod, data_dir: Path, now_utc: datetime, backfill_from: str = "2017-03") -> dict:
    """Run mod.run_engine against a copy of data_dir.  mod's fetchers are used as they are
    (real network in production, injected inputs in tests)."""
    lock_path = data_dir / "official_history.json"
    before = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else []
    work = Path(tempfile.mkdtemp(prefix="rollback_check_"))
    for f in data_dir.glob("*"):
        if f.is_file():
            shutil.copy2(f, work / f.name)
    point(mod, work)
    res = mod.run_engine(backfill_from, now_utc)
    if isinstance(res, tuple):                         # new interface: (payload, files)
        payload, files = res
        after = files.get(mod.LOCK_JSON, before)
    else:                                              # 1.0 interface: lock saved inside run_engine
        payload = res
        after = json.loads((work / "official_history.json").read_text(encoding="utf-8"))
    out = compare(before, after)
    o = payload.get("official", {})
    out["official_after"] = f"{o.get('signal_month')}->{o.get('holding_month')} {o.get('state')} " \
                            f"RB {o.get('risk_budget')}"
    out["action_after"] = (payload.get("action") or {}).get("code", "n/a (no action field)")
    return out


def report(res: dict, ref: str, now_utc: datetime) -> str:
    lines = [f"## Rollback dry run: `{ref}` on {now_utc:%Y-%m-%d %H:%M} UTC data",
             f"- latest locked month now: {res['latest_before']}",
             f"- would append (newer months): {res['appended'] or 'none'}",
             f"- **would fill skipped months**: {res['gap_fills'] or 'none'}",
             f"- **would change locked months**: {res['changed'] or 'none'}",
             f"- **would remove locked months**: {res['removed'] or 'none'}",
             f"- official after the run: {res['official_after']} (action: {res['action_after']})"]
    for m, t in res["targets"].items():
        lines.append(f"  - {m}: {t['state']} RB {t['risk_budget']} ({t['adaptive_source']})")
    lines.append("")
    lines.append("**결론: 수동 검토 필요 — 이 코드로 되돌리면 승인되지 않은 목표 비중이 생깁니다. 워크플로를 "
                 "끈 상태로 두고 보유 자산을 유지하세요.**" if res["needs_review"] else
                 "결론: 기존 잠금 변경·빈 달 채우기 없음.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True, help="git ref whose run_pipeline.py would be restored")
    ap.add_argument("--data-dir", default=str(ROOT / "docs" / "data"))
    ap.add_argument("--report", help="also append the markdown report to this file")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        mod = load_ref(args.ref)
        res = check(mod, Path(args.data_dir), now)
    except Exception as e:  # noqa: BLE001
        print(f"rollback dry run failed: {e}")
        return 1
    text = report(res, args.ref, now)
    print(text)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 2 if res["needs_review"] else 0


if __name__ == "__main__":
    sys.exit(main())
