"""Gather who requested and who approved a manual release, from the GitHub API (GITHUB_TOKEN).

Runs in the update-portfolio workflow only when release_month is set, after the release-gate job
(environment pds-manual-release) was approved.  Writes release_auth.json; run_pipeline.py decides
(release_auth_problems): run on main, requester has admin permission, the environment has required
reviewers, and this run carries an approval for it.  Any API error leaves a field empty, which
rejects the release (fail closed).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ENVIRONMENT = "pds-manual-release"


def api(path: str) -> object:
    req = urllib.request.Request(f"{os.environ.get('GITHUB_API_URL', 'https://api.github.com')}{path}", headers={
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def environment_summary(env: dict | None) -> dict | None:
    if not env:
        return None
    reviewers, prevent_self = [], None
    for rule in env.get("protection_rules") or []:
        if rule.get("type") == "required_reviewers":
            prevent_self = rule.get("prevent_self_review")
            for rv in rule.get("reviewers") or []:
                who = rv.get("reviewer") or {}
                reviewers.append(who.get("login") or who.get("slug") or who.get("name"))
    pol = env.get("deployment_branch_policy") or {}
    return {"name": env.get("name"), "required_reviewers": reviewers, "prevent_self_review": prevent_self,
            "can_admins_bypass": env.get("can_admins_bypass"),
            "branch_policy": None if not pol else ("protected" if pol.get("protected_branches") else "custom")}


def gather(repo: str, run_id: str, actor: str, ref: str, month: str) -> dict:
    out = {"release_month": month, "ref": ref, "actor": actor, "actor_permission": None,
           "environment": None, "approvals": [], "errors": []}
    try:
        out["actor_permission"] = api(f"/repos/{repo}/collaborators/{actor}/permission").get("permission")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"permission: {e}")
    try:
        out["environment"] = environment_summary(api(f"/repos/{repo}/environments/{ENVIRONMENT}"))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"environment: {e}")
    try:
        for a in api(f"/repos/{repo}/actions/runs/{run_id}/approvals") or []:
            out["approvals"].append({"user": (a.get("user") or {}).get("login"), "state": a.get("state"),
                                     "comment": a.get("comment"),
                                     "environments": [e.get("name") for e in a.get("environments") or []],
                                     "at_utc": None})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"approvals: {e}")
    return out


def main() -> None:
    month = os.environ.get("MANUAL_RELEASE_MONTH", "").strip()
    if not month:
        return
    auth = gather(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
                  os.environ.get("GITHUB_TRIGGERING_ACTOR") or os.environ["GITHUB_ACTOR"],
                  os.environ.get("GITHUB_REF", ""), month)
    Path(os.environ.get("RELEASE_AUTH_PATH", "release_auth.json")).write_text(
        json.dumps(auth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: auth[k] for k in ("actor", "actor_permission", "ref", "approvals", "errors")},
                     ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
