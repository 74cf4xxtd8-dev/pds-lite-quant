"""Read-only audit of who can change PDS-Lite operations, against the README recommendations.

Uses GITHUB_TOKEN (read) for what it can see: collaborators and their permission, rules that apply
to main, the pds-manual-release environment, and the permissions each workflow file declares.
Actions settings (default token permission, allowed actions) and variable values are not
readable with GITHUB_TOKEN; the report says so and shows PDS_HALT as this run saw it.
Report only: exit code is always 0.

    GH_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/ops_audit.py [--report out.md]
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = "pds-manual-release"
WRITE_SCOPES_ALLOWED = {"update.yml": {"contents"}}          # the only workflow that may write


def api(path: str):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code}


def workflow_permissions(wf_dir: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(wf_dir.glob("*.yml")):
        wf = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        perms = {"workflow": wf.get("permissions")}
        for name, job in (wf.get("jobs") or {}).items():
            if "permissions" in job:
                perms[name] = job["permissions"]
        out[f.name] = perms
    return out


def write_scopes(perms: dict) -> set[str]:
    scopes = set()
    for p in perms.values():
        if p in ("write-all",):
            scopes.add("*")
        elif isinstance(p, dict):
            scopes |= {k for k, v in p.items() if v == "write"}
    return scopes


BOT = "github-actions[bot]"
PROVENANCE_DAYS = 35


def main_commit_provenance(commits: list[dict], files: dict[str, list[str]],
                           pulls: dict[str, list[dict]]) -> list[tuple[str, bool | None, str]]:
    """Detective control for what the main rules cannot prevent: the bot may only have changed
    docs/data, and every other change on main should have come through a merged pull request."""
    bot_bad, direct = [], []
    for c in commits:
        sha = c["sha"]
        who = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("name")
        msg = (c.get("commit", {}).get("message") or "").splitlines()[0][:60]
        if who == BOT:
            outside = [f for f in files.get(sha, []) if not f.startswith("docs/data/")]
            if outside:
                bot_bad.append(f"{sha[:7]} {outside[:3]}")
        elif not any(p.get("merged_at") for p in pulls.get(sha, [])):
            direct.append(f"{sha[:7]} {who}: {msg}")
    n_bot = sum(1 for c in commits if ((c.get("author") or {}).get("login")
                                       or (c.get("commit", {}).get("author") or {}).get("name")) == BOT)
    # docs/data is bot-only; a person's commit there is acceptable only as part of a lock restore PR
    # (branch restore/<id>, verified by tools/lock_guard.py).  Catches a PR that switched the CI check off.
    human_data, restores = [], []
    for c in commits:
        sha = c["sha"]
        who = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("name")
        data = [f for f in files.get(sha, []) if f.startswith("docs/data/")]
        if who == BOT or not data:
            continue
        refs = [(p.get("head") or {}).get("ref", "") for p in pulls.get(sha, []) if p.get("merged_at")]
        (restores if any(r.startswith("restore/RESTORE-") for r in refs) else human_data).append(
            f"{sha[:7]} {who} {data[:3]}" + (f" (PR {refs[0]})" if refs else ""))
    rows_data = [(f"main 최근 {PROVENANCE_DAYS}일: 사람 커밋의 docs/data 변경은 잠금 복구 PR뿐", not human_data,
                  "; ".join(human_data[:6]) if human_data else
                  (f"잠금 복구 PR 경유 {len(restores)}건: " + "; ".join(restores[:4])) if restores else "없음")]
    return [(f"main 최근 {PROVENANCE_DAYS}일: 봇 커밋은 docs/data만 변경", not bot_bad,
             "; ".join(bot_bad) if bot_bad else f"봇 커밋 {n_bot}건 모두 docs/data"),
            (f"main 최근 {PROVENANCE_DAYS}일: 봇 외 변경은 모두 PR 병합 경유", not direct,
             "; ".join(direct[:8]) + (f" 외 {len(direct) - 8}건" if len(direct) > 8 else "") if direct
             else f"봇 외 커밋 {len(commits) - n_bot}건 모두 PR 경유")] + rows_data


def fetch_provenance(repo: str):
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=PROVENANCE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = api(f"/repos/{repo}/commits?sha=main&since={since}&per_page=100")
    if not isinstance(commits, list):
        return None
    files, pulls = {}, {}
    for c in commits:
        who = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("name")
        d = api(f"/repos/{repo}/commits/{c['sha']}")
        files[c["sha"]] = [f["filename"] for f in d.get("files", [])] if isinstance(d, dict) else []
        if who != BOT:
            pr = api(f"/repos/{repo}/commits/{c['sha']}/pulls")
            pulls[c["sha"]] = pr if isinstance(pr, list) else []
    return commits, files, pulls


def evaluate(collab, main_branch, main_rules, env, wf_perms, halt_seen) -> list[tuple[str, bool | None, str]]:
    """(item, ok? (None = cannot verify), detail)."""
    rows = []
    if isinstance(collab, list):
        people = [f"{c['login']}({c.get('role_name') or ('admin' if c.get('permissions', {}).get('admin') else 'write')})"
                  for c in collab]
        rows.append(("협업자는 소유자 1명(admin)뿐", len(collab) == 1 and bool(collab[0].get("permissions", {}).get("admin")),
                     ", ".join(people)))
    else:
        rows.append(("협업자는 소유자 1명(admin)뿐", None, f"조회 불가 {collab}"))
    if isinstance(main_rules, list):
        types = sorted({r.get("type") for r in main_rules})
        rows.append(("main: 삭제 금지(deletion)", "deletion" in types, f"적용 규칙 {types or '없음'}"))
        rows.append(("main: 강제 푸시 금지(non_fast_forward)", "non_fast_forward" in types, ""))
        blocking = [t for t in types if t in ("pull_request", "required_status_checks", "update")]
        rows.append(("main: 봇 데이터 커밋을 막는 규칙 없음", not blocking,
                     f"막는 규칙 {blocking}" if blocking else "pull_request·status check·update 규칙 없음"))
    else:
        rows.append(("main 규칙", None, f"조회 불가 {main_rules}"))
    if isinstance(main_branch, dict) and "protected" in main_branch:
        rows.append(("main 보호 표시(protected)", bool(main_branch["protected"]), ""))
    if isinstance(env, dict) and env.get("name"):
        rv = [r for r in env.get("protection_rules") or [] if r.get("type") == "required_reviewers"]
        reviewers = [(x.get("reviewer") or {}).get("login") for r in rv for x in r.get("reviewers") or []]
        rows.append((f"환경 {ENVIRONMENT}: 필수 검토자", bool(reviewers), f"{reviewers}"))
        psr = any(r.get("prevent_self_review") for r in rv)
        rows.append((f"환경 {ENVIRONMENT}: 승인 형태", None,
                     "독립 2인 승인(prevent self-review 켬)" if psr and len(reviewers) > 1 else
                     "본인 재확인(요청자=승인자 가능) — 독립 승인 아님" if not psr else
                     "prevent self-review 켬, 검토자 1명 → 그 검토자가 요청하면 아무도 승인 못 함"))
        rows.append((f"환경 {ENVIRONMENT}: 관리자 우회 끔", env.get("can_admins_bypass") is False,
                     f"can_admins_bypass={env.get('can_admins_bypass')}"))
        pol = env.get("deployment_branch_policy") or {}
        rows.append((f"환경 {ENVIRONMENT}: 배포 브랜치 제한", bool(pol), f"{pol or '모든 브랜치'}"))
    else:
        rows.append((f"환경 {ENVIRONMENT} 존재", False, "없음 → 수동 해제는 항상 거부됨(fail-closed)"))
    for name, perms in wf_perms.items():
        extra = write_scopes(perms) - WRITE_SCOPES_ALLOWED.get(name, set())
        rows.append((f"워크플로 {name}: 쓰기 권한 최소", not extra, f"{perms}" + (f" → 초과 {sorted(extra)}" if extra else "")))
    rows.append(("PDS_HALT (이 실행이 본 값)", None, repr(halt_seen)))
    rows.append(("Actions 설정(기본 토큰 권한 읽기 전용, GitHub 액션만 허용, PR 생성·승인 금지, 포크 승인)", None,
                 "GITHUB_TOKEN으로 조회 불가 → README 체크리스트로 직접 확인"))
    return rows


def report(rows) -> str:
    mark = {True: "✅", False: "⚠️", None: "❔"}
    lines = ["## PDS-Lite 운영 권한 점검", "", "| 항목 | 상태 | 내용 |", "|---|---|---|"]
    lines += [f"| {item} | {mark[ok]} | {detail} |" for item, ok, detail in rows]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report")
    args = ap.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    rows = evaluate(api(f"/repos/{repo}/collaborators"), api(f"/repos/{repo}/branches/main"),
                    api(f"/repos/{repo}/rules/branches/main"), api(f"/repos/{repo}/environments/{ENVIRONMENT}"),
                    workflow_permissions(ROOT / ".github" / "workflows"), os.environ.get("PDS_HALT_SEEN", ""))
    prov = fetch_provenance(repo)
    rows += main_commit_provenance(*prov) if prov else [("main 커밋 출처", None, "조회 불가")]
    text = report(rows)
    print(text)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
