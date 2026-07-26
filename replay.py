from __future__ import annotations

import argparse
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - depends on local environment.
    PlaywrightError = Exception  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_IMPORT_ERROR = exc


GRAPH_PATH = Path(os.environ.get("SKILL_GRAPH_PATH", Path(__file__).with_name("graph.json")))
ACTION_TIMEOUT_MS = int(os.environ.get("SKILL_ACTION_TIMEOUT_MS", "2500"))
REPAIR_TIMEOUT_MS = int(os.environ.get("SKILL_REPAIR_TIMEOUT_MS", "900"))
POSTCONDITION_TIMEOUT_MS = int(os.environ.get("SKILL_POSTCONDITION_TIMEOUT_MS", "2500"))
NAVIGATION_TIMEOUT_MS = int(os.environ.get("SKILL_NAVIGATION_TIMEOUT_MS", "10000"))
VALID_EDGE_STATUSES = {"verified", "stale", "broken"}


class ReplayError(Exception):
    """Base runtime error for expected replay failures."""


class SelectorExecutionError(ReplayError):
    def __init__(self, edge: dict[str, Any], selector: dict[str, Any], message: str):
        super().__init__(message)
        self.edge_id = edge["id"]
        self.selector = selector


class PostconditionError(ReplayError):
    pass


def run_skill(name: str, payload: dict, site_url: str) -> dict:
    """Replay a named skill against site_url using the file-backed graph."""
    report = _new_report(name, payload)

    try:
        if not site_url:
            raise ValueError("site_url is required")

        graph = load_graph()
        skill = _get_skill(graph, name)
        normalized_payload = _normalize_payload(payload, skill)
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

                for edge_id in skill["edges"]:
                    edge = graph["edges"][edge_id]
                    visit = _visit_record(edge)

                    try:
                        _execute_edge(page, edge, normalized_payload)
                        _verify_postcondition(page, edge, normalized_payload)
                        _mark_edge_verified(edge)
                        visit["status"] = "verified"
                        report["visited_edges"].append(visit)
                    except SelectorExecutionError as exc:
                        report["repairs_attempted"] += 1
                        repair = _attempt_relocation(page, edge, str(exc))
                        report["repair_outcomes"].append(repair)

                        if repair["status"] != "repaired":
                            _mark_edge_broken(edge, str(exc))
                            visit["status"] = "broken"
                            visit["error"] = str(exc)
                            report["visited_edges"].append(visit)
                            save_graph(graph)
                            return _fallback_report(report, site_url, edge, str(exc), repair)

                        _write_repaired_selector(edge, repair["selector"])
                        try:
                            _execute_edge(page, edge, normalized_payload)
                            _verify_postcondition(page, edge, normalized_payload)
                            _mark_edge_verified(edge)
                            visit["status"] = "verified_after_repair"
                            visit["repaired_selector"] = repair["selector"]
                            report["visited_edges"].append(visit)
                            save_graph(graph)
                        except (SelectorExecutionError, PostconditionError, PlaywrightError, PlaywrightTimeoutError) as retry_exc:
                            _mark_edge_broken(edge, str(retry_exc))
                            visit["status"] = "broken_after_repair"
                            visit["error"] = str(retry_exc)
                            report["visited_edges"].append(visit)
                            save_graph(graph)
                            return _fallback_report(report, site_url, edge, str(retry_exc), repair)
                    except PostconditionError as exc:
                        _mark_edge_stale(edge, str(exc))
                        visit["status"] = "postcondition_failed"
                        visit["error"] = str(exc)
                        report["visited_edges"].append(visit)
                        save_graph(graph)
                        return _fallback_report(report, site_url, edge, str(exc), None)

                save_graph(graph)
            finally:
                browser.close()
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report

    report["status"] = "success"
    report["result"] = {
        "message": "skill completed",
        "site_url": site_url,
        "success": skill.get("success", {}),
    }
    return report


def load_graph(path: Path = GRAPH_PATH) -> dict[str, Any]:
    graph = json.loads(path.read_text(encoding="utf-8"))
    _validate_graph(graph)
    return graph


def save_graph(graph: dict[str, Any], path: Path = GRAPH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _new_report(name: str, payload: Any) -> dict[str, Any]:
    return {
        "status": "error",
        "skill_name": name,
        "input": payload if isinstance(payload, dict) else {},
        "visited_edges": [],
        "repairs_attempted": 0,
        "repair_outcomes": [],
        "fallback_needed": False,
        "fallback_used": False,
        "result": {},
        "error": None,
    }


def _get_skill(graph: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return graph["skills"][name]
    except KeyError as exc:
        known = ", ".join(sorted(graph.get("skills", {}).keys()))
        raise ValueError(f"unknown skill {name!r}; expected one of: {known}") from exc


def _normalize_payload(payload: dict[str, Any], skill: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    schema = skill.get("payload_schema", {})
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"missing required payload fields: {', '.join(missing)}")

    if schema.get("additional_properties") is False:
        unknown = sorted(set(payload.keys()) - set(properties.keys()))
        if unknown:
            raise ValueError(f"unknown payload fields: {', '.join(unknown)}")

    normalized: dict[str, str] = {}
    for key, definition in properties.items():
        if key not in payload:
            continue
        if definition.get("type") != "string":
            raise ValueError(f"unsupported payload field type for {key!r}")
        value = str(payload[key]).strip()
        if definition.get("min_length", 0) and not value:
            raise ValueError(f"payload field {key!r} cannot be empty")
        normalized[key] = value

    return normalized


def _validate_graph(graph: dict[str, Any]) -> None:
    for key in ("nodes", "edges", "skills"):
        if not isinstance(graph.get(key), dict):
            raise ValueError(f"graph.{key} must be an object")

    for node_id, node in graph["nodes"].items():
        for field in ("id", "kind", "description"):
            if field not in node:
                raise ValueError(f"node {node_id!r} missing {field!r}")

    for edge_id, edge in graph["edges"].items():
        for field in (
            "id",
            "from_node",
            "to_node",
            "target_description",
            "selector",
            "action",
            "postcondition",
            "status",
            "last_checked",
        ):
            if field not in edge:
                raise ValueError(f"edge {edge_id!r} missing {field!r}")
        if edge["status"] not in VALID_EDGE_STATUSES:
            raise ValueError(f"edge {edge_id!r} has invalid status {edge['status']!r}")
        if edge["from_node"] not in graph["nodes"]:
            raise ValueError(f"edge {edge_id!r} references unknown from_node {edge['from_node']!r}")
        if edge["to_node"] not in graph["nodes"]:
            raise ValueError(f"edge {edge_id!r} references unknown to_node {edge['to_node']!r}")

    for skill_name, skill in graph["skills"].items():
        for field in ("name", "description", "payload_schema", "edges"):
            if field not in skill:
                raise ValueError(f"skill {skill_name!r} missing {field!r}")
        for edge_id in skill["edges"]:
            if edge_id not in graph["edges"]:
                raise ValueError(f"skill {skill_name!r} references unknown edge {edge_id!r}")


def _execute_edge(page: Any, edge: dict[str, Any], payload: dict[str, str]) -> None:
    action = edge["action"]
    action_type = action["type"]

    if action_type == "verify":
        return

    selector = edge["selector"]
    locator = _locator_from_selector(page, selector)

    try:
        target = locator.first
        target.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        if action_type == "click":
            target.click(timeout=ACTION_TIMEOUT_MS)
        elif action_type == "fill":
            target.fill(_payload_value(payload, action), timeout=ACTION_TIMEOUT_MS)
        elif action_type == "select":
            _select_or_fill(target, _payload_value(payload, action))
        else:
            raise ReplayError(f"unsupported action type {action_type!r}")
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise SelectorExecutionError(edge, selector, str(exc)) from exc


def _verify_postcondition(page: Any, edge: dict[str, Any], payload: dict[str, str]) -> None:
    postcondition = edge.get("postcondition", {"type": "none"})
    post_type = postcondition.get("type", "none")

    if post_type in {"none", "action_completed"}:
        return

    try:
        if post_type == "selector_visible":
            _wait_for_selector_metadata(page, postcondition["selector"], POSTCONDITION_TIMEOUT_MS)
            return

        if post_type == "any_selector_visible":
            errors = []
            for selector in postcondition.get("selectors", []):
                try:
                    _wait_for_selector_metadata(page, selector, POSTCONDITION_TIMEOUT_MS)
                    return
                except (PlaywrightError, PlaywrightTimeoutError, PostconditionError) as exc:
                    errors.append(str(exc))
            raise PostconditionError("; ".join(errors) or "no postcondition selector matched")

        if post_type == "text_visible":
            values = postcondition.get("values") or [postcondition.get("value")]
            for value in [item for item in values if item]:
                try:
                    page.get_by_text(_text_matcher(value, postcondition.get("exact", False))).first.wait_for(
                        state="visible",
                        timeout=POSTCONDITION_TIMEOUT_MS,
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
            expected = payload[postcondition.get("payload_key") or edge["action"]["payload_key"]]
            locator = _locator_from_selector(page, edge["selector"]).first
            actual = locator.input_value(timeout=POSTCONDITION_TIMEOUT_MS)
            if actual != expected:
                raise PostconditionError(f"expected value {expected!r}, got {actual!r}")
            return

        raise PostconditionError(f"unsupported postcondition type {post_type!r}")
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise PostconditionError(str(exc)) from exc


def _attempt_relocation(page: Any, edge: dict[str, Any], failure: str) -> dict[str, Any]:
    context = _capture_page_context(page)
    checked: list[dict[str, Any]] = []

    for selector in _relocation_selectors(edge):
        try:
            locator = _locator_from_selector(page, selector).first
            locator.wait_for(state="visible", timeout=REPAIR_TIMEOUT_MS)
            checked.append({"selector": selector, "matched": True})
            return {
                "edge_id": edge["id"],
                "status": "repaired",
                "selector": selector,
                "reason": "structured relocation matched a visible element",
                "failure": failure,
                "checked": checked,
                "page_context": context,
            }
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            checked.append({"selector": selector, "matched": False, "error": _short_error(exc)})

    return {
        "edge_id": edge["id"],
        "status": "not_found",
        "selector": None,
        "reason": "no relocation candidate matched a visible element",
        "failure": failure,
        "checked": checked,
        "page_context": context,
    }


def _relocation_selectors(edge: dict[str, Any]) -> list[dict[str, Any]]:
    relocation = edge.get("relocation", {})
    selectors: list[dict[str, Any]] = []

    for selector in relocation.get("alternate_selectors", []):
        selectors.append(_coerce_selector(selector))

    for test_id in relocation.get("test_ids", []):
        selectors.append({"strategy": "test_id", "value": test_id})

    for role in relocation.get("roles", []):
        for name in relocation.get("names", []):
            selectors.append({"strategy": "role", "role": role, "name": name, "exact": False})

    for label in relocation.get("labels", []):
        selectors.append({"strategy": "label", "value": label, "exact": False})

    for placeholder in relocation.get("placeholders", []):
        selectors.append({"strategy": "placeholder", "value": placeholder, "exact": False})

    for text in relocation.get("text", []):
        selectors.append({"strategy": "text", "value": text, "exact": False})

    return _dedupe_selectors(selectors, edge.get("selector"))


def _locator_from_selector(page: Any, selector: dict[str, Any]) -> Any:
    selector = _coerce_selector(selector)
    strategy = selector["strategy"]

    if strategy == "css":
        return page.locator(selector["value"])
    if strategy == "role":
        return page.get_by_role(selector["role"], name=_text_matcher(selector["name"], selector.get("exact", True)))
    if strategy == "label":
        return page.get_by_label(_text_matcher(selector["value"], selector.get("exact", True)))
    if strategy == "placeholder":
        return page.get_by_placeholder(_text_matcher(selector["value"], selector.get("exact", True)))
    if strategy == "text":
        return page.get_by_text(_text_matcher(selector["value"], selector.get("exact", True)))
    if strategy == "test_id":
        return page.get_by_test_id(selector["value"])

    raise ReplayError(f"unsupported selector strategy {strategy!r}")


def _coerce_selector(selector: Any) -> dict[str, Any]:
    if isinstance(selector, str):
        return {"strategy": "css", "value": selector}
    if not isinstance(selector, dict):
        raise ReplayError(f"selector must be an object or string, got {type(selector).__name__}")
    if "strategy" not in selector:
        raise ReplayError(f"selector missing strategy: {selector!r}")
    return deepcopy(selector)


def _text_matcher(value: str, exact: bool) -> str | re.Pattern[str]:
    if exact:
        return value
    return re.compile(re.escape(value), re.IGNORECASE)


def _wait_for_selector_metadata(page: Any, selector: dict[str, Any], timeout_ms: int) -> None:
    _locator_from_selector(page, selector).first.wait_for(state="visible", timeout=timeout_ms)


def _select_or_fill(locator: Any, value: str) -> None:
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

    if "last_error" in locals():
        raise last_error


def _payload_value(payload: dict[str, str], action: dict[str, Any]) -> str:
    key = action.get("payload_key")
    if not key:
        raise ReplayError(f"action {action!r} is missing payload_key")
    try:
        return payload[key]
    except KeyError as exc:
        raise ReplayError(f"payload missing {key!r}") from exc


def _capture_page_context(page: Any) -> dict[str, Any]:
    try:
        elements = page.locator("button, a, input, textarea, select, label, [role]").evaluate_all(
            """
            elements => elements.slice(0, 80).map((el, index) => ({
              index,
              tag: el.tagName.toLowerCase(),
              id: el.id || null,
              role: el.getAttribute('role'),
              name: el.getAttribute('name'),
              type: el.getAttribute('type'),
              text: (el.innerText || el.textContent || '').trim().slice(0, 120),
              ariaLabel: el.getAttribute('aria-label'),
              placeholder: el.getAttribute('placeholder'),
              testId: el.getAttribute('data-testid'),
              hidden: el.hidden || getComputedStyle(el).display === 'none' || getComputedStyle(el).visibility === 'hidden'
            }))
            """
        )
    except (PlaywrightError, PlaywrightTimeoutError):
        elements = []

    return {
        "url": page.url,
        "title": _safe_page_title(page),
        "visible_elements": [element for element in elements if not element.get("hidden")],
    }


def _safe_page_title(page: Any) -> str | None:
    try:
        return page.title()
    except (PlaywrightError, PlaywrightTimeoutError):
        return None


def _write_repaired_selector(edge: dict[str, Any], selector: dict[str, Any]) -> None:
    previous = deepcopy(edge["selector"])
    original = previous.get("original") or previous
    edge["selector"] = deepcopy(selector)
    edge["selector"]["original"] = original
    edge["selector"]["repaired_from"] = previous
    edge["selector"]["repaired_at"] = _now()
    edge["status"] = "stale"
    edge["last_error"] = None


def _mark_edge_verified(edge: dict[str, Any]) -> None:
    edge["status"] = "verified"
    edge["last_checked"] = _now()
    edge["last_error"] = None


def _mark_edge_stale(edge: dict[str, Any], error: str) -> None:
    edge["status"] = "stale"
    edge["last_error"] = error


def _mark_edge_broken(edge: dict[str, Any], error: str) -> None:
    edge["status"] = "broken"
    edge["last_error"] = error


def _fallback_report(
    report: dict[str, Any],
    site_url: str,
    edge: dict[str, Any],
    reason: str,
    repair: dict[str, Any] | None,
) -> dict[str, Any]:
    report["status"] = "fallback"
    report["fallback_needed"] = True
    report["fallback_used"] = False
    report["error"] = None
    report["result"] = {
        "fallback": {
            "skill_name": report["skill_name"],
            "payload": report["input"],
            "site_url": site_url,
            "reason": reason,
            "failed_edge": {
                "id": edge["id"],
                "target_description": edge["target_description"],
                "action": edge["action"],
                "status": edge["status"],
                "selector": edge["selector"],
            },
            "repair": repair,
        }
    }
    return report


def _visit_record(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge["id"],
        "target_description": edge["target_description"],
        "action": edge["action"]["type"],
        "status": "started",
    }


def _dedupe_selectors(selectors: list[dict[str, Any]], current_selector: dict[str, Any] | None) -> list[dict[str, Any]]:
    seen = {_selector_signature(current_selector)} if current_selector else set()
    result = []
    for selector in selectors:
        signature = _selector_signature(selector)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(selector)
    return result


def _selector_signature(selector: dict[str, Any] | None) -> str:
    if selector is None:
        return ""
    scrubbed = {key: value for key, value in selector.items() if key not in {"original", "repaired_from", "repaired_at"}}
    return json.dumps(scrubbed, sort_keys=True)


def _short_error(exc: Exception) -> str:
    return str(exc).splitlines()[0][:240]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _headless() -> bool:
    return os.environ.get("SKILL_REPLAY_HEADLESS", "1").lower() not in {"0", "false", "no"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a stored skill graph against a local target site.")
    parser.add_argument("skill_name", choices=["book_appointment", "request_quote"])
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--payload", required=True, help="JSON object matching the skill payload contract.")
    args = parser.parse_args()

    result = run_skill(args.skill_name, json.loads(args.payload), args.site_url)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"success", "fallback"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
