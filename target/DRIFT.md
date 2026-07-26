# Target app selector contract

Run the app from `target/` with:

```powershell
python -m http.server 4173
```

Open `http://localhost:4173/?version=v1` or `http://localhost:4173/?version=v2`.

The `version` query parameter is the only source of drift. `version=v2` selects v2; missing, invalid, or `version=v1` selects v1. No random DOM changes occur.

## `v1` labels and selectors

| Workflow | Element | Exact label | CSS selector |
|---|---|---|---|
| Booking | Navigation | Book appointment | `#booking-nav` |
| Booking | Name | Customer name | `#customer-name` |
| Booking | Service | Service | `#service` |
| Booking | Date | Date | `#appointment-date` |
| Booking | Time | Time | `#appointment-time` |
| Booking | Submit | Book appointment | `#book-appointment` |
| Quote | Navigation | Request quote | `#quote-nav` |
| Quote | Identity | Company | `#company` |
| Quote | Category | Request category | `#request-category` |
| Quote | Notes | Notes | `#notes` |
| Quote | Submit | Request quote | `#request-quote` |

## Deterministic `v2` drift

| Workflow | `v1` label and selector | `v2` label and selector |
|---|---|---|
| Booking heading | Book an appointment | Schedule a service visit |
| Booking navigation | Book appointment (`#booking-nav`) | Schedule visit (`#schedule-nav`) |
| Booking name | Customer name (`#customer-name`) | Client name (`#visitor-name`) |
| Booking service | Service (`#service`) | Visit type (`#visit-type`) |
| Booking date | Date (`#appointment-date`) | Preferred day (`#visit-date`) |
| Booking time | Time (`#appointment-time`) | Arrival window (`#visit-window`) |
| Booking submit | Book appointment (`#book-appointment`) | Schedule visit (`#schedule-visit`), moved into `.v2-actions` |
| Quote heading | Request a quote | Get a project estimate |
| Quote navigation | Request quote (`#quote-nav`) | Get an estimate (`#estimate-nav`) |
| Quote identity | Company (`#company`) | Organization (`#organization`) |
| Quote category | Request category (`#request-category`) | Project type (`#project-type`) |
| Quote notes | Notes (`#notes`) | Project details (`#project-details`) |
| Quote submit | Request quote (`#request-quote`) | Send estimate request (`#send-estimate`), moved into `.v2-actions` |

The changed IDs break ID-based v1 replay. The replacement labels, headings, associated `<label>` elements, and native control types preserve semantic clues for repair.

## Stable success states

- Booking container: `#booking-confirmation`
  - Heading: **Appointment confirmed**
  - Body: **The service visit has been added to the schedule.**
- Quote container: `#quote-confirmation`
  - Heading: **Quote request received**
  - Body: **The request is ready for the estimating team.**

These confirmation selectors and strings are identical in v1 and v2.
