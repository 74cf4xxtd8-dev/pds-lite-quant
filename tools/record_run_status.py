"""Write docs/data/run_status.json after every workflow run, success or failure.

* Keeps the last RECENT_KEEP runs in "recent" (the daily check log; Telegram only gets state changes).
  Every run commits this file, so git history keeps the full record.
* Reads this run's outcome from run_result.json (written by run_pipeline.main, outside docs/data);
  a failed run has none, so it can never re-announce an older run's events.
* Operator halt: the halt notice goes out once (not again while the previous successful run was
  already halted), and the report lists other update-portfolio runs still queued or running.
* Alerts an earlier run could not deliver (notify_pending, set by send_alert.py) are resent with
  the next successful run and kept through failed runs.
* Step outputs: action=<FOLLOW_OFFICIAL|WAIT|HALTED|NONE>, notify=<true|false>, halt=<true|false>.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RECENT_KEEP = 60
DATA = Path("docs/data")
ACTIVE = ("in_progress", "queued", "waiting", "pending", "requested")


def this_runs_result(path: Path, started_utc: datetime | None) -> dict | None:
    if not path.exists():
        return None
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
        gen = datetime.fromisoformat(r["generated_at_utc"])
    except (ValueError, KeyError, TypeError):
        return None
    if started_utc is not None and gen < started_utc:
        return None
    return r


def other_active_runs(runs: list[dict], own_run_id: str | None) -> list[dict]:
    return [{"id": r.get("id"), "status": r.get("status"), "event": r.get("event"),
             "created_at": r.get("created_at"), "url": r.get("html_url")}
            for r in runs if str(r.get("id")) != str(own_run_id) and r.get("status") in ACTIVE]


def fetch_active_runs() -> list[dict] | None:
    """Other update-portfolio runs still queued/running (GITHUB_TOKEN with actions: read)."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not repo or not token:
        return None
    runs = []
    try:
        for st in ACTIVE:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/actions/workflows/update.yml/runs?status={st}&per_page=20",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                runs += json.loads(r.read().decode()).get("workflow_runs", [])
    except Exception:  # noqa: BLE001
        return None
    return other_active_runs(runs, os.environ.get("GITHUB_RUN_ID"))


def _last_ok(prev: dict | None) -> dict | None:
    return next((r for r in reversed((prev or {}).get("recent") or []) if r.get("ok")), None)


def build(prev: dict | None, ok: bool, result: dict | None, log: list[str], event: str | None,
          run_url: str | None, now_utc: datetime, active_runs: list[dict] | None = None) -> dict:
    result = result if ok else None
    action = (result or {}).get("action")
    halt = bool((result or {}).get("operator_halt"))
    notify = list((result or {}).get("notify") or [])
    last = _last_ok(prev)
    if halt and last and last.get("operator_halt"):             # halt already announced
        notify = [e for e in notify if e.get("event") != "OPERATOR_HALT"]
    # alerts an earlier run could not deliver: resend with the next successful run, keep otherwise
    pending = [dict(e, resend=True) for e in (prev or {}).get("notify_pending") or []]
    if ok:
        notify, keep = pending + notify, []
    else:
        notify, keep = [], pending
    entry = {"at_utc": now_utc.isoformat(timespec="seconds"), "ok": ok, "event": event,
             "action": (action or {}).get("code"), "month": (action or {}).get("month"),
             "reason": (action or {}).get("reason"), "operator_halt": halt if ok else None,
             "new_locks": (result or {}).get("new_locks"),
             "notified": [f"{e['event']} {e['signal_month']}" for e in notify], "run_url": run_url}
    rel = (result or {}).get("manual_release")
    if rel:
        entry["manual_release"] = {k: rel.get(k) for k in ("signal_month", "actor", "outcome", "problems")}
    recent = ((prev or {}).get("recent") or [])[-(RECENT_KEEP - 1):] + [entry]
    out = {"ok": ok, "action": action, "operator_halt": halt, "notify": notify, "notify_pending": keep,
           "finished_utc": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
           "event": event, "run_url": run_url,
           "log_tail": [line for line in log if not line.startswith("[lock]")][-40:],
           "recent": recent}
    if halt:
        out["halt_report"] = dict((result or {}).get("halt_report") or {}, other_active_runs=active_runs)
    return out


def main() -> None:
    ok = os.environ.get("RUN_OUTCOME") == "success"
    started = os.environ.get("RUN_STARTED_UTC")
    started_utc = datetime.fromtimestamp(int(started), timezone.utc) if started else None
    log = Path("run.log").read_text(encoding="utf-8", errors="replace").splitlines() \
        if Path("run.log").exists() else []
    status = DATA / "run_status.json"
    try:
        prev = json.loads(status.read_text(encoding="utf-8")) if status.exists() else None
    except ValueError:
        prev = None
    result = this_runs_result(Path(os.environ.get("RUN_RESULT_PATH", "run_result.json")), started_utc) if ok else None
    active = fetch_active_runs() if (result or {}).get("operator_halt") else None
    out = build(prev, ok, result, log, os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("RUN_URL"),
                datetime.now(timezone.utc), active)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"action={(out['action'] or {}).get('code', 'NONE')}\n")
            f.write(f"notify={'true' if out['notify'] else 'false'}\n")
            f.write(f"halt={'true' if out['operator_halt'] else 'false'}\n")


if __name__ == "__main__":
    main()
