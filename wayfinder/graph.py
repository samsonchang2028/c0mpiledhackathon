"""Graph store: load, validate, and atomically persist per-site state-action graphs.

Schema 2.0 separates two things that schema 1.0 conflated:

* ``node.action_set`` is the node's *identity*. Two observed screens are the same
  node when the same set of intents is available in them. This is the clustering
  key, and it is what keeps the graph from exploding as pages get parameterized.
* ``node.signature`` is a *drift detector* only. It is a hash of the normalized
  DOM skeleton; when it changes, edges anchored to that node become suspect. It
  never decides node identity.

Edges carry a ranked locator ensemble rather than one selector, a mutation class
that gates how the edge may be verified, and calibrated freshness metadata.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import SITES_ROOT, graph_path

SCHEMA_VERSION = "2.0"

EDGE_STATUSES = {"fresh", "suspect", "broken", "unverified"}
MUTATION_CLASSES = {"read", "write", "destructive"}
ACTION_TYPES = {"click", "fill", "select", "navigate", "verify"}

# Verification policy per mutation class. This is enforced by the replay engine,
# not left to the caller: a destructive edge can never be executed to prove it
# works, only confirmed to exist and to reach its confirmation boundary.
VERIFY_POLICY = {
    "read": "replay",  # execute freely
    "write": "sandbox",  # execute only against a sandbox/synthetic account
    "destructive": "structural",  # never execute; confirm the control exists
}


class GraphError(Exception):
    """Raised when a stored graph violates the schema."""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_sites() -> list[str]:
    if not SITES_ROOT.exists():
        return []
    return sorted(p.name for p in SITES_ROOT.iterdir() if (p / "graph.json").is_file())


def load(site_id: str) -> dict[str, Any]:
    path = graph_path(site_id)
    if not path.is_file():
        raise GraphError(f"no graph for site {site_id!r} at {path}")
    graph = json.loads(path.read_text(encoding="utf-8"))
    validate(graph)
    return graph


def save(graph: dict[str, Any], site_id: str | None = None) -> None:
    site_id = site_id or graph["site"]["id"]
    path = graph_path(site_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(graph, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_all() -> dict[str, dict[str, Any]]:
    graphs = {}
    for site_id in list_sites():
        try:
            graphs[site_id] = load(site_id)
        except (GraphError, json.JSONDecodeError):
            continue
    return graphs


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

_NODE_FIELDS = ("id", "kind", "description", "action_set")
_EDGE_FIELDS = (
    "id",
    "from_node",
    "to_node",
    "intent",
    "target_description",
    "action",
    "postcondition",
    "mutation_class",
    "status",
)
_FLOW_FIELDS = ("name", "description", "edges", "payload_schema")


def validate(graph: dict[str, Any]) -> None:
    if graph.get("schema_version") != SCHEMA_VERSION:
        raise GraphError(
            f"unsupported schema_version {graph.get('schema_version')!r}; expected {SCHEMA_VERSION!r}"
        )

    site = graph.get("site")
    if not isinstance(site, dict) or "id" not in site or "base_url" not in site:
        raise GraphError("graph.site must be an object with 'id' and 'base_url'")

    for key in ("nodes", "edges", "flows"):
        if not isinstance(graph.get(key), dict):
            raise GraphError(f"graph.{key} must be an object")

    for node_id, node in graph["nodes"].items():
        for field in _NODE_FIELDS:
            if field not in node:
                raise GraphError(f"node {node_id!r} missing {field!r}")
        if not isinstance(node["action_set"], list):
            raise GraphError(f"node {node_id!r} action_set must be a list of intents")

    for edge_id, edge in graph["edges"].items():
        for field in _EDGE_FIELDS:
            if field not in edge:
                raise GraphError(f"edge {edge_id!r} missing {field!r}")
        if edge["status"] not in EDGE_STATUSES:
            raise GraphError(f"edge {edge_id!r} has invalid status {edge['status']!r}")
        if edge["mutation_class"] not in MUTATION_CLASSES:
            raise GraphError(f"edge {edge_id!r} has invalid mutation_class {edge['mutation_class']!r}")
        if edge["action"].get("type") not in ACTION_TYPES:
            raise GraphError(f"edge {edge_id!r} has invalid action type {edge['action'].get('type')!r}")
        for ref in ("from_node", "to_node"):
            if edge[ref] not in graph["nodes"]:
                raise GraphError(f"edge {edge_id!r} references unknown node {edge[ref]!r}")
        if not edge.get("locators"):
            raise GraphError(f"edge {edge_id!r} must carry at least one locator")

    for flow_name, flow in graph["flows"].items():
        for field in _FLOW_FIELDS:
            if field not in flow:
                raise GraphError(f"flow {flow_name!r} missing {field!r}")
        for edge_id in flow["edges"]:
            if edge_id not in graph["edges"]:
                raise GraphError(f"flow {flow_name!r} references unknown edge {edge_id!r}")

    _validate_action_sets(graph)


def _validate_action_sets(graph: dict[str, Any]) -> None:
    """Node identity must agree with the edges that actually leave the node.

    If these drift apart the abstraction layer is lying, which is the failure
    mode that makes a state-action graph non-executable.
    """
    observed: dict[str, set[str]] = {node_id: set() for node_id in graph["nodes"]}
    for edge in graph["edges"].values():
        observed[edge["from_node"]].add(edge["intent"])

    for node_id, node in graph["nodes"].items():
        declared = set(node["action_set"])
        if declared != observed[node_id]:
            missing = sorted(observed[node_id] - declared)
            extra = sorted(declared - observed[node_id])
            raise GraphError(
                f"node {node_id!r} action_set disagrees with its edges "
                f"(missing={missing}, extra={extra})"
            )


# --------------------------------------------------------------------------
# Mutators
# --------------------------------------------------------------------------


def mark_fresh(edge: dict[str, Any], method: str) -> None:
    edge["status"] = "fresh"
    edge["last_verified_at"] = now()
    edge["verify_method"] = method
    edge["last_error"] = None


def mark_suspect(edge: dict[str, Any], reason: str) -> None:
    if edge["status"] != "broken":
        edge["status"] = "suspect"
    edge["last_error"] = reason


def mark_broken(edge: dict[str, Any], reason: str) -> None:
    edge["status"] = "broken"
    edge["last_error"] = reason


def apply_heal(edge: dict[str, Any], locator: dict[str, Any], evidence: dict[str, Any]) -> None:
    """Promote a re-grounded locator to the head of the edge's ensemble.

    The previous ensemble is kept behind it: a healed locator is a hypothesis
    until the edge re-verifies, and the old entries stay useful if the site
    rolls back.
    """
    ensemble = [deepcopy(locator)] + [
        entry for entry in edge["locators"] if _locator_key(entry) != _locator_key(locator)
    ]
    edge["locators"] = ensemble[:6]
    edge.setdefault("heal_history", []).append(
        {
            "at": now(),
            "locator": deepcopy(locator),
            "score": evidence.get("score"),
            "signals": evidence.get("signals"),
            "method": evidence.get("method"),
        }
    )
    edge["heal_history"] = edge["heal_history"][-10:]


def _locator_key(locator: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in locator.items() if k != "note"}, sort_keys=True)


def edges_for_node(graph: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return [edge for edge in graph["edges"].values() if edge["from_node"] == node_id]


def flow_edges(graph: dict[str, Any], flow_name: str) -> list[dict[str, Any]]:
    flow = graph["flows"][flow_name]
    return [graph["edges"][edge_id] for edge_id in flow["edges"]]


def flows_using_edge(graph: dict[str, Any], edge_id: str) -> list[str]:
    return sorted(
        name for name, flow in graph["flows"].items() if edge_id in flow["edges"]
    )


def flow_status(graph: dict[str, Any], flow_name: str) -> str:
    """A flow is only as fresh as its weakest edge."""
    statuses = {edge["status"] for edge in flow_edges(graph, flow_name)}
    for status in ("broken", "suspect", "unverified"):
        if status in statuses:
            return status
    return "fresh"


def shared_edges(graph: dict[str, Any]) -> dict[str, list[str]]:
    """Edges used by more than one flow. Verifying these once pays for every
    dependent flow, which is the only graph-shaped economy that matters in v1."""
    usage: dict[str, list[str]] = {}
    for edge_id in graph["edges"]:
        flows = flows_using_edge(graph, edge_id)
        if len(flows) > 1:
            usage[edge_id] = flows
    return usage


def iter_all_edges(graphs: Iterable[dict[str, Any]]):
    for graph in graphs:
        for edge in graph["edges"].values():
            yield graph, edge
