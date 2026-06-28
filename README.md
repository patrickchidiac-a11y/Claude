# ✈️ Flight Price Watcher — Beirut → Mexico City

A daily watcher that tracks the round-trip economy fare from **Beirut (BEY)** to
**Mexico City (MEX)** for your dad's September visit, and tells you when to buy.

It runs as a **scheduled GitHub Action** (on GitHub's servers, every day), so it
keeps working long after this chat session ends — and it works **with zero
account setup**.

## What it watches

| Setting | Value |
|---|---|
| Route | BEY → MEX → BEY, round trip |
| Airlines | **Turkish Airlines (via Istanbul)** and **Air France (via Paris)** only |
| Stops | exactly **1**, connecting via **Paris (CDG)** or **Istanbul (IST)** |
| Max flight time | **< 25 h** |
| Passengers / cabin | 1, economy |
| Stay length | ~25 days |
| Candidate departures | **Sep 1, Sep 3, Sep 6** (returns 25 days later) — picks the cheapest |
| Currencies reported | **USD** and **MXN** |

The event is **Sep 11–17**. Every candidate lands your dad **≥5 days before** the
event and returns **well after** it, so he always has a buffer on both ends. The
margin is flexible — edit `flight_watcher/config.json` to change anything.

## How you get told to buy — no setup required

Each run records the cheapest valid fare to `price_history.json` and keeps a
single **GitHub issue** ("✈️ BEY→MEX price tracker") up to date with the latest
USD/MXN price and the full trend.

- When today's fare is the **lowest in 2+ weeks**, the watcher posts a
  **"🟢 BUY NOW"** comment on that issue (and on any new all-time low).
- GitHub automatically **emails you** and **pushes to your GitHub app** for that
  comment — so the buy alert reaches your phone and inbox with **nothing to set up**.

> Make sure you're **watching the repo** (repo page → Watch → All Activity) and
> that **Issues** notifications are enabled in the GitHub app, so the alert
> reaches you.

## Where the prices come from

By default the watcher reads live fares from **Google Flights** via the keyless
`fast-flights` library — **no API key, no account** — and **restricts the search
to Turkish Airlines and Air France** (set in `config.json` → `airlines`). These
are the same fares those carriers sell on their own sites for the Istanbul/Paris
one-stop routing, but pulled reliably instead of scraping airline websites
(which block automated access). Round-trip search filters on the outbound leg's
stop/connection/flight-time; the round-trip price is what you'd actually pay.

This keyless source is best-effort: on a day Google rate-limits the runner, that
day may be skipped (the buy logic tolerates gaps). For a rock-solid, official
feed you can optionally switch to Amadeus — see below.

## Optional upgrades

### Amadeus (more precise, filters both legs)
1. Free account at <https://developers.amadeus.com> → create an app → copy the
   **API Key** + **Secret**, move the app to **Production**.
2. Add repo secrets (**Settings → Secrets and variables → Actions**):
   `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`. The watcher uses Amadeus
   automatically when these are present.

### A dedicated daily email (in addition to the GitHub alert)
Use Gmail SMTP with an app password:
1. Create a Google **App Password**: <https://myaccount.google.com/apppasswords>.
2. Add repo secrets: `MAIL_USERNAME` (your Gmail), `MAIL_PASSWORD` (the app
   password), `MAIL_TO` (where to send). If unset, this step is skipped.

## First run

Go to the **Actions** tab → **Flight Price Watcher** → **Run workflow**. After
that it runs automatically every day at ~13:17 UTC. (GitHub disables scheduled
workflows after 60 days of repo inactivity; this watcher commits daily, which
keeps the schedule alive on its own.)

## Files

| File | Purpose |
|---|---|
| `flight_watcher/config.json` | Route, dates, constraints — edit to tune the search |
| `flight_watcher/check_prices.py` | Daily price-check + buy-signal logic |
| `flight_watcher/requirements.txt` | Python deps for the keyless source |
| `flight_watcher/price_history.json` | Append-only daily price log (USD + MXN) |
| `flight_watcher/latest_result.json` | Most recent run's result |
| `.github/workflows/flight-price-watcher.yml` | The daily schedule + notifications |
