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
    text = report(rows)
    print(text)
    if args.report:
        with open(args.report, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
