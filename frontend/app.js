const exploreForm = document.querySelector("#explore-form");
const urlInput = document.querySelector("#url-input");
const exploreButton = document.querySelector("#explore-button");
const statusBox = document.querySelector("#status");
const sitesList = document.querySelector("#sites-list");
const refreshSitesButton = document.querySelector("#refresh-sites");
const resultSection = document.querySelector("#result");
const resultTitle = document.querySelector("#result-title");
const resultSubtitle = document.querySelector("#result-subtitle");
const coverageRow = document.querySelector("#coverage-row");
const flowsList = document.querySelector("#flows-list");
const verifyButton = document.querySelector("#verify-button");
const downloadMd = document.querySelector("#download-md");
const downloadJson = document.querySelector("#download-json");
const svg = document.querySelector("#graph-svg");

let currentSiteId = null;

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

function setStatus(kind, message) {
  statusBox.hidden = false;
  statusBox.className = `status ${kind}`;
  statusBox.textContent = message;
}

function clearStatus() {
  statusBox.hidden = true;
}

// ---------------------------------------------------------------------
// Known sites
// ---------------------------------------------------------------------

async function loadSites() {
  try {
    const { sites } = await fetchJSON("/api/sites");
    sitesList.innerHTML = "";
    if (!sites.length) {
      sitesList.innerHTML = '<p class="empty">No sites explored yet.</p>';
      return;
    }
    for (const site of sites) {
      const row = document.createElement("div");
      row.className = "site-row";
      row.innerHTML = `
        <div>
          <div>${site.site_id}</div>
          <div class="muted">${site.base_url || ""}</div>
        </div>
        <div class="muted">${site.nodes}n / ${site.edges}e / ${site.flows}f &middot; self-heal ${(site.self_heal_rate * 100).toFixed(0)}%</div>
      `;
      row.addEventListener("click", () => openSite(site.site_id));
      sitesList.appendChild(row);
    }
  } catch (err) {
    sitesList.innerHTML = `<p class="empty">Could not load sites: ${err.message}</p>`;
  }
}

async function openSite(siteId) {
  clearStatus();
  try {
    const graph = await fetchJSON(`/api/sites/${encodeURIComponent(siteId)}/graph`);
    currentSiteId = siteId;
    renderResult(siteId, graph, null);
  } catch (err) {
    setStatus("error", `Could not open ${siteId}: ${err.message}`);
  }
}

refreshSitesButton.addEventListener("click", loadSites);

// ---------------------------------------------------------------------
// Explore
// ---------------------------------------------------------------------

exploreForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = urlInput.value.trim();
  if (!url) return;

  exploreButton.disabled = true;
  setStatus("working", `Launching a browser and exploring ${url} — this drives real navigation, usually 10-40s...`);
  resultSection.hidden = true;

  try {
    const result = await fetchJSON("/api/explore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    currentSiteId = result.site_id;
    setStatus(
      "ok",
      `Explored ${result.site_id}: ${result.coverage.nodes} states, ${result.coverage.edges} actions, ` +
        `${result.coverage.flows} flows discovered (${result.unverified_write_edges} write/destructive edges recorded but not executed).` +
        (result.warnings.length ? ` Warnings: ${result.warnings.join("; ")}` : "")
    );
    renderResult(result.site_id, result.graph, result);
    loadSites();
  } catch (err) {
    setStatus("error", `Exploration failed: ${err.message}`);
  } finally {
    exploreButton.disabled = false;
  }
});

// ---------------------------------------------------------------------
// Verify
// ---------------------------------------------------------------------

verifyButton.addEventListener("click", async () => {
  if (!currentSiteId) return;
  verifyButton.disabled = true;
  verifyButton.textContent = "Verifying...";
  try {
    const result = await fetchJSON(`/api/sites/${encodeURIComponent(currentSiteId)}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const graph = await fetchJSON(`/api/sites/${encodeURIComponent(currentSiteId)}/graph`);
    renderResult(currentSiteId, graph, null);
    setStatus(
      "ok",
      `Verify run: ${result.summary.fresh} fresh, ${result.summary.suspect} suspect, ` +
        `${result.summary.broken} broken, ${result.summary.healed} healed. ` +
        (result.drift_events.length
          ? `Drift detected on ${result.drift_events.length} node(s).`
          : "No structural drift detected.")
    );
  } catch (err) {
    setStatus("error", `Verify failed: ${err.message}`);
  } finally {
    verifyButton.disabled = false;
    verifyButton.textContent = "Verify now";
  }
});

// ---------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------

function renderResult(siteId, graph, exploreSummary) {
  resultSection.hidden = false;
  resultTitle.textContent = siteId;
  resultSubtitle.textContent = graph.site?.base_url || "";

  downloadMd.href = `/api/sites/${encodeURIComponent(siteId)}/guide.md`;
  downloadJson.href = `/api/sites/${encodeURIComponent(siteId)}/manifest.json`;

  const nodeCount = Object.keys(graph.nodes || {}).length;
  const edgeCount = Object.keys(graph.edges || {}).length;
  const flowCount = Object.keys(graph.flows || {}).length;
  const freshEdges = Object.values(graph.edges || {}).filter((e) => e.status === "fresh").length;

  coverageRow.innerHTML = "";
  const stats = [
    ["States", nodeCount],
    ["Actions", edgeCount],
    ["Flows", flowCount],
    ["Fresh edges", `${freshEdges}/${edgeCount}`],
  ];
  for (const [label, value] of stats) {
    const el = document.createElement("div");
    el.className = "stat";
    el.innerHTML = `<b>${value}</b>${label}`;
    coverageRow.appendChild(el);
  }

  renderGraph(graph);
  renderFlows(graph);
}

function renderFlows(graph) {
  flowsList.innerHTML = "";
  const flows = Object.entries(graph.flows || {});
  if (!flows.length) {
    flowsList.innerHTML = '<p class="empty">No flows synthesized from this graph yet.</p>';
    return;
  }

  for (const [name, flow] of flows) {
    const edges = (flow.edges || []).map((id) => graph.edges[id]).filter(Boolean);
    const status = worstStatus(edges);
    const card = document.createElement("div");
    card.className = "flow-card";
    const stepsHtml = edges
      .map((e) => `<div>${e.action.type} &middot; ${escapeHtml(e.target_description)} <span class="muted">[${e.mutation_class}]</span></div>`)
      .join("");
    card.innerHTML = `
      <div class="flow-name">
        <span>${escapeHtml(name)}${flow.ontology_term ? ` <span class="muted">(${flow.ontology_term})</span>` : ""}</span>
        <span class="badge ${status}">${status}</span>
      </div>
      <p>${escapeHtml(flow.description || "")}</p>
      <div class="steps">${stepsHtml}</div>
    `;
    flowsList.appendChild(card);
  }
}

function worstStatus(edges) {
  const statuses = new Set(edges.map((e) => e.status));
  for (const s of ["broken", "suspect", "unverified"]) {
    if (statuses.has(s)) return s;
  }
  return edges.length ? "fresh" : "unverified";
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

// ---------------------------------------------------------------------
// Graph visualization: a small dependency-free force layout over SVG.
// ---------------------------------------------------------------------

function renderGraph(graph) {
  const width = 900;
  const height = 560;
  const nodeIds = Object.keys(graph.nodes || {});
  const edges = Object.values(graph.edges || {});

  if (!nodeIds.length) {
    svg.innerHTML = "";
    return;
  }

  const positions = new Map();
  nodeIds.forEach((id, index) => {
    const angle = (2 * Math.PI * index) / nodeIds.length;
    positions.set(id, {
      x: width / 2 + Math.cos(angle) * (width / 3),
      y: height / 2 + Math.sin(angle) * (height / 3),
      vx: 0,
      vy: 0,
    });
  });

  const edgePairs = edges
    .filter((e) => positions.has(e.from_node) && positions.has(e.to_node) && e.from_node !== e.to_node)
    .map((e) => [e.from_node, e.to_node]);

  // Cheap force simulation: repel all pairs, attract along edges, recenter.
  const iterations = 220;
  for (let iter = 0; iter < iterations; iter++) {
    for (const idA of nodeIds) {
      const a = positions.get(idA);
      for (const idB of nodeIds) {
        if (idA === idB) continue;
        const b = positions.get(idB);
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distSq = Math.max(dx * dx + dy * dy, 1);
        const force = 2400 / distSq;
        a.vx += (dx / Math.sqrt(distSq)) * force;
        a.vy += (dy / Math.sqrt(distSq)) * force;
      }
    }
    for (const [fromId, toId] of edgePairs) {
      const a = positions.get(fromId);
      const b = positions.get(toId);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const target = 150;
      const force = (dist - target) * 0.01;
      a.vx += (dx / dist) * force;
      a.vy += (dy / dist) * force;
      b.vx -= (dx / dist) * force;
      b.vy -= (dy / dist) * force;
    }
    for (const id of nodeIds) {
      const p = positions.get(id);
      p.vx += (width / 2 - p.x) * 0.0015;
      p.vy += (height / 2 - p.y) * 0.0015;
      p.x += p.vx * 0.6;
      p.y += p.vy * 0.6;
      p.vx *= 0.75;
      p.vy *= 0.75;
      p.x = Math.max(40, Math.min(width - 40, p.x));
      p.y = Math.max(40, Math.min(height - 40, p.y));
    }
  }

  const svgns = "http://www.w3.org/2000/svg";
  svg.innerHTML = "";
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const defs = document.createElementNS(svgns, "defs");
  defs.innerHTML = `
    <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L7,3 z" fill="#5c6270"></path>
    </marker>
  `;
  svg.appendChild(defs);

  const edgeLayer = document.createElementNS(svgns, "g");
  const selfLoopLayer = document.createElementNS(svgns, "g");
  const nodeLayer = document.createElementNS(svgns, "g");
  svg.appendChild(edgeLayer);
  svg.appendChild(selfLoopLayer);
  svg.appendChild(nodeLayer);

  for (const edge of edges) {
    const a = positions.get(edge.from_node);
    const b = positions.get(edge.to_node);
    if (!a || !b) continue;

    const strokeClass = edge.mutation_class;
    const dash = edge.mutation_class === "write" ? "6,4" : edge.mutation_class === "destructive" ? "2,4" : "none";
    const color = edge.mutation_class === "destructive" ? "#e5484d" : "#4a5164";

    if (edge.from_node === edge.to_node) {
      const loop = document.createElementNS(svgns, "circle");
      loop.setAttribute("cx", a.x + 26);
      loop.setAttribute("cy", a.y - 26);
      loop.setAttribute("r", 14);
      loop.setAttribute("fill", "none");
      loop.setAttribute("stroke", color);
      loop.setAttribute("stroke-dasharray", dash);
      loop.setAttribute("stroke-width", "1.4");
      selfLoopLayer.appendChild(loop);
      continue;
    }

    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const nodeRadius = 22;
    const x2 = b.x - (dx / dist) * nodeRadius;
    const y2 = b.y - (dy / dist) * nodeRadius;

    const line = document.createElementNS(svgns, "line");
    line.setAttribute("x1", a.x);
    line.setAttribute("y1", a.y);
    line.setAttribute("x2", x2);
    line.setAttribute("y2", y2);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "1.6");
    line.setAttribute("stroke-dasharray", dash);
    line.setAttribute("marker-end", "url(#arrow)");
    line.setAttribute("opacity", "0.85");
    const title = document.createElementNS(svgns, "title");
    title.textContent = `${edge.intent} (${edge.mutation_class}, ${edge.status})`;
    line.appendChild(title);
    edgeLayer.appendChild(line);
  }

  for (const id of nodeIds) {
    const node = graph.nodes[id];
    const p = positions.get(id);
    const statuses = Object.values(graph.edges || {})
      .filter((e) => e.from_node === id)
      .map((e) => e.status);
    const nodeStatus = worstNodeStatus(statuses);

    const g = document.createElementNS(svgns, "g");
    g.setAttribute("transform", `translate(${p.x}, ${p.y})`);

    const circle = document.createElementNS(svgns, "circle");
    circle.setAttribute("r", 20);
    circle.setAttribute("class", "node-circle");
    circle.setAttribute("fill", nodeColor(nodeStatus));
    const title = document.createElementNS(svgns, "title");
    title.textContent = `${node.description || id}\nkind: ${node.kind}\nactions: ${(node.action_set || []).join(", ")}`;
    circle.appendChild(title);
    g.appendChild(circle);

    const label = document.createElementNS(svgns, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("y", 34);
    label.textContent = truncate(node.description || id, 20);
    g.appendChild(label);

    nodeLayer.appendChild(g);
  }
}

function worstNodeStatus(statuses) {
  if (!statuses.length) return "unverified";
  for (const s of ["broken", "suspect", "unverified"]) {
    if (statuses.includes(s)) return s;
  }
  return "fresh";
}

function nodeColor(status) {
  return { fresh: "#35c56a", suspect: "#e8b339", broken: "#e5484d", unverified: "#5c6270" }[status] || "#5c6270";
}

function truncate(text, max) {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// ---------------------------------------------------------------------

loadSites();
