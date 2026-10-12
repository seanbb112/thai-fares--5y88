"""Hourly Google Flights sweep: Ireland -> Thailand, 27/12/26, max one stop.

Reads config.json, queries Google Flights (via fast-flights' query encoder and
primp), and writes:
  ../data/latest.json   every itinerary found this run
  ../data/history.csv   cheapest price per route per run (appended)
  ../dashboard/data.js  what the dashboard loads
  ../data/run.log       one line per run, plus any errors

Full search:   python tracker.py           (every route; picks the 5-route watchlist)
Quick check:   python tracker.py --quick   (only the watchlist; rest kept from the last full search)
Scheduled:     ../.github/workflows/track.yml (quick every 30 min, full every 2 hours)
"""

import csv
import json
import random
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
DASH = ROOT / "dashboard"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

IRL = ZoneInfo("Europe/Dublin")


def now_irl():
    """Irish wall-clock time, whatever clock the machine (e.g. GitHub's UTC runner) uses."""
    return datetime.now(IRL).replace(tzinfo=None)


URL = "https://www.google.com/travel/flights"
# Google serves an EU cookie-consent page from Ireland unless consent is recorded.
CONSENT = {"Cookie": "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg"}

client = Client(impersonate="chrome_145", impersonate_os="windows", referer=True, cookie_store=True)
errors = []
query_count = 0


def log(msg):
    DATA.mkdir(exist_ok=True)
    with open(DATA / "run.log", "a", encoding="utf-8") as f:
        f.write(f"{now_irl():%d/%m/%y %H:%M:%S} {msg}\n")


def booking_url(it, origin, dest, date):
    """Google Flights link narrowed to this one itinerary.

    Google has no link to a single flight, so filter the search by its airlines, departure
    hour and layover length; the itinerary then shows alone (or first) with its booking options.
    """
    first = it["legs"][0]
    hour = int(first["dep"][11:13])
    lay = it["layovers"][0]["minutes"] if it["layovers"] else None
    q = create_query(
        flights=[FlightQuery(
            date=date, from_airport=origin, to_airport=dest, max_stops=it["stops"],
            airlines=sorted({l["code"] for l in it["legs"] if l["code"]}) or None,
            earliest_departure_hour=hour, latest_departure_hour=hour,
            min_layover_minutes=lay - 5 if lay else None, max_layover_minutes=lay + 5 if lay else None,
        )],
        trip="one-way", seat="economy", passengers=Passengers(adults=1),
        language=CONFIG["language"], currency=CONFIG["currency"],
    )
    return q.url()


def search(origin, dest, date, max_stops):
    """Return (google_flights_url, [itinerary dicts]) for one route and date."""
    global query_count
    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest, max_stops=max_stops)],
        trip="one-way", seat="economy", passengers=Passengers(adults=1),
        language=CONFIG["language"], currency=CONFIG["currency"],
    )
    url = q.url()
    time.sleep(random.uniform(*CONFIG["delay_seconds"]))
    query_count += 1
    try:
        html = client.get(URL, params=q.params(), headers=CONSENT, timeout=40).text
        script = LexborHTMLParser(html).css_first(r"script.ds\:1")
        if script is None:
            title = "consent page" if "consent.google" in html else "no data block"
            raise RuntimeError(f"Google returned {title}")
        raw = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
        if raw.endswith("errorHasStatus: true"):
            return url, []
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001 - one failed route must not stop the sweep
        errors.append(f"{origin}-{dest} {date}: {e}")
        return url, None

    out = []
    # payload[2] = "best" flights, payload[3] = all other flights
    for block in (payload[2], payload[3]):
        if not block or not block[0]:
            continue
        for k in block[0]:
            try:
                f, price = k[0], k[1][0][1]
            except (IndexError, TypeError):
                continue
            if price is None:
                continue
            legs = []
            for s in f[2]:
                dep_t, arr_t = (s[8] or []) + [None, None], (s[10] or []) + [None, None]
                legs.append({
                    "from": s[3], "to": s[6],
                    "dep": "%04d-%02d-%02dT%02d:%02d" % (*s[20], dep_t[0] or 0, dep_t[1] or 0),
                    "arr": "%04d-%02d-%02dT%02d:%02d" % (*s[21], arr_t[0] or 0, arr_t[1] or 0),
                    "flight": f"{s[22][0]}{s[22][1]}" if s[22] else "",
                    "code": s[22][0] if s[22] else "",
                    "carrier": s[22][3] if s[22] and len(s[22]) > 3 else "",
                    "minutes": s[11],
                })
            layovers = [{"airport": l[1], "minutes": l[0]} for l in (f[13] or [])]
            out.append({
                "price": price,
                "airlines": f[1],
                "legs": legs,
                "stops": len(legs) - 1,
                "layovers": layovers,
                "total_minutes": f[9],
            })
    for it in out:
        it["book_url"] = booking_url(it, origin, dest, date)
    # Google repeats some itineraries across blocks; keep the cheapest of each
    seen = {}
    for it in out:
        key = tuple(l["flight"] + l["dep"] for l in it["legs"])
        if key not in seen or it["price"] < seen[key]["price"]:
            seen[key] = it
    return url, sorted(seen.values(), key=lambda x: x["price"])


def pick_watchlist(results, travel_dates, n=5):
    """The n search routes giving the cheapest valid fares; a stopover pair counts as one route."""
    best = {}
    for t in results["through"]:
        if t["date"] in travel_dates:
            k = ("through", t["origin"], t["dest"], t["date"])
            best[k] = min(best.get(k, 1e9), t["price"])
    for s in results["stopover"]:
        k = ("stopover", s["date"], s["hub"], s["dest"], s["leg2"]["legs"][0]["dep"][:10])
        best[k] = min(best.get(k, 1e9), s["price"])
    out = []
    for k, price in sorted(best.items(), key=lambda kv: kv[1])[:n]:
        if k[0] == "through":
            out.append({"type": "through", "origin": k[1], "dest": k[2], "date": k[3], "price": price})
        else:
            out.append({"type": "stopover", "date": k[1], "hub": k[2], "dest": k[3], "date2": k[4], "price": price})
    return out


def run(mode="full"):
    started = now_irl()
    tc, sc = CONFIG["through"], CONFIG["stopover"]
    travel_dates = CONFIG["travel_dates"]
    results = {"through": [], "stopover": [], "routes": []}
    # Must be in Thailand by this time (Thai local time, as Google reports arrivals)
    arrive_by = datetime.fromisoformat(CONFIG["arrive_by"]) if CONFIG.get("arrive_by") else None

    prev = None
    if (DATA / "latest.json").exists():
        prev = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    watch = (prev or {}).get("meta", {}).get("watchlist") or []
    if mode == "quick" and not watch:
        mode = "full"  # nothing to watch yet: do a full search first

    def on_time(it):
        return arrive_by is None or datetime.fromisoformat(it["legs"][-1]["arr"]) <= arrive_by

    def route_row(kind, o, d, date, url, its):
        results["routes"].append({"kind": kind, "origin": o, "dest": d, "date": date, "url": url,
                                  "ok": its is not None, "count": len(its or []),
                                  "cheapest": its[0]["price"] if its else None})

    def through(o, d, date):
        url, its = search(o, d, date, tc["max_stops"])
        if its:
            its = [it for it in its if on_time(it)]
        route_row("through", o, d, date, url, its)
        for it in its or []:
            results["through"].append({**it, "origin": o, "dest": d, "date": date, "url": url})

    leg1_cache, leg2_cache = {}, {}  # shared by every departure date / pair in this run

    def outward(hub, travel):
        if (hub, travel) not in leg1_cache:
            url1, leg1 = search(sc["origin"], hub, travel, sc["leg_max_stops"])
            leg1_cache[(hub, travel)] = (url1, leg1)
            route_row("leg1", sc["origin"], hub, travel, url1, leg1)
        return leg1_cache[(hub, travel)]

    def onward(hub, dest, date2):
        if (hub, dest, date2) not in leg2_cache:
            url2, leg2 = search(hub, dest, date2, sc["leg_max_stops"])
            if leg2:
                leg2 = [it for it in leg2 if on_time(it)]
            leg2_cache[(hub, dest, date2)] = (url2, leg2)
            route_row("leg2", hub, dest, date2, url2, leg2)
        return leg2_cache[(hub, dest, date2)]

    def combine(travel, hub, url1, leg1, dest, url2, leg2):
        for a in leg1:
            arr = datetime.fromisoformat(a["legs"][-1]["arr"])
            for b in leg2 or []:
                dep = datetime.fromisoformat(b["legs"][0]["dep"])
                gap = (dep - arr).total_seconds() / 60
                if gap < sc["min_connection_minutes"]:
                    continue
                nights = (dep.date() - arr.date()).days
                if nights > sc["max_extra_days"]:
                    continue
                results["stopover"].append({
                    "price": a["price"] + b["price"], "date": travel, "hub": hub, "dest": dest,
                    "nights": nights, "gap_minutes": round(gap),
                    "leg1": {**a, "url": url1}, "leg2": {**b, "url": url2},
                })

    if mode == "full":
        # 1. Through-tickets (one ticket, direct or one stop); compare dates from Dublin only
        for date in travel_dates + CONFIG["compare_dates"]:
            for o in (tc["origins"] if date in travel_dates else ["DUB"]):
                for d in tc["destinations"]:
                    through(o, d, date)
        # 2. Self-built stopover: Ireland -> hub (direct), hub -> Thailand (direct), separate tickets
        for travel in travel_dates:
            base = datetime.strptime(travel, "%Y-%m-%d")
            for hub in sc["hubs"]:
                url1, leg1 = outward(hub, travel)
                if not leg1:
                    continue  # no direct flight to this hub on the day; skip its onward searches
                for dest in sc["destinations"]:
                    for extra in range(sc["max_extra_days"] + 2):  # +1: overnight arrivals land the next day
                        day2 = base + timedelta(days=extra)
                        if arrive_by and day2.date() > arrive_by.date():
                            break  # anything leaving the hub later lands too late
                        date2 = day2.strftime("%Y-%m-%d")
                        url2, leg2 = onward(hub, dest, date2)
                        combine(travel, hub, url1, leg1, dest, url2, leg2)
    else:
        # Quick check: re-search only the watched routes; everything else stays as the last full search left it
        th_keys = {(w["origin"], w["dest"], w["date"]) for w in watch if w["type"] == "through"}
        so_keys = {(w["date"], w["hub"], w["dest"], w["date2"]) for w in watch if w["type"] == "stopover"}
        leg1_keys = {(sc["origin"], w["hub"], w["date"]) for w in watch if w["type"] == "stopover"}
        leg2_keys = {(w["hub"], w["dest"], w["date2"]) for w in watch if w["type"] == "stopover"}
        old = prev["results"]
        results["through"] = [t for t in old["through"] if (t["origin"], t["dest"], t["date"]) not in th_keys]
        results["stopover"] = [s for s in old["stopover"]
                               if (s["date"], s["hub"], s["dest"], s["leg2"]["legs"][0]["dep"][:10]) not in so_keys]
        results["routes"] = [r for r in old["routes"] if not (
            (r["kind"] == "through" and (r["origin"], r["dest"], r["date"]) in th_keys)
            or (r["kind"] == "leg1" and (r["origin"], r["dest"], r["date"]) in leg1_keys)
            or (r["kind"] == "leg2" and (r["origin"], r["dest"], r["date"]) in leg2_keys))]
        for w in watch:
            if w["type"] == "through":
                through(w["origin"], w["dest"], w["date"])
            else:
                url1, leg1 = outward(w["hub"], w["date"])
                url2, leg2 = onward(w["hub"], w["dest"], w["date2"])
                if leg1:
                    combine(w["date"], w["hub"], url1, leg1, w["dest"], url2, leg2)

    results["through"].sort(key=lambda x: x["price"])
    # For each date/hub/dest/nights keep the cheapest five combos so the file stays small
    results["stopover"].sort(key=lambda x: x["price"])
    kept, counts = [], {}
    for c in results["stopover"]:
        k = (c["date"], c["hub"], c["dest"], c["nights"])
        counts[k] = counts.get(k, 0) + 1
        if counts[k] <= 5:
            kept.append(c)
    results["stopover"] = kept

    finished = now_irl()
    meta = {
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "mode": mode,
        "last_full": finished.isoformat(timespec="seconds") if mode == "full"
                     else prev["meta"].get("last_full", prev["meta"]["finished"]),
        "watchlist": pick_watchlist(results, travel_dates) if mode == "full" else watch,
        "queries": query_count,
        "errors": errors,
        "target_eur": CONFIG["target_eur"],
        "travel_dates": travel_dates,
        "arrive_by": CONFIG.get("arrive_by"),
    }
    return meta, results


def update_daily_low(meta, results, through_tr):
    """Keep the cheapest valid itinerary seen on each (Irish) calendar day, with when it was seen."""
    path = DATA / "daily_low.json"
    lows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cands = []
    if through_tr:
        cands.append({"kind": "through", "price": through_tr[0]["price"], "it": through_tr[0]})
    if results["stopover"]:
        cands.append({"kind": "stopover", "price": results["stopover"][0]["price"], "it": results["stopover"][0]})
    if not cands:
        return
    best = min(cands, key=lambda c: c["price"])
    day = meta["finished"][:10]
    if day not in lows or best["price"] < lows[day]["price"]:
        lows[day] = {**best, "seen": meta["finished"]}
    lows = dict(sorted(lows.items())[-30:])  # last 30 days is plenty
    path.write_text(json.dumps(lows), encoding="utf-8")


def write(meta, results):
    DATA.mkdir(exist_ok=True)
    DASH.mkdir(exist_ok=True)
    # If Google blocked the whole run, keep the last good data on the dashboard
    if meta["queries"] and len(meta["errors"]) >= meta["queries"] * 0.8:
        log(f"RUN FAILED {len(meta['errors'])}/{meta['queries']} queries errored; first: {meta['errors'][:1]}")
        prev = DATA / "latest.json"
        if prev.exists():
            old = json.loads(prev.read_text(encoding="utf-8"))
            old["meta"]["last_attempt_failed"] = meta["finished"]
            meta, results = old["meta"], old["results"]
        else:
            return

    else:
        (DATA / "latest.json").write_text(json.dumps({"meta": meta, "results": results}), encoding="utf-8")
        hist = DATA / "history.csv"
        new = not hist.exists()
        with open(hist, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["run", "kind", "key", "price_eur"])
            ts = meta["finished"]
            best_t = results["through"][0] if results["through"] else None
            best_s = results["stopover"][0] if results["stopover"] else None
            # chart series: best across the travel dates, plus best per date for the tooltip
            through_tr = [t for t in results["through"] if t["date"] in meta["travel_dates"]]
            if through_tr:
                w.writerow([ts, "best", "through", through_tr[0]["price"]])
            if best_s:
                w.writerow([ts, "best", "stopover", best_s["price"]])
            for d in meta["travel_dates"]:
                t = next((x for x in results["through"] if x["date"] == d), None)
                s = next((x for x in results["stopover"] if x["date"] == d), None)
                if t:
                    w.writerow([ts, "best_date", f"through {d}", t["price"]])
                if s:
                    w.writerow([ts, "best_date", f"stopover {d}", s["price"]])
            for r in results["routes"]:
                if r["cheapest"] is not None:
                    w.writerow([ts, r["kind"], f"{r['origin']}-{r['dest']} {r['date']}", r["cheapest"]])
        log(f"ok {meta['mode']} {meta['queries']} queries, {len(meta['errors'])} errors, "
            f"best through {best_t['price'] if best_t else 'none'}, best stopover {best_s['price'] if best_s else 'none'}")
        update_daily_low(meta, results, through_tr)

    daily_low = {}
    if (DATA / "daily_low.json").exists():
        daily_low = json.loads((DATA / "daily_low.json").read_text(encoding="utf-8"))

    history = []
    hist = DATA / "history.csv"
    if hist.exists():
        with open(hist, encoding="utf-8") as f:
            history = [r for r in csv.DictReader(f)]
    payload = {"meta": meta, "results": results, "history": history, "daily_low": daily_low}
    (DASH / "data.js").write_text("window.TRACKER = " + json.dumps(payload) + ";\n", encoding="utf-8")


if __name__ == "__main__":
    try:
        m, r = run("quick" if "--quick" in sys.argv else "full")
        write(m, r)
    except Exception:
        traceback.print_exc()
        log("CRASH " + traceback.format_exc().replace("\n", " | "))
        sys.exit(1)
