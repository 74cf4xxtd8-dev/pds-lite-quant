"""Send a Telegram alert and record whether it was delivered (docs/data/run_status.json "delivery").

    python tools/send_alert.py state   [--pages URL]                 # state changes of this run
    python tools/send_alert.py failure --what pipeline|commit [--run-url URL]

* The Telegram reply is checked ({"ok": true}); one retry.  curl -s used to hide failures.
* Not delivered -> run_status.json gets delivery.status = "failed", and for state alerts the
  undelivered events go to notify_pending, so the next successful run sends them again.  The
  workflow then commits run_status.json and marks the run failed (GitHub's own failure notice).
* exit 0 sent / nothing to send / no token configured, exit 3 not delivered.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_message  # noqa: E402

STATUS = Path("docs/data/run_status.json")


def telegram(text: str, token: str, chat: str) -> tuple[bool, str]:
    base = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    err = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{base}/bot{token}/sendMessage", data=data),
                                        timeout=20) as r:
                body = json.loads(r.read().decode() or "{}")
            if body.get("ok") is True:
                return True, ""
            err = f"telegram ok=false: {body.get('description', '')}"[:200]
        except urllib.error.HTTPError as e:
            try:
                desc = json.loads(e.read().decode()).get("description", "")
            except Exception:  # noqa: BLE001
                desc = ""
            err = f"HTTP {e.code} {desc}"[:200]
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
        if attempt == 0:
            time.sleep(float(os.environ.get("ALERT_RETRY_SECONDS", "5")))
    return False, err


def failure_text(status: dict, what: str, run_url: str) -> str:
    act = status.get("action") or {}
    tail = "\n".join((status.get("log_tail") or [])[-3:])[:600]
    if what == "commit":
        head = ("[PDS-Lite] ⚠️ 커밋 실패: 이번 실행 결과(잠금 포함)가 저장되지 않았습니다. 직전 Official이 그대로 "
                "유효합니다. 다음 실행이 다시 판단합니다(10일 마감이 지났으면 BLOCKED).")
        if act:
            head += f"\n이번 실행의 판단(저장 안 됨): {act.get('code')} {act.get('month')}"
    else:
        head = "[PDS-Lite] 실행 실패: 새 목표 비중 없음, 직전 Official 유지 (fail-closed)"
    return f"{head}\n{tail}\n{run_url}\n실행 기록: Actions 아티팩트 pds-lite-run-*".strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["state", "failure"])
    ap.add_argument("--status", default=str(STATUS))
    ap.add_argument("--pages", default="")
    ap.add_argument("--run-url", default=os.environ.get("RUN_URL", ""))
    ap.add_argument("--what", choices=["pipeline", "commit"], default="pipeline")
    args = ap.parse_args(argv)
    path = Path(args.status)
    status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if args.kind == "state":
        text = notify_message.message(notify_message.payload_from_status(status), args.pages)
    else:
        text = failure_text(status, args.what, args.run_url)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    token, chat = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_CHAT", "")
    if not text:
        rec = {"kind": args.kind, "status": "nothing_to_send", "at_utc": now}
    elif not token or not chat:
        rec = {"kind": args.kind, "status": "skipped_no_telegram_secret", "at_utc": now}
    else:
        ok, err = telegram(text, token, chat)
        rec = {"kind": args.kind, "status": "sent" if ok else "failed", "error": err or None, "at_utc": now}
        if not ok and args.kind == "state":
            status["notify_pending"] = status.get("notify") or []
    status["delivery"] = rec
    if path.exists() or rec["status"] == "failed":
        path.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rec, ensure_ascii=False))
    return 3 if rec["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
