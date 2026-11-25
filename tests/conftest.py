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
            "EVENTS_JSON", "ADAPTIVE_MODE", "MANUAL_RELEASE_MONTH", "OFFICIAL_REQUIRES_AUTHOR_CORE",
            "OFFICIAL_REQUIRES_F2R_PUBLISHED", "assert_lock_append_only")
ORIGINAL = {k: getattr(H.NEW, k) for k in _PATCHED}


@pytest.fixture(autouse=True)
def _restore_pipeline_module():
    for k, v in ORIGINAL.items():
        setattr(H.NEW, k, v)
    yield
    for k, v in ORIGINAL.items():
        setattr(H.NEW, k, v)
