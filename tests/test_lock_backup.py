"""Monthly backup checklist: in the new-lock alert, in run_status.json on every run, in the job
summary and in ops-audit - and still there when Telegram is not set up or the alert fails.

Everything runs in temporary directories; the real docs/data is only read.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H  # noqa: E402

sys.path.insert(0, str(H.ROOT / "tools"))
import lock_manifest as LM  # noqa: E402
import notify_message as NOTIFY  # noqa: E402
import record_run_status as RS  # noqa: E402
import send_alert as SA  # noqa: E402

COMMIT = "c0ffee" + "0" * 34
REPO = "owner/pds-lite-quant"
REAL_LOCK = H.ROOT / "docs" / "data" / "official_history.json"


def lock_bytes(months=("2026-09", "2026-10", "2026-11")) -> bytes:
    h = [{"signal_month": m, "core": {"SPY": 1.0}, "adaptive": {"SPY": 1.0}, "origin": "LIVE",
          "locked_at_utc": f"{m}-30T22:41:00+00:00"} for m in months]
    return json.dumps(h, ensure_ascii=False, indent=1).encode()


def workdir(tmp_path, notify, lock=lock_bytes()) -> Path:
    data = tmp_path / "docs" / "data"
    data.mkdir(parents=True)
    (data / "official_history.json").write_bytes(lock)
    (data / "portfolio_latest.json").write_text(json.dumps({"official": {
        "signal_month": "2026-11", "holding_month": "2026-12", "state": "NORMAL", "risk_budget": 1.0,
        "us": [{"ticker": "SPY", "adaptive": 1.0}]}}), encoding="utf-8")
    (data / "run_status.json").write_text(json.dumps({"ok": True, "notify": notify, "log_tail": []}), encoding="utf-8")
    return data / "run_status.json"


def server(replies):
    seen, queue = [], list(replies)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen.append(urllib.parse.parse_qs(body)["text"][0])
            code, reply = queue.pop(0) if queue else (200, {"ok": True})
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


@pytest.fixture()
def alert_env(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "t"); monkeypatch.setenv("TG_CHAT", "c")
    monkeypatch.setenv("ALERT_RETRY_SECONDS", "0")
    monkeypatch.setenv("DATA_COMMIT", COMMIT); monkeypatch.setenv("GITHUB_REPOSITORY", REPO)


def send(tmp_path, monkeypatch, notify, replies=((200, {"ok": True}),), lock=lock_bytes()):
    st = workdir(tmp_path, notify, lock)
    monkeypatch.chdir(tmp_path)
    srv, seen = server(replies)
    monkeypatch.setenv("TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    try:
        code = SA.main(["state", "--status", str(st), "--pages", "https://p/"])
    finally:
        srv.shutdown()
    return code, seen, json.loads(st.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the manifest itself
def test_manifest_of_the_committed_lock_matches_git():
    m = LM.manifest(REAL_LOCK)
    b = REAL_LOCK.read_bytes()
    assert m["readable"] and m["months"] >= 116 and m["first"] == "2017-03"
    assert m["sha256"] == hashlib.sha256(b).hexdigest() and m["bytes"] == len(b)
    blob = subprocess.run(["git", "-C", str(H.ROOT), "hash-object", str(REAL_LOCK)], capture_output=True, text=True)
    if blob.returncode == 0:
        assert m["git_blob"] == blob.stdout.strip()                       # = GitHub's file SHA


def test_checklist_text(tmp_path):
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    m = LM.manifest(p)
    t = LM.checklist(m, REPO, COMMIT)
    assert "📦" in t and "3개월" in t and "2026-09~2026-11" in t and m["sha256"] in t and m["git_blob"] in t
    assert f"데이터 커밋 {COMMIT}" in t
    assert f"https://raw.githubusercontent.com/{REPO}/{COMMIT}/docs/data/official_history.json" in t
    for f in ("official_events.json", "manual_releases.json", "reference_adaptive.json"):
        assert f in t
    bad = tmp_path / "bad.json"; bad.write_bytes(b"[{")
    tb = LM.checklist(LM.manifest(bad), REPO, COMMIT)
    assert "읽을 수 없음" in tb and "raw.githubusercontent" not in tb                  # never a link to a broken file
    assert "missing" in LM.checklist(LM.manifest(tmp_path / "none.json"), REPO, COMMIT)


# ---------------------------------------------------------------- Telegram
@pytest.mark.parametrize("event,with_list", [("LOCKED", True), ("MANUAL_RELEASE", True), ("PENDING", False),
                                             ("BLOCKED", False), ("MANUAL_RELEASE_REJECTED", False),
                                             ("CORE_MISMATCH", False), ("OPERATOR_HALT", False)])
def test_new_lock_alerts_carry_the_checklist(tmp_path, monkeypatch, alert_env, event, with_list):
    code, seen, rec = send(tmp_path, monkeypatch, [{"signal_month": "2026-11", "event": event, "detail": "x"}])
    assert code == 0 and rec["delivery"]["status"] == "sent" and len(seen) == 1
    text = seen[0]
    m = LM.manifest(tmp_path / "docs" / "data" / "official_history.json")
    assert ("📦" in text) is with_list
    if with_list:
        assert m["sha256"] in text and COMMIT in text and "3개월" in text and "2026-11" in text
        assert text.index("📦") < text.index("https://p/")                       # checklist, then the page


def test_failed_alert_is_resent_with_the_checklist(tmp_path, monkeypatch, alert_env):
    lost = [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}]
    code, seen, rec = send(tmp_path, monkeypatch, lost, replies=[(500, {"ok": False})] * 2)
    assert code == 3 and rec["delivery"]["status"] == "failed" and rec["notify_pending"] == lost
    nxt = RS.build(rec, True, {"generated_at_utc": "x", "notify": [], "action": None}, [], "schedule", "u",
                   datetime(2026, 12, 4, 22, 45, tzinfo=timezone.utc))
    assert nxt["notify"][0]["resend"] is True
    (tmp_path / "docs" / "data" / "run_status.json").write_text(json.dumps(nxt), encoding="utf-8")
    srv, seen2 = server([(200, {"ok": True})])
    monkeypatch.setenv("TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    try:
        assert SA.main(["state", "--status", str(tmp_path / "docs" / "data" / "run_status.json")]) == 0
    finally:
        srv.shutdown()
    assert seen2[0].startswith("[PDS-Lite]\n(재전송") and "📦" in seen2[0] and COMMIT in seen2[0]


def test_existing_alert_text_unchanged_without_backup():
    p = {"notify": [{"signal_month": "2026-11", "event": "PENDING", "detail": "d"}], "action": {"in_force": "2026-10"}}
    assert NOTIFY.message(p, "https://x/") == NOTIFY.message(p, "https://x/", "")
    assert "📦" not in NOTIFY.message(p)


# ---------------------------------------------------------------- run_status.json (no Telegram needed)
def run_status_main(tmp_path, monkeypatch, notify, ok=True, halt=False, lock=lock_bytes()):
    data = tmp_path / "docs" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "official_history.json").write_bytes(lock)
    monkeypatch.chdir(tmp_path)
    started = int(datetime(2026, 12, 2, 22, 40, tzinfo=timezone.utc).timestamp())
    res = tmp_path / "run_result.json"
    res.write_text(json.dumps({"generated_at_utc": datetime(2026, 12, 2, 22, 41, tzinfo=timezone.utc).isoformat(),
                               "action": {"code": "FOLLOW_OFFICIAL", "month": "2026-11"}, "notify": notify,
                               "new_locks": len(notify), "operator_halt": halt}), encoding="utf-8")
    out = tmp_path / "gh_output"
    for k, v in {"RUN_OUTCOME": "success" if ok else "failure", "RUN_STARTED_UTC": str(started),
                 "RUN_RESULT_PATH": str(res), "GITHUB_OUTPUT": str(out), "DATA_COMMIT": COMMIT}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    RS.main()
    return json.loads((data / "run_status.json").read_text(encoding="utf-8")), out.read_text()


def test_run_status_records_the_manifest_every_run(tmp_path, monkeypatch):
    locked = [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}]
    st, outputs = run_status_main(tmp_path, monkeypatch, locked)
    lm = st["lock_manifest"]
    assert lm["changed_this_run"] is True and lm["backup_due"] is True and lm["download_commit"] is None
    assert "백업 완료가 아니다" in lm["backup_note"]                                # due, never "done"
    assert lm["months"] == 3 and lm["last"] == "2026-11" and len(lm["sha256"]) == 64 and "SHA-256" in lm["backup_rule"]
    assert "backup_due=true" in outputs
    # the next run: nothing new, the same facts, and a commit known to hold this exact file
    (tmp_path / "gh_output").unlink()
    st2, outputs2 = run_status_main(tmp_path, monkeypatch, [])
    assert st2["lock_manifest"]["sha256"] == lm["sha256"] and st2["lock_manifest"]["changed_this_run"] is False
    assert st2["lock_manifest"]["download_commit"] == COMMIT and "backup_due=false" in outputs2


@pytest.mark.parametrize("kind", ["failed_run", "halted_run", "resend_only"])
def test_manifest_present_but_no_new_backup_due(tmp_path, monkeypatch, kind):
    if kind == "failed_run":
        st, outputs = run_status_main(tmp_path, monkeypatch, [], ok=False)
    elif kind == "halted_run":
        st, outputs = run_status_main(tmp_path, monkeypatch, [{"signal_month": "-", "event": "OPERATOR_HALT"}], halt=True)
    else:                    # an earlier lock's alert resent: the backup of that lock was due then
        data = tmp_path / "docs" / "data"; data.mkdir(parents=True)
        (data / "run_status.json").write_text(json.dumps({"recent": [{"ok": True}], "notify_pending": [
            {"signal_month": "2026-11", "event": "LOCKED"}]}), encoding="utf-8")
        st, outputs = run_status_main(tmp_path, monkeypatch, [])
        assert st["notify"][0]["resend"] is True
    assert st["lock_manifest"]["readable"] and st["lock_manifest"]["changed_this_run"] is False
    assert st["lock_manifest"]["backup_due"] is False
    assert "backup_due=false" in outputs


# ---------------------------------------------------------------- job summary, ops-audit, workflow
def test_job_summary_cli(tmp_path, monkeypatch):
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    env = dict(os.environ, DATA_COMMIT=COMMIT, GITHUB_REPOSITORY=REPO)
    due = subprocess.run([sys.executable, str(H.ROOT / "tools" / "lock_manifest.py"), "--lock", str(p), "--summary",
                          "--due", "true"], capture_output=True, text=True, env=env)
    assert due.returncode == 0 and "오늘 백업" in due.stdout and "📦" in due.stdout and COMMIT in due.stdout
    quiet = subprocess.run([sys.executable, str(H.ROOT / "tools" / "lock_manifest.py"), "--lock", str(p), "--summary",
                            "--due", "false"], capture_output=True, text=True, env=env)
    assert quiet.returncode == 0 and "📦 오늘" not in quiet.stdout and LM.manifest(p)["sha256"] in quiet.stdout


def test_ops_audit_shows_the_same_manifest(tmp_path):
    import ops_audit as OA
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    m = LM.manifest(p)
    rows = OA.lock_backup_rows(m, {"sha": COMMIT, "date": "2026-12-02T22:41:10Z"}, REPO, fetch=lambda u, t: p.read_bytes())
    text = OA.report(rows)
    assert m["sha256"] in text and COMMIT in text and "raw.githubusercontent.com" in text and LM.RULE in text
    assert ("내려받기 링크 SHA-256 = 매니페스트", True) in [(i, ok) for i, ok, _ in rows]
    wrong = OA.lock_backup_rows(m, {"sha": COMMIT}, REPO, fetch=lambda u, t: b"other bytes")
    assert ("내려받기 링크 SHA-256 = 매니페스트", False) in [(i, ok) for i, ok, _ in wrong]
    def offline(u, t):
        raise OSError("no network")
    unreachable = OA.lock_backup_rows(m, {"sha": COMMIT}, REPO, fetch=offline, wait=0)
    assert ("내려받기 링크 SHA-256 = 매니페스트", None) in [(i, ok) for i, ok, _ in unreachable]
    bad = tmp_path / "b.json"; bad.write_bytes(b"{")
    assert OA.lock_backup_rows(LM.manifest(bad), None, REPO)[0][1] is False
    assert "조회 불가" in OA.report(OA.lock_backup_rows(m, None, REPO))


def test_workflow_wiring():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    names = [s.get("name", "") for s in wf["jobs"]["run"]["steps"]]
    i = names.index("Backup checklist (job summary)")
    assert names.index("Commit if changed") < i < names.index("Telegram alert (state changes, after a successful commit)")
    step = wf["jobs"]["run"]["steps"][i]
    assert "steps.commit.outcome == 'success'" in step["if"]               # only what was saved
    assert step["env"]["DUE"] == "${{ steps.status.outputs.backup_due }}"
    assert "lock_manifest.py --summary" in step["run"] and "GITHUB_STEP_SUMMARY" in step["run"]
    assert "github.event" not in step["run"]
    src = (H.ROOT / "tools" / "record_run_status.py").read_text(encoding="utf-8")
    assert "backup_due=" in src and "lock_manifest.status_record" in src


# ---------------------------------------------------------------- exceptions (final checks)
def test_commit_failure_alert_has_no_checklist(tmp_path, monkeypatch, alert_env):
    """The lock was not saved: the failure alert must not ask for a backup of it."""
    st = workdir(tmp_path, [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}])
    monkeypatch.chdir(tmp_path)
    srv, seen = server([(200, {"ok": True})])
    monkeypatch.setenv("TELEGRAM_API", f"http://127.0.0.1:{srv.server_port}")
    try:
        assert SA.main(["failure", "--status", str(st), "--what", "commit", "--run-url", "u"]) == 0
    finally:
        srv.shutdown()
    assert "커밋 실패" in seen[0] and "저장되지 않았습니다" in seen[0] and "📦" not in seen[0]


@pytest.mark.parametrize("lock", [b"[{", b"", b"{}", b"[]"])
def test_damaged_lock_never_gets_a_download_link(tmp_path, monkeypatch, alert_env, lock):
    code, seen, rec = send(tmp_path, monkeypatch, [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x"}],
                           lock=lock)
    assert code == 0 and "📦" in seen[0] and "읽을 수 없음" in seen[0] and "raw.githubusercontent" not in seen[0]
    st, outputs = run_status_main(tmp_path / "rs", monkeypatch, [{"signal_month": "2026-11", "event": "LOCKED"}], lock=lock)
    assert st["lock_manifest"]["readable"] is False and st["lock_manifest"]["backup_due"] is False
    assert "backup_due=false" in outputs


def test_missing_lock_file(tmp_path):
    m = LM.manifest(tmp_path / "none.json")
    assert m["readable"] is False and m["error"] == "missing"
    assert "raw.githubusercontent" not in LM.checklist(m, REPO, COMMIT)


def test_resend_after_a_newer_lock_names_the_current_file(tmp_path, monkeypatch, alert_env):
    """An undelivered 2026-11 alert resent after 2026-12 was locked: the checklist describes the
    file as it is now (it contains 2026-11 too), so the backup taken from it is complete."""
    newer = lock_bytes(("2026-10", "2026-11", "2026-12"))
    pending = [{"signal_month": "2026-11", "event": "LOCKED", "detail": "x", "resend": True}]
    code, seen, rec = send(tmp_path, monkeypatch, pending, lock=newer)
    assert code == 0 and seen[0].startswith("[PDS-Lite]\n(재전송") and "2026-10~2026-12" in seen[0]
    assert hashlib.sha256(newer).hexdigest() in seen[0]


def test_no_repository_or_commit_gives_instructions_not_a_broken_link(tmp_path, monkeypatch):
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    t = LM.checklist(LM.manifest(p), None, None)
    assert "확인 불가" in t and "raw.githubusercontent" not in t and LM.manifest(p)["sha256"] in t


# ---------------------------------------------------------------- download link = reference bytes
@pytest.fixture()
def raw_host(monkeypatch):
    """A local stand-in for raw.githubusercontent.com. Each path answers as configured:
    bytes -> 200 with that body, int -> that HTTP status, ("slow", s) -> body after s seconds."""
    routes, hits = {}, []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            r = routes.get(self.path, 404)
            if isinstance(r, tuple):
                import time
                time.sleep(r[1]); r = r[2]
            if isinstance(r, int):
                self.send_response(r); self.end_headers(); return
            self.send_response(200); self.send_header("Content-Length", str(len(r))); self.end_headers()
            try:
                self.wfile.write(r)
            except OSError:
                pass

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("PDS_RAW_BASE", base)
    yield base, routes, hits
    srv.shutdown()


LOCK_PATH = "docs/data/official_history.json"


def link_for(routes, reply, commit=COMMIT):
    url = LM.raw_url(REPO, commit, LOCK_PATH)
    routes["/" + url.split("/", 3)[3]] = reply
    return url


@pytest.mark.parametrize("reply, result, ok", [
    ("same", "match", True),                    # the actual response bytes hash to the reference
    (b"[]", "mismatch", False),                 # 200, but different bytes
    ("one-byte-off", "mismatch", False),        # 200, one byte changed
    ("truncated", "mismatch", False),           # 200, cut short
    (b"", "empty", False),                      # 200 with an empty body
    (404, "not_found", False),                  # wrong commit / wrong path
    (500, "http_error", None),                  # server error: unknown, not a verdict
    (503, "http_error", None),
    (403, "http_error", None),
])
def test_link_response_bytes_vs_reference_sha256(raw_host, reply, result, ok):
    base, routes, hits = raw_host
    ref = lock_bytes()
    body = {"same": ref, "one-byte-off": ref[:-1] + b"X", "truncated": ref[: len(ref) // 2]}.get(reply, reply)
    url = link_for(routes, body)
    v = LM.verify_link(url, hashlib.sha256(ref).hexdigest(), timeout=5)
    assert (v["result"], v["ok"]) == (result, ok), v
    assert hits == ["/" + url.split("/", 3)[3]]                      # really requested, once
    if isinstance(body, bytes):
        assert v["bytes"] == len(body) and v["sha256"] == hashlib.sha256(body).hexdigest()
    else:
        assert v["sha256"] is None
    assert {True: "✅", False: "❌", None: "❔"}[ok] in LM.link_line(v)


def test_link_timeout_is_unknown_and_retried(raw_host):
    base, routes, hits = raw_host
    ref = lock_bytes()
    url = link_for(routes, ("slow", 2, ref))
    v = LM.verify_link(url, hashlib.sha256(ref).hexdigest(), timeout=0.3, tries=2, wait=0)
    assert (v["result"], v["ok"]) == ("timeout", None) and len(hits) == 2


def test_link_server_error_then_success_counts_as_match(raw_host):
    base, routes, hits = raw_host
    ref = lock_bytes()
    url = LM.raw_url(REPO, COMMIT, LOCK_PATH)
    answers = iter([500, ref])
    def getter(u, t):
        a = next(answers)
        if isinstance(a, int):
            import urllib.error
            raise urllib.error.HTTPError(u, a, "x", {}, None)
        return a
    v = LM.verify_link(url, hashlib.sha256(ref).hexdigest(), getter=getter, tries=3, wait=0)
    assert v["result"] == "match" and v["ok"] is True


def test_definite_answers_are_not_retried(raw_host):
    base, routes, hits = raw_host
    url = link_for(routes, 404)
    v = LM.verify_link(url, "0" * 64, tries=3, wait=0)
    assert v["result"] == "not_found" and len(hits) == 1


def test_network_failure_is_unknown(monkeypatch):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # nothing listens
    monkeypatch.setenv("PDS_RAW_BASE", f"http://127.0.0.1:{port}")
    v = LM.verify_link(LM.raw_url(REPO, COMMIT, LOCK_PATH), "0" * 64, timeout=2)
    assert (v["result"], v["ok"]) == ("network", None)


@pytest.mark.parametrize("url", [
    None, 123, "",
    "https://example.com/owner/pds-lite-quant/" + COMMIT + "/docs/data/official_history.json",     # other host
    "http://raw.githubusercontent.com/owner/pds-lite-quant/" + COMMIT + "/docs/data/official_history.json",  # http
    "https://raw.githubusercontent.com.evil.io/owner/pds-lite-quant/" + COMMIT + "/docs/data/official_history.json",
    "https://raw.githubusercontent.com/owner/pds-lite-quant/main/docs/data/official_history.json",  # branch, not commit
    "https://raw.githubusercontent.com/owner/pds-lite-quant/c0ffee/docs/data/official_history.json",  # short sha
    "https://raw.githubusercontent.com/owner/pds-lite-quant/" + COMMIT + "/docs/data/../../README.md",
    "https://raw.githubusercontent.com/owner/pds-lite-quant/" + COMMIT + "/tools/lock_manifest.py",
    "https://raw.githubusercontent.com/owner/pds-lite-quant/" + COMMIT + "/docs/data/official_history.json?x=1",
    "https://raw.githubusercontent.com/" + COMMIT + "/docs/data/official_history.json",               # no repo
    "file:///etc/passwd",
])
def test_bad_url_is_rejected_without_a_request(url, monkeypatch):
    monkeypatch.delenv("PDS_RAW_BASE", raising=False)
    called = []
    v = LM.verify_link(url, "0" * 64, getter=lambda u, t: called.append(u) or b"x")
    assert (v["result"], v["ok"]) == ("bad_url", False) and called == []


def test_the_real_link_form_is_accepted(monkeypatch):
    monkeypatch.delenv("PDS_RAW_BASE", raising=False)
    url = LM.raw_url("74cf4xxtd8-dev/pds-lite-quant", "88decb746f5776209724e1c6aeef1be1b8910675", LOCK_PATH)
    assert url == ("https://raw.githubusercontent.com/74cf4xxtd8-dev/pds-lite-quant/"
                   "88decb746f5776209724e1c6aeef1be1b8910675/docs/data/official_history.json")
    v = LM.verify_link(url, "1750dd4adcc74ce5832193a8a87da73c1ab401d36b242dea3b4fe5a3510e709e",
                       getter=lambda u, t: REAL_LOCK.read_bytes())
    assert v["result"] == "match"              # the committed lock under the real link form


@pytest.mark.parametrize("expected", [None, "", "abc", "Z" * 64, 5])
def test_missing_or_malformed_reference_never_passes(expected):
    v = LM.verify_link(LM.raw_url(REPO, COMMIT, LOCK_PATH), expected, getter=lambda u, t: b"x")
    assert (v["result"], v["ok"]) == ("bad_reference", False)


def test_summary_cli_reports_the_link_check_and_never_fails_the_run(tmp_path, raw_host):
    base, routes, hits = raw_host
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    env = dict(os.environ, DATA_COMMIT=COMMIT, GITHUB_REPOSITORY=REPO, PDS_RAW_BASE=base)
    cmd = [sys.executable, str(H.ROOT / "tools" / "lock_manifest.py"), "--lock", str(p), "--summary", "--due"]
    link_for(routes, lock_bytes())
    ok = subprocess.run(cmd + ["true", "--verify-link"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0 and "✅ 내려받기 링크 확인: match" in ok.stdout
    link_for(routes, b"tampered")
    bad = subprocess.run(cmd + ["true", "--verify-link"], capture_output=True, text=True, env=env)
    assert bad.returncode == 0 and "❌ 내려받기 링크 확인: mismatch" in bad.stdout
    link_for(routes, 404)
    gone = subprocess.run(cmd + ["true", "--verify-link"], capture_output=True, text=True, env=env)
    assert gone.returncode == 0 and "❌ 내려받기 링크 확인: not_found" in gone.stdout
    n = len(hits)
    quiet = subprocess.run(cmd + ["false", "--verify-link"], capture_output=True, text=True, env=env)
    assert quiet.returncode == 0 and "내려받기 링크 확인" not in quiet.stdout and len(hits) == n   # not due: no request


def test_ops_audit_link_row_uses_the_real_download(tmp_path, raw_host):
    import ops_audit as OA
    base, routes, hits = raw_host
    p = tmp_path / "l.json"; p.write_bytes(lock_bytes())
    m = LM.manifest(p)
    def row(rows):
        return [(ok, d) for i, ok, d in rows if i == "내려받기 링크 SHA-256 = 매니페스트"][0]
    link_for(routes, lock_bytes())
    assert row(OA.lock_backup_rows(m, {"sha": COMMIT}, REPO, wait=0))[0] is True
    link_for(routes, b"")
    assert row(OA.lock_backup_rows(m, {"sha": COMMIT}, REPO, wait=0))[0] is False
    link_for(routes, 502)
    ok, detail = row(OA.lock_backup_rows(m, {"sha": COMMIT}, REPO, wait=0))
    assert ok is None and "HTTP 502" in detail


def test_update_workflow_verifies_the_link():
    wf = yaml.safe_load((H.ROOT / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8"))
    step = [s for s in wf["jobs"]["run"]["steps"] if s.get("name") == "Backup checklist (job summary)"][0]
    assert "--verify-link" in step["run"] and step["run"].rstrip().endswith("|| true")
