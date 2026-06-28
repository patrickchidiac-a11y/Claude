#!/usr/bin/env python3
"""
Daily flight-price watcher: Beirut (BEY) -> Mexico City (MEX), round trip.

Searches the Amadeus Flight Offers Search API for each candidate departure date,
keeps only offers that satisfy the user's constraints (1 stop, connecting via
Paris/CDG or Istanbul/IST, journey under N hours each direction), records the
cheapest valid offer per day to price_history.json, prints prices in USD + MXN,
and emits a BUY signal when today's best price is the lowest in >= 14 days.

Requires two environment variables (set them as GitHub Actions secrets):
  AMADEUS_CLIENT_ID
  AMADEUS_CLIENT_SECRET

Get free credentials at https://developers.amadeus.com (Self-Service).
"""

import json
import os
import sys
import re
from datetime import date, datetime, timedelta
from urllib import request as urlrequest
from urllib import parse as urlparse
from urllib.error import HTTPError, URLError

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
HISTORY_PATH = os.path.join(HERE, "price_history.json")
LATEST_PATH = os.path.join(HERE, "latest_result.json")

# Amadeus "production" host. The free Self-Service tier works here once the app
# is moved to production in the developer portal. For the test sandbox swap to
# "test.api.amadeus.com".
AMADEUS_HOST = os.environ.get("AMADEUS_HOST", "api.amadeus.com")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def http_post_form(url, data):
    body = urlparse.urlencode(data).encode()
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlrequest.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def http_get_json(url, headers=None, timeout=60):
    req = urlrequest.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def iso_duration_to_hours(text):
    """'PT19H30M' -> 19.5 (hours, as float)."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", text or "")
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return hours + minutes / 60.0


# --------------------------------------------------------------------------- #
# Amadeus access
# --------------------------------------------------------------------------- #
def amadeus_token(client_id, client_secret):
    url = f"https://{AMADEUS_HOST}/v1/security/oauth2/token"
    payload = http_post_form(
        url,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    return payload["access_token"]


def search_offers(token, origin, dest, dep_date, ret_date, adults, cabin, currency):
    params = {
        "originLocationCode": origin,
        "destinationLocationCode": dest,
        "departureDate": dep_date,
        "returnDate": ret_date,
        "adults": str(adults),
        "travelClass": cabin,
        "currencyCode": currency,
        "max": "50",
        "nonStop": "false",
    }
    url = f"https://{AMADEUS_HOST}/v2/shopping/flight-offers?" + urlparse.urlencode(params)
    data = http_get_json(url, headers={"Authorization": f"Bearer {token}"})
    return data.get("data", [])


# --------------------------------------------------------------------------- #
# Constraint filtering
# --------------------------------------------------------------------------- #
def itinerary_ok(itin, cfg):
    """Return (ok, journey_hours, connection_codes) for one direction."""
    segments = itin.get("segments", [])
    stops = len(segments) - 1
    if stops < 1 or stops > cfg["max_stops_each_direction"]:
        return False, None, []

    # connection airport(s) = every arrival point except the final destination
    connections = [s["arrival"]["iataCode"] for s in segments[:-1]]
    allowed = set(cfg["allowed_connection_airports"])
    if not all(code in allowed for code in connections):
        return False, None, connections

    journey_hours = iso_duration_to_hours(itin.get("duration"))
    if journey_hours is None or journey_hours > cfg["max_journey_hours_each_direction"]:
        return False, journey_hours, connections

    return True, journey_hours, connections


def best_valid_offer(offers, cfg):
    """Pick the cheapest offer whose outbound AND return both pass constraints."""
    best = None
    for offer in offers:
        itins = offer.get("itineraries", [])
        if len(itins) != 2:
            continue
        out_ok, out_h, out_conn = itinerary_ok(itins[0], cfg)
        ret_ok, ret_h, ret_conn = itinerary_ok(itins[1], cfg)
        if not (out_ok and ret_ok):
            continue
        price = float(offer["price"]["grandTotal"])
        carriers = sorted({s["carrierCode"] for it in itins for s in it["segments"]})
        record = {
            "price": price,
            "currency": offer["price"]["currency"],
            "outbound_hours": round(out_h, 1),
            "return_hours": round(ret_h, 1),
            "outbound_via": out_conn,
            "return_via": ret_conn,
            "carriers": carriers,
        }
        if best is None or price < best["price"]:
            best = record
    return best


# --------------------------------------------------------------------------- #
# FX: USD -> MXN (free endpoint, no key; falls back to an approximate rate)
# --------------------------------------------------------------------------- #
def usd_to_mxn_rate():
    try:
        data = http_get_json("https://open.er-api.com/v6/latest/USD", timeout=30)
        rate = data.get("rates", {}).get("MXN")
        if rate:
            return float(rate), "live"
    except (HTTPError, URLError, ValueError, KeyError):
        pass
    return 18.5, "fallback (~18.5, live rate unavailable)"


# --------------------------------------------------------------------------- #
# GitHub Actions output plumbing
# --------------------------------------------------------------------------- #
def emit_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(f"{key}={value}\n")


def write_summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(md + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = load_json(CONFIG_PATH, {})
    client_id = os.environ.get("AMADEUS_CLIENT_ID")
    client_secret = os.environ.get("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET "
              "(GitHub repo Settings -> Secrets -> Actions).", file=sys.stderr)
        sys.exit(2)

    token = amadeus_token(client_id, client_secret)
    stay = timedelta(days=cfg["stay_days"])

    overall = None
    overall_meta = {}
    for dep in cfg["departure_candidates"]:
        ret = (datetime.strptime(dep, "%Y-%m-%d").date() + stay).isoformat()
        try:
            offers = search_offers(
                token, cfg["origin"], cfg["destination"], dep, ret,
                cfg["passengers"], cfg["cabin"], cfg["currency_primary"],
            )
        except (HTTPError, URLError) as exc:
            print(f"  {dep} -> {ret}: query failed ({exc})", file=sys.stderr)
            continue
        best = best_valid_offer(offers, cfg)
        if best:
            print(f"  {dep} -> {ret}: cheapest valid "
                  f"{best['currency']} {best['price']:.0f} "
                  f"via {best['outbound_via']}/{best['return_via']} "
                  f"({best['outbound_hours']}h / {best['return_hours']}h)")
            if overall is None or best["price"] < overall["price"]:
                overall = best
                overall_meta = {"departure": dep, "return": ret}
        else:
            print(f"  {dep} -> {ret}: no offer met the constraints")

    if overall is None:
        print("No valid offers found across any candidate date today.", file=sys.stderr)
        write_summary("### ✈️ Flight watcher\nNo offers met the constraints today.")
        emit_output("buy_signal", "false")
        sys.exit(0)

    rate, rate_src = usd_to_mxn_rate()
    price_usd = overall["price"]
    price_mxn = round(price_usd * rate)

    today = date.today().isoformat()
    history = load_json(HISTORY_PATH, [])
    history = [h for h in history if h["date"] != today]  # idempotent for same day
    history.append({
        "date": today,
        "price_usd": round(price_usd, 2),
        "price_mxn": price_mxn,
        "fx_usd_mxn": round(rate, 3),
        "departure": overall_meta["departure"],
        "return": overall_meta["return"],
        "via": f"{overall['outbound_via']}/{overall['return_via']}",
        "carriers": overall["carriers"],
        "hours_out": overall["outbound_hours"],
        "hours_ret": overall["return_hours"],
    })
    history.sort(key=lambda h: h["date"])
    with open(HISTORY_PATH, "w") as fh:
        json.dump(history, fh, indent=2)

    prices = [h["price_usd"] for h in history]
    all_time_low = min(prices)
    is_new_low = price_usd <= all_time_low
    enough_history = len(history) >= cfg["buy_signal"]["min_history_days"]
    buy_signal = bool(is_new_low and enough_history)

    # Console + result file
    print("\n=== BEST TODAY ===")
    print(f"Route   : BEY -> MEX -> BEY, via {overall['outbound_via']}/{overall['return_via']}")
    print(f"Dates   : depart {overall_meta['departure']}, return {overall_meta['return']}")
    print(f"Airlines: {', '.join(overall['carriers'])}")
    print(f"Journey : {overall['outbound_hours']}h out / {overall['return_hours']}h back")
    print(f"Price   : USD {price_usd:,.0f}  |  MXN {price_mxn:,}  (FX {rate:.2f}, {rate_src})")
    print(f"History : {len(history)} day(s), all-time low USD {all_time_low:,.0f}")
    print(f"BUY     : {'YES — lowest in 2+ weeks' if buy_signal else ('new low (need 14d history)' if is_new_low else 'no')}")

    result = {
        "date": today,
        "price_usd": round(price_usd, 2),
        "price_mxn": price_mxn,
        "buy_signal": buy_signal,
        "is_new_low": is_new_low,
        "all_time_low_usd": round(all_time_low, 2),
        "history_days": len(history),
        "details": overall,
        "departure": overall_meta["departure"],
        "return": overall_meta["return"],
    }
    with open(LATEST_PATH, "w") as fh:
        json.dump(result, fh, indent=2)

    # GitHub Actions outputs / summary
    emit_output("buy_signal", "true" if buy_signal else "false")
    emit_output("price_usd", f"{price_usd:.0f}")
    emit_output("price_mxn", f"{price_mxn}")
    emit_output("via", f"{overall['outbound_via']}/{overall['return_via']}")
    emit_output("dep_date", overall_meta["departure"])
    emit_output("ret_date", overall_meta["return"])

    flag = "🟢 **BUY — lowest in 2+ weeks**" if buy_signal else (
        "🔵 new low (need ≥14 days of history first)" if is_new_low else "—")
    write_summary(
        f"### ✈️ Flight watcher — {today}\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Best price | **USD {price_usd:,.0f}** / **MXN {price_mxn:,}** |\n"
        f"| Route | BEY→MEX→BEY via {overall['outbound_via']}/{overall['return_via']} |\n"
        f"| Dates | {overall_meta['departure']} → {overall_meta['return']} |\n"
        f"| Airlines | {', '.join(overall['carriers'])} |\n"
        f"| Journey | {overall['outbound_hours']}h out / {overall['return_hours']}h back |\n"
        f"| All-time low | USD {all_time_low:,.0f} ({len(history)} days tracked) |\n"
        f"| Signal | {flag} |\n"
    )


if __name__ == "__main__":
    main()
