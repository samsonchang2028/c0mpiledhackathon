"""FastAPI serving layer (Design doc §5.4 / §8) + the frontend shell.

Routes are the API-design section of the doc made concrete:

- POST /api/explore              -> build-time exploration engine (§5.1)
- GET  /api/sites                -> list known site graphs
- GET  /api/sites/{id}/graph     -> raw graph (debugging / the visualizer)
- GET  /api/sites/{id}/manifest  -> get_tools()-shaped JSON, downloadable
- GET  /api/sites/{id}/guide.md  -> the same thing as a human-readable guide
- POST /api/sites/{id}/verify    -> on-demand verification & freshness engine (§5.2)
- POST /api/sites/{id}/flows/{name}/run -> get_path + execute, for real (§8)
- POST /api/outcomes             -> report_outcome (§7)
- GET  /api/ontology/search      -> search_ontology (§8)
- GET  /                          -> the paste-a-link frontend
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import explorer, graph as graph_lib, guide as guide_lib, ontology, outcomes, replay, verify
from .paths import REPO_ROOT

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Wayfinder", version="0.2.0", description="Verified, self-healing agent-navigation graphs.")

FRONTEND_DIR = REPO_ROOT / "frontend"


class ExploreRequest(BaseModel):
    url: str = Field(..., min_length=1)
    site_id: str | None = None


class VerifyRequest(BaseModel):
    sandbox: bool = False
    edge_ids: list[str] | None = None


class RunFlowRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    site_url: str | None = None


class OutcomeRequest(BaseModel):
    site_id: str
    edge_id: str | None = None
    flow_name: str | None = None
    success: bool
    evidence: dict[str, Any] | None = None
    reporter_id: str = "anonymous"


def _load_or_404(site_id: str) -> dict[str, Any]:
    try:
        return graph_lib.load(site_id)
    except graph_lib.GraphError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/explore")
def api_explore(request: ExploreRequest) -> dict[str, Any]:
    """Paste a URL in, get a verified-shape graph out."""
    try:
        report = explorer.explore_site(request.url, site_id=request.site_id)
    except explorer.ExplorationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("exploration failed url=%s", request.url)
        raise HTTPException(status_code=502, detail=f"exploration failed: {exc}") from exc

    g = report.graph
    ontology.apply_ontology(g)
    graph_lib.save(g)

    return {
        "site_id": g["site"]["id"],
        "base_url": g["site"]["base_url"],
        "nodes_visited": report.nodes_visited,
        "edges_discovered": report.edges_discovered,
        "unverified_write_edges": report.unverified_write_edges,
        "warnings": report.warnings,
        "coverage": {
            "nodes": len(g["nodes"]),
            "edges": len(g["edges"]),
            "flows": len(g["flows"]),
        },
        "graph": g,
    }


@app.get("/api/sites")
def api_list_sites() -> dict[str, Any]:
    sites = []
    for site_id in graph_lib.list_sites():
        try:
            g = graph_lib.load(site_id)
        except graph_lib.GraphError:
            continue
        sites.append(
            {
                "site_id": site_id,
                "base_url": g.get("site", {}).get("base_url"),
                "explored_at": g.get("site", {}).get("explored_at"),
                "nodes": len(g.get("nodes", {})),
                "edges": len(g.get("edges", {})),
                "flows": len(g.get("flows", {})),
                "self_heal_rate": outcomes.self_heal_rate(g),
            }
        )
    return {"sites": sites}


@app.get("/api/sites/{site_id}/graph")
def api_get_graph(site_id: str) -> dict[str, Any]:
    return _load_or_404(site_id)


@app.get("/api/sites/{site_id}/manifest")
def api_get_manifest(site_id: str) -> dict[str, Any]:
    g = _load_or_404(site_id)
    return guide_lib.build_manifest(g)


@app.get("/api/sites/{site_id}/guide.md")
def api_get_guide_markdown(site_id: str) -> PlainTextResponse:
    g = _load_or_404(site_id)
    markdown = guide_lib.build_markdown(g)
    return PlainTextResponse(
        markdown,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{site_id}-agent-guide.md"'},
    )


@app.get("/api/sites/{site_id}/manifest.json")
def api_download_manifest(site_id: str) -> JSONResponse:
    g = _load_or_404(site_id)
    manifest = guide_lib.build_manifest(g)
    return JSONResponse(
        manifest,
        headers={"Content-Disposition": f'attachment; filename="{site_id}-agent-manifest.json"'},
    )


@app.post("/api/sites/{site_id}/verify")
def api_verify(site_id: str, request: VerifyRequest) -> dict[str, Any]:
    g = _load_or_404(site_id)
    site_url = g.get("site", {}).get("base_url")
    if not site_url:
        raise HTTPException(status_code=422, detail="graph has no site.base_url to verify against")
    result = verify.verify_site(g, site_url, sandbox=request.sandbox, edge_ids=request.edge_ids, site_id=site_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=502, detail=result.get("error"))
    return result


@app.post("/api/sites/{site_id}/flows/{flow_name}/run")
def api_run_flow(site_id: str, flow_name: str, request: RunFlowRequest) -> dict[str, Any]:
    g = _load_or_404(site_id)
    site_url = request.site_url or g.get("site", {}).get("base_url")
    if not site_url:
        raise HTTPException(status_code=422, detail="no site_url on graph or in request")
    report = replay.run_flow(g, flow_name, request.payload, site_url, site_id=site_id)
    outcomes.record_traffic(site_id, flow_name, report.get("status", "error"))
    return report


@app.post("/api/outcomes")
def api_report_outcome(request: OutcomeRequest) -> dict[str, Any]:
    return outcomes.report_outcome(
        site_id=request.site_id,
        edge_id=request.edge_id,
        flow_name=request.flow_name,
        success=request.success,
        evidence=request.evidence,
        reporter_id=request.reporter_id,
    )


@app.get("/api/ontology/search")
def api_search_ontology(intent: str = Query(..., min_length=1)) -> dict[str, Any]:
    graphs = graph_lib.load_all()
    return {"intent": intent, "matches": ontology.search_ontology(graphs, intent)}


# --------------------------------------------------------------------------
# Continuous verification (Design doc §5.2) — off by default; the demo can
# turn this on to show edges going suspect/healed without any agent asking.
# --------------------------------------------------------------------------

_auto_verify_stop = threading.Event()


def _auto_verify_loop(interval_s: float) -> None:
    logger.info("auto-verify loop started interval_s=%s", interval_s)
    while not _auto_verify_stop.wait(interval_s):
        for site_id in graph_lib.list_sites():
            try:
                g = graph_lib.load(site_id)
                site_url = g.get("site", {}).get("base_url")
                if not site_url:
                    continue
                priority = outcomes.reverify_priority(g)
                # Traffic-weighted: touch the highest-priority slice each tick
                # rather than the whole graph, so cost scales with churn.
                slice_size = max(1, len(priority) // 3)
                verify.verify_site(g, site_url, edge_ids=priority[:slice_size], site_id=site_id)
                logger.info("auto-verify site=%s edges_checked=%s", site_id, slice_size)
            except Exception:
                logger.exception("auto-verify failed site=%s", site_id)


@app.on_event("startup")
def _start_auto_verify() -> None:
    interval_s = float(os.environ.get("WAYFINDER_AUTO_VERIFY_INTERVAL_S", "0"))
    if interval_s > 0:
        thread = threading.Thread(target=_auto_verify_loop, args=(interval_s,), daemon=True)
        thread.start()


@app.on_event("shutdown")
def _stop_auto_verify() -> None:
    _auto_verify_stop.set()


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------

if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=404, detail="frontend not built")
    return FileResponse(str(index_path))
