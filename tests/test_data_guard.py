"""data-guard.yml: the operational-data guard runs main's workflow and main's tools/lock_guard.py on
pull_request_target, reads the PR only as git objects and never executes the PR's code.

GitHub's own part (pull_request_target loads the workflow from the default branch) cannot run in
this test suite; it is shown on GitHub by the job summary of every data-guard run.  What is tested
here is everything the workflow itself does: its step scripts are taken from the YAML and run with
bash against a stand-in for GitHub (a bare repository served over file:// with refs/pull/N/head,
and a local HTTP server for the approval API), for the scenarios
    normal PR / docs/data change / workflow change / checker change / restore with approval,
plus shell-injection payloads in every event value and static security assertions.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H  # noqa: E402
from test_lock_guard import BOT, OWNER, RID, Repo, entry, make_restore  # noqa: E402

sys.path.insert(0, str(H.ROOT / "tools"))
import lock_guard as LG  # noqa: E402

WF_PATH = H.ROOT / ".github" / "workflows" / "data-guard.yml"
WF_TEXT = WF_PATH.read_text(encoding="utf-8")
WF = yaml.safe_load(WF_TEXT)
TOKEN = "ghs_readonly_job_token_for_tests"


# ---------------------------------------------------------------- a stand-in for GitHub
@pytest.fixture()
def gh(tmp_path):
    """origin = <srv>/owner/repo.git.  main: the real checker and workflow, data by the bot, then two
    hand edits of the lock (so a restore PR has something to repair)."""
    srv = tmp_path / "srv"
    bare = srv / "owner" / "repo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    r = Repo(tmp_path / "work")
    r.write("tools/lock_guard.py", (H.ROOT / "tools" / "lock_guard.py").read_bytes())
    r.write(".github/workflows/data-guard.yml", WF_PATH.read_bytes())
    r.write(".github/workflows/tests.yml", (H.ROOT / ".github" / "workflows" / "tests.yml").read_bytes())
    r.write("run_pipeline.py", b"# engine\n"); r.write("README.md", b"# readme\n")
    r.code = r.commit("code")
    r.write(LG.LOCK, [entry("2026-07"), entry("2026-08"), entry("2026-09")]); r.write(LG.EVENTS, [])
    r.write("docs/data/manual_releases.json", []); r.write("docs/data/portfolio_latest.json", {"v": 1})
    r.first = r.commit("data: 1", who=BOT)
    r.write(LG.LOCK, [entry("2026-07"), entry("2026-08"), entry("2026-09"), entry("2026-10")])
    r.write(LG.EVENTS, [{"signal_month": "2026-10", "event": "LOCKED", "at_utc": "2026-11-02T22:41:00+00:00"}])
    r.good = r.commit("data: 2", who=BOT)
    h = r.read(LG.LOCK); h[1] = entry("2026-08", 0.7); r.write(LG.LOCK, h)
    r.bad1 = r.commit("edit lock by hand")
    h = r.read(LG.LOCK); del h[2]; r.write(LG.LOCK, h)
    r.bad2 = r.commit("delete a month by hand")
    r.main = r.bad2
    r.git("remote", "add", "origin", str(bare))
    r.git("push", "-q", "origin", "main")
    r.srv, r.tmp, r.runs = srv, tmp_path, 0
    return r


def open_pr(r: Repo, n: int, changes: dict, base: str = "main", msg: str = "pr") -> str:
    """Commit `changes` (path -> bytes | json | None = delete) on top of `base`, publish as refs/pull/n/head."""
    r.git("checkout", "-q", "-B", f"pr{n}", base)
    for path, data in changes.items():
        if data is None:
            r.git("rm", "-q", path)
        else:
            r.write(path, data)
    head = r.commit(msg)
    r.git("push", "-q", "-f", "origin", f"{head}:refs/pull/{n}/head")
    r.git("checkout", "-q", "main")
    return head


def event(r: Repo, n: int, head: str, **over) -> dict:
    """The pull_request_target event values the workflow reads (what GitHub would put in ${{ }})."""
    ctx = dict(number=str(n), base_sha=r.main, base_ref="main", head_sha=head, default_branch="main",
               workflow_ref="owner/repo/.github/workflows/data-guard.yml@refs/heads/main", head_ref=f"feature-{n}",
               labels=[], user="contributor", halt="", workflow_sha=r.main, event_name="pull_request_target")
    ctx.update(over)
    return ctx


def restore_requested(ctx) -> bool:
    return ctx["head_ref"].startswith("restore/") or "lock-restore" in ctx["labels"]


def resolve(value, ctx) -> str:
    """Evaluate the ${{ }} expressions this workflow uses.  An expression not listed here fails the
    test, so a new event input cannot slip into the workflow without a matching test."""
    if not isinstance(value, str) or "${{" not in value:
        return str(value)
    expr = " ".join(value.split())
    table = {
        "${{ github.event.pull_request.number }}": ctx["number"],
        "${{ github.event.pull_request.base.sha }}": ctx["base_sha"],
        "${{ github.event.pull_request.base.ref }}": ctx["base_ref"],
        "${{ github.event.pull_request.head.sha }}": ctx["head_sha"],
        "${{ github.event.repository.default_branch }}": ctx["default_branch"],
        "${{ github.workflow_ref }}": ctx["workflow_ref"],
        "${{ github.event.pull_request.head.ref }}": ctx["head_ref"],
        "${{ contains(github.event.pull_request.labels.*.name, 'lock-restore') }}":
            "true" if "lock-restore" in ctx["labels"] else "false",
        "${{ github.event.pull_request.user.login }}": ctx["user"],
        "${{ vars.PDS_HALT || '' }}": ctx["halt"],
        "${{ (startsWith(github.event.pull_request.head.ref, 'restore/') || "
        "contains(github.event.pull_request.labels.*.name, 'lock-restore')) && github.token || '' }}":
            TOKEN if restore_requested(ctx) else "",
    }
    assert expr in table, f"unmapped expression in data-guard.yml: {expr}"
    return table[expr]


def run_guard(r: Repo, ctx: dict, api_base: str | None = None) -> dict:
    """Run the guard job's steps as the runner would (bash -e -o pipefail, step env over job env),
    stopping at the first failing step.  Returns {"failed": step index | None, "summary", "out"}."""
    r.runs += 1
    home = r.tmp / f"runner{r.runs}"
    for d in ("temp", "home", "workspace"):
        (home / d).mkdir(parents=True)
    summary = home / "summary.md"; summary.write_text("")
    job = WF["jobs"]["guard"]
    env = {"PATH": os.environ["PATH"], "HOME": str(home / "home"), "LANG": "C.UTF-8",
           "RUNNER_TEMP": str(home / "temp"), "GITHUB_STEP_SUMMARY": str(summary),
           "GITHUB_SERVER_URL": f"file://{r.srv}", "GITHUB_REPOSITORY": "owner/repo",
           "GITHUB_WORKFLOW_SHA": ctx["workflow_sha"], "GITHUB_EVENT_NAME": ctx["event_name"],
           "GITHUB_RUN_ID": "4242", "GITHUB_API_URL": api_base or "http://127.0.0.1:9"}
    env.update({k: resolve(v, ctx) for k, v in job["env"].items()})
    out = []
    for i, step in enumerate(job["steps"]):
        script = home / f"step{i}.sh"; script.write_text(step["run"], encoding="utf-8")
        e = dict(env, **{k: resolve(v, ctx) for k, v in (step.get("env") or {}).items()})
        p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], env=e,
                           cwd=home / "workspace", capture_output=True, text=True, timeout=120)
        out.append(p.stdout + p.stderr)
        if p.returncode != 0:
            return {"failed": i, "summary": summary.read_text(encoding="utf-8"), "out": out, "env": e}
    return {"failed": None, "summary": summary.read_text(encoding="utf-8"), "out": out, "env": e}


GUARD_STEP = 2     # the step that runs main's checker


def proof_rows(summary: str) -> dict:
    rows = {}
    for line in summary.splitlines():
        m = re.match(r"\| (.+?) \| (.+) \|$", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


# ---------------------------------------------------------------- the four scenarios
def test_normal_pr_passes_and_proves_mains_checker(gh):
    head = open_pr(gh, 1, {"README.md": b"# readme v2\n", "run_pipeline.py": b"# engine v2\n"})
    res = run_guard(gh, event(gh, 1, head))
    assert res["failed"] is None, res["out"]
    rows = proof_rows(res["summary"])
    main_blob = LG.git(gh.path, "rev-parse", f"{gh.main}:tools/lock_guard.py").stdout.decode().strip()
    assert rows["이벤트"] == "`pull_request_target`"
    assert rows["워크플로 커밋이 기본 브랜치 이력에 있음"] == "true"
    assert rows["실행할 lock_guard.py blob"].startswith(f"`{main_blob}`")
    assert rows["main의 lock_guard.py blob"] == f"`{main_blob}`"
    assert rows["PR head의 lock_guard.py blob"].endswith("같음")
    assert "일반 PR — 통과" in res["summary"]


@pytest.mark.parametrize("change", ["lock", "events", "portfolio", "new file"])
def test_docs_data_change_fails(gh, change):
    lock = gh.read(LG.LOCK)
    data = {"lock": {LG.LOCK: lock + [entry("2026-11")]}, "events": {LG.EVENTS: []},
            "portfolio": {"docs/data/portfolio_latest.json": {"v": 99}},
            "new file": {"docs/data/extra.json": {"x": 1}}}[change]
    head = open_pr(gh, 2, data)
    res = run_guard(gh, event(gh, 2, head))
    assert res["failed"] == GUARD_STEP and "일반 PR — 실패" in res["summary"]


NEUTERED_WORKFLOW = b"""name: data-guard
on: pull_request_target
permissions: {}
jobs:
  guard:
    name: data-guard
    runs-on: ubuntu-latest
    steps:
      - run: exit 0
"""


@pytest.mark.parametrize("how", ["neutered", "deleted"])
def test_workflow_change_does_not_change_the_verdict(gh, how):
    """The PR rewrites (or deletes) data-guard.yml and tests.yml and changes the lock: still judged by
    main's workflow and checker, and the summary says the PR's workflow was not used."""
    lock = gh.read(LG.LOCK)
    wf = NEUTERED_WORKFLOW if how == "neutered" else None
    head = open_pr(gh, 3, {".github/workflows/data-guard.yml": wf, ".github/workflows/tests.yml": None,
                           LG.LOCK: lock[:-1]})
    res = run_guard(gh, event(gh, 3, head))
    assert res["failed"] == GUARD_STEP and "일반 PR — 실패" in res["summary"]
    row = proof_rows(res["summary"])["main / PR의 data-guard.yml blob"]
    assert "다름 → 이번 실행에 쓰이지 않음" in row and ("(없음)" in row) == (how == "deleted")


def test_workflow_only_change_passes_the_data_check_but_is_reported(gh):
    head = open_pr(gh, 4, {".github/workflows/data-guard.yml": NEUTERED_WORKFLOW})
    res = run_guard(gh, event(gh, 4, head))
    assert res["failed"] is None
    assert "다름 → 이번 실행에 쓰이지 않음" in proof_rows(res["summary"])["main / PR의 data-guard.yml blob"]


def test_checker_change_is_never_executed(gh):
    """The PR replaces tools/lock_guard.py with code that would pass everything and leave a marker
    if it ever ran, and changes the lock.  main's checker rejects it; the marker never appears."""
    marker = gh.tmp / "PR_CODE_RAN"
    evil = f"import pathlib, sys\npathlib.Path({str(marker)!r}).write_text('ran')\nsys.exit(0)\n".encode()
    lock = gh.read(LG.LOCK); lock[0] = entry("2026-07", 0.5)
    head = open_pr(gh, 5, {"tools/lock_guard.py": evil, LG.LOCK: lock})
    res = run_guard(gh, event(gh, 5, head))
    assert res["failed"] == GUARD_STEP and "일반 PR — 실패" in res["summary"]
    assert not marker.exists()
    rows = proof_rows(res["summary"])
    assert "다름 → 이번 실행에 쓰이지 않음" in rows["PR head의 lock_guard.py blob"]
    assert rows["실행할 lock_guard.py blob"].split()[0] == rows["main의 lock_guard.py blob"]
    assert not list((gh.tmp / f"runner{gh.runs}" / "workspace").iterdir())       # nothing checked out


def test_checker_change_without_data_change_passes_and_is_reported(gh):
    head = open_pr(gh, 6, {"tools/lock_guard.py": b"# rewritten checker\n"})
    res = run_guard(gh, event(gh, 6, head))
    assert res["failed"] is None
    assert "다름 → 이번 실행에 쓰이지 않음" in proof_rows(res["summary"])["PR head의 lock_guard.py blob"]


# ---------------------------------------------------------------- fail-closed conditions
def test_pr_head_moved_after_the_event(gh):
    old = open_pr(gh, 7, {"README.md": b"# v2\n"})
    open_pr(gh, 7, {"README.md": b"# v3\n"})                               # force-pushed since
    res = run_guard(gh, event(gh, 7, old))
    assert res["failed"] == 0 and "PR head moved" in res["out"][0]


def test_workflow_commit_not_on_main_fails_before_extracting(gh):
    """If a run ever loaded the workflow from a commit that is not on main (e.g. the PR's), the proof
    step stops before anything is extracted or run."""
    head = open_pr(gh, 8, {"README.md": b"# v2\n"})
    res = run_guard(gh, event(gh, 8, head, workflow_sha=head))
    assert res["failed"] == 1 and "not on main" in res["out"][1]
    assert "(추출 안 함)" in proof_rows(res["summary"])["실행할 lock_guard.py blob"]
    assert not (gh.tmp / f"runner{gh.runs}" / "temp" / "guard" / "lock_guard.py").exists()


def test_other_event_fails(gh):
    head = open_pr(gh, 9, {"README.md": b"# v2\n"})
    res = run_guard(gh, event(gh, 9, head, event_name="pull_request"))
    assert res["failed"] == 1 and "not a pull_request_target run" in res["out"][1]


def test_stacked_pr_on_another_base_branch(gh):
    gh.git("checkout", "-q", "-B", "base-feature", "main")
    gh.write("run_pipeline.py", b"# engine on the base branch\n")
    base = gh.commit("base branch work")
    gh.git("push", "-q", "origin", "base-feature"); gh.git("checkout", "-q", "main")
    head = open_pr(gh, 10, {"README.md": b"# v2\n"}, base="base-feature")
    assert run_guard(gh, event(gh, 10, head, base_sha=base, base_ref="base-feature"))["failed"] is None
    bad = open_pr(gh, 11, {LG.EVENTS: []}, base="base-feature")
    res = run_guard(gh, event(gh, 11, bad, base_sha=base, base_ref="base-feature"))
    assert res["failed"] == GUARD_STEP


# ---------------------------------------------------------------- shell injection
def payloads(marker: Path) -> list[str]:
    m = str(marker)
    return [f"$(touch {m})", f"`touch {m}`", f"x\"; touch {m}; echo \"", f"x'; touch {m}; echo '",
            f"a;touch {m}", f"a|touch {m}", f"a&&touch {m}", f"a\ntouch {m}", f"${{IFS}}$(touch${{IFS}}{m})",
            f"restore/$(touch {m})", f"--report={m}"]


@pytest.mark.parametrize("field", ["head_ref", "user", "workflow_ref", "halt"])
def test_injection_in_free_text_values_is_inert(gh, field):
    """Branch name, author, workflow ref and PDS_HALT reach the shell only through env and are only
    ever expanded inside quotes or passed as argv: no payload runs."""
    marker = gh.tmp / "INJECTED"
    head = open_pr(gh, 12, {"README.md": b"# v2\n"})
    for p in payloads(marker):
        res = run_guard(gh, event(gh, 12, head, **{field: p}))
        assert not marker.exists(), (field, p)
        if field != "head_ref" or not p.startswith("restore/"):
            assert res["failed"] is None, (field, p, res["out"])
        else:                                    # a restore request without a record: rejected, nothing ran
            assert res["failed"] == GUARD_STEP


@pytest.mark.parametrize("field", ["number", "base_sha", "head_sha", "base_ref", "default_branch", "workflow_sha"])
def test_injection_in_structured_values_is_rejected(gh, field):
    marker = gh.tmp / "INJECTED"
    head = open_pr(gh, 13, {"README.md": b"# v2\n"})
    extra = {"number": ["", "1.5", "-1", "0x1"], "base_ref": ["", "..", "a/../b", "no-such-branch"],
             "default_branch": ["", "..", "a/../b", "no-such-branch"]}.get(field, ["", "main", "0" * 39, "G" * 40])
    for p in payloads(marker) + extra:
        res = run_guard(gh, event(gh, 13, head, **{field: p}))
        assert not marker.exists(), (field, p)
        assert res["failed"] == 0, (field, p)                   # rejected in the first step
        if p != "no-such-branch":                               # well-formed name: the fetch fails instead
            msg = {"number": "bad PR number", "base_ref": "is not a plain branch name",
                   "default_branch": "is not a plain branch name"}.get(field, "is not a commit SHA")
            assert msg in res["out"][0], (field, p, res["out"][0])      # by validation, before any git call


# ---------------------------------------------------------------- restore PR, approval API
@pytest.fixture()
def api(monkeypatch):
    """Local stand-in for api.github.com: environment, this run's approvals, approver permission."""
    state = {"env": {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"reviewer": {"login": "owner"}}]}],
                     "can_admins_bypass": False},
             "approvals": [{"user": {"login": "owner"}, "state": "approved", "comment": f"LOCK-RESTORE 승인 {RID}",
                            "environments": [{"name": LG.ENVIRONMENT}]}],
             "permission": {"permission": "admin"}, "seen": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["seen"].append((self.path, self.headers.get("Authorization")))
            routes = {f"/repos/owner/repo/environments/{LG.ENVIRONMENT}": state["env"],
                      "/repos/owner/repo/actions/runs/4242/approvals": state["approvals"],
                      "/repos/owner/repo/collaborators/owner/permission": state["permission"]}
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404); self.end_headers()
            self.wfile.write(json.dumps(body if body is not None else {"message": "Not Found"}).encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()


def restore_pr(gh) -> str:
    head = make_restore(gh)                         # branch restore/<RID> from the damaged main
    gh.git("push", "-q", "-f", "origin", f"{head}:refs/pull/20/head")
    gh.git("checkout", "-q", "main")
    return head


def test_restore_pr_passes_with_the_environment_approval(gh, api):
    head = restore_pr(gh)
    ctx = event(gh, 20, head, head_ref=f"restore/{RID}", labels=["lock-restore"], user="owner", halt="1")
    res = run_guard(gh, ctx, api["base"])
    assert res["failed"] is None, res["summary"]
    assert "복구 PR(잠금 복구 검증) — 통과" in res["summary"] and "복구 전후 비교" in res["summary"]
    assert {p for p, _ in api["seen"]} == {f"/repos/owner/repo/environments/{LG.ENVIRONMENT}",
                                           "/repos/owner/repo/actions/runs/4242/approvals",
                                           "/repos/owner/repo/collaborators/owner/permission"}
    assert all(a == f"Bearer {TOKEN}" for _, a in api["seen"])


@pytest.mark.parametrize("breakage", ["no approval", "admins may bypass", "approver not admin", "not halted", "no label"])
def test_restore_pr_rejected(gh, api, breakage):
    head = restore_pr(gh)
    ctx = event(gh, 20, head, head_ref=f"restore/{RID}", labels=["lock-restore"], user="owner", halt="1")
    if breakage == "no approval":
        api["approvals"] = []
    elif breakage == "admins may bypass":
        api["env"]["can_admins_bypass"] = True
    elif breakage == "approver not admin":
        api["permission"] = {"permission": "write"}
    elif breakage == "not halted":
        ctx["halt"] = ""
    else:
        ctx["labels"] = []
    res = run_guard(gh, ctx, api["base"])
    assert res["failed"] == GUARD_STEP and "복구 PR(잠금 복구 검증) — 실패" in res["summary"]


def test_restore_pr_fails_closed_when_the_api_is_unreachable(gh):
    head = restore_pr(gh)
    ctx = event(gh, 20, head, head_ref=f"restore/{RID}", labels=["lock-restore"], user="owner", halt="1")
    res = run_guard(gh, ctx)                       # api base points at a closed port
    assert res["failed"] == GUARD_STEP


def test_normal_pr_gets_no_token_and_makes_no_api_call(gh, api):
    head = open_pr(gh, 21, {"README.md": b"# v2\n"})
    res = run_guard(gh, event(gh, 21, head), api["base"])
    assert res["failed"] is None and res["env"]["GH_TOKEN"] == "" and api["seen"] == []


# ---------------------------------------------------------------- static security assertions
def runs():
    return [(j, s) for j, job in WF["jobs"].items() for s in job.get("steps", []) if "run" in s]


def test_trigger_permissions_and_jobs():
    on = WF.get("on", WF.get(True))                 # PyYAML reads the bare key `on` as True
    assert list(on) == ["pull_request_target"]
    assert set(on["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "edited", "labeled", "unlabeled"}
    assert WF["permissions"] == {}
    assert set(WF["jobs"]) == {"restore-gate", "guard"}
    gate, guard = WF["jobs"]["restore-gate"], WF["jobs"]["guard"]
    assert gate["environment"] == LG.ENVIRONMENT and gate["permissions"] == {}
    assert "restore/" in gate["if"] and LG.LABEL in gate["if"]
    assert [list(s) for s in gate["steps"]] == [["run"]] and "echo" in gate["steps"][0]["run"]
    assert guard["permissions"] == {"contents": "read", "actions": "read"}
    assert guard["needs"] == "restore-gate" and guard["if"].replace(" ", "") == "${{!cancelled()}}"
    assert guard["name"] == "data-guard"
    assert "github.event.pull_request.number" in WF["concurrency"]["group"]


def test_no_pr_code_no_actions_no_secrets_no_writes():
    steps = [s for job in WF["jobs"].values() for s in job.get("steps", [])]
    assert all("uses" not in s for s in steps)                  # no checkout, no setup-python, no third-party action
    assert "secrets." not in WF_TEXT and "write" not in json.dumps(WF.get("permissions")) + json.dumps(
        [j.get("permissions") for j in WF["jobs"].values()])
    assert WF_TEXT.count("github.token") == 1                     # only in GH_TOKEN, only for restore requests
    gh_token = " ".join(WF["jobs"]["guard"]["steps"][GUARD_STEP]["env"]["GH_TOKEN"].split())
    assert gh_token.startswith("${{ (startsWith(github.event.pull_request.head.ref, 'restore/') ||")
    for job, s in runs():
        assert "${{" not in s["run"], (job, s.get("name"))         # event values only through env
        for bad in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH", "pip ", "npm ", "make ", "checkout", "worktree",
                    "source ", "eval ", "curl ", "wget "):
            assert bad not in s["run"], (job, bad)
    script = "\n".join(s["run"] for _, s in runs())
    assert re.findall(r"python3?\b[^\n]*", script) == [
        'python3 -I "$G/lock_guard.py" --repo "$R" --base "$mb" --head "$HEAD_SHA" \\']
    assert 'git init -q --bare "$R"' in script and "core.hooksPath /dev/null" in script
    assert 'cat-file blob "$GITHUB_WORKFLOW_SHA:tools/lock_guard.py"' in script   # the checker comes from main


def test_every_event_value_is_quoted_in_the_scripts():
    names = set()
    for job in WF["jobs"].values():
        names |= set(job.get("env") or {})
        for s in job.get("steps", []):
            names |= set(s.get("env") or {})
    names -= {"GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GH_TOKEN"}
    for _, s in runs():
        for n in names:
            for m in re.finditer(r"\$\{?" + n + r"\b", s["run"]):
                line = s["run"][s["run"].rfind("\n", 0, m.start()) + 1:s["run"].find("\n", m.end())]
                before = line[:m.start() - (s["run"].rfind("\n", 0, m.start()) + 1)]
                assert before.count('"') % 2 == 1 or "[[" in line, (n, line)   # inside "..." or [[ ]] (no splitting)


def test_tests_workflow_keeps_only_a_fallback_and_no_second_approval():
    tw = yaml.safe_load((H.ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    assert "restore-gate" not in tw["jobs"]
    assert all(j.get("environment") != LG.ENVIRONMENT for j in tw["jobs"].values())
    fallback = tw["jobs"]["lock-guard"]
    assert fallback["permissions"] == {"contents": "read"}
    assert "restore/" in fallback["if"] and "lock-restore" in fallback["if"] and "!(" in fallback["if"]
    assert "GH_TOKEN" not in json.dumps(fallback)


def test_ops_audit_reports_data_guard_runs():
    import ops_audit as OA
    assert OA.DATA_GUARD == WF_PATH.name
    assert OA.data_guard_rows({"_http_error": 404})[0][1] is False
    assert OA.data_guard_rows({"workflow_runs": []})[0][1] is None
    ok = OA.data_guard_rows({"workflow_runs": [{"event": "pull_request_target", "conclusion": "success",
                                                "pull_requests": [{"number": 12}], "created_at": "2026-12-03T00:00:00Z"}]})
    assert ok[0][1] is None and "#12 pull_request_target success" in ok[0][2]
    blocked = OA.data_guard_rows([{"event": "pull_request_target", "conclusion": "startup_failure"}])
    assert blocked[0][1] is False and "이벤트 정책" in blocked[0][2]


CUSTOM = {"protected_branches": False, "custom_branch_policies": True}


@pytest.mark.parametrize("pol, policies, ok, text", [
    (CUSTOM, {"total_count": 1, "branch_policies": [{"id": 1, "name": "main", "type": "branch"}]}, True, "main(branch)"),
    (None, None, False, "제한 없음"),
    ({"protected_branches": True, "custom_branch_policies": False}, None, False, "보호 브랜치만"),
    (CUSTOM, {"total_count": 2, "branch_policies": [{"name": "main"}, {"name": "restore/*"}]}, False, "restore/*(branch)"),
    (CUSTOM, {"total_count": 1, "branch_policies": [{"name": "main", "type": "tag"}]}, False, "main(tag)"),
    (CUSTOM, {"total_count": 1, "branch_policies": [{"name": "mai*", "type": "branch"}]}, False, "mai*"),
    (CUSTOM, {"total_count": 0, "branch_policies": []}, False, "비어 있음"),
    (CUSTOM, {"_http_error": 403}, None, "조회 불가"),
])
def test_ops_audit_restore_environment_branch_rule(pol, policies, ok, text):
    import ops_audit as OA
    env = {"name": LG.ENVIRONMENT, "can_admins_bypass": False, "deployment_branch_policy": pol,
           "protection_rules": [{"type": "required_reviewers", "reviewers": [{"reviewer": {"login": "owner"}}]}]}
    row = [r for r in OA.restore_environment_rows(env, policies) if "배포 브랜치" in r[0]]
    assert len(row) == 1 and row[0][1] is ok and text in row[0][2], row
