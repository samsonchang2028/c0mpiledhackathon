"""Self-healing: re-ground a broken edge against the live DOM.

The rule that makes this honest: **the healer may only use what was observed
when the edge was first recorded.** It never gets a list of the replacement
labels. Everything it knows lives in ``edge["semantics"]`` — the role, the
control type, the option set, the region, and the ordinal position captured at
exploration time — and everything else comes from the page as it exists now.

Scoring is explainable on purpose. Every candidate carries the signals that
produced its score, so a repair can be audited (and demoed) rather than trusted.
When the top two candidates are too close to call, an LLM adjudicates using the
edge's natural-language intent.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# Deterministic scoring weights. Tuned so that a signal which is *structurally*
# decisive (a unique control type, an unchanged option set, a stable name
# attribute) outweighs lexical similarity, which is the first thing a redesign
# throws away.
WEIGHTS = {
    "role_match": 3.0,
    "tag_match": 1.5,
    "type_match": 2.0,
    "option_overlap": 4.0,
    "payload_compatible": 2.5,
    "lexical": 2.5,
    "region_id_match": 1.5,
    "region_kind_match": 0.5,
    "ordinal_match": 1.5,
    "unique_in_scope": 3.0,
    "name_attr_stable": 3.0,
    "test_id_stable": 3.5,
    "required_match": 0.3,
}

ACCEPT_THRESHOLD = float(os.environ.get("WAYFINDER_HEAL_ACCEPT", "5.0"))
MARGIN_THRESHOLD = float(os.environ.get("WAYFINDER_HEAL_MARGIN", "1.5"))

_STOPWORDS = {"the", "a", "an", "of", "for", "your", "please", "select", "choose", "enter"}


def heal_edge(
    edge: dict[str, Any],
    inventory: list[dict[str, Any]],
    payload: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Find the live element that this edge should now point at.

    Returns a result dict with ``status`` of ``healed`` or ``not_found``, the
    proposed locator, and the full scored candidate list as evidence.
    """
    semantics = edge.get("semantics") or {}
    payload = payload or {}
    expected_value = _expected_value(edge, payload)

    scored = []
    for candidate in inventory:
        score, signals = _score(candidate, semantics, expected_value, inventory)
        scored.append({"candidate": candidate, "score": round(score, 3), "signals": signals})

    scored.sort(key=lambda entry: entry["score"], reverse=True)
    shortlist = scored[:5]

    if not shortlist:
        return {
            "status": "not_found",
            "reason": "no interactive elements on the page",
            "locator": None,
            "candidates": [],
            "method": "none",
        }

    best = shortlist[0]
    runner_up = shortlist[1] if len(shortlist) > 1 else None
    margin = best["score"] - (runner_up["score"] if runner_up else 0.0)

    if best["score"] < ACCEPT_THRESHOLD:
        return {
            "status": "not_found",
            "reason": f"best candidate scored {best['score']} < accept threshold {ACCEPT_THRESHOLD}",
            "locator": None,
            "candidates": _summarize(shortlist),
            "method": "heuristic",
        }

    method = "heuristic"
    if margin < MARGIN_THRESHOLD:
        # Structurally ambiguous. This is exactly where a model earns its keep:
        # the tie-break needs the edge's *intent*, not more DOM statistics.
        adjudicated = _adjudicate(edge, shortlist)
        if adjudicated is not None:
            best = adjudicated
            method = "model"
        else:
            method = "heuristic_low_margin"

    return {
        "status": "healed",
        "locator": build_locator(best["candidate"]),
        "score": best["score"],
        "margin": round(margin, 3),
        "signals": best["signals"],
        "candidates": _summarize(shortlist),
        "method": method,
        "matched": {
            "accessible_name": best["candidate"].get("accessible_name"),
            "css_path": best["candidate"].get("css_path"),
            "id": best["candidate"].get("id"),
        },
    }


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _score(
    candidate: dict[str, Any],
    semantics: dict[str, Any],
    expected_value: str | None,
    inventory: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    signals: dict[str, Any] = {}
    score = 0.0

    if candidate.get("disabled"):
        return -10.0, {"disabled": True}

    # Role is close to a gate: a button never heals into a text input.
    expected_role = semantics.get("role")
    if expected_role:
        if candidate.get("role") == expected_role:
            score += WEIGHTS["role_match"]
            signals["role_match"] = True
        else:
            score -= WEIGHTS["role_match"]
            signals["role_match"] = False

    if semantics.get("tag") and candidate.get("tag") == semantics["tag"]:
        score += WEIGHTS["tag_match"]
        signals["tag_match"] = True

    expected_type = semantics.get("input_type")
    if expected_type:
        if candidate.get("type") == expected_type:
            score += WEIGHTS["type_match"]
            signals["type_match"] = True
        else:
            score -= WEIGHTS["type_match"] * 0.5
            signals["type_match"] = False

    # Option sets survive redesigns far more often than labels do: the copy on a
    # <select> changes, the business values inside it usually don't.
    expected_options = semantics.get("option_labels") or []
    candidate_options = [opt.get("label") or opt.get("value") for opt in (candidate.get("options") or [])]
    if expected_options and candidate_options:
        overlap = _jaccard(_normalize_set(expected_options), _normalize_set(candidate_options))
        score += WEIGHTS["option_overlap"] * overlap
        signals["option_overlap"] = round(overlap, 3)

    # The value we are about to enter has to be enterable here.
    if expected_value:
        compatible = _value_compatible(candidate, expected_value)
        if compatible is not None:
            if compatible:
                score += WEIGHTS["payload_compatible"]
            else:
                score -= WEIGHTS["payload_compatible"]
            signals["payload_compatible"] = compatible

    observed_name = semantics.get("observed_name")
    if observed_name:
        lexical = max(
            _token_overlap(observed_name, candidate.get("accessible_name") or ""),
            _token_overlap(observed_name, candidate.get("placeholder") or ""),
            _token_overlap(observed_name, candidate.get("text") or ""),
        )
        score += WEIGHTS["lexical"] * lexical
        signals["lexical"] = round(lexical, 3)

    expected_region = semantics.get("region") or {}
    candidate_region = candidate.get("region") or {}
    if expected_region.get("id") and expected_region["id"] == candidate_region.get("id"):
        score += WEIGHTS["region_id_match"]
        signals["region_id_match"] = True
    elif expected_region.get("kind") and expected_region["kind"] == candidate_region.get("kind"):
        score += WEIGHTS["region_kind_match"]
        signals["region_kind_match"] = True

    if semantics.get("ordinal_in_region") is not None:
        if candidate.get("ordinal_in_region") == semantics["ordinal_in_region"]:
            score += WEIGHTS["ordinal_match"]
            signals["ordinal_match"] = True

    # If exactly one control of this shape exists in the region, position alone
    # identifies it regardless of what it is now called.
    if _is_unique_in_scope(candidate, inventory, semantics):
        score += WEIGHTS["unique_in_scope"]
        signals["unique_in_scope"] = True

    if semantics.get("name_attr") and candidate.get("name") == semantics["name_attr"]:
        score += WEIGHTS["name_attr_stable"]
        signals["name_attr_stable"] = True

    if semantics.get("test_id") and candidate.get("test_id") == semantics["test_id"]:
        score += WEIGHTS["test_id_stable"]
        signals["test_id_stable"] = True

    if semantics.get("required") is not None and candidate.get("required") == semantics["required"]:
        score += WEIGHTS["required_match"]

    return score, signals


def _is_unique_in_scope(
    candidate: dict[str, Any],
    inventory: list[dict[str, Any]],
    semantics: dict[str, Any],
) -> bool:
    """Is this the only control of the expected shape inside the expected region?"""
    expected_role = semantics.get("role")
    expected_type = semantics.get("input_type")
    expected_region = (semantics.get("region") or {}).get("id")

    if not expected_role:
        return False

    peers = [
        element
        for element in inventory
        if element.get("role") == expected_role
        and (expected_type is None or element.get("type") == expected_type)
        and (expected_region is None or (element.get("region") or {}).get("id") == expected_region)
    ]
    return len(peers) == 1 and peers[0].get("css_path") == candidate.get("css_path")


def _value_compatible(candidate: dict[str, Any], value: str) -> bool | None:
    """Can `value` actually be entered into this control? None if unknowable."""
    options = candidate.get("options")
    if options:
        available = _normalize_set(
            [opt.get("label") for opt in options] + [opt.get("value") for opt in options]
        )
        return _normalize(value) in available

    if candidate.get("type") == "date":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))

    if candidate.get("role") == "textbox":
        return True

    return None


def _expected_value(edge: dict[str, Any], payload: dict[str, str]) -> str | None:
    key = (edge.get("action") or {}).get("payload_key")
    if not key:
        return None
    return payload.get(key)


# --------------------------------------------------------------------------
# Locator construction
# --------------------------------------------------------------------------


def build_locator(candidate: dict[str, Any]) -> dict[str, Any]:
    """Pick the most durable way to address this element."""
    if candidate.get("test_id"):
        return {"strategy": "test_id", "value": candidate["test_id"]}
    if candidate.get("id"):
        return {"strategy": "css", "value": f"#{candidate['id']}"}
    if candidate.get("accessible_name") and candidate.get("role") in {"button", "link", "combobox", "textbox"}:
        return {
            "strategy": "role",
            "role": candidate["role"],
            "name": candidate["accessible_name"],
            "exact": False,
        }
    if candidate.get("name"):
        return {"strategy": "css", "value": f"[name='{candidate['name']}']"}
    return {"strategy": "css", "value": candidate["css_path"]}


def build_ensemble(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """A ranked set of locators for one element, most durable first.

    Storing several means a single markup tweak downgrades an edge instead of
    breaking it — the next locator in the ensemble usually still resolves.
    """
    ensemble: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(locator: dict[str, Any] | None) -> None:
        if not locator:
            return
        key = json.dumps(locator, sort_keys=True)
        if key not in seen:
            seen.add(key)
            ensemble.append(locator)

    if candidate.get("test_id"):
        add({"strategy": "test_id", "value": candidate["test_id"]})
    if candidate.get("accessible_name"):
        add(
            {
                "strategy": "role",
                "role": candidate.get("role", "button"),
                "name": candidate["accessible_name"],
                "exact": False,
            }
        )
        if candidate.get("role") in {"textbox", "combobox"}:
            add({"strategy": "label", "value": candidate["accessible_name"], "exact": False})
    if candidate.get("id"):
        add({"strategy": "css", "value": f"#{candidate['id']}"})
    if candidate.get("name"):
        add({"strategy": "css", "value": f"[name='{candidate['name']}']"})
    add({"strategy": "css", "value": candidate["css_path"]})
    return ensemble[:5]


def semantics_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Record what an element looked like at capture time.

    This is the *only* thing the healer is allowed to remember, so it has to be
    a faithful snapshot rather than a hint about the future.
    """
    return {
        "role": candidate.get("role"),
        "tag": candidate.get("tag"),
        "input_type": candidate.get("type"),
        "observed_name": candidate.get("accessible_name"),
        "name_attr": candidate.get("name"),
        "test_id": candidate.get("test_id"),
        "region": candidate.get("region"),
        "ordinal_in_region": candidate.get("ordinal_in_region"),
        "option_labels": [
            opt.get("label") or opt.get("value") for opt in (candidate.get("options") or [])
        ]
        or None,
        "required": candidate.get("required"),
    }


# --------------------------------------------------------------------------
# Model adjudication
# --------------------------------------------------------------------------

_ADJUDICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "choice_index": {
            "type": "integer",
            "description": "Index of the chosen candidate, or -1 if none is a plausible match.",
        },
        "reason": {"type": "string", "description": "One sentence explaining the choice."},
    },
    "required": ["choice_index", "reason"],
    "additionalProperties": False,
}


def _adjudicate(edge: dict[str, Any], shortlist: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Break a structural tie using the edge's stated intent.

    Returns None when the model is unavailable or declines to choose, in which
    case the caller falls back to the deterministic ranking.
    """
    if os.environ.get("WAYFINDER_HEAL_MODEL", "auto").lower() == "off":
        return None

    try:
        import anthropic
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic()
    except Exception:
        return None

    options = [
        {
            "index": index,
            "role": entry["candidate"].get("role"),
            "label": entry["candidate"].get("accessible_name"),
            "type": entry["candidate"].get("type"),
            "region": (entry["candidate"].get("region") or {}).get("id"),
            "options": [
                opt.get("label") for opt in (entry["candidate"].get("options") or [])
            ][:8],
            "heuristic_score": entry["score"],
        }
        for index, entry in enumerate(shortlist)
    ]

    semantics = edge.get("semantics") or {}
    prompt = (
        "A stored browser automation step no longer resolves because the site's UI changed.\n"
        "Pick the control on the NEW page that now serves the same purpose.\n\n"
        f"Step intent: {edge.get('target_description')}\n"
        f"Action: {edge.get('action', {}).get('type')}\n"
        f"Originally it was a '{semantics.get('role')}' labelled "
        f"{semantics.get('observed_name')!r} in region {(semantics.get('region') or {}).get('id')!r}.\n\n"
        f"Candidates on the new page:\n{json.dumps(options, indent=2)}\n\n"
        "Return the index of the control that fulfils the step's intent. "
        "Return -1 if none of them plausibly does."
    )

    try:
        response = client.messages.create(
            model=os.environ.get("WAYFINDER_MODEL", "claude-opus-5"),
            max_tokens=2000,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": _ADJUDICATION_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None

    if response.stop_reason == "refusal":
        return None

    try:
        text = next(block.text for block in response.content if block.type == "text")
        decision = json.loads(text)
    except (StopIteration, json.JSONDecodeError, AttributeError):
        return None

    index = decision.get("choice_index", -1)
    if not isinstance(index, int) or not 0 <= index < len(shortlist):
        return None

    chosen = dict(shortlist[index])
    chosen["signals"] = {**chosen.get("signals", {}), "model_reason": decision.get("reason")}
    return chosen


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_set(values) -> set[str]:
    return {_normalize(value) for value in values if value and _normalize(value)}


def _tokens(value: str | None) -> set[str]:
    return {token for token in _normalize(value).split() if token and token not in _STOPWORDS}


def _token_overlap(left: str | None, right: str | None) -> float:
    return _jaccard(_tokens(left), _tokens(right))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _summarize(shortlist: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": entry["candidate"].get("accessible_name"),
            "role": entry["candidate"].get("role"),
            "css_path": entry["candidate"].get("css_path"),
            "score": entry["score"],
            "signals": entry["signals"],
        }
        for entry in shortlist
    ]
