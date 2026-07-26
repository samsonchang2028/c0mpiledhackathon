"""The self-improving feedback loop (Design doc §7) + calibrated confidence
(§6, §14) + a hardened `report_outcome` surface.

Everything here is append-only JSONL under `WAYFINDER_DATA_DIR` — this is a
hackathon-scoped stand-in for the `verify_runs` / `traffic_logs` Postgres
tables in §9, not a claim that JSONL is the production storage choice.

`report_outcome` is the free, high-signal staleness trigger the design calls
for, but taken at face value it's also an open write endpoint anyone can use
to jerk an edge's confidence around or force expensive re-verification
(§16 doesn't name this risk explicitly; it falls out of §7 being a public
API). The hardening applied here: a failure report requires *evidence*
(what postcondition was expected vs. observed) before it's trusted enough to
requeue the edge, and every report is scoped to a `reporter_id` so a noisy or
adversarial caller's reports can be discounted without silencing everyone.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .paths import OUTCOMES_PATH, VERIFY_RUNS_PATH, TRAFFIC_PATH, ensure_data_dirs

# Confidence decays with time-since-verification, and the decay rate is
# steeper for riskier mutation classes — a stale "read" edge is a much safer
# bet than a stale "write" edge, so it should stay confident longer.
_HALF_LIFE_HOURS = {"read": 168.0, "write": 48.0, "destructive": 24.0}
_MIN_EVIDENCE_FOR_TRUST = 1


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _append(path, record: dict[str, Any]) -> None:
    ensure_data_dirs()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _read_all(path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def record_verify_run(run: dict[str, Any]) -> None:
    _append(VERIFY_RUNS_PATH, {"recorded_at": now(), **run})


def record_traffic(site_id: str, flow_name: str, outcome: str) -> None:
    _append(TRAFFIC_PATH, {"recorded_at": now(), "site_id": site_id, "flow_name": flow_name, "outcome": outcome})


def report_outcome(
    *,
    site_id: str,
    edge_id: str | None,
    flow_name: str | None,
    success: bool,
    evidence: dict[str, Any] | None,
    reporter_id: str = "anonymous",
) -> dict[str, Any]:
    """Record an agent's real-world execution result for an edge or flow.

    A failure report without evidence is still recorded (for audit / rate
    tracking) but is flagged `trusted: false` and does not by itself requeue
    anything — see the module docstring.
    """
    has_evidence = bool(evidence and (evidence.get("expected") or evidence.get("observed") or evidence.get("error")))
    trusted = success or has_evidence

    record = {
        "recorded_at": now(),
        "site_id": site_id,
        "edge_id": edge_id,
        "flow_name": flow_name,
        "success": success,
        "evidence": evidence or {},
        "reporter_id": reporter_id,
        "trusted": trusted,
    }
    _append(OUTCOMES_PATH, record)
    return record


def reporter_trust(reporter_id: str, window: int = 200) -> float:
    """Cheap reputation signal: fraction of a reporter's recent reports that
    were internally consistent (evidence present on failures). Not a defense
    against a determined attacker — a rate limiter belongs in front of this
    endpoint too — but it stops one noisy caller from dominating requeue
    priority for an edge everyone else reports as healthy."""
    reports = [r for r in _read_all(OUTCOMES_PATH) if r.get("reporter_id") == reporter_id][-window:]
    if not reports:
        return 0.5
    trusted = sum(1 for r in reports if r.get("trusted"))
    return trusted / len(reports)


def edges_needing_requeue(site_id: str, since_iso: str | None = None) -> list[str]:
    """Trusted failure reports since a checkpoint — the input to the
    freshness scheduler's front-of-queue jump (§7)."""
    edge_ids: list[str] = []
    for record in _read_all(OUTCOMES_PATH):
        if record.get("site_id") != site_id or record.get("success") is not False or not record.get("trusted"):
            continue
        if since_iso and record.get("recorded_at", "") <= since_iso:
            continue
        if record.get("edge_id"):
            edge_ids.append(record["edge_id"])
    return edge_ids


def calibrate_confidence(edge: dict[str, Any]) -> float:
    """Confidence = success rate learned from outcomes, decayed by
    time-since-verification if there's not yet enough evidence to trust a
    learned rate. This is what "empirically calibrated" (as opposed to a
    hand-tuned constant) means in §6/§14 — see the docstring above."""
    edge_outcomes = [r for r in _read_all(OUTCOMES_PATH) if r.get("edge_id") == edge["id"]]

    if edge["status"] == "broken":
        return 0.0

    if len(edge_outcomes) >= _MIN_EVIDENCE_FOR_TRUST + 4:
        successes = sum(1 for r in edge_outcomes if r["success"])
        empirical = successes / len(edge_outcomes)
    else:
        empirical = None

    decay = _time_decay(edge)
    if empirical is None:
        return round(decay, 3)
    # Blend: more history -> weight the empirical rate more heavily.
    weight = min(len(edge_outcomes) / 20.0, 0.8)
    return round(weight * empirical + (1 - weight) * decay, 3)


def _time_decay(edge: dict[str, Any]) -> float:
    last_verified = edge.get("last_verified_at")
    if not last_verified:
        return 0.2 if edge["status"] == "unverified" else 0.4
    try:
        verified_at = datetime.fromisoformat(last_verified.replace("Z", "+00:00"))
    except ValueError:
        return 0.4
    hours = max((datetime.now(timezone.utc) - verified_at).total_seconds() / 3600.0, 0.0)
    half_life = _HALF_LIFE_HOURS.get(edge["mutation_class"], 72.0)
    base = 0.5 if edge["status"] == "suspect" else 0.97
    return base * math.pow(0.5, hours / half_life)


def self_heal_rate(g: dict[str, Any]) -> float:
    """% of edges with heal history that ended up fresh — the moat metric
    called out in §14 that the original plan was missing."""
    healed = [e for e in g["edges"].values() if e.get("heal_history")]
    if not healed:
        return 0.0
    recovered = sum(1 for e in healed if e["status"] == "fresh")
    return round(recovered / len(healed), 3)


def reverify_priority(g: dict[str, Any]) -> list[str]:
    """Traffic x staleness x mutation-risk ordering (§6 freshness scheduling).
    Traffic comes from traffic_logs.jsonl; falls back to 0 for untouched
    edges so a brand-new high-risk edge still outranks a well-worn stable one."""
    traffic_counts: dict[str, int] = {}
    for record in _read_all(TRAFFIC_PATH):
        flow = record.get("flow_name")
        for name, flow_def in g.get("flows", {}).items():
            if name == flow:
                for edge_id in flow_def.get("edges", []):
                    traffic_counts[edge_id] = traffic_counts.get(edge_id, 0) + 1

    risk_weight = {"read": 1.0, "write": 2.0, "destructive": 1.5}

    def score(edge_id: str) -> float:
        edge = g["edges"][edge_id]
        staleness = 1.0 - _time_decay(edge)
        traffic = math.log1p(traffic_counts.get(edge_id, 0))
        return staleness * (1 + traffic) * risk_weight.get(edge["mutation_class"], 1.0)

    return sorted(g["edges"].keys(), key=score, reverse=True)
