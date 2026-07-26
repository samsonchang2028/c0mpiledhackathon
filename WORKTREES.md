# Worktree Split

This repo is intentionally split into three parallel workstreams:

1. Worktree 1 owns the local target website the agent will operate on.
2. Worktree 2 owns the replay runtime, graph store, and self-heal loop.
3. Worktree 3 owns the FastAPI layer, fallback adapter, and demo-facing orchestration.

The goal is to keep parallel work unblocked by defining one stable contract up front:

- Worktree 2 is the source of truth for the runtime contract.
- Worktree 1 provides the concrete DOM, labels, and drift scenarios that the runtime must handle.
- Worktree 3 wraps the runtime contract without redesigning it.

## Shared Product Goal

Build a hackathon demo that proves:

- a stored skill graph can execute a website workflow,
- UI drift breaks the stored selector path,
- the system attempts one structured self-heal,
- successful repair updates the graph,
- unrecoverable failure triggers a pluggable fallback path instead of a dead end.

## Demo Skills

The local target app must support exactly two public skills:

- `book_appointment(...)`
- `request_quote(...)`

Both skills should succeed in `v1` and should experience at least one intentionally broken stored edge in `v2`.

## Shared Runtime Contract

Worktree 2 owns the implementation, but all worktrees should build toward this contract:

```python
def run_skill(name: str, payload: dict, site_url: str) -> dict:
    ...
```

Expected result shape:

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

## Shared Constraints

- Keep the implementation small and demo-safe.
- No database server.
- No auth system.
- No background cron verifier.
- Use direct file storage for the graph.
- Use a mock fallback provider by default unless real credentials are provided later.
- Optimize for a live demo on Sunday, July 26, 2026, not for production completeness.

## Merge Strategy

Recommended merge order:

1. Worktree 2, because it defines the runtime contract.
2. Worktree 1, because it defines the final target DOM and drift behavior.
3. Worktree 3, because it composes the other two pieces for demo use.
