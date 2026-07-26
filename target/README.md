# Target app

Static demo site for the `book_appointment` and `request_quote` workflows. It has no backend and stores no submitted data.

## Run

From the repository root:

```powershell
cd target
python -m http.server 4173
```

Then open:

- v1: <http://localhost:4173/?version=v1>
- v2: <http://localhost:4173/?version=v2>

The version links in the page header switch modes. An absent or unrecognized `version` query parameter uses v1.

## Manual test: book an appointment

Repeat these steps in both v1 and v2:

1. Open the version URL.
2. Select **Book appointment** in v1 or **Schedule visit** in v2.
3. Enter a customer/client name.
4. Choose a service/visit type.
5. Choose a date/preferred day.
6. Choose a time/arrival window.
7. Select **Book appointment** in v1 or **Schedule visit** in v2.
8. Verify the heading **Appointment confirmed** and the text **The service visit has been added to the schedule.**

## Manual test: request a quote

Repeat these steps in both v1 and v2:

1. Open the version URL.
2. Select **Request quote** in v1 or **Get an estimate** in v2.
3. Enter a company/organization name.
4. Choose a request category/project type.
5. Enter notes/project details.
6. Select **Request quote** in v1 or **Send estimate request** in v2.
7. Verify the heading **Quote request received** and the text **The request is ready for the estimating team.**

See [DRIFT.md](DRIFT.md) for the exact label and selector contract.
