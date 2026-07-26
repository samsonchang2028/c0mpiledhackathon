"""Exploration engine: paste a URL, get back a verified-shape state-action graph.

Safety rule (Design doc §10 / §6): **exploration never executes a mutating
action to discover it.** Filling a text field is harmless and is captured
structurally from the live DOM without typing into it; a submit-like button is
recorded as an edge (locator, intent, presumed destination) but is never
clicked. Only read/navigational clicks (open a nav item, switch a tab, expand
a section) are actually driven during the crawl. This means the graph this
module produces for an arbitrary, unauthorized site is honest about what it
does *not* know: write and destructive edges start life as `status:
"unverified"` and only earn `"fresh"` once an owner runs verification against
a sandbox (see `wayfinder/verify.py`).

Node identity follows Design doc §4.2 / §6: nodes are clustered by their
*action set* (what intents are available), not by URL or raw DOM text, so a
handful of near-duplicate pages collapse into one node instead of exploding
the graph.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urljoin

from . import dom, graph as graph_lib, heal

try:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
except ImportError as exc:  # pragma: no cover
    sync_playwright = None
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

MAX_NODES = int(os.environ.get("WAYFINDER_EXPLORE_MAX_NODES", "10"))
MAX_DEPTH = int(os.environ.get("WAYFINDER_EXPLORE_MAX_DEPTH", "2"))
MAX_ACTIONS_PER_NODE = int(os.environ.get("WAYFINDER_EXPLORE_MAX_ACTIONS", "12"))
NAV_TIMEOUT_MS = int(os.environ.get("WAYFINDER_EXPLORE_NAV_TIMEOUT_MS", "12000"))
ACTION_TIMEOUT_MS = int(os.environ.get("WAYFINDER_EXPLORE_ACTION_TIMEOUT_MS", "3000"))
SETTLE_MS = int(os.environ.get("WAYFINDER_EXPLORE_SETTLE_MS", "350"))

_WRITE_VERBS = {
    "book", "buy", "purchase", "pay", "order", "checkout", "submit", "send",
    "confirm", "request", "apply", "subscribe", "schedule", "reserve",
    "register", "signup", "sign up", "add to cart", "place order", "donate",
    "save", "update", "create",
}
_DESTRUCTIVE_VERBS = {
    "delete", "remove", "cancel", "unsubscribe", "deactivate", "close account",
    "revoke", "terminate", "clear",
}
_CONFIRMATION_HINTS = {
    "confirmed", "success", "thank you", "received", "complete", "scheduled",
    "booked", "submitted", "welcome", "you're in", "all set",
}


class ExplorationError(Exception):
    pass


@dataclass
class _StateRecord:
    node_id: str
    signature: dict[str, Any]
    action_set: list[str]
    url: str


@dataclass
class ExplorationReport:
    graph: dict[str, Any]
    nodes_visited: int
    edges_discovered: int
    unverified_write_edges: int
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def explore_site(url: str, site_id: str | None = None) -> ExplorationReport:
    if sync_playwright is None:
        raise ExplorationError(f"Playwright is not installed: {_IMPORT_ERROR}")
    if not url or not urlparse(url).scheme:
        raise ExplorationError("url must be an absolute http(s) URL")

    origin = _origin(url)
    site_id = site_id or _slugify(urlparse(url).netloc) or "site"

    g: dict[str, Any] = {
        "schema_version": graph_lib.SCHEMA_VERSION,
        "site": {"id": site_id, "base_url": url, "explored_at": graph_lib.now()},
        "nodes": {},
        "edges": {},
        "flows": {},
    }
    report = ExplorationReport(graph=g, nodes_visited=0, edges_discovered=0, unverified_write_edges=0)

    signature_index: dict[tuple[str, str], str] = {}  # (skeleton_hash, url_bucket) -> node_id
    node_counter = 0
    edge_counter = 0

    def new_node_id(hint: str) -> str:
        nonlocal node_counter
        node_counter += 1
        slug = _slugify(hint) or f"state{node_counter}"
        candidate = slug
        n = 2
        while candidate in g["nodes"]:
            candidate = f"{slug}-{n}"
            n += 1
        return candidate

    def new_edge_id(from_node: str, hint: str) -> str:
        nonlocal edge_counter
        edge_counter += 1
        slug = _slugify(hint) or f"action{edge_counter}"
        candidate = f"{from_node}.{slug}"
        n = 2
        while candidate in g["edges"]:
            candidate = f"{from_node}.{slug}-{n}"
            n += 1
        return candidate

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=_headless())
        try:
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                raise ExplorationError(f"could not load {url}: {exc}") from exc
            page.wait_for_timeout(SETTLE_MS)

            queue: list[tuple[str, int]] = []
            start_node = _record_state(page, g, "home", signature_index, new_node_id=new_node_id)
            queue.append((start_node, 0))
            visited_nodes = {start_node}
            # Every node's re-entry path: the ordered list of read/nav edge ids
            # to click, starting from the root URL, to reach that node's exact
            # DOM state again. Most nodes are reached via a client-side click
            # with no URL change, so a plain reload cannot reproduce them —
            # replaying the recorded path is the only general way back in.
            entry_paths: dict[str, list[str]] = {start_node: []}

            while queue and len(g["nodes"]) < MAX_NODES:
                node_id, depth = queue.pop(0)
                report.nodes_visited += 1

                if not _enter_node(page, url, entry_paths[node_id], g):
                    if not _enter_node(page, url, entry_paths[node_id], g):
                        report.warnings.append(f"could not re-enter state {node_id!r}; skipping")
                        continue

                inventory = dom.capture_inventory(page)
                actions = _select_actions(inventory)[:MAX_ACTIONS_PER_NODE]

                for candidate in actions:
                    # Re-establish this node's exact state before EVERY action —
                    # a prior action in this same loop may have navigated away
                    # (e.g. a version-switch link), and evaluating a later
                    # candidate against the wrong page silently corrupts it.
                    # A single reset failure is usually transient (a slow local
                    # server, a Playwright navigation race) rather than the
                    # state genuinely being gone, so retry once before giving
                    # up on this one action — and only this one action, not
                    # every action still queued for the node.
                    if not _enter_node(page, url, entry_paths[node_id], g):
                        if not _enter_node(page, url, entry_paths[node_id], g):
                            report.warnings.append(
                                f"could not re-enter state {node_id!r} for action {candidate.get('accessible_name')!r}; skipped it"
                            )
                            continue

                    intent, mutation_class = _classify(candidate)
                    edge_id = new_edge_id(node_id, intent)

                    if mutation_class == "read" and candidate["_kind"] == "nav":
                        edge, to_node_id = _explore_read_edge(
                            page, g, node_id, edge_id, intent, candidate,
                            origin, url, signature_index, new_node_id,
                        )
                        if edge is None:
                            continue
                        g["edges"][edge_id] = edge
                        report.edges_discovered += 1
                        if to_node_id and to_node_id not in visited_nodes and depth + 1 <= MAX_DEPTH:
                            visited_nodes.add(to_node_id)
                            entry_paths[to_node_id] = entry_paths[node_id] + [edge_id]
                            queue.append((to_node_id, depth + 1))
                    else:
                        edge = _structural_edge(
                            node_id, edge_id, intent, mutation_class, candidate, g, new_node_id
                        )
                        g["edges"][edge_id] = edge
                        report.edges_discovered += 1
                        if mutation_class != "read":
                            report.unverified_write_edges += 1

                    # Re-sync the node's declared action_set as edges accrue.
                    g["nodes"][node_id]["action_set"] = sorted(
                        {e["intent"] for e in g["edges"].values() if e["from_node"] == node_id}
                    )

            # Persist each node's re-entry path so verify.py and any future
            # caller can navigate straight to a specific node's state without
            # re-deriving it — the same replay this crawl used to get there.
            for nid, path in entry_paths.items():
                if nid in g["nodes"]:
                    g["nodes"][nid]["entry_path"] = path
        finally:
            browser.close()

    _synthesize_flows(g, start_node)
    graph_lib.validate(g)
    return report


# --------------------------------------------------------------------------
# State recording
# --------------------------------------------------------------------------


def _record_state(
    page: Any,
    g: dict[str, Any],
    hint: str,
    signature_index: dict[tuple[str, str], str],
    new_node_id,
) -> str:
    signature = dom.capture_signature(page)
    inventory = dom.capture_inventory(page)
    action_set = dom.observed_action_set(inventory)
    # Node identity per Design doc §6: cluster by *available action set*, not
    # raw DOM shape. Two forms with an identical wrapper skeleton (a heading,
    # a <form>, a submit button) but different fields are different states —
    # a gross skeleton hash alone conflates them, so the field fingerprint
    # (which specific controls, by name, are present) is part of the key too.
    bucket = signature["skeleton_hash"] + "|" + _fields_fingerprint(inventory)

    existing = signature_index.get((bucket, _url_bucket(page.url)))
    if existing:
        return existing

    node_id = new_node_id(hint)
    landmarks = signature.get("landmarks") or []
    description = _describe_state(page, landmarks, hint)
    kind = _classify_node_kind(inventory, description)

    g["nodes"][node_id] = {
        "id": node_id,
        "kind": kind,
        "description": description,
        "action_set": action_set,
        "signature": {"skeleton_hash": signature["skeleton_hash"], "landmark_hash": signature["landmark_hash"]},
        "url_pattern": _url_bucket(page.url),
    }
    signature_index[(bucket, _url_bucket(page.url))] = node_id
    return node_id


def _enter_node(page: Any, root_url: str, entry_path: list[str], g: dict[str, Any]) -> bool:
    """Re-enter a node's exact state by replaying its recorded path from root.

    A plain reload only reproduces states reachable by URL. Most SPA states
    (a tab switch, a revealed form) have no URL of their own, so the only
    general way to get back to one is to redo the clicks that got us there
    the first time — the same idea `replay.py` uses to execute a flow for
    real, applied here to re-ground the crawler between probes.
    """
    try:
        page.goto(root_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        for edge_id in entry_path:
            edge = g["edges"][edge_id]
            locator = _locator_for(page, edge["locators"][0])
            locator.first.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
            locator.first.click(timeout=ACTION_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_MS)
        return True
    except (PlaywrightError, PlaywrightTimeoutError, KeyError):
        return False


# --------------------------------------------------------------------------
# Action selection & classification
# --------------------------------------------------------------------------


def _select_actions(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the interactive elements worth trying, capped by
    ``MAX_ACTIONS_PER_NODE`` — but a page's chrome (a version switcher, a
    persistent nav bar) tends to appear before its main content in DOM order,
    so a plain top-N-in-document-order slice can starve out the very form
    fields and submit button the crawl exists to find. Elements inside the
    node's own ``<form>`` are sorted first so the cap bites page chrome
    before it bites the content that matters.
    """
    seen_signature: set[str] = set()
    selected = []
    for element in inventory:
        if element.get("disabled"):
            continue
        role = element.get("role")
        if role not in {"button", "link", "combobox", "textbox", "tab"}:
            continue
        sig = f"{role}|{element.get('accessible_name')}|{element.get('type')}"
        if sig in seen_signature:
            continue
        seen_signature.add(sig)

        if role in {"button", "link"}:
            element = {**element, "_kind": "nav"}
        else:
            element = {**element, "_kind": "field"}
        selected.append(element)

    selected.sort(key=lambda el: 0 if (el.get("region") or {}).get("kind") == "form" else 1)
    return selected


def _classify(candidate: dict[str, Any]) -> tuple[str, str]:
    """Decide intent + mutation class.

    Label text alone is not reliable: a nav tab and the form it reveals can
    share the exact same accessible name (a "Request quote" tab that opens a
    form whose submit button is also labelled "Request quote"), and a
    verb-keyword match would flag the tab itself as a write action, so it
    would never be clicked and the form behind it would never be explored.
    Region context breaks the tie: only a control that lives inside a
    ``<form>`` is treated as the mutating action; the same verb outside a
    form is presumed navigational. Destructive verbs are the one exception —
    "Cancel subscription" as a bare nav link should stay unexecuted even if
    it isn't inside a form.
    """
    name = (candidate.get("accessible_name") or candidate.get("text") or "control").strip()
    lowered = name.lower()
    intent_slug = _slugify(name) or "action"
    region_kind = (candidate.get("region") or {}).get("kind")
    in_form_region = region_kind == "form"

    if candidate.get("_kind") == "field":
        return f"enter_{intent_slug}", "read"

    if any(verb in lowered for verb in _DESTRUCTIVE_VERBS):
        return intent_slug, "destructive"

    if candidate.get("type") == "submit" or in_form_region:
        return intent_slug, "write"

    if any(verb in lowered for verb in _WRITE_VERBS):
        # A write-shaped verb outside any form is almost always a nav tab or
        # section toggle (e.g. "Book appointment" as a sidebar link) —
        # explore it; the real submit button will be found, and correctly
        # classified, once the section it reveals is inventoried.
        return f"open_{intent_slug}", "read"

    return f"open_{intent_slug}", "read"


def _explore_read_edge(
    page: Any,
    g: dict[str, Any],
    from_node: str,
    edge_id: str,
    intent: str,
    candidate: dict[str, Any],
    origin: str,
    base_url: str,
    signature_index: dict[tuple[str, str], str],
    new_node_id,
) -> tuple[dict[str, Any] | None, str | None]:
    locator = heal.build_locator(candidate)
    before_signature = dom.capture_signature(page)

    try:
        target = _locator_for(page, locator)
        target.first.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        target.first.click(timeout=ACTION_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
    except (PlaywrightError, PlaywrightTimeoutError):
        return None, None

    if urlparse(page.url).netloc and _origin(page.url) != origin:
        # Left the site entirely (external link) — record the edge but don't
        # follow it, and return to base so the crawl can continue.
        edge = _edge_record(
            from_node, edge_id, "external", intent, "read", candidate, locator,
            postcondition={"type": "url_contains", "value": urlparse(page.url).netloc},
            status="unverified",
        )
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError):
            pass
        return edge, None

    to_node = _record_state(page, g, intent, signature_index, new_node_id=new_node_id)
    after_signature = dom.capture_signature(page)
    drift = dom.signature_drift(before_signature, after_signature)

    if to_node == from_node and not drift["drifted"]:
        # Dead click — nothing changed. Don't pollute the graph with a no-op edge.
        return None, None

    postcondition = _infer_postcondition(page, before_signature, after_signature)
    edge = _edge_record(
        from_node, edge_id, to_node, intent, "read", candidate, locator,
        postcondition=postcondition, status="fresh",
    )
    return edge, to_node


def _structural_edge(
    from_node: str,
    edge_id: str,
    intent: str,
    mutation_class: str,
    candidate: dict[str, Any],
    g: dict[str, Any],
    new_node_id,
) -> dict[str, Any]:
    """Record an edge without executing it (fill/select/write/destructive)."""
    locator = heal.build_locator(candidate)

    if candidate.get("_kind") == "field":
        action_type = "select" if candidate.get("role") == "combobox" else "fill"
        to_node = from_node  # form fields are self-loops within the form node
        postcondition = {"type": "action_target_has_value"}
        action = {"type": action_type, "payload_key": _slugify(candidate.get("accessible_name") or intent).replace("-", "_")}
    else:
        action_type = "click"
        # Presumed destination: a synthetic confirmation-shaped node, not executed.
        hint = f"{intent}_result"
        to_node_id = new_node_id(hint)
        g["nodes"][to_node_id] = {
            "id": to_node_id,
            "kind": "state",
            "description": f"Presumed result of '{candidate.get('accessible_name') or intent}' (not executed during exploration).",
            "action_set": [],
            "signature": None,
            "url_pattern": None,
        }
        to_node = to_node_id
        postcondition = {"type": "none"}
        action = {"type": action_type}

    return _edge_record(
        from_node, edge_id, to_node, intent, mutation_class, candidate, locator,
        postcondition=postcondition, status="unverified", action_override=action,
    )


def _edge_record(
    from_node: str,
    edge_id: str,
    to_node: str,
    intent: str,
    mutation_class: str,
    candidate: dict[str, Any],
    locator: dict[str, Any],
    postcondition: dict[str, Any],
    status: str,
    action_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action = action_override or {"type": "click"}
    return {
        "id": edge_id,
        "from_node": from_node,
        "to_node": to_node,
        "intent": intent,
        "target_description": (candidate.get("accessible_name") or candidate.get("text") or intent).strip(),
        "action": action,
        "locators": heal.build_ensemble(candidate),
        "semantics": heal.semantics_from_candidate(candidate),
        "postcondition": postcondition,
        "mutation_class": mutation_class,
        "status": status,
        "last_verified_at": graph_lib.now() if status == "fresh" else None,
        "verify_method": "exploration_replay" if status == "fresh" else None,
        "last_error": None,
        "confidence": 0.6 if status == "fresh" else 0.15,
        "heal_history": [],
    }


def _infer_postcondition(page: Any, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    drift = dom.signature_drift(before, after)
    if drift["kind"] == "structural":
        return {"type": "action_completed"}
    return {"type": "url_contains", "value": urlparse(page.url).path or "/"}


def _locator_for(page: Any, locator: dict[str, Any]) -> Any:
    strategy = locator["strategy"]
    if strategy == "css":
        return page.locator(locator["value"])
    if strategy == "role":
        import re as _re
        return page.get_by_role(locator["role"], name=_re.compile(_re.escape(locator["name"]), _re.IGNORECASE))
    if strategy == "test_id":
        return page.get_by_test_id(locator["value"])
    if strategy == "label":
        return page.get_by_label(locator["value"])
    raise ExplorationError(f"unsupported locator strategy {strategy!r}")


# --------------------------------------------------------------------------
# Node description / kind
# --------------------------------------------------------------------------


def _describe_state(page: Any, landmarks: list[str], hint: str) -> str:
    heading = next((mark.split(":", 1)[1] for mark in landmarks if mark.startswith("h1:") or mark.startswith("h2:")), None)
    try:
        title = page.title()
    except (PlaywrightError, PlaywrightTimeoutError):
        title = None
    if heading:
        return heading.strip()
    if title:
        return title.strip()
    return hint.replace("_", " ").replace("-", " ").strip().capitalize() or "Untitled state"


def _classify_node_kind(inventory: list[dict[str, Any]], description: str) -> str:
    lowered = description.lower()
    if any(hint in lowered for hint in _CONFIRMATION_HINTS):
        return "state"
    has_text_input = any(el.get("role") in {"textbox", "combobox"} for el in inventory)
    has_submit = any(
        el.get("role") == "button" and (
            el.get("type") == "submit" or any(v in (el.get("accessible_name") or "").lower() for v in _WRITE_VERBS)
        )
        for el in inventory
    )
    if has_text_input and has_submit:
        return "form"
    return "page"


# --------------------------------------------------------------------------
# Flow synthesis
# --------------------------------------------------------------------------


def _synthesize_flows(g: dict[str, Any], start_node: str) -> None:
    """Compile the discovered edges into named, agent-callable flows.

    Heuristic v1: for every write/destructive edge, walk backward through the
    graph to the nearest reachable "read" entry point, collecting any
    same-node fill/select edges along the way as the flow's payload contract.
    This is exactly the compiled-path idea in Design doc §4.4, minus the
    ontology classification (left for a later pass — see `wayfinder/ontology.py`).

    Self-loops are excluded from "incoming" edges: a form's own fill/select
    edges target their own node, so without this a field could be picked as
    its own entry point and appear twice in the compiled flow. The graph's
    start node also gets no entry edge — `replay.run_flow` already navigates
    to `site_url` before the first edge, so a flow rooted at the start node
    needs no "how did we get here" step.
    """
    incoming: dict[str, list[dict[str, Any]]] = {}
    for edge in g["edges"].values():
        if edge["from_node"] == edge["to_node"]:
            continue  # self-loop; never a real entry into a node
        incoming.setdefault(edge["to_node"], []).append(edge)

    for edge in list(g["edges"].values()):
        if edge["mutation_class"] == "read":
            continue

        form_node = edge["from_node"]
        path_edges = [
            other for other in g["edges"].values()
            if other["from_node"] == form_node and other["action"]["type"] in {"fill", "select"}
        ]
        entry_edges = [] if form_node == start_node else incoming.get(form_node, [])
        entry_edge = entry_edges[0] if entry_edges else None

        ordered = ([entry_edge] if entry_edge else []) + path_edges + [edge]
        ordered = [e for e in ordered if e is not None]
        # Defensive de-dup: preserve order, drop repeats by edge id.
        seen_ids: set[str] = set()
        deduped = []
        for step in ordered:
            if step["id"] in seen_ids:
                continue
            seen_ids.add(step["id"])
            deduped.append(step)
        ordered = deduped
        if not ordered:
            continue

        flow_name = _slugify(edge["intent"]).replace("-", "_") or "flow"
        base_name = flow_name
        n = 2
        while flow_name in g["flows"]:
            flow_name = f"{base_name}_{n}"
            n += 1

        properties = {}
        required = []
        for step in path_edges:
            key = step["action"].get("payload_key")
            if not key:
                continue
            properties[key] = {"type": "string", "min_length": 1}
            required.append(key)

        g["flows"][flow_name] = {
            "name": flow_name,
            "description": f"Discovered flow ending in '{edge['target_description']}' ({edge['mutation_class']}).",
            "edges": [e["id"] for e in ordered],
            "payload_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additional_properties": False,
            },
            "success": {"confirmation_texts": []},
            "ontology_term": None,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _url_bucket(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _fields_fingerprint(inventory: list[dict[str, Any]]) -> str:
    """A coarse fingerprint of *which* controls are present, by role + name.

    Two DOM states with the same wrapper skeleton (heading, form, submit
    button) but different fields — e.g. a booking form vs. a quote form built
    from the same layout — must not collapse into one node. Text content
    elsewhere on the page is deliberately excluded; only interactive-control
    identity counts, matching the "available action set" abstraction rule.
    """
    fields = sorted(
        f"{el.get('role')}:{(el.get('accessible_name') or el.get('name') or '').strip().lower()}"
        for el in inventory
        if el.get("role") in {"textbox", "combobox", "checkbox", "radio", "button", "link"}
    )
    import hashlib

    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()[:16]


def _slugify(text: str | None) -> str:
    if not text:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40]


def _headless() -> bool:
    return os.environ.get("WAYFINDER_EXPLORE_HEADLESS", "1").lower() not in {"0", "false", "no"}
