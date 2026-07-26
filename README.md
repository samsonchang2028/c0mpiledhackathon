# Skill Graph Demo API

This repository combines the local target app, replay runtime, and demo-facing FastAPI surface.

Worktree 3 owns only the API layer and fallback adapter. It calls Worktree 2 directly through:

```python
replay.run_skill(name, payload, site_url)
```

It does not implement the target website, graph storage, Playwright replay, or repair internals.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Target App

Start the Worktree 1 target app in a separate terminal:

```powershell
cd target
python -m http.server 4173
```

Target URLs:

- Baseline: `http://localhost:4173/?version=v1`
- Drift: `http://localhost:4173/?version=v2`

## API Configuration

```powershell
$env:TARGET_SITE_URL = "http://localhost:4173/?version=v1"
$env:FALLBACK_PROVIDER = "mock"
```

- `TARGET_SITE_URL` defaults to `http://localhost:4173/?version=v1`.
- Set `TARGET_SITE_URL` to `http://localhost:4173/?version=v2` for the drift demo.
- `FALLBACK_PROVIDER` defaults to `mock` and makes no outbound calls.
- No auth is required for the local demo API.

## Run API

```powershell
uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

## Routes

### `POST /skills/book_appointment`

```json
{
  "customer_name": "Sam Kim",
  "service": "Consultation",
  "date": "2026-07-26",
  "time": "09:00"
}
```

### `POST /skills/request_quote`

```json
{
  "company": "Compiled Hackathon",
  "category": "New installation",
  "notes": "Need a quote for a demo workflow repair project."
}
```

### `POST /skills/{name}`

`name` must be `book_appointment` or `request_quote`. This route accepts a raw JSON object and passes it through to `replay.run_skill`.

## Example Curl

```powershell
curl.exe -X POST http://127.0.0.1:8000/skills/book_appointment `
  -H "Content-Type: application/json" `
  -d '{ "customer_name": "Sam Kim", "service": "Consultation", "date": "2026-07-26", "time": "09:00" }'
```

```powershell
curl.exe -X POST http://127.0.0.1:8000/skills/request_quote `
  -H "Content-Type: application/json" `
  -d '{ "company": "Compiled Hackathon", "category": "New installation", "notes": "Need a quote for a demo workflow repair project." }'
```

## Fallback Behavior

The server calls `replay.run_skill(name, payload, site_url)` first. If the runtime report has `fallback_needed: true`, has `status: "fallback"`, and has not already marked `fallback_used: true`, the server invokes the fallback adapter.

The default mock adapter returns a stable `fallback_result` with:

- `provider`
- `status`
- `ticket_id`
- `skill_name`
- `site_url`
- `message`
- `provider_payload`

No credentials or outbound network calls are required for the mock fallback.
