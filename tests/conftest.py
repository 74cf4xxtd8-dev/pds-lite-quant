"""Every test patches module globals of run_pipeline (network fetchers, output paths, switches).
Restore them after each test so that no test - in particular the live real-data tests - ever runs
on another test's synthetic inputs or settings."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H  # noqa: E402

_PATCHED = ("fetch_prices", "fetch_unrate", "fetch_f2r_state", "fetch_reference_workbook",
            "OUT_DIR", "LATEST_JSON", "LOCK_JSON", "UNRATE_CACHE", "REF_LEDGER_JSON", "DIAG_DIR",
            "EVENTS_JSON", "RELEASES_JSON", "ADAPTIVE_MODE", "MANUAL_RELEASE_MONTH", "MANUAL_RELEASE_REASON",
            "OPERATOR_HALT", "OFFICIAL_REQUIRES_AUTHOR_CORE", "STATUS_JSON", "RUN_RESULT_PATH", "RELEASE_AUTH_PATH",
            "OFFICIAL_REQUIRES_F2R_PUBLISHED", "assert_lock_append_only")
ORIGINAL = {k: getattr(H.NEW, k) for k in _PATCHED}


REAL_DATA = H.ROOT / "docs" / "data"


def _real_data_state():
    return {f.name: (f.stat().st_size, f.stat().st_mtime_ns) for f in REAL_DATA.rglob("*") if f.is_file()}


@pytest.fixture(autouse=True)
def _restore_pipeline_module():
    for k, v in ORIGINAL.items():
        setattr(H.NEW, k, v)
    before = _real_data_state()
    yield
    for k, v in ORIGINAL.items():
        setattr(H.NEW, k, v)
    after = _real_data_state()
    if after != before:                               # a test wrote to the committed operating data
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        pytest.fail(f"test touched the real docs/data: {changed}")
