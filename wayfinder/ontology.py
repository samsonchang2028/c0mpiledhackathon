"""Cross-site action ontology (Design doc §4.5): classify each discovered
flow into a small set of canonical intents so flows from unrelated sites with
completely different markup become queryable as one index (`search_ontology`
in §8).

v1 classification is keyword-based over the flow's name/description/edge
text, with an optional LLM fallback for whatever the keyword table misses.
Good enough to prove the "10,000 site-graphs queryable as one index" idea on
a handful of demo sites; a real deployment would embed + classify
(§6) and let owners confirm high-value mappings.
"""

from __future__ import annotations

import json
import os
from typing import Any

CANONICAL_TERMS = [
    "search", "filter", "paginate", "authenticate", "add_to_cart", "checkout",
    "apply", "book", "request_quote", "contact", "subscribe", "register",
    "cancel", "download", "upload", "review", "donate",
]

_KEYWORDS: dict[str, list[str]] = {
    "search": ["search", "find", "lookup", "query"],
    "filter": ["filter", "sort", "narrow", "refine"],
    "paginate": ["next page", "load more", "paginate"],
    "authenticate": ["login", "log in", "sign in", "authenticate"],
    "add_to_cart": ["add to cart", "add to bag", "add item"],
    "checkout": ["checkout", "place order", "pay", "purchase", "buy"],
    "apply": ["apply", "job application", "submit application"],
    "book": ["book", "schedule", "reserve", "appointment"],
    "request_quote": ["quote", "estimate", "get pricing"],
    "contact": ["contact", "get in touch", "send message"],
    "subscribe": ["subscribe", "newsletter", "sign up for updates"],
    "register": ["register", "sign up", "create account"],
    "cancel": ["cancel", "unsubscribe", "delete account"],
    "download": ["download"],
    "upload": ["upload", "attach file"],
    "review": ["review", "rate", "leave feedback"],
    "donate": ["donate", "contribute", "give"],
}


def classify_flow(flow: dict[str, Any]) -> str | None:
    haystack = " ".join(
        [flow.get("name", ""), flow.get("description", "")]
        + [flow.get("name", "").replace("_", " ")]
    ).lower()

    best_term = None
    best_hits = 0
    for term, keywords in _KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in haystack)
        if hits > best_hits:
            best_hits = hits
            best_term = term

    if best_term:
        return best_term
    return _classify_with_model(flow)


def apply_ontology(g: dict[str, Any]) -> None:
    for flow in g.get("flows", {}).values():
        flow["ontology_term"] = classify_flow(flow)


def search_ontology(graphs: dict[str, dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    """`search_ontology(intent)` (Design doc §8): find matching flows across
    every locally-known site graph, regardless of each site's own markup."""
    intent = intent.strip().lower().replace(" ", "_")
    matches = []
    for site_id, g in graphs.items():
        for name, flow in g.get("flows", {}).items():
            term = flow.get("ontology_term")
            if term == intent or (term and intent in term) or intent in name.lower():
                matches.append(
                    {
                        "site_id": site_id,
                        "flow_name": name,
                        "ontology_term": term,
                        "description": flow.get("description"),
                        "status": _flow_status(g, flow),
                    }
                )
    return matches


def _flow_status(g: dict[str, Any], flow: dict[str, Any]) -> str:
    statuses = {g["edges"][eid]["status"] for eid in flow["edges"] if eid in g["edges"]}
    for status in ("broken", "suspect", "unverified"):
        if status in statuses:
            return status
    return "fresh" if statuses else "unverified"


def _classify_with_model(flow: dict[str, Any]) -> str | None:
    if os.environ.get("WAYFINDER_HEAL_MODEL", "auto").lower() == "off":
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=os.environ.get("WAYFINDER_MODEL", "claude-opus-5"),
            max_tokens=500,
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"term": {"type": ["string", "null"], "enum": CANONICAL_TERMS + [None]}},
                        "required": ["term"],
                        "additionalProperties": False,
                    },
                },
            },
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Classify this web action flow into exactly one canonical term from "
                        f"{CANONICAL_TERMS}, or null if none fit.\n\n"
                        f"Flow name: {flow.get('name')}\nDescription: {flow.get('description')}"
                    ),
                }
            ],
        )
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text).get("term")
    except Exception:
        return None
