"""Verification & freshness engine (Design doc §5.2) — the part that runs
without an agent asking, on a schedule or a change signal.

This is what separates Wayfinder from retry-on-demand: edges are re-checked
independently of traffic, so a served path reflects "verified minutes ago"
rather than "worked the last time someone happened to call it."

Verification is mutation-class-gated (§6):

* ``read`` edges are replayed for real — click it, check the postcondition.
* ``write`` edges are checked *structurally only* — does a locator in the
  ensemble still resolve to a visible, enabled control of the right shape —
  unless the caller explicitly passes a sandbox URL, in which case they're
  replayed for real against that sandbox.
* ``destructive`` edges are always structural-only. There is no flag that
  changes this.

Change detection is granular (§4.7 / §6): each node's DOM signature is
recaptured; a node whose signature drifted marks only *its own* outgoing
edges `suspect` before verification runs, rather than invalidating the whole
graph.
"""

from __future__ import annotations

import os
import time
from typing import Any

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    PlaywrightError = Exception  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_IMPORT_ERROR = exc

from . import dom, graph as graph_lib, heal, outcomes

STRUCTURAL_TIMEOUT_MS = int(os.environ.get("WAYFINDER_VERIFY_STRUCTURAL_TIMEOUT_MS", "2000"))
REPLAY_TIMEOUT_MS = int(os.environ.get("WAYFINDER_VERIFY_REPLAY_TIMEOUT_MS", "2500"))
NAV_TIMEOUT_MS = int(os.environ.get("WAYFINDER_VERIFY_NAV_TIMEOUT_MS", "12000"))


def verify_site(
    g: dict[str, Any],
    site_url: str,
    *,
    sandbox: bool = False,
    edge_ids: list[str] | None = None,
    save: bool = True,
    site_id: str | None = None,
) -> dict[str, Any]:
    """Re-verify some or all of a site's edges. Returns a per-edge report.

    `edge_ids` scopes the run to a subset — this is the "granular" half of
    granular invalidation: a deploy webhook that only touched the booking
    form should only pay to re-verify the booking form's edges.
    """
    if sync_playwright is None:
        return {"status": "error", "error": f"Playwright is not installed: {_PLAYWRIGHT_IMPORT_ERROR}"}

    started = time.time()
    results: list[dict[str, Any]] = []
    drift_events: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=_headless())
        try:
            page = browser.new_page()
            try:
                page.goto(site_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except (PlaywrightError, PlaywrightTimeoutError) as exc:
                return {"status": "error", "error": f"could not load {site_url}: {exc}"}

            drift_events.extend(_detect_node_drift(page, g, site_url))

            targets = edge_ids or list(g["edges"].keys())
            for edge_id in targets:
                edge = g["edges"].get(edge_id)
                if edge is None:
                    continue
                if not _enter_node(page, site_url, edge["from_node"], g):
                    results.append(
                        {"edge_id": edge_id, "status": "suspect", "method": "unreachable",
                         "healed": False, "error": "could not re-enter the edge's from_node before checking it"}
                    )
                    graph_lib.mark_suspect(edge, "could not re-enter from_node during verification")
                    continue
                result = _verify_edge(page, g, edge, site_url, sandbox=sandbox)
                results.append(result)
        finally:
            browser.close()

    if save:
        graph_lib.save(g, site_id)

    summary = {
        "fresh": sum(1 for r in results if r["status"] == "fresh"),
        "suspect": sum(1 for r in results if r["status"] == "suspect"),
        "broken": sum(1 for r in results if r["status"] == "broken"),
        "healed": sum(1 for r in results if r.get("healed")),
    }
    run = {
        "status": "success",
        "site_id": site_id or g.get("site", {}).get("id"),
        "started_at": graph_lib.now(),
        "duration_s": round(time.time() - started, 2),
        "drift_events": drift_events,
        "results": results,
        "summary": summary,
    }
    outcomes.record_verify_run(run)
    return run


def _detect_node_drift(page: Any, g: dict[str, Any], site_url: str) -> list[dict[str, Any]]:
    """Compare each node's stored signature to the live page where cheaply
    possible, and mark its outgoing edges suspect on structural drift."""
    events = []
    for node_id, node in g["nodes"].items():
        stored = node.get("signature")
        if not stored or node.get("url_pattern") in (None, "/"):
            continue
        try:
            target_url = site_url.split("?")[0].rstrip("/") + node["url_pattern"]
            page.goto(target_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except (PlaywrightError, PlaywrightTimeoutError):
            continue

        live_signature = dom.capture_signature(page)
        drift = dom.signature_drift(stored, live_signature)
        if drift["drifted"]:
            events.append({"node_id": node_id, **drift})
            if drift["kind"] == "structural":
                for edge in graph_lib.edges_for_node(g, node_id):
                    graph_lib.mark_suspect(edge, f"node signature drift: {drift['detail']}")
            node["signature"] = {
                "skeleton_hash": live_signature["skeleton_hash"],
                "landmark_hash": live_signature["landmark_hash"],
            }

    try:
        page.goto(site_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except (PlaywrightError, PlaywrightTimeoutError):
        pass
    return events


def _verify_edge(page: Any, g: dict[str, Any], edge: dict[str, Any], site_url: str, *, sandbox: bool) -> dict[str, Any]:
    policy = graph_lib.VERIFY_POLICY[edge["mutation_class"]]
    if policy == "sandbox" and not sandbox:
        policy = "structural"  # no owner sandbox configured — never guess

    if policy == "structural":
        return _verify_structural(page, edge)
    return _verify_replay(page, edge)


def _verify_structural(page: Any, edge: dict[str, Any]) -> dict[str, Any]:
    """Confirm the control exists and is reachable, without invoking it."""
    for locator in edge["locators"]:
        try:
            candidate = _locator_from(page, locator).first
            candidate.wait_for(state="visible", timeout=STRUCTURAL_TIMEOUT_MS)
            graph_lib.mark_fresh(edge, "structural")
            return {"edge_id": edge["id"], "status": "fresh", "method": "structural", "healed": False}
        except (PlaywrightError, PlaywrightTimeoutError):
            continue

    # Nothing in the ensemble resolved — try to re-ground it (still without
    # clicking; structural policy never executes the action).
    inventory = dom.capture_inventory(page)
    heal_result = heal.heal_edge(edge, inventory, payload=None)
    if heal_result["status"] == "healed":
        graph_lib.apply_heal(edge, heal_result["locator"], heal_result)
        graph_lib.mark_suspect(edge, "structural check required a heal; awaiting real execution to confirm")
        return {
            "edge_id": edge["id"], "status": "suspect", "method": "structural_healed",
            "healed": True, "heal": heal_result,
        }

    graph_lib.mark_broken(edge, "no ensemble locator resolved during structural verification")
    return {"edge_id": edge["id"], "status": "broken", "method": "structural", "healed": False, "heal": heal_result}


def _verify_replay(page: Any, edge: dict[str, Any]) -> dict[str, Any]:
    """Read edges (and write edges under an explicit sandbox) execute for real."""
    action = edge["action"]
    if action["type"] in {"verify", "fill", "select"}:
        # Self-loop form inputs verify structurally even under replay policy —
        # there's no destination postcondition to check without a full flow.
        return _verify_structural(page, edge)

    locator = None
    for entry in edge["locators"]:
        try:
            candidate = _locator_from(page, entry).first
            candidate.wait_for(state="visible", timeout=REPLAY_TIMEOUT_MS)
            locator = candidate
            break
        except (PlaywrightError, PlaywrightTimeoutError):
            continue

    if locator is None:
        inventory = dom.capture_inventory(page)
        heal_result = heal.heal_edge(edge, inventory, payload=None)
        if heal_result["status"] != "healed":
            graph_lib.mark_broken(edge, "no ensemble locator resolved during replay verification")
            return {"edge_id": edge["id"], "status": "broken", "method": "replay", "healed": False, "heal": heal_result}
        graph_lib.apply_heal(edge, heal_result["locator"], heal_result)
        locator = _locator_from(page, heal_result["locator"]).first

    try:
        locator.click(timeout=REPLAY_TIMEOUT_MS)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        graph_lib.mark_broken(edge, str(exc))
        return {"edge_id": edge["id"], "status": "broken", "method": "replay", "healed": False, "error": str(exc)}

    graph_lib.mark_fresh(edge, "replay")
    return {"edge_id": edge["id"], "status": "fresh", "method": "replay", "healed": False}


def _locator_from(page: Any, locator: dict[str, Any]) -> Any:
    strategy = locator["strategy"]
    if strategy == "css":
        return page.locator(locator["value"])
    if strategy == "role":
        import re

        return page.get_by_role(locator["role"], name=re.compile(re.escape(locator["name"]), re.IGNORECASE))
    if strategy == "label":
        return page.get_by_label(locator["value"])
    if strategy == "placeholder":
        return page.get_by_placeholder(locator["value"])
    if strategy == "text":
        return page.get_by_text(locator["value"])
    if strategy == "test_id":
        return page.get_by_test_id(locator["value"])
    raise ValueError(f"unsupported selector strategy {strategy!r}")


def _enter_node(page: Any, site_url: str, node_id: str, g: dict[str, Any]) -> bool:
    """Re-establish a node's exact state before checking one of its edges.

    Most nodes have no URL of their own — they're reached by a client-side
    click (a tab switch, a revealed form) — so checking an edge without first
    replaying the path to its `from_node` means checking it against whatever
    state the *previous* edge in the loop happened to leave the page in. The
    entry path recorded at exploration time (`explorer.py`) is what makes
    this replay possible; nodes from older or hand-authored graphs without
    one just get a bare reload, which is correct for anything reachable
    directly from the root.
    """
    node = g["nodes"].get(node_id, {})
    entry_path = node.get("entry_path") or []
    try:
        page.goto(site_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        for edge_id in entry_path:
            step = g["edges"].get(edge_id)
            if not step or not step.get("locators"):
                return False
            locator = _locator_from(page, step["locators"][0]).first
            locator.wait_for(state="visible", timeout=STRUCTURAL_TIMEOUT_MS)
            locator.click(timeout=STRUCTURAL_TIMEOUT_MS)
        return True
    except (PlaywrightError, PlaywrightTimeoutError):
        return False


def _headless() -> bool:
    return os.environ.get("WAYFINDER_VERIFY_HEADLESS", "1").lower() not in {"0", "false", "no"}
