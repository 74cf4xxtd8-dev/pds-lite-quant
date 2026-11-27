"""tools/lock_guard.py: a PR may not change docs/data; a lock restore passes only its own checks.

Every scenario runs on a throwaway git repository built here (bot-written records, a damaging
human commit, a restore branch), never on this repository's docs/data.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H  # noqa: E402

sys.path.insert(0, str(H.ROOT / "tools"))
import lock_guard as LG  # noqa: E402

BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")
OWNER = ("owner", "owner@example.com")
RID = "RESTORE-20261220-1"
APPROVAL = [{"user": "owner", "state": "approved", "comment": f"LOCK-RESTORE 승인 {RID}",
             "environments": [LG.ENVIRONMENT]}]
ENV_OK = {"required_reviewers": ["owner"], "can_admins_bypass": False}


def entry(sm: str, rb: float = 1.0) -> dict:
    core = {"SPY": 0.6, "BIL": 0.4}
    return {"status": "OFFICIAL", "signal_month": sm, "holding_month": sm, "state": "NORMAL" if rb == 1 else "DEFENSIVE",
            "risk_budget": rb, "core": core, "adaptive": {"SPY": 0.6 * rb, "BIL": 1 - 0.6 * rb},
            "sources": {"f2r": "F2R_PUBLISHED"}, "locked_at_utc": f"{sm}-03T22:41:00+00:00", "origin": "LIVE"}


class Repo:
    def __init__(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, who=OWNER) -> str:
        env = dict(os.environ, GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_COMMITTER_NAME=who[0],
                   GIT_COMMITTER_EMAIL=who[1])
        r = subprocess.run(["git", "-C", self.path, "-c", "commit.gpgsign=false", *args], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def write(self, rel: str, data) -> None:
        p = Path(self.path) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else LG.dumps(data))

    def read(self, rel: str):
        return json.loads((Path(self.path) / rel).read_text(encoding="utf-8"))

    def commit(self, msg: str, who=OWNER) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, who=who)
        return self.git("rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    """main: code (owner) -> 3 months (bot) -> 4 months, GOOD (bot) -> RB edited by a person (BAD1)
    -> a month deleted by a person (BAD2).  Restores start from BAD2."""
    r = Repo(tmp_path / "repo")
    r.write("run_pipeline.py", b"# engine\n"); r.write("tools/lock_guard.py", b"# guard\n")
    r.write("handbook/README.md", b"# docs\n")
    r.code = r.commit("code")
    r.write(LG.LOCK, [entry("2026-07"), entry("2026-08"), entry("2026-09")]); r.write(LG.EVENTS, [])
    r.write("docs/data/manual_releases.json", []); r.write("docs/data/portfolio_latest.json", {"v": 1})
    r.first = r.commit("data: 1", who=BOT)
    r.write(LG.LOCK, [entry("2026-07"), entry("2026-08"), entry("2026-09"), entry("2026-10")])
    r.write(LG.EVENTS, [{"signal_month": "2026-10", "event": "LOCKED", "at_utc": "2026-11-02T22:41:00+00:00"}])
    r.write("docs/data/portfolio_latest.json", {"v": 2})
    r.good = r.commit("data: 2", who=BOT)
    h = r.read(LG.LOCK); h[1] = entry("2026-08", 0.7); r.write(LG.LOCK, h)
    r.bad1 = r.commit("edit lock by hand")
    h = r.read(LG.LOCK); del h[2]; r.write(LG.LOCK, h)
    r.bad2 = r.commit("delete a month by hand")
    return r


def run(r: Repo, base: str, head: str, **kw):
    args = dict(event="pull_request", head_ref="feature", label=False, halt_seen="1")
    args.update(kw)
    return LG.check(r.path, base, head, **args)


def failed(res) -> list[str]:
    return [i for i, ok, _ in res["rows"] if ok is False]


# ------------------------------------------------------------------ normal PRs
def branch_from(r: Repo, base: str, name: str) -> None:
    r.git("checkout", "-q", "-B", name, base)


def test_normal_pr_without_data_changes_passes(repo):
    branch_from(repo, repo.good, "feature")
    repo.write("handbook/README.md", b"# docs v2\n"); repo.write("run_pipeline.py", b"# engine v2\n")
    head = repo.commit("docs and code")
    res = run(repo, repo.good, head)
    assert res["mode"] == "normal" and res["ok"], res


@pytest.mark.parametrize("change", ["rb", "delete", "append", "events", "releases", "portfolio", "restores_file"])
def test_normal_pr_changing_operational_data_fails(repo, change):
    branch_from(repo, repo.good, "feature")
    h = repo.read(LG.LOCK)
    if change == "rb":
        h[0] = entry("2026-07", 0.55); repo.write(LG.LOCK, h)
    elif change == "delete":
        repo.write(LG.LOCK, h[:-1])
    elif change == "append":
        repo.write(LG.LOCK, h + [entry("2026-11")])
    elif change == "events":
        repo.write(LG.EVENTS, [])
    elif change == "releases":
        repo.write("docs/data/manual_releases.json", [{"x": 1}])
    elif change == "portfolio":
        repo.write("docs/data/portfolio_latest.json", {"v": 99})
    else:
        repo.write(LG.RESTORES, [{"restore_id": RID}])
    head = repo.commit(f"change {change}")
    res = run(repo, repo.good, head)
    assert res["mode"] == "normal" and not res["ok"]
    assert "docs/data" in res["rows"][0][2] and "12장" in res["rows"][0][2]


# ------------------------------------------------------------------ restore PRs
def make_restore(r: Repo, *, files=(LG.LOCK,), source=None, rid=RID, edit_after=None, record_edit=None,
                 event_edit=None, extra_file=None, two_records=False, skip_event=False, skip_record=False) -> str:
    """A restore branch from BAD2: put `files` back to `source` (default GOOD), append one restore
    record and one LOCK_RESTORED event.  The keyword arguments break one thing at a time."""
    source = source or r.good
    r.git("checkout", "-q", "-B", f"restore/{rid}", r.bad2)
    before = {f: LG.show(r.path, r.bad2, f) for f in files}
    for f in files:
        if f != LG.EVENTS:
            r.write(f, LG.show(r.path, source, f))
    if edit_after:
        edit_after(r)
    events = json.loads(LG.show(r.path, source if LG.EVENTS in files else r.bad2, LG.EVENTS))
    lock_after = r.read(LG.LOCK)
    ev = {"signal_month": lock_after[-1]["signal_month"], "event": "LOCK_RESTORED", "at_utc": "2026-12-20T01:00:00+00:00",
          "detail": f"{rid}: official_history.json <- {source[:7]}"}
    if event_edit:
        event_edit(events, ev)
    if not skip_event:
        events.append(ev)
    r.write(LG.EVENTS, events)
    after = {f: (Path(r.path) / f).read_bytes() for f in files}
    lb, la = LG.show(r.path, r.bad2, LG.LOCK), after.get(LG.LOCK, LG.show(r.path, r.bad2, LG.LOCK))
    sb, sa = LG.lock_summary(lb), LG.lock_summary(la)
    rec = {"restore_id": rid, "incident": "hand edits on main (RB changed, a month deleted)",
           "detected_by": "manifest SHA-256 mismatch", "files": list(files),
           "source": {"kind": "git", "commit": source},
           "before": {f: {"sha256": LG.sha256(before[f])} for f in files},
           "after": {f: {"sha256": LG.sha256(after[f])} for f in files},
           "lock": {"before": {"readable": sb["readable"], "months": sb["months"], "last": sb["last"]},
                    "after": {"months": sa["months"], "last": sa["last"]}, "diff": LG.lock_diff(lb, la)},
           "external_backup": {"file": "official_history_2026-10_good.json", "sha256": LG.sha256(la)},
           "approval": {"approver": "owner", "statement": f"LOCK-RESTORE 승인 {rid}"}}
    if record_edit:
        record_edit(rec)
    if not skip_record:
        r.write(LG.RESTORES, [rec, dict(rec, restore_id=rid + "0")] if two_records else [rec])
    if extra_file:
        r.write(extra_file, b"# changed in the restore PR\n")
    return r.commit("restore")


def restore_ok(r: Repo, head: str, **kw):
    args = dict(event="pull_request", head_ref=f"restore/{RID}", label=True, halt_seen="1", environment=ENV_OK,
                approvals=APPROVAL, approver_permission="admin", pr_author="owner")
    args.update(kw)
    return LG.check(r.path, r.bad2, head, **args)


def test_valid_restore_passes_with_full_comparison(repo):
    head = make_restore(repo)
    res = restore_ok(repo, head)
    assert res["mode"] == "restore" and res["ok"], failed(res)
    cmp_ = res["comparison"]["lock"]
    assert cmp_["before"] == {"readable": True, "months": 3, "last": "2026-10"}
    assert cmp_["after"] == {"months": 4, "last": "2026-10"}
    assert cmp_["diff"] == {"removed": [], "added": ["2026-09"], "changed": ["2026-08"]}
    assert any(i == "승인 형태" and "self-confirmation" in d for i, _, d in res["rows"])
    assert LG.show(repo.path, head, LG.LOCK) == LG.show(repo.path, repo.good, LG.LOCK)   # byte-identical


def test_restore_push_run_defers_approval_to_the_pr_run(repo):
    head = make_restore(repo)
    res = restore_ok(repo, head, event="push", label=False, environment=None, approvals=None, approver_permission=None)
    assert res["ok"] and any(i == "환경 승인" and ok is None for i, ok, _ in res["rows"])


def test_restore_of_the_audit_record_too(repo):
    def wipe_events(r):          # the events file was also damaged on main before the restore
        pass
    repo.git("checkout", "-q", "main")
    repo.write(LG.EVENTS, [{"signal_month": "x", "event": "FORGED", "at_utc": "z"}])
    repo.bad2 = repo.commit("damage events")
    head = make_restore(repo, files=(LG.LOCK, LG.EVENTS), edit_after=wipe_events)
    res = restore_ok(repo, head)
    assert res["ok"], failed(res)


BREAKS = {
    "no label": dict(run=dict(label=False)),
    "branch/id mismatch": dict(run=dict(head_ref="restore/RESTORE-20261220-9")),
    "no record": dict(make=dict(skip_record=True)),
    "two records": dict(make=dict(two_records=True)),
    "missing field": dict(make=dict(record_edit=lambda rec: rec.pop("incident"))),
    "malformed source": dict(make=dict(record_edit=lambda rec: rec.update(source="git"))),
    "bad id": dict(make=dict(record_edit=lambda rec: rec.update(restore_id="restore-1")), run=dict(head_ref="restore/restore-1")),
    "restored bytes edited": dict(make=dict(edit_after=lambda r: r.write(LG.LOCK, LG.show(r.path, r.good, LG.LOCK) + b" "))),
    "source written by a person": dict(make=dict(source="bad1")),
    "source not on main": dict(make=dict(source="side")),
    "external source": dict(make=dict(record_edit=lambda rec: rec["source"].update(kind="external_backup"))),
    "before sha wrong": dict(make=dict(record_edit=lambda rec: rec["before"][LG.LOCK].update(sha256="0" * 64))),
    "after sha wrong": dict(make=dict(record_edit=lambda rec: rec["after"][LG.LOCK].update(sha256="0" * 64))),
    "month count wrong": dict(make=dict(record_edit=lambda rec: rec["lock"]["after"].update(months=5))),
    "diff understated": dict(make=dict(record_edit=lambda rec: rec["lock"]["diff"].update(changed=[]))),
    "external backup mismatch": dict(make=dict(record_edit=lambda rec: rec["external_backup"].update(sha256="1" * 64))),
    "no approval reason": dict(make=dict(record_edit=lambda rec: rec["approval"].update(statement="ok"))),
    "code changed too": dict(make=dict(extra_file="tools/lock_guard.py")),
    "engine changed too": dict(make=dict(extra_file="run_pipeline.py")),
    "other data changed": dict(make=dict(extra_file="docs/data/portfolio_latest.json")),
    "no event": dict(make=dict(skip_event=True)),
    "event without id": dict(make=dict(event_edit=lambda evs, ev: ev.update(detail="restored"))),
    "event wrong month": dict(make=dict(event_edit=lambda evs, ev: ev.update(signal_month="2026-09"))),
    "older events altered": dict(make=dict(event_edit=lambda evs, ev: evs.clear())),
    "two events": dict(make=dict(event_edit=lambda evs, ev: evs.append(dict(ev)))),
    "not halted": dict(run=dict(halt_seen="")),
    "halt off": dict(run=dict(halt_seen="false")),
    "no approval": dict(run=dict(approvals=[])),
    "approval by someone else": dict(run=dict(approvals=[dict(APPROVAL[0], user="mallory")])),
    "approval rejected": dict(run=dict(approvals=[dict(APPROVAL[0], state="rejected")])),
    "approval comment without id": dict(run=dict(approvals=[dict(APPROVAL[0], comment="ok")])),
    "approval for another environment": dict(run=dict(approvals=[dict(APPROVAL[0], environments=["pds-manual-release"])])),
    "environment without reviewers": dict(run=dict(environment={"required_reviewers": [], "can_admins_bypass": False})),
    "admins may bypass": dict(run=dict(environment={"required_reviewers": ["owner"], "can_admins_bypass": True})),
    "environment missing": dict(run=dict(environment=None)),
    "approver not admin": dict(run=dict(approver_permission="write")),
}


@pytest.mark.parametrize("case", sorted(BREAKS))
def test_restore_rejected_when_anything_is_off(repo, case):
    spec = BREAKS[case]
    make = dict(spec.get("make", {}))
    if make.get("source") == "bad1":
        make["source"] = repo.bad1
    elif make.get("source") == "side":                 # a bot-written version that never reached main
        repo.git("checkout", "-q", "-B", "side", repo.good)
        h = repo.read(LG.LOCK); h[0] = entry("2026-07", 0.6); repo.write(LG.LOCK, h)
        make["source"] = repo.commit("data: side", who=BOT)
    head = make_restore(repo, **make)
    res = restore_ok(repo, head, **spec.get("run", {}))
    assert res["mode"] == "restore" and not res["ok"], (case, res["rows"])


def test_label_or_branch_alone_never_relaxes_the_check(repo):
    """A restore marker on a PR that changes code (not data) still fails: no record, no restore."""
    branch_from(repo, repo.good, f"restore/{RID}")
    repo.write("run_pipeline.py", b"# sneaky\n")
    head = repo.commit("code under a restore branch")
    for kw in (dict(head_ref=f"restore/{RID}"), dict(label=True), dict(head_ref=f"restore/{RID}", label=True)):
        res = run(repo, repo.good, head, **kw)
        assert res["mode"] == "restore" and not res["ok"]


def test_restore_record_without_markers_is_a_normal_pr_and_fails(repo):
    head = make_restore(repo)
    res = run(repo, repo.bad2, head, head_ref="feature", label=False)
    assert res["mode"] == "normal" and not res["ok"]


def test_cli_exit_codes(repo, tmp_path):
    good = make_restore(repo)
    out = tmp_path / "r.md"
    env = dict(os.environ, PDS_HALT_SEEN="1")
    env.pop("GH_TOKEN", None)
    ok = subprocess.run([sys.executable, str(H.ROOT / "tools" / "lock_guard.py"), "--repo", repo.path,
                         "--base", repo.bad2, "--head", good, "--event", "push", "--head-ref", f"restore/{RID}",
                         "--report", str(out)], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "통과" in out.read_text(encoding="utf-8") and "복구 전후 비교" in out.read_text(encoding="utf-8")
    bad = subprocess.run([sys.executable, str(H.ROOT / "tools" / "lock_guard.py"), "--repo", repo.path,
                          "--base", repo.bad2, "--head", good, "--event", "push", "--head-ref", "feature"],
                         capture_output=True, text=True, env=env)
    assert bad.returncode == 1 and "실패" in bad.stdout


def test_this_branch_does_not_change_operational_data():
    """The real repository: this PR/branch changes nothing under docs/data (normal mode)."""
    base = H.pr_merge_base_sha()
    head = H._sha(os.environ.get("PDS_PR_HEAD_SHA") or "HEAD")
    if not base or not head:
        pytest.skip("no merge-base with the target branch in this checkout")
    res = LG.check(str(H.ROOT), base, head, event="push", head_ref="ci", label=False, halt_seen="")
    assert res["mode"] == "normal" and res["ok"], res["rows"]


def test_ci_wiring():
    tw = yaml.safe_load((H.ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    gate, guard = tw["jobs"]["restore-gate"], tw["jobs"]["lock-guard"]
    assert gate["environment"] == LG.ENVIRONMENT and gate["permissions"] == {}
    assert "pull_request" in gate["if"] and "restore/" in gate["if"] and LG.LABEL in gate["if"]
    assert guard["permissions"] == {"contents": "read", "actions": "read"}
    assert set(guard["needs"]) == {"baseline", "restore-gate"} and "!cancelled()" in guard["if"]
    step = guard["steps"][-1]
    assert 'git show "$TARGET_SHA:tools/lock_guard.py"' in step["run"]          # judged by the target's copy
    assert '--base "$MERGE_BASE" --head "$HEAD_SHA"' in step["run"]             # the PR's own changes only
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert step["env"]["PDS_HALT_SEEN"] == "${{ vars.PDS_HALT || '' }}"
    assert "github.event" not in step["run"]                                    # inputs via env only


def test_ops_audit_flags_human_data_commits():
    import ops_audit as OA
    bot, owner = {"login": OA.BOT}, {"login": "owner"}
    commits = [{"sha": "a" * 40, "author": bot, "commit": {"message": "data: x"}},
               {"sha": "b" * 40, "author": owner, "commit": {"message": "Merge pull request #9"}},
               {"sha": "c" * 40, "author": owner, "commit": {"message": "Merge pull request #10"}}]
    files = {"a" * 40: ["docs/data/official_history.json"], "b" * 40: ["docs/data/official_history.json"],
             "c" * 40: ["docs/data/official_history.json", "docs/data/lock_restores.json"]}
    merged = "2026-12-20T00:00:00Z"
    pulls = {"b" * 40: [{"merged_at": merged, "head": {"ref": "fix-something"}}],
             "c" * 40: [{"merged_at": merged, "head": {"ref": f"restore/{RID}"}}]}
    rows = {i: (ok, d) for i, ok, d in OA.main_commit_provenance(commits, files, pulls)}
    row = rows[f"main 최근 {OA.PROVENANCE_DAYS}일: 사람 커밋의 docs/data 변경은 잠금 복구 PR뿐"]
    assert row[0] is False and "bbbbbbb" in row[1] and "ccccccc" not in row[1]
    clean = {i: ok for i, ok, _ in OA.main_commit_provenance(commits[:1] + commits[2:], files, pulls)}
    assert clean[f"main 최근 {OA.PROVENANCE_DAYS}일: 사람 커밋의 docs/data 변경은 잠금 복구 PR뿐"] is True


def test_ops_audit_checks_the_restore_environment():
    import ops_audit as OA
    assert OA.RESTORE_ENVIRONMENT == LG.ENVIRONMENT
    missing = OA.restore_environment_rows({"_http_error": 404})
    assert missing[0][1] is False and "항상 실패" in missing[0][2]
    env = {"name": LG.ENVIRONMENT, "can_admins_bypass": False,
           "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False,
                                 "reviewers": [{"reviewer": {"login": "owner"}}]}]}
    rows = {i: (ok, d) for i, ok, d in OA.restore_environment_rows(env)}
    assert rows[f"환경 {LG.ENVIRONMENT}: 필수 검토자"][0] is True
    assert rows[f"환경 {LG.ENVIRONMENT}: 관리자 우회 끔"][0] is True
    assert "본인 재확인" in rows[f"환경 {LG.ENVIRONMENT}: 승인 형태"][1]
    bypass = {i: ok for i, ok, _ in OA.restore_environment_rows(dict(env, can_admins_bypass=True))}
    assert bypass[f"환경 {LG.ENVIRONMENT}: 관리자 우회 끔"] is False
    empty = {i: ok for i, ok, _ in OA.restore_environment_rows(dict(env, protection_rules=[]))}
    assert empty[f"환경 {LG.ENVIRONMENT}: 필수 검토자"] is False
