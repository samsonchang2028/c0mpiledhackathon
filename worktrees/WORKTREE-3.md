# Worktree 3: FastAPI Surface + Fallback + Demo Orchestration

## Mission

Build the demo-facing backend surface around the runtime. This worktree owns the API, fallback adapter, operator ergonomics, and clean demo execution flow.

This layer should compose Worktree 2, not redesign it.

## Scope

Build:

- `server.py`
- request and response models
- `fallback.py`
- minimal run instructions for local demo use
- any tiny glue needed to point the runtime at the local target site

## Required API Surface

Support:

- `POST /skills/book_appointment`
- `POST /skills/request_quote`

You may also support:

- `POST /skills/{name}`

if that simplifies internal implementation, but the two concrete routes should remain first-class demo endpoints.

## Request Model

Use typed JSON validation at the API boundary.

Examples of likely fields:

- booking: customer name, service, date, time
- quote: customer or company, category, notes

The exact field set should align with Worktree 1's final form fields and Worktree 2's skill payload expectations.

## Response Model

Return the structured execution report produced by Worktree 2, preserving:

- `status`
- `skill_name`
- `input`
- `visited_edges`
- `repairs_attempted`
- `repair_outcomes`
- `fallback_needed`
- `fallback_used`
- `result`
- `error`

If fallback is invoked, include the fallback result clearly in the response.

## Fallback Requirements

Implement a pluggable fallback adapter in `fallback.py`.

Default behavior:

- mock or log-only provider,
- no real outbound call required,
- stable structured return value for demo use.

Design requirement:

- keep provider-specific payload creation isolated,
- make it easy to replace the mock with VOYGR or Callwright later.

## Demo Ergonomics

Make the project easy to run live:

- straightforward server startup,
- obvious local target URL configuration,
- clear failure messages,
- useful request and response logging for a live demo.

## Constraints

- Do not build Playwright replay internals.
- Do not define a different runtime contract than Worktree 2.
- Do not expand product scope beyond the two agreed skills.

## Acceptance Criteria

- Both endpoints validate input cleanly.
- The API can invoke Worktree 2's runtime entry point.
- Runtime success is returned cleanly.
- Runtime fallback is returned cleanly.
- The fallback provider can remain fully mocked without breaking the demo.

## Coordination Notes

Wait for Worktree 2's runtime contract if needed, but do not block on perfection. Build the API around the agreed `run_skill(name, payload, site_url)` seam and adjust imports once their implementation lands.
