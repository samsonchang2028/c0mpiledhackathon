# Worktree 2: Skill Runtime + Graph Store + Self-Heal

## Mission

Build the core product claim. This worktree owns the graph schema, replay engine, verification behavior, and one-shot repair flow.

This is the contract-defining worktree. Other worktrees should adapt to this interface instead of redesigning it.

## Scope

Build:

- `graph.json`
- `replay.py`
- repair module or helper for one structured relocation attempt
- graph read and write logic
- typed skill definitions for:
  - `book_appointment`
  - `request_quote`

## Required Runtime Entry Point

Expose a callable entry point:

```python
def run_skill(name: str, payload: dict, site_url: str) -> dict:
    ...
```

This function should be the integration seam for Worktree 3.

## Graph Requirements

Use a file-backed graph with:

- `nodes`
- `edges`
- `skills`

Each edge should carry enough information to support:

- original selector strategy,
- human-readable target description,
- action type,
- expected postcondition,
- verification status,
- `last_checked` timestamp.

Expected edge statuses:

- `verified`
- `stale`
- `broken`

Named skill paths should map the public skill names to ordered edge execution plans.

## Replay Requirements

The replay engine should:

- load a named skill,
- validate or at least normalize incoming payload data,
- open the target site with Playwright sync API,
- execute edges in order,
- verify expected postconditions after each step,
- update verification metadata on success.

## Repair Requirements

On selector failure:

1. capture current page context,
2. perform one structured relocation attempt,
3. retry the failed step once,
4. if successful, overwrite the repaired selector metadata in `graph.json`,
5. if unsuccessful, mark the edge `broken` and return structured fallback trigger data.

Do not build a recursive multi-attempt repair loop.

## Return Contract

Return a structured execution report with at least:

```json
{
  "status": "success | fallback | error",
  "skill_name": "book_appointment",
  "input": {},
  "visited_edges": [],
  "repairs_attempted": 0,
  "repair_outcomes": [],
  "fallback_needed": false,
  "fallback_used": false,
  "result": {},
  "error": null
}
```

If fallback is needed, include enough context for Worktree 3 to call the fallback adapter without re-deriving the task.

## Constraints

- Do not build the HTML target app.
- Do not build the FastAPI server.
- Do not hardwire a specific external fallback provider into replay.
- Keep graph storage file-backed and local.

## Acceptance Criteria

- Both skills run successfully against the `v1` target.
- At least one edge can fail and repair successfully against `v2`.
- A repaired edge is written back to `graph.json`.
- An unrecoverable edge returns fallback-ready structured data.

## Coordination Notes

Worktree 1 will finalize the exact labels and DOM drift details. Align selectors and verification text to their delivered `v1` and `v2` notes once available.
