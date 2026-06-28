#!/usr/bin/env python3
"""
Daily flight-price watcher: Beirut (BEY) -> Mexico City (MEX), round trip.

Default data source is KEYLESS (the `fast-flights` library, which reads Google
Flights) so the watcher runs with zero account setup. If Amadeus Self-Service
API credentials are present in the environment, it uses those instead (more
precise, official). Either way it:

  * searches each candidate departure date (return = departure + stay_days),
  * keeps only offers with 1 stop via Paris (CDG) or Istanbul (IST) and a
    flight time under the configured limit,
  * records the cheapest valid fare per day to price_history.json (USD + MXN),
  * emits a BUY signal when today's best fare is the lowest in >= 14 days.

Optional Amadeus credentials (set as GitHub Actions secrets):
  AMADEUS_CLIENT_ID, AMADEUS_CLIENT_SECRET
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

AMADEUS_HOST = os.environ.get("AMADEUS_HOST", "api.amadeus.com")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def http_get_json(url, headers=None, timeout=60):
    req = urlrequest.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post_form(url, data):
    body = urlparse.urlencode(data).encode()
    req = urlrequest.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlrequest.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def iso_duration_to_hours(text):
    """'PT19H30M' -> 19.5"""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", text or "")
    if not m:
        return None
    return int(m.group(1) or 0) + int(m.group(2) or 0) / 60.0


# Normalised record shape returned by every backend:
#   {price, currency, via, carriers, out_hours, ret_hours}
def make_record(price, currency, via_codes, carriers, out_hours, ret_hours):
    return {
        "price": float(price),
        "currency": currency,
        "via": "/".join(via_codes) if via_codes else "?",
        "carriers": sorted(set(carriers)),
        "out_hours": round(out_hours, 1) if out_hours is not None else None,
        "ret_hours": round(ret_hours, 1) if ret_hours is not None else None,
    }


# --------------------------------------------------------------------------- #
# Backend 1: keyless (fast-flights / Google Flights)
# --------------------------------------------------------------------------- #
# Google shows datacenter IPs an EU cookie-consent interstitial that lacks the
# results script, which makes the default fetcher crash. We fetch ourselves with
# a consent-bypass cookie + US/English locale, then hand the HTML to the parser.
def _ff_fetch_results(query):
    import primp
    from fast_flights.parser import parse
    from fast_flights.fetcher import URL

    client = primp.Client(impersonate="chrome_145", impersonate_os="windows",
                          referer=True, cookie_store=True)
    params = {**query.params(), "hl": "en", "gl": "US"}
    cookies = {"CONSENT": "YES+1", "SOCS": "CAISEwgDEgk"}
    res = client.get(URL, params=params, cookies=cookies, follow_redirects=True)
    html = res.text or ""
    status = getattr(res, "status_code", "?")
    has_results_script = "ds:1" in html
    if not has_results_script:
        snippet = " ".join(html.split())[:200]
        raise RuntimeError(
            f"no results script in page (HTTP {status}, {len(html)} bytes). "
            f"Snippet: {snippet!r}")
    return parse(html)


def fastflights_best(cfg, dep, ret):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    max_stops = cfg["max_stops_each_direction"]
    allowed = set(cfg["allowed_connection_airports"])
    max_minutes = cfg["max_journey_hours_each_direction"] * 60
    seat = cfg["cabin"].lower().replace("_", "-")
    airlines = cfg.get("airlines") or None  # e.g. ["TK", "AF"]; None = any airline

    query = create_query(
        flights=[
            FlightQuery(date=dep, from_airport=cfg["origin"],
                        to_airport=cfg["destination"], max_stops=max_stops,
                        airlines=airlines),
            FlightQuery(date=ret, from_airport=cfg["destination"],
                        to_airport=cfg["origin"], max_stops=max_stops,
                        airlines=airlines),
        ],
        seat=seat, trip="round-trip",
        passengers=Passengers(adults=cfg["passengers"]),
        currency="USD",
    )
    try:
        result = _ff_fetch_results(query)
    except Exception as exc:
        # fall back to the library's default fetcher, then re-raise with context
        print(f"    consent-bypass fetch failed: {exc}", file=sys.stderr)
        result = get_flights(query)

    best = None
    for opt in result:
        segs = getattr(opt, "flights", []) or []
        if len(segs) - 1 < 1 or len(segs) - 1 > max_stops:
            continue
        conns = [s.to_airport.code for s in segs[:-1]]
        if not all(c in allowed for c in conns):
            continue
        flight_minutes = sum((s.duration or 0) for s in segs)
        if flight_minutes <= 0 or flight_minutes > max_minutes:
            continue
        price = float(getattr(opt, "price", 0) or 0)
        if price <= 0:
            continue
        rec = make_record(price, "USD", conns, getattr(opt, "airlines", []),
                          flight_minutes / 60.0, None)
        if best is None or price < best["price"]:
            best = rec
    return best


# --------------------------------------------------------------------------- #
# Backend 2: Amadeus (optional, precise, filters both legs)
# --------------------------------------------------------------------------- #
def amadeus_token(cid, secret):
    payload = http_post_form(
        f"https://{AMADEUS_HOST}/v1/security/oauth2/token",
        {"grant_type": "client_credentials", "client_id": cid,
         "client_secret": secret},
    )
    return payload["access_token"]


def _amadeus_itin_ok(itin, cfg):
    segs = itin.get("segments", [])
    stops = len(segs) - 1
    if stops < 1 or stops > cfg["max_stops_each_direction"]:
        return False, None, []
    conns = [s["arrival"]["iataCode"] for s in segs[:-1]]
    if not all(c in set(cfg["allowed_connection_airports"]) for c in conns):
        return False, None, conns
    hours = iso_duration_to_hours(itin.get("duration"))
    if hours is None or hours > cfg["max_journey_hours_each_direction"]:
        return False, hours, conns
    return True, hours, conns


def amadeus_best(cfg, dep, ret, token):
    params = {
        "originLocationCode": cfg["origin"], "destinationLocationCode": cfg["destination"],
        "departureDate": dep, "returnDate": ret, "adults": str(cfg["passengers"]),
        "travelClass": cfg["cabin"], "currencyCode": "USD", "max": "50", "nonStop": "false",
    }
    if cfg.get("airlines"):
        params["includedAirlineCodes"] = ",".join(cfg["airlines"])
    url = f"https://{AMADEUS_HOST}/v2/shopping/flight-offers?" + urlparse.urlencode(params)
    offers = http_get_json(url, headers={"Authorization": f"Bearer {token}"}).get("data", [])

    best = None
    for offer in offers:
        itins = offer.get("itineraries", [])
        if len(itins) != 2:
            continue
        ok_o, h_o, c_o = _amadeus_itin_ok(itins[0], cfg)
        ok_r, h_r, c_r = _amadeus_itin_ok(itins[1], cfg)
        if not (ok_o and ok_r):
            continue
        price = float(offer["price"]["grandTotal"])
        carriers = [s["carrierCode"] for it in itins for s in it["segments"]]
        rec = make_record(price, "USD", c_o + c_r, carriers, h_o, h_r)
        if best is None or price < best["price"]:
            best = rec
    return best


# --------------------------------------------------------------------------- #
# FX: USD -> MXN (free, no key; falls back to an approximate rate)
# --------------------------------------------------------------------------- #
def usd_to_mxn_rate():
    try:
        data = http_get_json("https://open.er-api.com/v6/latest/USD", timeout=30)
        rate = data.get("rates", {}).get("MXN")
        if rate:
            return float(rate), "live"
    except (HTTPError, URLError, ValueError, KeyError):
        pass
    return 18.5, "fallback (~18.5)"


# --------------------------------------------------------------------------- #
# GitHub Actions plumbing
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
    cid = os.environ.get("AMADEUS_CLIENT_ID")
    secret = os.environ.get("AMADEUS_CLIENT_SECRET")
    use_amadeus = bool(cid and secret)
    token = amadeus_token(cid, secret) if use_amadeus else None
    backend = "Amadeus" if use_amadeus else "keyless (Google Flights)"
    print(f"Data source: {backend}")

    stay = timedelta(days=cfg["stay_days"])
    overall, overall_meta = None, {}
    for dep in cfg["departure_candidates"]:
        ret = (datetime.strptime(dep, "%Y-%m-%d").date() + stay).isoformat()
        try:
            best = (amadeus_best(cfg, dep, ret, token) if use_amadeus
                    else fastflights_best(cfg, dep, ret))
        except Exception as exc:  # network/parse hiccup on one date shouldn't kill the run
            print(f"  {dep} -> {ret}: lookup failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        if best:
            hrs = f"{best['out_hours']}h" + (f"/{best['ret_hours']}h" if best['ret_hours'] else "")
            print(f"  {dep} -> {ret}: USD {best['price']:.0f} via {best['via']} ({hrs})")
            if overall is None or best["price"] < overall["price"]:
                overall, overall_meta = best, {"departure": dep, "return": ret}
        else:
            print(f"  {dep} -> {ret}: no offer met the constraints")

    if overall is None:
        print("No valid offers found today.", file=sys.stderr)
        write_summary("### ✈️ Flight watcher\nNo offers met the constraints today.")
        emit_output("buy_signal", "false")
        emit_output("price_usd", "")
        sys.exit(0)

    rate, rate_src = usd_to_mxn_rate()
    price_usd = overall["price"]
    price_mxn = round(price_usd * rate)

    today = date.today().isoformat()
    history = [h for h in load_json(HISTORY_PATH, []) if h["date"] != today]
    history.append({
        "date": today,
        "price_usd": round(price_usd, 2),
        "price_mxn": price_mxn,
        "fx_usd_mxn": round(rate, 3),
        "departure": overall_meta["departure"],
        "return": overall_meta["return"],
        "via": overall["via"],
        "carriers": overall["carriers"],
        "hours_out": overall["out_hours"],
        "hours_ret": overall["ret_hours"],
        "source": backend,
    })
    history.sort(key=lambda h: h["date"])
    with open(HISTORY_PATH, "w") as fh:
        json.dump(history, fh, indent=2)

    prices = [h["price_usd"] for h in history]
    all_time_low = min(prices)
    is_new_low = price_usd <= all_time_low
    # "Lowest in 2+ weeks" = lowest within the trailing window (by date, so gaps
    # from skipped days don't distort it), once we have enough history.
    window_days = cfg["buy_signal"].get("window_days", 14)
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    window_prices = [h["price_usd"] for h in history if h["date"] >= cutoff]
    window_low = min(window_prices) if window_prices else price_usd
    is_window_low = price_usd <= window_low
    enough = len(history) >= cfg["buy_signal"]["min_history_days"]
    buy_signal = bool(is_window_low and enough)

    hrs = f"{overall['out_hours']}h out" + (f" / {overall['ret_hours']}h back" if overall['ret_hours'] else " (flight time)")
    print("\n=== BEST TODAY ===")
    print(f"Route   : BEY -> MEX -> BEY via {overall['via']}")
    print(f"Dates   : depart {overall_meta['departure']}, return {overall_meta['return']}")
    print(f"Airlines: {', '.join(overall['carriers'])}")
    print(f"Time    : {hrs}")
    print(f"Price   : USD {price_usd:,.0f}  |  MXN {price_mxn:,}  (FX {rate:.2f}, {rate_src})")
    print(f"History : {len(history)} day(s), all-time low USD {all_time_low:,.0f}")
    print(f"BUY     : {'YES — lowest in 2+ weeks' if buy_signal else ('new low (need 14d)' if is_new_low else 'no')}")

    with open(LATEST_PATH, "w") as fh:
        json.dump({
            "date": today, "price_usd": round(price_usd, 2), "price_mxn": price_mxn,
            "buy_signal": buy_signal, "is_new_low": is_new_low,
            "all_time_low_usd": round(all_time_low, 2), "history_days": len(history),
            "via": overall["via"], "carriers": overall["carriers"],
            "departure": overall_meta["departure"], "return": overall_meta["return"],
            "source": backend,
        }, fh, indent=2)

    emit_output("buy_signal", "true" if buy_signal else "false")
    emit_output("is_new_low", "true" if is_new_low else "false")
    emit_output("price_usd", f"{price_usd:.0f}")
    emit_output("price_mxn", f"{price_mxn}")
    emit_output("via", overall["via"])
    emit_output("dep_date", overall_meta["departure"])
    emit_output("ret_date", overall_meta["return"])
    emit_output("low_usd", f"{all_time_low:.0f}")
    emit_output("hist_days", f"{len(history)}")
    emit_output("carriers", ", ".join(overall["carriers"]))

    flag = "🟢 **BUY — lowest in 2+ weeks**" if buy_signal else (
        "🔵 new low (need ≥14 days first)" if is_new_low else "—")
    write_summary(
        f"### ✈️ Flight watcher — {today}\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Best price | **USD {price_usd:,.0f}** / **MXN {price_mxn:,}** |\n"
        f"| Route | BEY→MEX→BEY via {overall['via']} |\n"
        f"| Dates | {overall_meta['departure']} → {overall_meta['return']} |\n"
        f"| Airlines | {', '.join(overall['carriers'])} |\n"
        f"| All-time low | USD {all_time_low:,.0f} ({len(history)} days) |\n"
        f"| Source | {backend} |\n"
        f"| Signal | {flag} |\n"
    )


if __name__ == "__main__":
    main()
