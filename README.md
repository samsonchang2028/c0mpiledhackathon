# Skill Graph Demo

This project runs stored browser workflows against a local test site through a
FastAPI API. The test site has two versions:

- **v1** is the baseline UI.
- **v2** changes the UI labels and selectors to demonstrate automatic repair.

The site is static and does not save submitted data.

## 1. Setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 2. Start the test site

Open a second terminal:

```powershell
cd target
python -m http.server 4173
```

Leave this terminal running. You can inspect either site version in a browser:

- v1: <http://localhost:4173/?version=v1>
- v2: <http://localhost:4173/?version=v2>

## 3. Start the API

In the original terminal, choose the target version and start FastAPI:

```powershell
$env:TARGET_SITE_URL = "http://localhost:4173/?version=v1"
$env:FALLBACK_PROVIDER = "mock"
uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

The API is now available at <http://127.0.0.1:8000>. Interactive Swagger
documentation is available at <http://127.0.0.1:8000/docs>.

No authentication or external credentials are required.

## 4. Call the API

Open a third terminal and book an appointment:

```powershell
curl.exe -X POST http://127.0.0.1:8000/skills/book_appointment `
  -H "Content-Type: application/json" `
  -d '{ "customer_name": "Sam Kim", "service": "Consultation", "date": "2026-07-26", "time": "09:00" }'
```

Or request a quote:

```powershell
curl.exe -X POST http://127.0.0.1:8000/skills/request_quote `
  -H "Content-Type: application/json" `
  -d '{ "company": "Compiled Hackathon", "category": "New installation", "notes": "Need a quote for a demo workflow repair project." }'
```

Successful responses contain:

- `"status": "success"`
- `"visited_edges"` showing each completed browser action
- `"repairs_attempted": 0` when the stored selectors still work

## 5. Demonstrate UI repair

1. Stop the API with `Ctrl+C`.
2. Restart it against v2:

```powershell
$env:TARGET_SITE_URL = "http://localhost:4173/?version=v2"
uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

3. Repeat either API request above.

The response should still have `"status": "success"`, but
`"repairs_attempted"` will be greater than zero and `"repair_outcomes"` will
show the replacement selectors. Successful repairs are written to
`graph.json`, so later calls may not need to repair the same edges again.

If an action cannot be repaired, the API uses the local mock fallback and
returns `"status": "fallback"` with `"fallback_used": true`.

## API routes

- `POST /skills/book_appointment`
- `POST /skills/request_quote`
- `POST /skills/{name}`, where `name` is one of the two skill names above

The API calls `replay.run_skill(name, payload, site_url)` and returns its
execution report.
