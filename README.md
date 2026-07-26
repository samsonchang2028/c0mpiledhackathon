# Wayfinder

Paste a website link. Wayfinder drives a real browser, discovers the states
and actions on the site, compiles them into named flows, and gives you back
a verified, self-healing map an agent can execute — plus a downloadable
guide (Markdown + JSON manifest) summarizing it.

This is the engine described in `wayfinder-engineering-design-plan.md`,
scoped to a single-machine hackathon build: one process, file-backed storage,
Playwright for the browser, and an optional Claude call for the cases a
heuristic genuinely can't resolve on its own.

## What it actually does

- **Explores** an arbitrary URL: clicks through navigation and reveals forms,
  but **never executes a write or destructive action to discover it** — a
  submit button is recorded (selector, locator ensemble, presumed intent)
  without being clicked. Only after verification does a flow earn
  `status: "fresh"`.
- **Compiles** the discovered edges into named flows (`book_appointment`,
  `request_quote`, ...) with a typed input schema, classified into a small
  cross-site ontology (`book`, `checkout`, `apply`, ...) so flows from
  unrelated sites become queryable by intent.
- **Verifies** edges against the live site, mutation-class-gated: `read`
  edges replay for real, `write` edges are checked structurally only (does
  the control still exist and resolve) unless you pass a sandbox, and
  `destructive` edges are *always* structural-only — Wayfinder will not
  auto-execute a destructive action, ever.
- **Self-heals** broken selectors by scoring every interactive element on the
  live page against what the edge originally looked like (role, control
  type, option set, region, name attribute, ordinal position) — explainable,
  auditable scoring, not a pre-seeded answer key. When the top two candidates
  are too close to call, an LLM adjudicates using the edge's natural-language
  intent (`WAYFINDER_HEAL_MODEL=off` disables this and falls back to the
  heuristic ranking).
- **Serves** the result as a downloadable guide (`guide.md` / `manifest.json`)
  and as callable endpoints an agent (or a human) can hit directly.

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Start the server:

```powershell
uvicorn wayfinder.server:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> — paste a URL into the box and click **Explore**.
The page shows a live graph (states as nodes, actions as edges, colored by
verification status), a list of discovered flows, a **Verify now** button,
and download links for the agent guide.

### Try it against the bundled demo site

The included `target/` app is a small two-workflow site with a deliberate
UI-drift scenario (`v1` vs `v2`) for demonstrating self-heal:

```powershell
cd target
python -m http.server 4173
```

Then paste `http://localhost:4173/?version=v1` into the Wayfinder box.
Explore it, download the guide, then run `POST
/api/sites/{site_id}/flows/book_appointment/run` with `site_url` overridden
to `http://localhost:4173/?version=v2` — the flow still succeeds, and
`repairs_attempted` / `repair_outcomes` in the response show exactly which
selectors were re-grounded and why (see `target/DRIFT.md` for what changed).

### Continuous verification (off by default)

```powershell
$env:WAYFINDER_AUTO_VERIFY_INTERVAL_S = "60"
uvicorn wayfinder.server:app --host 127.0.0.1 --port 8000
```

With this set, a background loop re-verifies a traffic/staleness-weighted
slice of every known site's edges on an interval, independent of any agent
asking — the actual "verified minutes ago" claim, not retry-on-demand. Off
by default so pasting a stranger's URL doesn't start silently hammering it.

## Architecture

```
wayfinder/
  dom.py         live DOM perception: interactive-element inventory + drift signatures
  graph.py        graph schema v2, validation, load/save
  explorer.py     build-time exploration engine (paste-a-link -> graph)
  heal.py         explainable selector scoring + LLM tie-break adjudication
  replay.py       execute a flow for real (an agent calling the tool)
  verify.py       continuous, mutation-class-gated re-verification
  outcomes.py     report_outcome feedback loop + calibrated confidence
  ontology.py     cross-site canonical-intent classification + search
  guide.py        graph -> downloadable Markdown / JSON manifest
  server.py       FastAPI routes + frontend serving
frontend/         paste-a-link UI: graph visualization, flow list, downloads
target/           local two-workflow demo site with a v1->v2 drift scenario
```

### Node identity vs. drift detection

A node's identity is its **available action set** — which controls, by role
and name, are present — not a raw DOM-skeleton hash. Two forms built from an
identical wrapper (heading, `<form>`, submit button) but with different
fields — a booking form vs. a quote form — are different nodes even though
their gross HTML shape matches. The DOM skeleton hash is still captured, but
only as a drift signal: unchanged skeleton = no structural drift; changed
skeleton = the node's outgoing edges go `suspect` pending re-verification.

### Why exploration never clicks a submit button

`replay.py` refuses to execute a `destructive` edge under any circumstance,
and `verify.py` only executes a `write` edge for real when an explicit
sandbox is configured — otherwise it's checked structurally (locator
resolves, control is visible/enabled), never invoked. `explorer.py` follows
the same rule during the initial crawl: it will click a nav link or a tab to
reveal a form, but the form's submit button is recorded, not pressed. This
means a freshly-explored site's write/destructive flows start at
`status: "unverified"` — that's not a bug to paper over, it's the map being
honest about what it does and doesn't actually know yet.

### Known limitations (v1)

- Exploration is a bounded BFS (`WAYFINDER_EXPLORE_MAX_NODES` /
  `_MAX_DEPTH` / `_MAX_ACTIONS`), not a full vision-guided agent — it's
  DOM/accessibility-tree driven, with no screenshot reasoning.
- Re-entering a node replays its recorded click path from the root
  (`node["entry_path"]`), since most SPA states have no URL of their own.
  This is correct for everything the crawler itself discovered, but a
  hand-edited or externally-authored graph without `entry_path` set falls
  back to a bare reload, which only works for root-reachable states.
- Flow synthesis (`explorer._synthesize_flows`) is a heuristic — for every
  write/destructive edge, walk backward to the nearest non-self-loop entry
  edge and collect same-node fill/select edges as the payload contract. It
  gets the demo site exactly right; a site whose form spans multiple pages
  needs a smarter compiler.
- Ontology classification is keyword-first with an LLM fallback, not
  embeddings — fine for a handful of demo sites, not for a 10,000-site index.

## API

| Route | What it does |
|---|---|
| `POST /api/explore` | `{url, site_id?}` -> explore, classify, save, return the graph |
| `GET /api/sites` | list known site graphs with coverage + self-heal rate |
| `GET /api/sites/{id}/graph` | raw graph JSON |
| `GET /api/sites/{id}/manifest` | `get_tools()`-shaped JSON (not a download) |
| `GET /api/sites/{id}/guide.md` | downloadable human/agent-readable guide |
| `GET /api/sites/{id}/manifest.json` | downloadable manifest |
| `POST /api/sites/{id}/verify` | `{sandbox?, edge_ids?}` -> re-verify (scoped or full) |
| `POST /api/sites/{id}/flows/{name}/run` | `{payload, site_url?}` -> execute for real |
| `POST /api/outcomes` | `{site_id, edge_id?, flow_name?, success, evidence?, reporter_id?}` |
| `GET /api/ontology/search?intent=` | cross-site flow search by canonical intent |

Interactive docs at `/docs` once the server is running.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `WAYFINDER_DATA_DIR` | `./data` | outcomes/verify-run/traffic JSONL + calibration |
| `WAYFINDER_SITES_DIR` | `./sites` | one `graph.json` per explored site |
| `WAYFINDER_HEAL_MODEL` | `auto` | `off` disables the LLM tie-break in `heal.py` / `ontology.py` |
| `WAYFINDER_MODEL` | `claude-opus-5` | model used for heal adjudication / ontology fallback |
| `WAYFINDER_AUTO_VERIFY_INTERVAL_S` | `0` (off) | background continuous-verification interval |
| `WAYFINDER_EXPLORE_MAX_NODES` / `_MAX_DEPTH` / `_MAX_ACTIONS` | `10` / `2` / `12` | crawl bounds |

## Note on the original two-file demo

The original hackathon-scoped demo (`server.py`, `replay.py`, `fallback.py`,
`graph.json` at the repo root, driven by `WORKTREES.md`'s hand-authored
two-skill contract) still runs — `uvicorn server:app` from the repo root —
and is kept for reference. `wayfinder/` supersedes it: instead of a graph
hand-typed for one specific site, it's a general engine that builds the same
kind of graph for whatever URL you give it.
