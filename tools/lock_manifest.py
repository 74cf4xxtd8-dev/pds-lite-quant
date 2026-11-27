"""Monthly external backup of the official lock: the manifest line and the checklist.

The lock (docs/data/official_history.json) cannot be regenerated, so after every new lock (LOCKED
or MANUAL_RELEASE) the operator keeps a copy outside GitHub and adds one line to an external
manifest (handbook manual 12.2).  This module computes that line - months, range, SHA-256, git blob
- and the checklist text used by

* the Telegram alert of a new lock (send_alert.py),
* run_status.json "lock_manifest" on every run (record_run_status.py), and the job summary,
* the monthly ops-audit report.

Backup needed  <=>  the SHA-256 on the last line of the external manifest differs from this one.
The system cannot see the external backup: changed_this_run / backup_due only say that this run
SAVED a new lock (a backup is now due), never that a backup was made.

    python tools/lock_manifest.py [--summary] [--due]        # print the manifest / checklist
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCK = Path("docs/data/official_history.json")
BACKUP_FILES = (("docs/data/official_history.json", "필수"), ("docs/data/official_events.json", "권장"),
                ("docs/data/manual_releases.json", "권장"), ("docs/data/reference_adaptive.json", "권장"))
NEW_LOCK_EVENTS = ("LOCKED", "MANUAL_RELEASE")
RULE = "외부 매니페스트 마지막 줄의 SHA-256이 이 값과 다르면 백업이 필요하다(handbook ② 12.2)"
NOT_DONE = ("backup_due·changed_this_run은 '이번 실행이 새 잠금을 저장했다'는 뜻일 뿐 외부 백업 완료가 아니다. "
            "시스템은 외부 백업을 볼 수 없으므로 완료 여부는 매니페스트와 SHA-256을 비교해 운영자가 확인한다")


def git_blob(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def manifest(path: Path = LOCK) -> dict:
    if not path.exists():
        return {"file": str(path), "readable": False, "error": "missing"}
    b = path.read_bytes()
    out = {"file": str(path), "sha256": hashlib.sha256(b).hexdigest(), "git_blob": git_blob(b), "bytes": len(b)}
    try:
        h = json.loads(b.decode("utf-8"))
        out.update(readable=True, months=len(h), first=h[0]["signal_month"], last=h[-1]["signal_month"],
                   last_locked_at_utc=h[-1].get("locked_at_utc"), last_origin=h[-1].get("origin"))
    except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError) as e:
        out.update(readable=False, error=f"{type(e).__name__}"[:60])
    return out


def head_commit(repo: str = ".") -> str | None:
    if os.environ.get("DATA_COMMIT"):
        return os.environ["DATA_COMMIT"]
    r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def raw_base() -> str:
    return os.environ.get("PDS_RAW_BASE", "https://raw.githubusercontent.com").rstrip("/")


def raw_url(repo_slug: str, commit: str, path: str) -> str:
    return f"{raw_base()}/{repo_slug}/{commit}/{path}"


def _valid_link(url) -> bool:
    """Only a raw link of this kind: <raw base>/<owner>/<repo>/<40-hex commit>/docs/data/<file>.json"""
    if not isinstance(url, str) or not url.startswith(raw_base() + "/"):
        return False
    return re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[0-9a-f]{40}/docs/data/[A-Za-z0-9_.-]+\.json",
                        url[len(raw_base()) + 1:]) is not None and ".." not in url


def _get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# result -> ok: True = the link returns exactly the reference bytes; False = it definitely does not
# (wrong, empty, missing, malformed link); None = could not check now (server error, timeout, network)
LINK_RESULTS = {"match": True, "mismatch": False, "empty": False, "not_found": False, "bad_url": False,
                "bad_reference": False, "http_error": None, "timeout": None, "network": None}


def verify_link(url, expected_sha256, *, getter=None, timeout: float = 20, tries: int = 1, wait: float = 0) -> dict:
    """Download the link and compare the SHA-256 of the actual response bytes with the reference
    file's.  Never raises; see LINK_RESULTS."""
    out = {"url": url, "expected_sha256": expected_sha256, "sha256": None, "bytes": None, "result": None, "detail": ""}
    if not (isinstance(expected_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        out.update(result="bad_reference", detail="기준 파일 SHA-256이 없거나 형식이 아님")
    elif not _valid_link(url):
        out.update(result="bad_url", detail="raw 링크 형식(<owner>/<repo>/<40자 커밋>/docs/data/<파일>.json)이 아님")
    else:
        for attempt in range(max(1, tries)):
            try:
                body = (getter or _get)(url, timeout)
            except urllib.error.HTTPError as e:
                out.update(result="not_found" if e.code == 404 else "http_error", detail=f"HTTP {e.code}")
            except (TimeoutError, socket.timeout):
                out.update(result="timeout", detail=f"{timeout}s 안에 응답 없음")
            except urllib.error.URLError as e:
                timed_out = isinstance(e.reason, (TimeoutError, socket.timeout))
                out.update(result="timeout" if timed_out else "network", detail=f"{e.reason}"[:120])
            except (ValueError, OSError) as e:
                out.update(result="network", detail=f"{type(e).__name__}: {e}"[:120])
            else:
                got = hashlib.sha256(body).hexdigest()
                out.update(sha256=got, bytes=len(body))
                out.update(result="empty" if not body else "match" if got == expected_sha256 else "mismatch",
                           detail="" if body else "빈 응답")
            if LINK_RESULTS[out["result"]] is not None:        # a definite answer: no retry
                break
            if attempt + 1 < tries:
                time.sleep(wait)
    out["ok"] = LINK_RESULTS[out["result"]]
    return out


def link_line(v: dict) -> str:
    mark = {True: "✅", False: "❌", None: "❔"}[v["ok"]]
    return (f"{mark} 내려받기 링크 확인: {v['result']}" + (f" ({v['detail']})" if v["detail"] else "")
            + (f" · 응답 {v['bytes']}바이트 SHA-256 {v['sha256']}" if v["sha256"] else "")
            + f" · 기준 {v['expected_sha256']}")


def line(m: dict) -> str:
    """One manifest line: months | range | SHA-256 | git blob."""
    if not m.get("readable"):
        return f"잠금 파일을 읽을 수 없음({m.get('error')}) → 백업하지 말고 확인(② 12.3)"
    return (f"잠금 {m['months']}개월 · {m['first']}~{m['last']} · SHA-256 {m['sha256']} · git blob {m['git_blob']}")


def checklist(m: dict, repo_slug: str | None, commit: str | None) -> str:
    if not m.get("readable"):
        return "📦 월간 백업: " + line(m)
    link = (raw_url(repo_slug, commit, BACKUP_FILES[0][0]) if repo_slug and commit
            else "(데이터 커밋 확인 불가 → GitHub의 official_history.json History에서 최신 data: 커밋)")
    s = [f"📦 오늘 할 일: 월간 백업(외부 보관)",
         f"잠금 {m['months']}개월 · {m['first']}~{m['last']} (마지막 잠금 {m.get('last_locked_at_utc')}, {m.get('last_origin')})",
         f"SHA-256 {m['sha256']}",
         f"git blob {m['git_blob']}",
         f"데이터 커밋 {commit or '확인 불가'}",
         f"1) {BACKUP_FILES[0][1]} official_history.json: {link}",
         "2) " + BACKUP_FILES[1][1] + " " + " · ".join(Path(p).name for p, _ in BACKUP_FILES[1:])
         + " (같은 커밋, 경로만 바꿈)",
         "3) 외부 보관 → 매니페스트 1줄(개월 수·마지막 신호월·SHA-256·커밋) → 직전 백업과 점검 스크립트 대조(② 12.2)",
         "※ 이 알림은 백업 완료를 확인하지 않습니다. 완료 여부는 매니페스트로 직접 확인하세요."]
    return "\n".join(s)


def status_record(path: Path, notify: list[dict], checkout_commit: str | None) -> dict:
    """run_status.json "lock_manifest": the same facts on every run, Telegram or not."""
    m = manifest(path)
    changed = any(e.get("event") in NEW_LOCK_EVENTS for e in notify or [] if not e.get("resend"))
    m["changed_this_run"] = changed
    m["backup_due"] = changed and bool(m.get("readable"))     # a new lock was saved: back it up (not "done")
    # a commit that is known to hold exactly this file: the checkout, unless this run is about to
    # commit a new version (then the job summary and the alert carry the new commit)
    m["download_commit"] = None if changed else checkout_commit
    m["backup_rule"] = RULE
    m["backup_note"] = NOT_DONE
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default=str(LOCK))
    ap.add_argument("--summary", action="store_true", help="markdown for the Actions job summary")
    ap.add_argument("--due", default="false", help="true: this run saved a new lock -> full checklist")
    ap.add_argument("--verify-link", action="store_true",
                    help="with --due true: download the checklist's link and compare SHA-256 (never fails the run)")
    a = ap.parse_args(argv)
    m = manifest(Path(a.lock))
    commit, slug = head_commit(), os.environ.get("GITHUB_REPOSITORY")
    due = a.due.strip().lower() == "true"
    if a.summary:
        print("## 📦 잠금 백업" + (" — 이번 실행에서 새 잠금 저장: 오늘 백업" if due else ""))
        print(f"- {line(m)}")
        print(f"- 데이터 커밋 `{commit}`" if commit else "- 데이터 커밋: 확인 불가")
        print(f"- {RULE}")
        print(f"- {NOT_DONE}")
        if due:
            print("\n```\n" + checklist(m, slug, commit) + "\n```")
            if a.verify_link and m.get("readable") and slug and commit:
                v = verify_link(raw_url(slug, commit, str(LOCK)), m["sha256"], tries=3, wait=10)
                print(f"- {link_line(v)}")
    else:
        print(checklist(m, slug, commit) if due else line(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
