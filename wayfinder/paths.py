"""Filesystem layout for the Wayfinder data directory."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_ROOT = Path(os.environ.get("WAYFINDER_DATA_DIR", REPO_ROOT / "data"))
SITES_ROOT = Path(os.environ.get("WAYFINDER_SITES_DIR", REPO_ROOT / "sites"))

OUTCOMES_PATH = DATA_ROOT / "outcomes.jsonl"
VERIFY_RUNS_PATH = DATA_ROOT / "verify_runs.jsonl"
TRAFFIC_PATH = DATA_ROOT / "traffic.jsonl"
CALIBRATION_PATH = DATA_ROOT / "calibration.json"
EVIDENCE_ROOT = DATA_ROOT / "evidence"


def graph_path(site_id: str) -> Path:
    return SITES_ROOT / site_id / "graph.json"


def ensure_data_dirs() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
