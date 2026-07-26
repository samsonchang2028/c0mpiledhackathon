# c0mpiled-13 Writeup

## What We Built

We built a demo of a self-verifying tool layer for browser-based workflows.

The core idea is that an agent should not have to rediscover how to use a website every time it runs. Instead, a working workflow is stored as a skill graph, exposed as a callable tool, replayed through the browser, and repaired when the UI changes.

For the demo, we implemented:

- A local target web app with two workflows:
  - `book_appointment(...)`
  - `request_quote(...)`
- A deterministic `v1` / `v2` drift mode in the target app.
- A file-backed `graph.json` skill graph with nodes, edges, skill paths, selectors, postconditions, and verification status.
- A Playwright replay engine in `replay.py`.
- A one-shot self-heal path that tries to relocate a failed selector using stored semantic descriptions and visible page context.
- Graph writeback when a repair succeeds.
- A fallback path for unrecoverable failures.
- A FastAPI wrapper that exposes the skills through HTTP endpoints.

## Why It Matters

Agents often fail because they cannot reliably operate real software, not because they cannot reason.

Many internal tools, legacy SaaS dashboards, admin panels, and long-tail websites will never ship a clean API or agent protocol. Today, teams either rely on brittle RPA scripts or repeatedly ask agents to re-derive the same UI procedure from scratch.

This project treats website workflows as verified, callable skills:

- The agent calls `book_appointment(...)`, not "figure out how to use this page."
- The runtime executes known browser steps.
- Each edge is verified with a postcondition.
- If a selector breaks, the runtime attempts one repair.
- If repair works, the graph updates itself.
- If repair fails, the system reports a structured fallback condition.

The value is not a static instruction file. The value is the continuously tested skill graph and repair loop.

## Demo Flow

The demo target app has two modes:

- `v1`: baseline UI with the original selectors.
- `v2`: intentionally drifted UI with changed IDs, changed labels, and moved submit buttons.

The business meaning stays the same across both versions.

Demo sequence:

1. Start the target app.
2. Run a skill against `v1`.
3. Show a successful structured execution report.
4. Switch the target URL to `v2`.
5. Run the same skill again.
6. Show that the stored selector path breaks.
7. Show the repair attempt finding the equivalent element.
8. Show the graph updating after repair.
9. Show fallback behavior for unrecoverable failures.

## API Surface

The demo exposes:

- `POST /skills/book_appointment`
- `POST /skills/request_quote`
- `POST /skills/{name}`

The API returns an execution report with:

- runtime status,
- skill name,
- validated input,
- visited graph edges,
- repairs attempted,
- repair outcomes,
- fallback status,
- final result or error.

## What Worked

The project demonstrates the main architecture:

- A website workflow can be represented as a graph.
- A graph path can be exposed as a callable skill.
- Browser replay can execute the graph.
- UI drift can be deterministic and testable.
- Repair can be scoped to one failed edge rather than open-ended browsing.
- Fallback can be explicit instead of silent failure.

This is the shape of a production tool runtime, not just a generated markdown instruction file.

## What We Would Do With More Time

With more time, we would harden the system in these areas:

### Real LLM Repair

The current repair path is structured and deterministic. A production version would add an LLM relocation provider that consumes:

- the failed selector,
- the stored element description,
- the action type,
- page accessibility tree context,
- nearby DOM context,
- expected postcondition.

The LLM would return a strict selector/action target schema, not free-form instructions.

### Background Verification

The demo repairs on failure. A production version would also support proactive verification:

- TTL-based edge rechecks,
- scheduled skill replay in staging,
- alerting when a path becomes stale,
- automatic quarantining of broken skills.

### Skill Authoring

The demo hand-authors `graph.json`. A production version would add:

- Playwright trace import,
- codegen-to-graph conversion,
- assisted skill editing,
- visual review of graph nodes and edges,
- version history for repaired selectors.

### Better State Modeling

The demo graph is file-backed. A production version would use a real store with:

- graph versions,
- edge health history,
- tenant isolation,
- audit logs,
- repair diffs,
- rollback support.

### Safer Credential Handling

A production deployment would integrate with customer-approved access systems:

- SSO,
- service tokens,
- secrets managers,
- session storage encryption,
- customer-controlled permission policies.

The product should never store secrets in the skill graph.

### Sponsor Integration

Given more time, we would integrate a sponsor such as Hexclave as the authorization layer around skill execution:

- API keys for agent operators,
- team-level permissions,
- RBAC checks before a skill runs,
- audit metadata attached to every execution report,
- future credential vaulting for customer-owned app sessions.

That would strengthen the story that this is consent-based enterprise automation, not bot evasion.

### Production Fallbacks

The current fallback is mockable. A production version would support:

- VOYGR or Callwright phone fallback,
- human-in-the-loop escalation,
- official API handoff,
- ticket creation,
- retry policies,
- fallback status tracking.

### Evaluation

We would measure reliability with:

- replay success rate,
- repair success rate,
- mean time to repair,
- number of stale edges,
- number of human escalations,
- latency per skill call,
- UI drift recovery coverage.

## Positioning

This is not a public-web scraping product.

The wedge is customer-authorized automation for internal tools and licensed workflows where the operator has permission to use the UI but does not have a reliable API.

The reliability loop repairs UI drift. It does not bypass permission boundaries.

## Final Summary

We built the first version of a live skill runtime for websites: record a workflow, store it as a graph, expose it as a callable tool, replay it safely, verify each step, repair drift when possible, and fall back cleanly when not.

The demo is intentionally small, but the architecture points toward a bigger product: a continuously verified tool layer for the software agents actually need to use.
