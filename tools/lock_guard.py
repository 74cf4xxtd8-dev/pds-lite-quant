"""CI guard for the operational records in docs/data (read-only; exit 1 = the PR must not merge).

Normal PRs: a pull request may not change anything under docs/data.  Only the update-portfolio bot
writes there (directly on main, never through a PR), so a PR that touches the lock, the audit
records or any other data file is rejected.  The pipeline's own append-only check only covers one
run; this check covers commits.

Restore PRs (handbook manual chapter 12): putting a damaged record back to an earlier version is the
one exception, and it is checked more strictly, not less.  It is requested by the branch name
restore/<RESTORE-YYYYMMDD-N> (and, on pull_request runs, the label lock-restore), and passes only if
* the PR changes nothing but the restored files, docs/data/lock_restores.json and
  docs/data/official_events.json;
* exactly one restore record is appended to lock_restores.json, its id matches the branch;
* every restored file is byte-identical to the version at the source commit, which is on the
  target's history and was last written by github-actions[bot] (official_events.json: that version
  plus the one LOCK_RESTORED event);
* the record's before/after SHA-256, month count, last month and month-by-month diff equal what this
  script computes, and its external backup SHA-256 equals the restored lock;
* exactly one LOCK_RESTORED event naming the restore id is appended;
* PDS_HALT is set (operations stopped);
* on pull_request runs: the protected environment pds-lock-restore has required reviewers and this
  run carries its approval by the record's approver (admin) with the restore id in the comment.
A label or a record alone never passes anything: every fact in the record is recomputed from git.

    python tools/lock_guard.py --base <merge-base> --head <PR head> --event pull_request \
        --head-ref restore/RESTORE-20261220-1 --label true [--report out.md]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

DATA = "docs/data/"
LOCK = "docs/data/official_history.json"
EVENTS = "docs/data/official_events.json"
RESTORES = "docs/data/lock_restores.json"
RESTORABLE = (LOCK, EVENTS, "docs/data/manual_releases.json", "docs/data/reference_adaptive.json")
BOT = "github-actions[bot]"
LABEL = "lock-restore"
ENVIRONMENT = "pds-lock-restore"
ID_RE = re.compile(r"^RESTORE-\d{8}-\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HALT_OFF = ("", "0", "false", "no", "off")
RECORD_KEYS = ("restore_id", "incident", "detected_by", "files", "source", "before", "after",
               "lock", "external_backup", "approval")


# ---------------------------------------------------------------- git and formats
def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True)


def show(repo: str, rev: str, path: str) -> bytes | None:
    r = git(repo, "show", f"{rev}:{path}")
    return r.stdout if r.returncode == 0 else None


def changed_paths(repo: str, base: str, head: str) -> list[str]:
    r = git(repo, "diff", "--no-renames", "--name-only", base, head)
    if r.returncode != 0:
        raise RuntimeError(f"git diff {base[:7]}..{head[:7]} failed: {r.stderr.decode()[:200]}")
    return sorted(p for p in r.stdout.decode().splitlines() if p)


def sha256(b: bytes | None) -> str:
    return hashlib.sha256(b).hexdigest() if b is not None else "missing"


def dumps(obj) -> bytes:
    """Exactly how run_pipeline.save_json writes the records."""
    return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def load(b: bytes | None):
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def lock_summary(b: bytes | None) -> dict:
    h = load(b)
    if not isinstance(h, list) or not h:
        return {"readable": False, "months": None, "last": None}
    try:
        sms = [d["signal_month"] for d in h]
        weights_ok = all(abs(sum(d[k].values()) - 1) < 1e-8 and min(d[k].values()) >= -1e-12
                         for d in h for k in ("core", "adaptive"))
    except (KeyError, TypeError, AttributeError):
        return {"readable": False, "months": None, "last": None}
    return {"readable": True, "months": len(h), "last": sms[-1],
            "ordered_unique": sms == sorted(set(sms)), "weights_ok": weights_ok}


def lock_diff(before: bytes | None, after: bytes | None) -> dict:
    a = {d["signal_month"]: canon(d) for d in (load(before) or []) if isinstance(d, dict) and "signal_month" in d}
    b = {d["signal_month"]: canon(d) for d in (load(after) or []) if isinstance(d, dict) and "signal_month" in d}
    return {"removed": sorted(set(a) - set(b)), "added": sorted(set(b) - set(a)),
            "changed": sorted(m for m in a if m in b and a[m] != b[m])}


# ---------------------------------------------------------------- the check
def check(repo: str, base: str, head: str, *, event: str, head_ref: str, label: bool, halt_seen: str,
          environment: dict | None = None, approvals: list[dict] | None = None,
          approver_permission: str | None = None, pr_author: str | None = None) -> dict:
    """Returns {"mode", "ok", "rows": [(item, ok|None, detail)], "comparison": {...}}."""
    rows: list[tuple[str, bool | None, str]] = []
    changed = changed_paths(repo, base, head)
    data_changed = [p for p in changed if p.startswith(DATA)]
    requested = head_ref.startswith("restore/") or label
    if not requested:
        rows.append(("일반 PR: docs/data 변경 없음", not data_changed,
                     "변경 없음" if not data_changed else
                     f"변경 {data_changed} → 일반 PR은 운영 데이터를 바꿀 수 없다. 손상 복구라면 "
                     f"handbook ② 12장(브랜치 restore/<복구 ID>, 라벨 {LABEL}, 복구 기록)"))
        return {"mode": "normal", "ok": not data_changed, "rows": rows, "comparison": {}}
    try:
        return _restore(repo, base, head, changed, rows, event=event, head_ref=head_ref, label=label,
                        halt_seen=halt_seen, environment=environment, approvals=approvals,
                        approver_permission=approver_permission, pr_author=pr_author)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError) as e:   # malformed record: fail closed
        rows.append(("복구 기록 형식", False, f"{type(e).__name__}: {e}"[:200]))
        return {"mode": "restore", "ok": False, "rows": rows, "comparison": {}}


def _restore(repo, base, head, changed, rows, *, event, head_ref, label, halt_seen, environment,
             approvals, approver_permission, pr_author) -> dict:
    def row(item, ok, detail=""):
        ok = None if ok is None else bool(ok)
        rows.append((item, ok, detail))
        return ok

    def done(comparison=None):
        return {"mode": "restore", "ok": all(ok is not False for _, ok, _ in rows), "rows": rows,
                "comparison": comparison or {}}

    # R1 restore record: exactly one appended, written the pipeline way
    head_b, base_b = show(repo, head, RESTORES), show(repo, base, RESTORES)
    head_l, base_l = load(head_b), (load(base_b) if base_b is not None else [])
    if not row("복구 기록 파일", isinstance(head_l, list) and isinstance(base_l, list),
               f"{RESTORES} 이 PR에 {'있음' if head_b is not None else '없음'}"):
        return done()
    appended = (len(head_l) == len(base_l) + 1 and canon(head_l[:len(base_l)]) == canon(base_l)
                and head_b == dumps(head_l))
    if not row("복구 기록: 기존 기록 그대로 + 정확히 1건 추가(파이프라인 형식)", appended,
               f"기존 {len(base_l)}건 → {len(head_l)}건"):
        return done()
    rec = head_l[-1]
    missing = [k for k in RECORD_KEYS if k not in rec]
    if not row("복구 기록: 필수 항목", not missing and isinstance(rec.get("files"), list) and rec["files"],
               f"누락 {missing}" if missing else ""):
        return done()
    rid = str(rec["restore_id"])
    row("복구 ID 형식·중복 없음", bool(ID_RE.match(rid)) and rid not in {str(r.get("restore_id")) for r in base_l}, rid)

    # R2 request markers
    row("복구 요청 표시: 브랜치 restore/<복구 ID>", head_ref == f"restore/{rid}", f"브랜치 {head_ref!r}")
    if event == "pull_request":
        row(f"복구 요청 표시: PR 라벨 {LABEL}", bool(label), "")

    # R3 nothing but the restore
    files = [str(f) for f in rec["files"]]
    bad_files = [f for f in files if f not in RESTORABLE]
    row("복구 대상 파일", not bad_files and len(set(files)) == len(files),
        f"{files}" + (f" → 복구 불가 {bad_files}" if bad_files else ""))
    allowed = set(files) | {RESTORES, EVENTS}
    extra = [p for p in changed if p not in allowed]
    row("이 PR의 변경 = 복구 대상 + 복구 기록 + 감사 기록뿐", not extra and {RESTORES, EVENTS} <= set(changed)
        and set(files) <= set(changed),
        f"변경 {changed}" + (f" → 허용 밖 {extra}" if extra else ""))

    # R4 source: a bot-written version on the target's history
    src = rec.get("source") or {}
    commit = str(src.get("commit") or "")
    if not row("복구원 종류 git", src.get("kind") == "git",
               f"kind={src.get('kind')!r}" + ("" if src.get("kind") == "git" else
                                              " → 외부 백업 복구는 CI가 검증할 수 없다(② 12장 예외 절차)")):
        return done()
    ok_sha = bool(SHA_RE.match(commit)) and git(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0
    if not row("복구원 커밋 존재(40자 SHA)", ok_sha, commit):
        return done()
    row("복구원 커밋이 대상 브랜치 이력에 있음", git(repo, "merge-base", "--is-ancestor", commit, base).returncode == 0,
        f"{commit[:7]} ⊂ {base[:7]}")

    comparison = {}
    for f in files:
        before, after, source = show(repo, base, f), show(repo, head, f), show(repo, commit, f)
        writer = git(repo, "log", "-1", "--format=%an", commit, "--", f).stdout.decode().strip()
        row(f"{f}: 복구원 버전을 쓴 것은 {BOT}", writer == BOT, f"마지막 작성자 {writer!r}")
        if f == EVENTS:
            ev = load(after)
            same = (source is not None and isinstance(ev, list) and len(ev) >= 1
                    and canon(ev[:-1]) == canon(load(source)) and after == dumps(ev))
            row(f"{f}: 복구원 버전 + LOCK_RESTORED 1건", same, "")
        else:
            row(f"{f}: 복구본 = 복구원 버전(바이트 동일)", source is not None and after == source,
                f"{sha256(after)[:12]} vs {sha256(source)[:12]}")
        exp_b = ((rec.get("before") or {}).get(f) or {}).get("sha256")
        exp_a = ((rec.get("after") or {}).get(f) or {}).get("sha256")
        row(f"{f}: 기록의 복구 전 SHA-256", exp_b == sha256(before), f"기록 {exp_b} / 실제 {sha256(before)}")
        row(f"{f}: 기록의 복구 후 SHA-256", exp_a == sha256(after), f"기록 {exp_a} / 실제 {sha256(after)}")
        comparison[f] = {"before": {"sha256": sha256(before), "bytes": None if before is None else len(before)},
                         "after": {"sha256": sha256(after), "bytes": None if after is None else len(after)}}

    # R5 the lock itself
    if LOCK in files:
        b, a = show(repo, base, LOCK), show(repo, head, LOCK)
        sb, sa = lock_summary(b), lock_summary(a)
        diff = lock_diff(b, a)
        row("복구본 잠금 구조(읽힘·정렬·중복 없음·비중 합 1)",
            sa["readable"] and sa.get("ordered_unique") and sa.get("weights_ok"), f"{sa}")
        computed = {"before": {"readable": sb["readable"], "months": sb["months"], "last": sb["last"]},
                    "after": {"months": sa["months"], "last": sa["last"]}, "diff": diff}
        row("기록의 개월 수·마지막 신호월·전후 비교", canon(rec.get("lock")) == canon(computed),
            f"계산 {computed}")
        ext = rec.get("external_backup") or {}
        row("외부 백업 SHA-256 = 복구본", ext.get("sha256") == sha256(a) and bool(ext.get("file")),
            f"기록 {ext.get('sha256')} / 복구본 {sha256(a)}")
        comparison["lock"] = computed

    # R6 the audit event
    ev = load(show(repo, head, EVENTS))
    if EVENTS in files:
        prior = load(show(repo, commit, EVENTS))
    else:
        base_ev = show(repo, base, EVENTS)
        prior = [] if base_ev is None else load(base_ev)
    lock_after = load(show(repo, head, LOCK)) or []
    last_month = lock_after[-1]["signal_month"] if lock_after else None
    last_ev = ev[-1] if isinstance(ev, list) and ev else {}
    row("감사 기록: 기존 그대로 + LOCK_RESTORED 1건",
        isinstance(ev, list) and isinstance(prior, list) and len(ev) == len(prior) + 1
        and canon(ev[:-1]) == canon(prior) and last_ev.get("event") == "LOCK_RESTORED"
        and rid in str(last_ev.get("detail", "")) and last_ev.get("signal_month") == last_month
        and bool(last_ev.get("at_utc")),
        f"마지막 사건 {last_ev.get('event')} {last_ev.get('signal_month')} (잠금 마지막 {last_month})")

    # R7 operations stopped
    row("운영자 중지(PDS_HALT) 중", (halt_seen or "").strip().lower() not in HALT_OFF, f"PDS_HALT={halt_seen!r}")

    # R8 explicit approval (pull_request runs; GitHub's own approval record, not the PR's text)
    appr = rec.get("approval") or {}
    who = appr.get("approver")
    row("기록의 승인 근거(승인자·사유에 복구 ID)", bool(who) and rid in str(appr.get("statement", "")),
        f"승인자 {who!r}")
    if event == "pull_request":
        reviewers = (environment or {}).get("required_reviewers") or []
        row(f"보호 환경 {ENVIRONMENT}: 필수 검토자·관리자 우회 끔",
            bool(reviewers) and (environment or {}).get("can_admins_bypass") is False,
            f"검토자 {reviewers}, can_admins_bypass={(environment or {}).get('can_admins_bypass')}")
        ok_appr = [a for a in approvals or [] if a.get("state") == "approved" and ENVIRONMENT in (a.get("environments") or [])
                   and a.get("user") == who and rid in str(a.get("comment") or "")]
        row("이 실행의 환경 승인: 기록의 승인자, 코멘트에 복구 ID", bool(ok_appr),
            f"승인 {[(a.get('user'), a.get('state')) for a in approvals or []]}")
        row("승인자 권한 admin", approver_permission == "admin", f"{approver_permission!r}")
        if ok_appr:
            row("승인 형태", None, "본인 재확인(self-confirmation)" if who == pr_author else "독립 승인(independent)")
    else:
        row("환경 승인", None, f"{event} 실행: PR 실행에서 확인")
    return done(comparison)


# ---------------------------------------------------------------- report / GitHub API / CLI
def report(res: dict, base: str, head: str, guard_from: str) -> str:
    mark = {True: "✅", False: "❌", None: "❔"}
    title = "일반 PR" if res["mode"] == "normal" else "복구 PR(잠금 복구 검증)"
    lines = [f"## 운영 데이터 보호 검사: {title} — {'통과' if res['ok'] else '실패'}", "",
             f"- 비교: `{base[:7]}`(분기점) → `{head[:7]}`(PR head) · 검사 코드: {guard_from}", "",
             "| 항목 | 결과 | 내용 |", "|---|---|---|"]
    lines += [f"| {i} | {mark[ok]} | {str(d).replace('|', '/')} |" for i, ok, d in res["rows"]]
    if res["comparison"]:
        lines += ["", "### 복구 전후 비교", "```json", json.dumps(res["comparison"], ensure_ascii=False, indent=1), "```"]
    return "\n".join(lines)


def api(path: str):
    req = urllib.request.Request(f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def github_approval_facts(repo_slug: str, run_id: str, approver: str | None):
    env = approvals = perm = None
    try:
        e = api(f"/repos/{repo_slug}/environments/{ENVIRONMENT}")
        rv = [x for r in e.get("protection_rules") or [] if r.get("type") == "required_reviewers"
              for x in r.get("reviewers") or []]
        env = {"required_reviewers": [(x.get("reviewer") or {}).get("login") for x in rv],
               "can_admins_bypass": e.get("can_admins_bypass")}
    except Exception as ex:  # noqa: BLE001
        print(f"[warn] environment: {ex}")
    try:
        approvals = [{"user": (a.get("user") or {}).get("login"), "state": a.get("state"), "comment": a.get("comment"),
                      "environments": [x.get("name") for x in a.get("environments") or []]}
                     for a in api(f"/repos/{repo_slug}/actions/runs/{run_id}/approvals") or []]
    except Exception as ex:  # noqa: BLE001
        print(f"[warn] approvals: {ex}")
    if approver:
        try:
            perm = api(f"/repos/{repo_slug}/collaborators/{approver}/permission").get("permission")
        except Exception as ex:  # noqa: BLE001
            print(f"[warn] permission: {ex}")
    return env, approvals, perm


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    ap.add_argument("--head-ref", default="")
    ap.add_argument("--label", default="false")
    ap.add_argument("--pr-author", default="")
    ap.add_argument("--guard-from", default="this file")
    ap.add_argument("--report")
    a = ap.parse_args(argv)
    label = a.label.strip().lower() == "true"
    env = approvals = perm = None
    if a.event == "pull_request" and (a.head_ref.startswith("restore/") or label) and os.environ.get("GH_TOKEN"):
        recs = load(show(a.repo, a.head, RESTORES))
        rec = recs[-1] if isinstance(recs, list) and recs and isinstance(recs[-1], dict) else {}
        env, approvals, perm = github_approval_facts(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
                                                     (rec.get("approval") or {}).get("approver"))
    res = check(a.repo, a.base, a.head, event=a.event, head_ref=a.head_ref, label=label,
                halt_seen=os.environ.get("PDS_HALT_SEEN", ""), environment=env, approvals=approvals,
                approver_permission=perm, pr_author=a.pr_author or None)
    text = report(res, a.base, a.head, a.guard_from)
    print(text)
    if a.report:
        with open(a.report, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
