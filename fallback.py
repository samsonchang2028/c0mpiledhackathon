from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class FallbackAdapter(Protocol):
    provider_name: str

    def submit(
        self,
        *,
        skill_name: str,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class MockFallbackAdapter:
    provider_name = "mock"

    def submit(
        self,
        *,
        skill_name: str,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        provider_payload = self._build_provider_payload(
            skill_name=skill_name,
            payload=payload,
            site_url=site_url,
            runtime_report=runtime_report,
        )
        ticket_id = self._ticket_id(
            skill_name=skill_name,
            payload=payload,
            site_url=site_url,
        )

        result = {
            "provider": self.provider_name,
            "status": "queued",
            "ticket_id": ticket_id,
            "skill_name": skill_name,
            "site_url": site_url,
            "message": "Mock fallback recorded; no outbound call was made.",
            "provider_payload": provider_payload,
        }
        logger.info("mock fallback queued skill=%s ticket_id=%s", skill_name, ticket_id)
        return result

    def _build_provider_payload(
        self,
        *,
        skill_name: str,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        builders = {
            "book_appointment": self._build_booking_payload,
            "request_quote": self._build_quote_payload,
        }
        builder = builders.get(skill_name, self._build_generic_payload)
        return builder(payload=payload, site_url=site_url, runtime_report=runtime_report)

    def _build_booking_payload(
        self,
        *,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "handoff_type": "appointment_booking",
            "customer": payload.get("customer_name"),
            "service": payload.get("service"),
            "requested_slot": {
                "date": payload.get("date"),
                "time": payload.get("time"),
            },
            "site_url": site_url,
            "runtime_fallback": self._runtime_fallback_context(runtime_report),
        }

    def _build_quote_payload(
        self,
        *,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "handoff_type": "quote_request",
            "company": payload.get("company"),
            "category": payload.get("category"),
            "notes": payload.get("notes"),
            "site_url": site_url,
            "runtime_fallback": self._runtime_fallback_context(runtime_report),
        }

    def _build_generic_payload(
        self,
        *,
        payload: dict[str, Any],
        site_url: str,
        runtime_report: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "handoff_type": "generic_skill_handoff",
            "payload": payload,
            "site_url": site_url,
            "runtime_fallback": self._runtime_fallback_context(runtime_report),
        }

    def _runtime_fallback_context(self, runtime_report: dict[str, Any]) -> dict[str, Any]:
        result = runtime_report.get("result")
        if isinstance(result, dict) and isinstance(result.get("fallback"), dict):
            return result["fallback"]
        return {
            "error": runtime_report.get("error"),
            "visited_edges": runtime_report.get("visited_edges", []),
            "repair_outcomes": runtime_report.get("repair_outcomes", []),
        }

    def _ticket_id(
        self,
        *,
        skill_name: str,
        payload: dict[str, Any],
        site_url: str,
    ) -> str:
        seed = json.dumps(
            {
                "skill_name": skill_name,
                "payload": payload,
                "site_url": site_url,
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        return f"mock-{digest}"


def get_fallback_adapter(provider_name: str = "mock") -> FallbackAdapter:
    normalized = provider_name.strip().lower()
    if normalized == "mock":
        return MockFallbackAdapter()
    raise ValueError(
        f"Unsupported fallback provider '{provider_name}'. "
        "Set FALLBACK_PROVIDER=mock or add a provider adapter."
    )
