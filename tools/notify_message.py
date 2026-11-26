"""Telegram text for the state changes of one run (payload["notify"]); empty when nothing changed.

Sent: waiting started (once per month), author-Core mismatch, official locked, day-10 block,
manual release / rejection, operator halt.  Not sent: the repeated daily checks of days 1-10 -
those stay in run_status.json ("recent") and official_events.json.  Failures are alerted by the
workflow itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _line(e: dict, payload: dict) -> str:
    sm, kind, detail = e["signal_month"], e["event"], e.get("detail", "")
    act = payload.get("action") or {}
    off = payload.get("official") or {}
    if kind == "PENDING":
        return (f"⏳ {sm} 잠금 대기 시작: {detail}\n"
                f"매월 1~10일 매일 확인하고, 상태가 바뀔 때만 다시 알립니다. "
                f"리밸런싱하지 마세요(직전 공식 {act.get('in_force', '?')} 유지).")
    if kind == "CORE_MISMATCH":
        return f"⚠️ {sm} 원작자 Core 불일치: {detail}\n잠그지 않습니다. 원인 확인이 필요합니다."
    if kind == "LOCKED":
        s = f"✅ {sm} 공식 확정"
        if off.get("signal_month") == sm:
            top = ", ".join(f"{r['ticker']} {_pct(r['adaptive'])}" for r in off.get("us", [])[:5])
            s += (f" → {off['holding_month']} 보유. {off['state']} RB {off['risk_budget'] * 100:.0f}%\n"
                  f"{top}\n이 목표로만 리밸런싱하세요.")
        return s
    if kind == "BLOCKED":
        return (f"⛔ {sm} 자동 리밸런싱 중지(10일 마감): {detail}\n"
                f"현재 보유 자산을 그대로 유지하세요. 자동으로 잠기지 않으며, 직접 확인한 뒤 "
                f"release_month={sm}, release_reason을 넣어 실행해야만 해제됩니다.")
    if kind == "MANUAL_RELEASE":
        return f"🔓 {sm} 수동 해제로 확정: {detail}"
    if kind == "MANUAL_RELEASE_REJECTED":
        return f"✋ {sm} 수동 해제 거부: {detail}"
    if kind == "OPERATOR_HALT":
        s = ("⛔ 운영자 중지(PDS_HALT) 확인: 신규 잠금·목표 비중 변경·운영 데이터 변경 없음. "
             "현재 보유 자산을 그대로 유지하세요.")
        hr = payload.get("halt_report") or {}
        prev = hr.get("previous_run") or {}
        if prev:
            s += (f"\n직전 실행: {prev.get('at_utc')} {prev.get('action') or '실패'}"
                  f", 신규 잠금 {prev.get('new_locks') if prev.get('new_locks') is not None else '-'}")
        locks = hr.get("locks_last_72h") or []
        s += "\n최근 72시간 잠금: " + (", ".join(f"{x['signal_month']}({x['locked_at_utc']})" for x in locks)
                                     if locks else "없음")
        other = hr.get("other_active_runs")
        s += ("\n대기·실행 중인 다른 update-portfolio 실행: 확인 불가" if other is None else
              f"\n대기·실행 중인 다른 update-portfolio 실행: {len(other)}건")
        return s
    return f"ℹ️ {sm} {kind}: {detail}"


def message(payload: dict | None, pages_url: str = "") -> str:
    events = (payload or {}).get("notify") or []
    if not events:
        return ""
    body = "\n\n".join(("(재전송: 이전 실행에서 전달 실패) " if e.get("resend") else "") + _line(e, payload)
                       for e in events)
    return f"[PDS-Lite]\n{body}" + (f"\n{pages_url}" if pages_url else "")


def payload_from_status(status: dict, latest: Path = Path("docs/data/portfolio_latest.json")) -> dict:
    """run_status.json carries this run's notify list and action; the official block (for the
    'locked' line) comes from portfolio_latest.json, which a successful locking run just wrote."""
    if "official" in status:
        return status
    full = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
    full["notify"] = status.get("notify") or []
    full["action"] = status.get("action") or full.get("action")
    full["halt_report"] = status.get("halt_report")
    return full


def main(argv: list[str]) -> None:
    path = Path(argv[1] if len(argv) > 1 else "docs/data/run_status.json")
    print(message(payload_from_status(json.loads(path.read_text(encoding="utf-8"))),
                  argv[2] if len(argv) > 2 else ""))


if __name__ == "__main__":
    main(sys.argv)
