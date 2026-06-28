# ✈️ Flight Price Watcher — Beirut → Mexico City

A daily watcher that tracks the round-trip economy fare from **Beirut (BEY)** to
**Mexico City (MEX)** for your dad's September visit, and tells you when to buy.

It runs as a **scheduled GitHub Action** (on GitHub's servers, every day), so it
keeps working long after this chat session ends.

## What it watches

| Setting | Value |
|---|---|
| Route | BEY → MEX → BEY, round trip |
| Stops | exactly **1**, connecting via **Paris (CDG)** or **Istanbul (IST)** |
| Max journey time | **< 25 h** each direction |
| Passengers / cabin | 1, economy |
| Stay length | ~25 days |
| Candidate departure dates | **Sep 1, Sep 3, Sep 6** (returns 25 days later) — picks the cheapest |
| Currencies reported | **USD** and **MXN** |

The event is **Sep 11–17**. Every candidate lands your dad **at least 5 days
before** the event (by Sep 6) and returns **well after** it (Sep 26 – Oct 1), so
he always has a comfortable buffer on both ends. The margin is flexible — edit
`flight_watcher/config.json` to change dates, stay length, or constraints.

## The buy signal

Each day it records the cheapest valid fare to `flight_watcher/price_history.json`.
Once there are **≥ 14 days of history**, if today's fare is the **lowest ever
recorded**, it opens a GitHub issue titled **"🟢 BUY signal …"** — you get a
GitHub notification (email/app). That's your cue to book.

Every run also writes a summary you can read in the **Actions** tab, with the
current price in USD and MXN whether or not it's a buy.

## One-time setup (≈ 5 minutes)

The price data comes from the **Amadeus Self-Service API**, which has a free tier.

1. Create a free account at <https://developers.amadeus.com>.
2. Create an app to get an **API Key** and **API Secret**.
3. Move the app to **Production** (free) so it returns live fares — the default
   host is `api.amadeus.com`. (To experiment first, set repo variable
   `AMADEUS_HOST=test.api.amadeus.com` to use the sandbox.)
4. In this repo: **Settings → Secrets and variables → Actions → New repository secret**
   and add:
   - `AMADEUS_CLIENT_ID` = your API Key
   - `AMADEUS_CLIENT_SECRET` = your API Secret
5. Go to the **Actions** tab, pick **Flight Price Watcher**, and click
   **Run workflow** to confirm it works. After that it runs automatically every
   day at ~13:17 UTC.

> Note: GitHub disables scheduled workflows after 60 days with no repo activity.
> This watcher commits its price history daily, which counts as activity and
> keeps the schedule alive on its own.

## Email notifications (optional)

You get the buy alert two ways: the GitHub issue (above) **and** email. The email
step sends you a message **every day** with the current price in USD + MXN, and
when it's a buy the subject starts with **"🟢 BUY NOW"**.

To enable it, use Gmail SMTP with an app password:

1. Turn on 2-Step Verification on your Google account, then create an
   **App Password**: <https://myaccount.google.com/apppasswords> (pick "Mail").
2. Add three repo secrets (**Settings → Secrets and variables → Actions**):
   - `MAIL_USERNAME` = your Gmail address (e.g. `patrickchidiac@gmail.com`)
   - `MAIL_PASSWORD` = the 16-character app password (not your normal password)
   - `MAIL_TO` = where to send alerts (e.g. `patrickchidiac@gmail.com`)

If these secrets are absent, the email step is simply skipped — the GitHub issue
buy alert still works. Don't want a daily email? Change the email step's `if:`
condition in the workflow to `steps.check.outputs.buy_signal == 'true'` to get
mail only on a buy.

## Run it locally (optional)

```bash
export AMADEUS_CLIENT_ID=...   AMADEUS_CLIENT_SECRET=...
python3 flight_watcher/check_prices.py
```

No third-party Python packages required (standard library only).

## Files

| File | Purpose |
|---|---|
| `flight_watcher/config.json` | Route, dates, and constraints — edit this to tune the search |
| `flight_watcher/check_prices.py` | The daily price-check + buy-signal logic |
| `flight_watcher/price_history.json` | Append-only daily price log (USD + MXN) |
| `flight_watcher/latest_result.json` | Most recent run's result (overwritten each run) |
| `.github/workflows/flight-price-watcher.yml` | The daily schedule |
