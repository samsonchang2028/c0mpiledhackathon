from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any, Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

import replay
from fallback import get_fallback_adapter

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_TARGET_SITE_URL = "http://localhost:4173/?version=v1"
DEFAULT_FALLBACK_PROVIDER = "mock"


class SkillName(str, Enum):
    book_appointment = "book_appointment"
    request_quote = "request_quote"


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookAppointmentRequest(StrictRequestModel):
    customer_name: str = Field(..., min_length=1, max_length=120)
    service: str = Field(..., min_length=1, max_length=120)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: str = Field(..., pattern=r"^\d{2}:\d{2}$")


class RequestQuoteRequest(StrictRequestModel):
    company: str = Field(..., min_length=1, max_length=160)
    category: str = Field(..., min_length=1, max_length=120)
    notes: str = Field(..., min_length=1, max_length=2000)


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["success", "fallback", "error"]
    skill_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    visited_edges: list[Any] = Field(default_factory=list)
    repairs_attempted: int = 0
    repair_outcomes: list[Any] = Field(default_factory=list)
    fallback_needed: bool = False
    fallback_used: bool = False
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    fallback_result: dict[str, Any] | None = None


app = FastAPI(
    title="Skill Graph Demo API",
    version="0.1.0",
    description="Demo-facing FastAPI wrapper for replay.run_skill.",
)


def get_target_site_url() -> str:
    return os.getenv("TARGET_SITE_URL", DEFAULT_TARGET_SITE_URL)


def get_fallback_provider_name() -> str:
    return os.getenv("FALLBACK_PROVIDER", DEFAULT_FALLBACK_PROVIDER)


def call_runtime(skill_name: str, payload: dict[str, Any], site_url: str) -> dict[str, Any]:
    try:
        report = replay.run_skill(skill_name, payload, site_url)
    except Exception as exc:
        logger.exception("runtime execution failed skill=%s", skill_name)
        raise HTTPException(
            status_code=502,
            detail=f"Runtime execution failed for '{skill_name}': {exc}",
        ) from exc

    if not isinstance(report, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Runtime returned {type(report).__name__}; expected dict.",
        )
    return report


def normalize_runtime_report(
    *,
    report: dict[str, Any],
    skill_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(report)
    normalized.setdefault("status", "error")
    normalized.setdefault("skill_name", skill_name)
    normalized.setdefault("input", payload)
    normalized.setdefault("visited_edges", [])
    normalized.setdefault("repairs_attempted", 0)
    normalized.setdefault("repair_outcomes", [])
    normalized.setdefault("fallback_needed", False)
    normalized.setdefault("fallback_used", False)
    normalized.setdefault("result", {})
    normalized.setdefault("error", None)

    if normalized["status"] not in {"success", "fallback", "error"}:
        raise HTTPException(
            status_code=502,
            detail=(
                "Runtime report status must be one of "
                "'success', 'fallback', or 'error'."
            ),
        )
    if not isinstance(normalized["result"], dict):
        normalized["result"] = {"value": normalized["result"]}
    return normalized


def should_invoke_fallback(report: dict[str, Any]) -> bool:
    runtime_requested_fallback = bool(report.get("fallback_needed")) or (
        report.get("status") == "fallback"
    )
    return runtime_requested_fallback and not bool(report.get("fallback_used"))


def run_skill_with_demo_orchestration(
    skill_name: str,
    payload: dict[str, Any],
) -> ExecutionReport:
    site_url = get_target_site_url()
    logger.info(
        "skill request skill=%s site_url=%s payload=%s",
        skill_name,
        site_url,
        payload,
    )

    runtime_report = call_runtime(skill_name=skill_name, payload=payload, site_url=site_url)
    report = normalize_runtime_report(
        report=runtime_report,
        skill_name=skill_name,
        payload=payload,
    )

    if should_invoke_fallback(report):
        try:
            adapter = get_fallback_adapter(get_fallback_provider_name())
            fallback_result = adapter.submit(
                skill_name=skill_name,
                payload=payload,
                site_url=site_url,
                runtime_report=report,
            )
        except Exception as exc:
            logger.exception("fallback failed skill=%s", skill_name)
            raise HTTPException(
                status_code=502,
                detail=f"Fallback provider failed for '{skill_name}': {exc}",
            ) from exc

        report["status"] = "fallback"
        report["fallback_needed"] = True
        report["fallback_used"] = True
        report["fallback_result"] = fallback_result

    logger.info(
        "skill response skill=%s status=%s fallback_used=%s",
        skill_name,
        report.get("status"),
        report.get("fallback_used"),
    )
    return ExecutionReport(**report)


@app.post("/skills/book_appointment", response_model=ExecutionReport)
def book_appointment(request: BookAppointmentRequest) -> ExecutionReport:
    payload = jsonable_encoder(request)
    return run_skill_with_demo_orchestration(
        skill_name=SkillName.book_appointment.value,
        payload=payload,
    )


@app.post("/skills/request_quote", response_model=ExecutionReport)
def request_quote(request: RequestQuoteRequest) -> ExecutionReport:
    payload = jsonable_encoder(request)
    return run_skill_with_demo_orchestration(
        skill_name=SkillName.request_quote.value,
        payload=payload,
    )


@app.post("/skills/{name}", response_model=ExecutionReport)
def run_named_skill(
    name: SkillName,
    payload: dict[str, Any] = Body(...),
) -> ExecutionReport:
    return run_skill_with_demo_orchestration(skill_name=name.value, payload=payload)
