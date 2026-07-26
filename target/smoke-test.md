# Manual smoke-test checklist

Start the site with `cd target; python -m http.server 4173`.

## v1

- [ ] Open <http://localhost:4173/?version=v1>; the header shows v1 as active.
- [ ] Complete booking using all required fields.
- [ ] Booking ends at **Appointment confirmed**.
- [ ] Return to the v1 URL and complete the quote workflow.
- [ ] Quote ends at **Quote request received**.

## v2

- [ ] Open <http://localhost:4173/?version=v2>; the header shows v2 as active.
- [ ] Booking uses the drifted labels beginning with **Schedule visit**.
- [ ] Complete booking; it ends at **Appointment confirmed**.
- [ ] Return to the v2 URL and open **Get an estimate**.
- [ ] Complete the quote; it ends at **Quote request received**.

## Drift

- [ ] The page-header links switch between deterministic v1 and v2 URLs.
- [ ] v1 and v2 IDs and labels match [DRIFT.md](DRIFT.md).
- [ ] Both versions retain the same two workflow meanings and stable confirmation text.
