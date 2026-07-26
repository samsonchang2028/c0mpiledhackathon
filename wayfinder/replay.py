"""Execute a compiled flow against the live site, self-healing broken edges.

This is the "an agent calls the tool for real" path — distinct from
`wayfinder/verify.py`, which re-checks edges on a schedule and never executes
a write/destructive action just to confirm it still works (Design doc §6/§10).
Here the caller has deliberately invoked the flow, so every edge in it,
including writes, is executed for real; the only exception is a `destructive`
edge, which this module refuses to execute under any circumstance — those are
confirmed structurally only, forever, matching the design's explicit
non-goal in §3 ("Executing destructive actions to 'prove' they work").
"""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
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

from . import dom, graph as graph_lib, heal

ACTION_TIMEOUT_MS = int(os.environ.get("WAYFINDER_ACTION_TIMEOUT_MS", "2500"))
POSTCONDITION_TIMEOUT_MS = int(os.environ.get("WAYFINDER_POSTCONDITION_TIMEOUT_MS", "2500"))
NAVIGATION_TIMEOUT_MS = int(os.environ.get("WAYFINDER_NAVIGATION_TIMEOUT_MS", "12000"))
HEAL_TIMEOUT_MS = int(os.environ.get("WAYFINDER_HEAL_TIMEOUT_MS", "1200"))


class ReplayError(Exception):
    pass


class SelectorExecutionError(ReplayError):
    def __init__(self, edge: dict[str, Any], message: str):
        super().__init__(message)
        self.edge_id = edge["id"]


class PostconditionError(ReplayError):
    pass


class DestructiveRefusal(ReplayError):
    """Raised when a flow tries to run a destructive edge for real."""


def run_flow(
    g: dict[str, Any],
    flow_name: str,
    payload: dict[str, Any],
    site_url: str,
    *,
    save: bool = True,
    site_id: str | None = None,
) -> dict[str, Any]:
    report = _new_report(flow_name, payload)

    try:
        flow = g["flows"].get(flow_name)
        if flow is None:
            raise ValueError(f"unknown flow {flow_name!r}; expected one of: {sorted(g['flows'])}")
        normalized_payload = _normalize_payload(payload, flow)
        report["input"] = normalized_payload
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    if sync_playwright is None:
        report["status"] = "error"
        report["error"] = f"Playwright is not installed: {_PLAYWRIGHT_IMPORT_ERROR}"
        return report

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=_headless())
            try:
                page = browser.new_page()
                page.goto(site_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)

                for edge_id in flow["edges"]:
                    edge = g["edges"][edge_id]
                    visit = {"edge_id": edge_id, "target_description": edge["target_description"], "status": "started"}

                    if edge["mutation_class"] == "destructive":
                        report["status"] = "refused"
                        report["error"] = (
                            f"edge {edge_id!r} is classified 'destructive' and Wayfinder never "
                            "executes destructive actions automatically. Route this to a human or "
                            "an explicit confirmation step."
                        )
                        visit["status"] = "refused"
                        report["visited_edges"].append(visit)
                        return report

                    try:
                        _execute_edge(page, edge, normalized_payload)
                        _verify_postcondition(page, edge, normalized_payload)
                        graph_lib.mark_fresh(edge, "flow_execution")
                        visit["status"] = "verified"
                        report["visited_edges"].append(visit)
                    except SelectorExecutionError as exc:
                        report["repairs_attempted"] += 1
                        repair = _attempt_heal(page, edge, normalized_payload)
                        report["repair_outcomes"].append(repair)

                        if repair["status"] != "healed":
                            graph_lib.mark_broken(edge, str(exc))
                            visit.update(status="broken", error=str(exc))
                            report["visited_edges"].append(visit)
                            if save:
                                graph_lib.save(g, site_id)
                            return _fallback_report(report, site_url, edge, str(exc), repair)

                        graph_lib.apply_heal(edge, repair["locator"], repair)
                        try:
                            _execute_edge(page, edge, normalized_payload)
                            _verify_postcondition(page, edge, normalized_payload)
                            graph_lib.mark_fresh(edge, "healed_replay")
                            visit.update(status="verified_after_heal", healed_locator=repair["locator"])
                            report["visited_edges"].append(visit)
                            if save:
                                graph_lib.save(g, site_id)
                        except (SelectorExecutionError, PostconditionError, PlaywrightError, PlaywrightTimeoutError) as retry_exc:
                            graph_lib.mark_broken(edge, str(retry_exc))
                            visit.update(status="broken_after_heal", error=str(retry_exc))
                            report["visited_edges"].append(visit)
                            if save:
                                graph_lib.save(g, site_id)
                            return _fallback_report(report, site_url, edge, str(retry_exc), repair)
                    except PostconditionError as exc:
                        graph_lib.mark_suspect(edge, str(exc))
                        visit.update(status="postcondition_failed", error=str(exc))
                        report["visited_edges"].append(visit)
                        if save:
                            graph_lib.save(g, site_id)
                        return _fallback_report(report, site_url, edge, str(exc), None)

                if save:
                    graph_lib.save(g, site_id)
            finally:
                browser.close()
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    report["status"] = "success"
    report["result"] = {"message": "flow completed", "site_url": site_url, "success": flow.get("success", {})}
    return report


def _new_report(flow_name: str, payload: Any) -> dict[str, Any]:
    return {
        "status": "error",
        "flow_name": flow_name,
        "input": payload if isinstance(payload, dict) else {},
        "visited_edges": [],
        "repairs_attempted": 0,
        "repair_outcomes": [],
        "fallback_needed": False,
        "fallback_used": False,
        "result": {},
        "error": None,
    }


def _normalize_payload(payload: dict[str, Any], flow: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    schema = flow.get("payload_schema", {})
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"missing required payload fields: {', '.join(missing)}")

    normalized: dict[str, str] = {}
    for key in properties:
        if key in payload:
            normalized[key] = str(payload[key]).strip()
    return normalized


def _execute_edge(page: Any, edge: dict[str, Any], payload: dict[str, str]) -> None:
    action = edge["action"]
    action_type = action["type"]
    if action_type == "verify":
        return

    try:
        locator = _first_resolving_locator(page, edge["locators"])
        if locator is None:
            raise SelectorExecutionError(edge, "no locator in the ensemble resolved to a visible element")
        if action_type == "click":
            locator.click(timeout=ACTION_TIMEOUT_MS)
        elif action_type == "fill":
            locator.fill(_payload_value(payload, action), timeout=ACTION_TIMEOUT_MS)
        elif action_type == "select":
            _select_or_fill(locator, _payload_value(payload, action))
        else:
            raise ReplayError(f"unsupported action type {action_type!r}")
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise SelectorExecutionError(edge, str(exc)) from exc


def _first_resolving_locator(page: Any, locators: list[dict[str, Any]]) -> Any:
    for entry in locators:
        try:
            candidate = _locator_from(page, entry).first
            candidate.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
            return candidate
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
    return None


def _locator_from(page: Any, locator: dict[str, Any]) -> Any:
    strategy = locator["strategy"]
    if strategy == "css":
        return page.locator(locator["value"])
    if strategy == "role":
        return page.get_by_role(locator["role"], name=_text_matcher(locator["name"], locator.get("exact", False)))
    if strategy == "label":
        return page.get_by_label(_text_matcher(locator["value"], locator.get("exact", False)))
    if strategy == "placeholder":
        return page.get_by_placeholder(_text_matcher(locator["value"], locator.get("exact", False)))
    if strategy == "text":
        return page.get_by_text(_text_matcher(locator["value"], locator.get("exact", False)))
    if strategy == "test_id":
        return page.get_by_test_id(locator["value"])
    raise ReplayError(f"unsupported selector strategy {strategy!r}")


def _text_matcher(value: str, exact: bool):
    if exact:
        return value
    import re

    return re.compile(re.escape(value), re.IGNORECASE)


def _select_or_fill(locator: Any, value: str) -> None:
    last_error = None
    for kwargs in ({"label": value}, {"value": value}):
        try:
            locator.select_option(timeout=ACTION_TIMEOUT_MS, **kwargs)
            return
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            last_error = exc
    try:
        locator.fill(value, timeout=ACTION_TIMEOUT_MS)
        locator.press("Enter", timeout=ACTION_TIMEOUT_MS)
        return
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        last_error = exc
    if last_error:
        raise last_error


def _payload_value(payload: dict[str, str], action: dict[str, Any]) -> str:
    key = action.get("payload_key")
    if not key:
        raise ReplayError(f"action {action!r} is missing payload_key")
    try:
        return payload[key]
    except KeyError as exc:
        raise ReplayError(f"payload missing {key!r}") from exc


def _verify_postcondition(page: Any, edge: dict[str, Any], payload: dict[str, str]) -> None:
    postcondition = edge.get("postcondition") or {"type": "none"}
    post_type = postcondition.get("type", "none")
    if post_type in {"none", "action_completed"}:
        return

    try:
        if post_type == "selector_visible":
            _wait_visible(page, postcondition["selector"], POSTCONDITION_TIMEOUT_MS)
            return
        if post_type == "any_selector_visible":
            errors = []
            for selector in postcondition.get("selectors", []):
                try:
                    _wait_visible(page, selector, POSTCONDITION_TIMEOUT_MS)
                    return
                except (PlaywrightError, PlaywrightTimeoutError) as exc:
                    errors.append(str(exc))
            raise PostconditionError("; ".join(errors) or "no postcondition selector matched")
        if post_type == "text_visible":
            values = postcondition.get("values") or [postcondition.get("value")]
            for value in [v for v in values if v]:
                try:
                    page.get_by_text(_text_matcher(value, postcondition.get("exact", False))).first.wait_for(
                        state="visible", timeout=POSTCONDITION_TIMEOUT_MS
                    )
                    return
                except (PlaywrightError, PlaywrightTimeoutError):
                    continue
            raise PostconditionError(f"none of the expected texts appeared: {values}")
        if post_type == "url_contains":
            expected = postcondition["value"]
            if expected not in page.url:
                raise PostconditionError(f"expected URL to contain {expected!r}, got {page.url!r}")
            return
        if post_type == "action_target_has_value":
            expected = payload.get(postcondition.get("payload_key") or edge["action"].get("payload_key"))
            locator = _first_resolving_locator(page, edge["locators"])
            if locator is None:
                raise PostconditionError("could not re-resolve edge locator to check its value")
            actual = locator.input_value(timeout=POSTCONDITION_TIMEOUT_MS)
            if expected is not None and actual != expected:
                raise PostconditionError(f"expected value {expected!r}, got {actual!r}")
            return
        raise PostconditionError(f"unsupported postcondition type {post_type!r}")
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise PostconditionError(str(exc)) from exc


def _wait_visible(page: Any, locator: dict[str, Any], timeout_ms: int) -> None:
    _locator_from(page, locator).first.wait_for(state="visible", timeout=timeout_ms)


def _attempt_heal(page: Any, edge: dict[str, Any], payload: dict[str, str]) -> dict[str, Any]:
    inventory = dom.capture_inventory(page)
    result = heal.heal_edge(edge, inventory, payload)
    if result["status"] != "healed":
        return {"status": "not_found", "reason": result.get("reason"), "candidates": result.get("candidates", [])}
    return {
        "status": "healed",
        "locator": result["locator"],
        "score": result.get("score"),
        "margin": result.get("margin"),
        "method": result.get("method"),
        "signals": result.get("signals"),
        "candidates": result.get("candidates", []),
    }


def _fallback_report(report, site_url, edge, reason, repair) -> dict[str, Any]:
    report["status"] = "fallback"
    report["fallback_needed"] = True
    report["fallback_used"] = False
    report["error"] = None
    report["result"] = {
        "fallback": {
            "flow_name": report["flow_name"],
            "payload": report["input"],
            "site_url": site_url,
            "reason": reason,
            "failed_edge": {
                "id": edge["id"],
                "target_description": edge["target_description"],
                "action": edge["action"],
                "status": edge["status"],
                "mutation_class": edge["mutation_class"],
            },
            "repair": repair,
        }
    }
    return report


def _headless() -> bool:
    return os.environ.get("WAYFINDER_REPLAY_HEADLESS", "1").lower() not in {"0", "false", "no"}
