"""Write docs/data/run_status.json after every workflow run, success or failure.

* Keeps the last RECENT_KEEP runs in "recent" (the daily check log; Telegram only gets state changes).
* Uses portfolio_latest.json only if this run wrote it (generated after RUN_STARTED_UTC), so a failed
  or diagnostic run never re-announces an older run's events.
* Sets step outputs: action=<FOLLOW_OFFICIAL|WAIT|HALTED|NONE>, notify=<true|false>.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

RECENT_KEEP = 60
DATA = Path("docs/data")


def this_runs_payload(path: Path, started_utc: datetime | None) -> dict | None:
    if not path.exists():
        return None
    try:
        p = json.loads(path.read_text(encoding="utf-8"))
        gen = datetime.fromisoformat(p["generated_at_utc"])
    except (ValueError, KeyError, TypeError):
        return None
    if started_utc is not None and gen < started_utc:
        return None                                   # written by an earlier run
    return p


def build(prev: dict | None, ok: bool, payload: dict | None, log: list[str], event: str | None,
          run_url: str | None, now_utc: datetime) -> dict:
    payload = payload if ok else None
    action = (payload or {}).get("action")
    notify = (payload or {}).get("notify") or []
    entry = {"at_utc": now_utc.isoformat(timespec="seconds"), "ok": ok, "event": event,
             "action": (action or {}).get("code"), "month": (action or {}).get("month"),
             "reason": (action or {}).get("reason"),
             "new_locks": ((payload or {}).get("validation") or {}).get("new_official_locks"),
             "notified": [f"{e['event']} {e['signal_month']}" for e in notify],
             "run_url": run_url}
    recent = ((prev or {}).get("recent") or [])[-(RECENT_KEEP - 1):] + [entry]
    return {"ok": ok, "action": action, "notify": notify,
            "finished_utc": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "event": event, "run_url": run_url,
            "log_tail": [line for line in log if not line.startswith("[lock]")][-40:],
            "recent": recent}


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
    payload = this_runs_payload(DATA / "portfolio_latest.json", started_utc) if ok else None
    out = build(prev, ok, payload, log, os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("RUN_URL"),
                datetime.now(timezone.utc))
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"action={(out['action'] or {}).get('code', 'NONE')}\n")
            f.write(f"notify={'true' if out['notify'] else 'false'}\n")


if __name__ == "__main__":
    main()
