# Worktree 1: Target App + Drift Harness

## Mission

Build the local target website the agent will operate on. This worktree owns the demo UI only.

The site should feel like a lightweight internal operations tool, not a marketing page. It needs to be simple, deterministic, and intentionally breakable.

## Scope

Build a local app under `target/` with:

- one main page or tightly related local pages,
- a booking workflow for `book_appointment(...)`,
- a quote workflow for `request_quote(...)`,
- clear success states for both workflows,
- a deterministic UI drift toggle with `v1` and `v2`.

## Required Behaviors

### Booking Flow

Support a workflow that lets the agent:

- navigate to booking,
- fill customer name,
- choose a service,
- choose a date,
- choose a time,
- submit the booking,
- verify a confirmation state.

### Quote Flow

Support a workflow that lets the agent:

- navigate to quote request,
- fill customer or company identity,
- choose or enter a request category,
- provide notes,
- submit the quote request,
- verify a receipt or confirmation state.

## Drift Requirements

The app must support a deterministic `v1` and `v2` mode.

`v1`:

- uses the original labels and DOM structure that Worktree 2 will encode in `graph.json`.

`v2`:

- preserves the same business intent,
- changes labels, structure, or selectors enough to break at least one stored edge per skill,
- still exposes enough semantic clues for the repair step to recover.

Good drift examples:

- button text changes from "Book appointment" to "Schedule visit",
- field label changes from "Company" to "Organization",
- a button moves into a different container,
- a direct selector breaks but accessible name and nearby heading still make the target discoverable.

Bad drift examples:

- random breakage,
- fully removing the needed action,
- changing the task semantics,
- introducing nondeterministic DOM mutations that make the demo unreliable.

## Deliverables

- `target/index.html`
- any tiny local CSS or JS assets needed for the demo
- a short note in this file or a sibling note documenting:
  - the exact `v1` labels,
  - the exact `v2` changes,
  - the success-state text for both skills

## Constraints

- Do not build FastAPI.
- Do not build Playwright replay logic.
- Do not define the runtime response contract.
- Do not invent extra skills.

## Acceptance Criteria

- A human can manually complete both workflows in `v1`.
- A human can manually complete both workflows in `v2`.
- At least one selector-worthy target changes in `v2` for each skill.
- Confirmation text is obvious and stable enough for runtime verification.

## Coordination Notes

Send Worktree 2 the final:

- `v1` field labels,
- `v1` button labels,
- `v2` replacements,
- confirmation text for both flows.
