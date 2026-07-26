"""Compile a stored graph into the artifact agents actually consume.

Two outputs, matching Design doc §4.4 / §8:

* ``build_manifest`` — a small, token-minimal JSON document: one entry per
  flow, each with a typed input schema and an ordered, locator-bearing
  execution plan. This is the `get_path` response shape, precomputed for
  every flow instead of resolved per-request. An agent (or `replay.py`) can
  execute it directly without touching the rest of the graph.
* ``build_markdown`` — the same information rendered for a human or an LLM
  reading it out of context: what the site does, which flows are verified vs.
  unverified/write-gated, and the exact steps of each.

Both are what "download as a guide for agents to traverse" means here: the
full graph (signatures, heal history, scoring internals) stays server-side;
the guide is the minimal, trustworthy summary of it.
"""

from __future__ import annotations

from typing import Any


def build_manifest(g: dict[str, Any]) -> dict[str, Any]:
    site = g.get("site", {})
    flows = []
    for name, flow in g.get("flows", {}).items():
        edges = [g["edges"][edge_id] for edge_id in flow["edges"] if edge_id in g["edges"]]
        steps = [_step(edge) for edge in edges]
        worst_status = _worst_status(edges)
        flows.append(
            {
                "name": name,
                "description": flow.get("description", ""),
                "ontology_term": flow.get("ontology_term"),
                "input_schema": flow.get("payload_schema", {}),
                "confidence": min((e.get("confidence") or 0.0) for e in edges) if edges else 0.0,
                "status": worst_status,
                "executable": worst_status == "fresh",
                "steps": steps,
            }
        )

    return {
        "wayfinder_manifest_version": "0.1",
        "site": {
            "id": site.get("id"),
            "base_url": site.get("base_url"),
            "explored_at": site.get("explored_at"),
        },
        "coverage": {
            "nodes": len(g.get("nodes", {})),
            "edges": len(g.get("edges", {})),
            "flows": len(flows),
            "fresh_flows": sum(1 for f in flows if f["executable"]),
        },
        "flows": flows,
        "notes": [
            "Flows with status != 'fresh' were discovered structurally but never executed "
            "during exploration (Wayfinder never runs write/destructive actions to map a site). "
            "Verify them against a sandbox before an agent executes them for real.",
        ],
    }


def _step(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge["id"],
        "action": edge["action"],
        "target_description": edge["target_description"],
        "locators": edge["locators"],
        "postcondition": edge.get("postcondition", {"type": "none"}),
        "mutation_class": edge["mutation_class"],
        "status": edge["status"],
    }


def _worst_status(edges: list[dict[str, Any]]) -> str:
    statuses = {e["status"] for e in edges}
    for status in ("broken", "unverified", "suspect"):
        if status in statuses:
            return status
    return "fresh" if edges else "unverified"


def build_markdown(g: dict[str, Any]) -> str:
    site = g.get("site", {})
    manifest = build_manifest(g)
    lines: list[str] = []

    lines.append(f"# Agent guide: {site.get('id', 'site')}")
    lines.append("")
    lines.append(f"Base URL: {site.get('base_url', '')}")
    lines.append(f"Explored: {site.get('explored_at', 'unknown')}")
    lines.append("")
    lines.append(
        f"Discovered {manifest['coverage']['nodes']} states, "
        f"{manifest['coverage']['edges']} actions, "
        f"{manifest['coverage']['flows']} candidate flows "
        f"({manifest['coverage']['fresh_flows']} verified end-to-end)."
    )
    lines.append("")
    lines.append(
        "Wayfinder never executes a write or destructive action to discover it — those flows "
        "are mapped structurally (selectors + presumed effect) but marked `unverified` until an "
        "owner runs verification against a sandbox. Treat `unverified` flows as a hypothesis, not "
        "a guarantee."
    )
    lines.append("")

    lines.append("## States")
    lines.append("")
    lines.append("| id | kind | description | actions available |")
    lines.append("|---|---|---|---|")
    for node_id, node in g.get("nodes", {}).items():
        actions = ", ".join(node.get("action_set") or []) or "—"
        lines.append(f"| `{node_id}` | {node.get('kind')} | {node.get('description', '')} | {actions} |")
    lines.append("")

    lines.append("## Flows")
    lines.append("")
    for flow in manifest["flows"]:
        badge = "✅ verified" if flow["executable"] else f"⚠️ {flow['status']}"
        lines.append(f"### `{flow['name']}` — {badge}")
        lines.append("")
        lines.append(flow["description"])
        lines.append("")
        schema = flow["input_schema"]
        props = schema.get("properties", {})
        if props:
            lines.append("**Input:**")
            for key in props:
                required = " (required)" if key in schema.get("required", []) else ""
                lines.append(f"- `{key}`{required}")
            lines.append("")
        lines.append("**Steps:**")
        for i, step in enumerate(flow["steps"], start=1):
            mc = step["mutation_class"]
            mc_tag = f" [{mc}]" if mc != "read" else ""
            lines.append(
                f"{i}. **{step['action']['type']}**{mc_tag} — {step['target_description']} "
                f"(`{_primary_locator(step['locators'])}`, status: {step['status']})"
            )
        lines.append("")

    lines.append("## Raw manifest")
    lines.append("")
    lines.append(
        "The machine-readable form of this document (typed input schemas + locator-bearing "
        "execution plans) is available as JSON alongside this file."
    )
    lines.append("")

    return "\n".join(lines)


def _primary_locator(locators: list[dict[str, Any]]) -> str:
    if not locators:
        return "?"
    loc = locators[0]
    if loc["strategy"] == "css":
        return loc["value"]
    if loc["strategy"] == "role":
        return f"role={loc['role']} name={loc['name']!r}"
    if loc["strategy"] == "test_id":
        return f"test_id={loc['value']}"
    if loc["strategy"] == "label":
        return f"label={loc['value']!r}"
    return str(loc)
