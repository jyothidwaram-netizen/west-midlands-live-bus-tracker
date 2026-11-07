from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import psycopg2
import math
import re
import requests, time
import os
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import FileResponse

@app.get("/", include_in_schema=False)
def home_page():
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "index.html"))

def get_connection():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="westmidlands_bus",
        user="postgres",
        password=os.getenv("DB_PASSWORD")
    )

def london_time_after(after_minutes: int = 0):
    """Returns the real current time in the UK (handles BST automatically),
    plus however many minutes were requested, as a time string like '15:53:00'."""
    now_london = datetime.now(ZoneInfo("Europe/London"))
    target = now_london + timedelta(minutes=after_minutes)
    return target.strftime("%H:%M:%S")

def distance_meters(lat1, lon1, lat2, lon2):
    """Straight-line distance between two GPS points, in meters."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None
    lat_diff_m = (lat1 - lat2) * 111320
    lon_diff_m = (lon1 - lon2) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.sqrt(lat_diff_m ** 2 + lon_diff_m ** 2)

def to_seconds(t):
    """Accepts either a real time object or 'HH:MM:SS' text."""
    if hasattr(t, "hour"):
        return t.hour * 3600 + t.minute * 60 + t.second
    hours, minutes, seconds = [int(part) for part in str(t).split(":")]
    return hours * 3600 + minutes * 60 + seconds

def minutes_between(earlier_time, later_time):
    return (to_seconds(later_time) - to_seconds(earlier_time)) / 60

def get_frequency_text(cur, stop_name, line_name, from_time_str):
    """Looks at how many times this line departs from this stop in the hour
    after from_time_str, and describes the service as a plain sentence."""
    end_time_seconds = to_seconds(from_time_str) + 3600
    end_time_str = f"{(end_time_seconds // 3600) % 24:02d}:{(end_time_seconds % 3600) // 60:02d}:{end_time_seconds % 60:02d}"

    cur.execute("""
        SELECT COUNT(DISTINCT scheduled_time) FROM journey_stops
        WHERE line_name = %s
          AND stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
          AND scheduled_time::time > %s
          AND scheduled_time::time < %s
    """, (line_name, stop_name, from_time_str, end_time_str))
    count = cur.fetchone()[0]

    if count <= 1:
        return "Infrequent service — plan around this specific bus."
    interval = round(60 / count)
    return f"Route {line_name} runs about every {interval} min around this time."

def stand_where_text(street, indicator, bearing):
    bearing_words = {
        "N": "north-facing", "NE": "north-east-facing", "E": "east-facing",
        "SE": "south-east-facing", "S": "south-facing", "SW": "south-west-facing",
        "W": "west-facing", "NW": "north-west-facing"
    }
    indicator_words = {
        "adj": "adjacent to", "opp": "opposite", "o/s": "outside", "nr": "near",
        "before": "before", "after": "after", "cnr": "corner of", "by": "by"
    }
    if not street:
        return None

    # Some stops store their bay code inside "indicator", e.g. "Stop KL"
    # instead of a normal word like "opp" or "nr" — pull that out and
    # explain it plainly instead of showing the raw code.
    bay_code = None
    if indicator:
        bay_match = re.match(r'^Stop\s+([A-Za-z0-9]{1,4})$', indicator, re.IGNORECASE)
        if bay_match:
            bay_code = bay_match.group(1)
            indicator = None

    indicator_text = indicator_words.get(indicator, indicator) if indicator else None
    bearing_text = bearing_words.get(bearing, bearing) if bearing else None
    parts = []
    if indicator_text:
        parts.append(indicator_text)
    parts.append(street)
    text = " ".join(parts)
    if bearing_text:
        text += f" ({bearing_text} stop)"
    if bay_code:
        text = f"Look for Bay {bay_code} — {text}"
    return text

def get_live_buses_for_line(line_name: str):
    """Asks BODS for all live buses currently running, and returns only
    the ones matching this specific bus line (route number)."""
    api_key = os.getenv("BODS_API_KEY")
    if not api_key:
        return []

    bounding_box = "-2.3,52.3,-1.7,52.6"
    url = "https://data.bus-data.dft.gov.uk/api/v1/datafeed/"
    params = {"api_key": api_key, "boundingBox": bounding_box}

    try:
        response = requests.get(url, params=params, timeout=10)
    except requests.exceptions.RequestException:
        return []

    if response.status_code != 200:
        return []

    namespace = {"siri": "http://www.siri.org.uk/siri"}
    root = ET.fromstring(response.content)
    vehicles = root.findall(".//siri:VehicleActivity", namespace)

    matches = []
    for v in vehicles:
        line = v.find(".//siri:LineRef", namespace)
        lat = v.find(".//siri:Latitude", namespace)
        lon = v.find(".//siri:Longitude", namespace)
        if line is not None and lat is not None and lon is not None:
            if line.text == line_name:
                matches.append({
                    "line": line.text,
                    "latitude": float(lat.text),
                    "longitude": float(lon.text)
                })
    return matches
_place_cache = {}
_last_place_call = 0.0

def find_places(text, cur):
    """Ask OpenStreetMap (Nominatim) where a place is, then find the nearest real bus stop."""
    global _last_place_call
    key = text.strip().lower()
    hits = _place_cache.get(key)
    if hits is None:
        wait = 1.1 - (time.time() - _last_place_call)
        if wait > 0:
            time.sleep(wait)
        _last_place_call = time.time()
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": text, "format": "jsonv2", "limit": 3, "countrycodes": "gb",
                        "viewbox": "-2.3,52.65,-1.4,52.35", "bounded": 1},
                headers={"User-Agent": "west-midlands-bus-assistant/1.0 (learning project)"},
                timeout=8)
            hits = r.json()
        except Exception:
            return []
        _place_cache[key] = hits
    found = []
    for h in hits:
        lat, lon = float(h["lat"]), float(h["lon"])
        cur.execute("""
            SELECT stop_id, stop_name, locality,
                   SQRT(POWER(latitude - %s, 2) + POWER((longitude - %s) * 0.61, 2)) * 111320 AS dist_m
            FROM stops
            ORDER BY POWER(latitude - %s, 2) + POWER((longitude - %s) * 0.61, 2)
            LIMIT 1
        """, (lat, lon, lat, lon))
        row = cur.fetchone()
        if row and row[3] <= 800:
            place_name = h.get("name") or h.get("display_name", text).split(",")[0]
            found.append({"stop_id": row[0], "stop_name": row[1], "locality": row[2],
                          "label": f"{place_name} (nearest stop: {row[1]}, {round(row[3])} m)",
                          "is_place": True})
    return found


@app.get("/places/search")
def places_search(q: str):
    conn = get_connection()
    cur = conn.cursor()
    out = find_places(q, cur)
    cur.close()
    conn.close()
    return out
SEARCH_ALIASES = {
    "university of birmingham": "birmingham university",
    "uob": "birmingham university",
    "birmingham uni": "birmingham university",
}

@app.get("/stops/search")
def search_stops(q: str):
    term = q.strip()
    term = SEARCH_ALIASES.get(term.lower(), term)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT stop_id, stop_name, locality FROM stops
        WHERE stop_name ILIKE %s OR locality ILIKE %s
        ORDER BY (stop_name ILIKE %s) DESC, (stop_name ILIKE %s) DESC, stop_name
        LIMIT 60
        """, (f"%{term}%", f"%{term}%", f"%{term}%", "%univers%"))
    rows = cur.fetchall()
    seen = set()
    results = []
    for r in rows:
        key = (r[1], r[2])
        if key not in seen:
            seen.add(key)
            results.append(r)
    results = results[:10]
    out = [{"stop_id": r[0], "stop_name": r[1], "locality": r[2]} for r in results]
    if len(out) == 0 and len(term) >= 4:
        seen_ids = set()
        for p in find_places(term, cur):
            if p["stop_id"] not in seen_ids:
                seen_ids.add(p["stop_id"])
                out.append(p)
    cur.close()
    conn.close()
    return out

@app.get("/stops/{stop_id}/arrivals")
def get_arrivals(stop_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT stop_name, street, indicator, bearing FROM stops WHERE stop_id = %s
    """, (stop_id,))
    stop_info = cur.fetchone()

    current_time = london_time_after(0)

    cur.execute("""
        SELECT DISTINCT stops.stop_name, journey_stops.line_name, journey_stops.scheduled_time, journey_stops.operator
        FROM journey_stops
        JOIN stops ON journey_stops.stop_id = stops.stop_id
        WHERE stops.stop_id = %s
          AND journey_stops.scheduled_time::time > %s
        ORDER BY journey_stops.scheduled_time
        LIMIT 10
    """, (stop_id, current_time))
    results = cur.fetchall()
    cur.close()
    conn.close()

    stand_where = None
    if stop_info:
        stand_where = stand_where_text(stop_info[1], stop_info[2], stop_info[3])

    if not results:
        return {"message": "No scheduled departures found for this stop in our data.", "stand_where": stand_where}
    return {
        "stand_where": stand_where,
        "arrivals": [{"stop_name": r[0], "line_name": r[1], "departure_time": str(r[2]), "operator": r[3]} for r in results]
    }

@app.get("/trip")
def plan_trip(from_stop: str, to_stop: str, after_minutes: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(after_minutes)
    cur.execute("""
        SELECT DISTINCT a.line_name, a.operator,
               a.scheduled_time AS depart_time, b.scheduled_time AS arrive_time,
               a.journey_id, a.stop_id, b.stop_id
        FROM journey_stops a
        JOIN journey_stops b
          ON a.journey_id = b.journey_id
         AND b.stop_sequence > a.stop_sequence
        WHERE a.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
          AND b.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
          AND a.scheduled_time::time > %s
        ORDER BY a.scheduled_time
        LIMIT 10
    """, (from_stop, to_stop, current_time))
    results = cur.fetchall()

    if not results:
        cur.close()
        conn.close()
        return {"message": "No direct bus found between these two stops in our data."}

    trips = []
    for r in results:
        line_name, operator, depart_time, arrive_time, journey_id, from_stop_id, to_stop_id = r
        duration = round(minutes_between(depart_time, arrive_time))
        frequency_text = get_frequency_text(cur, from_stop, line_name, str(depart_time))
        trips.append({
            "line_name": line_name, "operator": operator,
            "depart_time": str(depart_time), "arrive_time": str(arrive_time),
            "duration_minutes": duration,
            "status": "recommended",
            "status_label": "Recommended",
            "reason": "Direct bus — no change needed.",
            "frequency": frequency_text,
            "legs": [
                {"journey_id": journey_id, "from_stop_id": from_stop_id, "to_stop_id": to_stop_id}
            ]
        })

    cur.close()
    conn.close()
    return trips

@app.get("/trip_transfer_old")
def plan_trip_transfer_old(from_stop: str, to_stop: str, after_minutes: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(after_minutes)
    cur.execute("""
                WITH leg1 AS (
            SELECT a.journey_id AS journey1, a.line_name AS line1, a.operator AS operator1,
                   a.scheduled_time AS depart1, a.stop_id AS from_stop_id1, b.stop_id AS transfer_stop_id1,
                   s_b.stop_name AS transfer_name, s_b.locality AS transfer_locality1,
                   s_b.latitude AS transfer_lat1, s_b.longitude AS transfer_lon1,
                   b.scheduled_time AS arrive1
            FROM journey_stops a
            JOIN journey_stops b
              ON a.journey_id = b.journey_id
             AND b.stop_sequence > a.stop_sequence
            JOIN stops s_b ON s_b.stop_id = b.stop_id
            WHERE a.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
              AND EXTRACT(EPOCH FROM a.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time) BETWEEN 0 AND 5400
        ),
        near AS (
            SELECT DISTINCT s2.stop_id
            FROM (SELECT DISTINCT transfer_lat1, transfer_lon1 FROM leg1) t
            JOIN stops s2
              ON ABS(s2.latitude - t.transfer_lat1) < 0.004
             AND ABS(s2.longitude - t.transfer_lon1) < 0.007
        ),
        leg2 AS (
            SELECT c.journey_id AS journey2, c.line_name AS line2, c.operator AS operator2,
                   c.stop_id AS transfer_stop_id2, d.stop_id AS to_stop_id2,
                   s_c.stop_name AS transfer_name, s_c.locality AS transfer_locality2,
                   s_c.latitude AS transfer_lat2, s_c.longitude AS transfer_lon2,
                   c.scheduled_time AS depart2, d.scheduled_time AS arrive2
            FROM journey_stops c
            JOIN journey_stops d
              ON c.journey_id = d.journey_id
             AND d.stop_sequence > c.stop_sequence
            JOIN stops s_c ON s_c.stop_id = c.stop_id
            WHERE d.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
              AND c.stop_id IN (SELECT stop_id FROM near)
              AND EXTRACT(EPOCH FROM c.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time) BETWEEN 0 AND 9000
        )
        SELECT leg1.line1, leg1.operator1, leg1.depart1, leg1.transfer_name, leg1.arrive1,
               leg1.transfer_lat1, leg1.transfer_lon1,
               leg2.line2, leg2.operator2, leg2.depart2, leg2.arrive2,
               leg2.transfer_lat2, leg2.transfer_lon2,
               leg1.journey1, leg1.from_stop_id1, leg1.transfer_stop_id1,
               leg2.journey2, leg2.transfer_stop_id2, leg2.to_stop_id2
        FROM leg1
        JOIN leg2
          ON ABS(leg1.transfer_lat1 - leg2.transfer_lat2) < 0.004
         AND ABS(leg1.transfer_lon1 - leg2.transfer_lon2) < 0.007
                  AND leg2.depart2 > leg1.arrive1
         AND leg1.journey1 != leg2.journey2
         AND leg1.arrive1 > leg1.depart1
         AND leg2.arrive2 > leg2.depart2
        ORDER BY leg2.arrive2
        LIMIT 1000
    """, (from_stop, current_time, to_stop, current_time))
    results = cur.fetchall()

    if not results:
        cur.close()
        conn.close()
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    trips = []
    for r in results:
        (line1, operator1, depart1, transfer_stop, arrive1, lat1, lon1,
         line2, operator2, depart2, arrive2, lat2, lon2,
         journey1, from_stop_id1, transfer_stop_id1, journey2, transfer_stop_id2, to_stop_id2) = r

        dist_m = distance_meters(lat1, lon1, lat2, lon2)
        if dist_m is not None and dist_m > 500:
            continue  # different real-world places that happen to share a stop name — not a real transfer

        gap_minutes = minutes_between(arrive1, depart2)
        total_duration = round(minutes_between(depart1, arrive2))

        walk_minutes = (dist_m / 84) if dist_m is not None else 0  # ~1.4 m/s walking pace
        spare_minutes = gap_minutes - walk_minutes

        if spare_minutes < 1:
            status = "tight"
            status_label = "Tight connection"
            reason = f"Buses are only {round(gap_minutes)} min apart, leaving little time after the {round(dist_m) if dist_m else '?'}m walk."
        elif gap_minutes > 25:
            status = "long_wait"
            status_label = "Long wait"
            reason = f"Works, but there's a {round(gap_minutes)} min wait between buses."
        else:
            status = "recommended"
            status_label = "Recommended"
            reason = f"About {round(dist_m) if dist_m else 0}m walk, {round(spare_minutes)} min to spare after walking."

        frequency_text = get_frequency_text(cur, from_stop, line1, str(depart1))

        trips.append({
            "line1": line1, "operator1": operator1, "depart1": str(depart1),
            "transfer_stop": transfer_stop, "arrive1": str(arrive1),
            "line2": line2, "operator2": operator2, "depart2": str(depart2),
            "arrive2": str(arrive2),
            "connection_minutes": round(gap_minutes),
            "walk_meters": round(dist_m) if dist_m is not None else None,
            "duration_minutes": total_duration,
            "status": status,
            "status_label": status_label,
            "reason": reason,
            "frequency": frequency_text,
            "legs": [
                {"journey_id": journey1, "from_stop_id": from_stop_id1, "to_stop_id": transfer_stop_id1},
                {"journey_id": journey2, "from_stop_id": transfer_stop_id2, "to_stop_id": to_stop_id2}
            ]
        })

        if len(trips) >= 80:
            break

    cur.close()
    conn.close()
        # remove repeats, put the best connections first, keep the top 5
    seen = set()
    unique = []
    best = {}
    for t in trips:
        key = (t["line1"], t["line2"], t["arrive2"])
        if key not in best or (t["walk_meters"] or 0) < (best[key]["walk_meters"] or 0):
            best[key] = t
    unique = list(best.values())
    rank = {"recommended": 0, "long_wait": 1, "tight": 2}
    unique.sort(key=lambda t: (rank.get(t["status"], 3), t["arrive2"]))
    trips = unique[:5]
    if not trips:
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    return trips

@app.get("/trip_transfer_v1")
def plan_trip_transfer_v1(from_stop: str, to_stop: str, after_minutes: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(after_minutes)

    cur.execute("""
        WITH origin_raw AS (
            SELECT s2.stop_id,
                   MIN(111320 * SQRT(POWER(s2.latitude - s1.latitude, 2)
                                   + POWER((s2.longitude - s1.longitude) * 0.6088, 2))) AS walk_m
            FROM stops s1
            JOIN stops s2
              ON ABS(s2.latitude - s1.latitude) < 0.01
             AND ABS(s2.longitude - s1.longitude) < 0.016
            WHERE s1.stop_name = %s
            GROUP BY s2.stop_id
        ),
        origin AS (
            SELECT stop_id, walk_m FROM origin_raw WHERE walk_m <= 1100
        ),
        leg2 AS (
            SELECT c.journey_id AS journey2, c.line_name AS line2, c.operator AS operator2,
                   c.stop_id AS transfer_stop_id2, d.stop_id AS to_stop_id2,
                   s_c.stop_name AS transfer_name, s_c.locality AS transfer_locality2,
                   s_c.latitude AS transfer_lat2, s_c.longitude AS transfer_lon2,
                   c.scheduled_time AS depart2, d.scheduled_time AS arrive2
            FROM journey_stops d
            JOIN journey_stops c
              ON c.journey_id = d.journey_id
             AND c.stop_sequence < d.stop_sequence
            JOIN stops s_c ON s_c.stop_id = c.stop_id
            WHERE d.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
              AND EXTRACT(EPOCH FROM c.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time) BETWEEN 0 AND 9000
        ),
        c_places AS (
            SELECT DISTINCT transfer_lat2, transfer_lon2 FROM leg2
        ),
        target AS (
            SELECT DISTINCT s2.stop_id
            FROM c_places t
            JOIN stops s2
              ON ABS(s2.latitude - t.transfer_lat2) < 0.004
             AND ABS(s2.longitude - t.transfer_lon2) < 0.007
        ),
        leg1 AS (
            SELECT a.journey_id AS journey1, a.line_name AS line1, a.operator AS operator1,
                   a.scheduled_time AS depart1, a.stop_id AS from_stop_id1, b.stop_id AS transfer_stop_id1,
                   s_b.stop_name AS transfer_name, s_b.locality AS transfer_locality1,
                   s_b.latitude AS transfer_lat1, s_b.longitude AS transfer_lon1,
                   b.scheduled_time AS arrive1,
                   o.walk_m AS start_walk_m, s_a.stop_name AS board_name
            FROM origin o
            JOIN journey_stops a ON a.stop_id = o.stop_id
            JOIN stops s_a ON s_a.stop_id = a.stop_id
            JOIN journey_stops b
              ON a.journey_id = b.journey_id
             AND b.stop_sequence > a.stop_sequence
             AND b.stop_id IN (SELECT stop_id FROM target)
            JOIN stops s_b ON s_b.stop_id = b.stop_id
            WHERE EXTRACT(EPOCH FROM a.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time)
                  BETWEEN (o.walk_m / 84.0 * 60) AND 5400
        )
        SELECT leg1.line1, leg1.operator1, leg1.depart1, leg1.transfer_name, leg1.arrive1,
               leg1.transfer_lat1, leg1.transfer_lon1,
               leg2.line2, leg2.operator2, leg2.depart2, leg2.arrive2,
               leg2.transfer_lat2, leg2.transfer_lon2,
               leg1.journey1, leg1.from_stop_id1, leg1.transfer_stop_id1,
               leg2.journey2, leg2.transfer_stop_id2, leg2.to_stop_id2,
               leg1.start_walk_m, leg1.board_name
        FROM leg1
        JOIN leg2
          ON ABS(leg1.transfer_lat1 - leg2.transfer_lat2) < 0.004
         AND ABS(leg1.transfer_lon1 - leg2.transfer_lon2) < 0.007
         AND leg2.depart2 > leg1.arrive1
         AND leg1.journey1 != leg2.journey2
         AND leg1.arrive1 > leg1.depart1
         AND leg2.arrive2 > leg2.depart2
        ORDER BY leg2.arrive2
        LIMIT 1000
    """, (from_stop, to_stop, current_time, current_time))
    results = cur.fetchall()

    if not results:
        cur.close()
        conn.close()
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    trips = []
    for r in results:
        (line1, operator1, depart1, transfer_stop, arrive1, lat1, lon1,
         line2, operator2, depart2, arrive2, lat2, lon2,
         journey1, from_stop_id1, transfer_stop_id1, journey2, transfer_stop_id2, to_stop_id2,
         start_walk_m, board_name) = r

        dist_m = distance_meters(lat1, lon1, lat2, lon2)
        if dist_m is not None and dist_m > 500:
            continue  # not a real transfer

        gap_minutes = minutes_between(arrive1, depart2)
        start_walk_min = (start_walk_m or 0) / 84.0
        total_duration = round(minutes_between(depart1, arrive2) + start_walk_min)

        walk_minutes = (dist_m / 84) if dist_m is not None else 0
        spare_minutes = gap_minutes - walk_minutes

        if spare_minutes < 1:
            status = "tight"
            status_label = "Tight connection"
            reason = f"Buses are only {round(gap_minutes)} min apart, leaving little time after the {max(1, round(walk_minutes))} min walk."
        elif gap_minutes > 25:
            status = "long_wait"
            status_label = "Long wait"
            reason = f"Works, but there's a {round(gap_minutes)} min wait between buses."
        else:
            status = "recommended"
            status_label = "Recommended"
            reason = f"About {max(1, round(walk_minutes))} min walk, {round(spare_minutes)} min to spare after walking."

        trips.append({
            "line1": line1, "operator1": operator1, "depart1": str(depart1),
            "transfer_stop": transfer_stop, "arrive1": str(arrive1),
            "line2": line2, "operator2": operator2, "depart2": str(depart2),
            "arrive2": str(arrive2),
            "connection_minutes": round(gap_minutes),
            "walk_meters": round(dist_m) if dist_m is not None else None,
            "board_stop": board_name,
            "start_walk_minutes": max(0, round(start_walk_min)),
            "duration_minutes": total_duration,
            "status": status,
            "status_label": status_label,
            "reason": reason,
            "legs": [
                {"journey_id": journey1, "from_stop_id": from_stop_id1, "to_stop_id": transfer_stop_id1},
                {"journey_id": journey2, "from_stop_id": transfer_stop_id2, "to_stop_id": to_stop_id2}
            ]
        })

    # keep one result per pair of buses and arrival time (the quickest one), best connections first
    best = {}
    for t in trips:
        key = (t["line1"], t["line2"], t["arrive2"])
        if key not in best or t["duration_minutes"] < best[key]["duration_minutes"]:
            best[key] = t
    unique = list(best.values())
    rank = {"recommended": 0, "long_wait": 1, "tight": 2}
    unique.sort(key=lambda t: (rank.get(t["status"], 3), t["arrive2"]))
    trips = unique[:5]

    # only look up "how often does it run" for the few cards we actually show
    for t in trips:
        t["frequency"] = get_frequency_text(cur, t["board_stop"], t["line1"], t["depart1"])

    cur.close()
    conn.close()
    if not trips:
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    return trips

@app.get("/trip_transfer_v2")
def plan_trip_transfer_v2(from_stop: str, to_stop: str, after_minutes: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(after_minutes)

    cur.execute("""
        WITH origin_raw AS (
            SELECT s2.stop_id,
                   MIN(111320 * SQRT(POWER(s2.latitude - s1.latitude, 2)
                                   + POWER((s2.longitude - s1.longitude) * 0.6088, 2))) AS walk_m
            FROM stops s1
            JOIN stops s2
              ON ABS(s2.latitude - s1.latitude) < 0.01
             AND ABS(s2.longitude - s1.longitude) < 0.016
            WHERE s1.stop_name = %s
            GROUP BY s2.stop_id
        ),
        origin AS (
            SELECT stop_id, walk_m FROM origin_raw WHERE walk_m <= 1000
        ),
        leg2 AS (
            SELECT c.journey_id AS journey2, c.line_name AS line2, c.operator AS operator2,
                   c.stop_id AS transfer_stop_id2, d.stop_id AS to_stop_id2,
                   s_c.stop_name AS transfer_name, s_c.locality AS transfer_locality2,
                   s_c.latitude AS transfer_lat2, s_c.longitude AS transfer_lon2,
                   c.scheduled_time AS depart2, d.scheduled_time AS arrive2
            FROM journey_stops d
            JOIN journey_stops c
              ON c.journey_id = d.journey_id
             AND c.stop_sequence < d.stop_sequence
            JOIN stops s_c ON s_c.stop_id = c.stop_id
            WHERE d.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
              AND EXTRACT(EPOCH FROM c.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time) BETWEEN 180 AND 7200
        ),
        c_places AS (
            SELECT DISTINCT transfer_lat2, transfer_lon2 FROM leg2
        ),
        target AS (
            SELECT DISTINCT s2.stop_id
            FROM c_places t
            JOIN stops s2
              ON ABS(s2.latitude - t.transfer_lat2) < 0.004
             AND ABS(s2.longitude - t.transfer_lon2) < 0.007
        ),
        leg1_a AS (
            SELECT a.journey_id, a.line_name, a.operator, a.scheduled_time, a.stop_id,
                   a.stop_sequence, o.walk_m,
                   ROW_NUMBER() OVER (PARTITION BY a.stop_id, a.line_name
                                      ORDER BY a.scheduled_time::time) AS rn
            FROM origin o
            JOIN journey_stops a ON a.stop_id = o.stop_id
            WHERE EXTRACT(EPOCH FROM a.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time)
                  BETWEEN (o.walk_m / 84.0 * 60) AND 3600
        ),
        leg1_first AS (
            SELECT * FROM leg1_a WHERE rn <= 3
        ),
        leg1 AS (
            SELECT a.journey_id AS journey1, a.line_name AS line1, a.operator AS operator1,
                   a.scheduled_time AS depart1, a.stop_id AS from_stop_id1, b.stop_id AS transfer_stop_id1,
                   s_b.stop_name AS transfer_name, s_b.locality AS transfer_locality1,
                   s_b.latitude AS transfer_lat1, s_b.longitude AS transfer_lon1,
                   b.scheduled_time AS arrive1,
                   a.walk_m AS start_walk_m, s_a.stop_name AS board_name
            FROM leg1_first a
            JOIN stops s_a ON s_a.stop_id = a.stop_id
            JOIN journey_stops b
              ON a.journey_id = b.journey_id
             AND b.stop_sequence > a.stop_sequence
             AND b.stop_id IN (SELECT stop_id FROM target)
            JOIN stops s_b ON s_b.stop_id = b.stop_id
        )
        SELECT leg1.line1, leg1.operator1, leg1.depart1, leg1.transfer_name, leg1.arrive1,
               leg1.transfer_lat1, leg1.transfer_lon1,
               leg2.line2, leg2.operator2, leg2.depart2, leg2.arrive2,
               leg2.transfer_lat2, leg2.transfer_lon2,
               leg1.journey1, leg1.from_stop_id1, leg1.transfer_stop_id1,
               leg2.journey2, leg2.transfer_stop_id2, leg2.to_stop_id2,
               leg1.start_walk_m, leg1.board_name
        FROM leg1
        JOIN leg2
          ON ABS(leg1.transfer_lat1 - leg2.transfer_lat2) < 0.004
         AND ABS(leg1.transfer_lon1 - leg2.transfer_lon2) < 0.007
         AND leg2.depart2 > leg1.arrive1
         AND EXTRACT(EPOCH FROM leg2.depart2::time) - EXTRACT(EPOCH FROM leg1.arrive1::time) BETWEEN 0 AND 2700
         AND leg1.journey1 != leg2.journey2
         AND leg1.arrive1 > leg1.depart1
         AND leg2.arrive2 > leg2.depart2
        ORDER BY leg2.arrive2
        LIMIT 600
    """, (from_stop, to_stop, current_time, current_time))
    results = cur.fetchall()

    if not results:
        cur.close()
        conn.close()
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    trips = []
    for r in results:
        (line1, operator1, depart1, transfer_stop, arrive1, lat1, lon1,
         line2, operator2, depart2, arrive2, lat2, lon2,
         journey1, from_stop_id1, transfer_stop_id1, journey2, transfer_stop_id2, to_stop_id2,
         start_walk_m, board_name) = r

        dist_m = distance_meters(lat1, lon1, lat2, lon2)
        if dist_m is not None and dist_m > 500:
            continue  # not a real transfer

        gap_minutes = minutes_between(arrive1, depart2)
        start_walk_min = (start_walk_m or 0) / 84.0
        total_duration = round(minutes_between(depart1, arrive2) + start_walk_min)

        walk_minutes = (dist_m / 84) if dist_m is not None else 0
        spare_minutes = gap_minutes - walk_minutes

        if spare_minutes < 1:
            status = "tight"
            status_label = "Tight connection"
            reason = f"Buses are only {round(gap_minutes)} min apart, leaving little time after the {max(1, round(walk_minutes))} min walk."
        elif gap_minutes > 25:
            status = "long_wait"
            status_label = "Long wait"
            reason = f"Works, but there's a {round(gap_minutes)} min wait between buses."
        else:
            status = "recommended"
            status_label = "Recommended"
            reason = f"About {max(1, round(walk_minutes))} min walk, {round(spare_minutes)} min to spare after walking."

        trips.append({
            "line1": line1, "operator1": operator1, "depart1": str(depart1),
            "transfer_stop": transfer_stop, "arrive1": str(arrive1),
            "line2": line2, "operator2": operator2, "depart2": str(depart2),
            "arrive2": str(arrive2),
            "connection_minutes": round(gap_minutes),
            "walk_meters": round(dist_m) if dist_m is not None else None,
            "board_stop": board_name,
            "start_walk_minutes": max(0, round(start_walk_min)),
            "duration_minutes": total_duration,
            "status": status,
            "status_label": status_label,
            "reason": reason,
            "legs": [
                {"journey_id": journey1, "from_stop_id": from_stop_id1, "to_stop_id": transfer_stop_id1},
                {"journey_id": journey2, "from_stop_id": transfer_stop_id2, "to_stop_id": to_stop_id2}
            ]
        })

    # one result per pair of buses (the earliest arrival), best connections first,
    # and at most 2 results per first bus so you see different options
    best = {}
    for t in trips:
        key = (t["line1"], t["line2"])
        if key not in best or (t["arrive2"], t["duration_minutes"]) < (best[key]["arrive2"], best[key]["duration_minutes"]):
            best[key] = t
    unique = list(best.values())
    rank = {"recommended": 0, "long_wait": 1, "tight": 2}
    unique.sort(key=lambda t: (rank.get(t["status"], 3), t["arrive2"]))

    picked = []
    per_line = {}
    for t in unique:
        if per_line.get(t["line1"], 0) >= 2:
            continue
        per_line[t["line1"]] = per_line.get(t["line1"], 0) + 1
        picked.append(t)
        if len(picked) >= 5:
            break
    trips = picked

    # only look up "how often does it run" for the few cards we actually show
    for t in trips:
        t["frequency"] = get_frequency_text(cur, t["board_stop"], t["line1"], t["depart1"])

    cur.close()
    conn.close()
    if not trips:
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    return trips

@app.get("/trip_transfer")
def plan_trip_transfer(from_stop: str, to_stop: str, after_minutes: int = 0):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(after_minutes)

    cur.execute("""
        WITH origin_raw AS (
            SELECT s2.stop_id,
                   MIN(111320 * SQRT(POWER(s2.latitude - s1.latitude, 2)
                                   + POWER((s2.longitude - s1.longitude) * 0.6088, 2))) AS walk_m
            FROM stops s1
            JOIN stops s2
              ON s2.latitude BETWEEN s1.latitude - 0.01 AND s1.latitude + 0.01
             AND s2.longitude BETWEEN s1.longitude - 0.016 AND s1.longitude + 0.016
            WHERE s1.stop_name = %s
            GROUP BY s2.stop_id
        ),
        origin AS (
            SELECT stop_id, walk_m FROM origin_raw WHERE walk_m <= 1000
        ),
        leg2 AS (
            SELECT c.journey_id AS journey2, c.line_name AS line2, c.operator AS operator2,
                   c.stop_id AS transfer_stop_id2, d.stop_id AS to_stop_id2,
                   s_c.stop_name AS transfer_name, s_c.locality AS transfer_locality2,
                   s_c.latitude AS transfer_lat2, s_c.longitude AS transfer_lon2,
                   c.scheduled_time AS depart2, d.scheduled_time AS arrive2
            FROM journey_stops d
            JOIN journey_stops c
              ON c.journey_id = d.journey_id
             AND c.stop_sequence < d.stop_sequence
            JOIN stops s_c ON s_c.stop_id = c.stop_id
            WHERE d.stop_id IN (SELECT stop_id FROM stops WHERE stop_name = %s)
              AND EXTRACT(EPOCH FROM c.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time) BETWEEN 180 AND 7200
        ),
        c_places AS (
            SELECT DISTINCT transfer_lat2, transfer_lon2 FROM leg2
        ),
        target AS (
            SELECT DISTINCT s2.stop_id
            FROM c_places t
            JOIN stops s2
              ON s2.latitude BETWEEN t.transfer_lat2 - 0.004 AND t.transfer_lat2 + 0.004
             AND s2.longitude BETWEEN t.transfer_lon2 - 0.007 AND t.transfer_lon2 + 0.007
        ),
        leg1_a AS (
            SELECT a.journey_id, a.line_name, a.operator, a.scheduled_time, a.stop_id,
                   a.stop_sequence, o.walk_m,
                   ROW_NUMBER() OVER (PARTITION BY a.stop_id, a.line_name
                                      ORDER BY a.scheduled_time::time) AS rn
            FROM origin o
            JOIN journey_stops a ON a.stop_id = o.stop_id
            WHERE EXTRACT(EPOCH FROM a.scheduled_time::time) - EXTRACT(EPOCH FROM %s::time)
                  BETWEEN (o.walk_m / 84.0 * 60) AND 3600
        ),
        leg1_first AS (
            SELECT * FROM leg1_a WHERE rn <= 3
        ),
        leg1 AS (
            SELECT a.journey_id AS journey1, a.line_name AS line1, a.operator AS operator1,
                   a.scheduled_time AS depart1, a.stop_id AS from_stop_id1, b.stop_id AS transfer_stop_id1,
                   s_b.stop_name AS transfer_name, s_b.locality AS transfer_locality1,
                   s_b.latitude AS transfer_lat1, s_b.longitude AS transfer_lon1,
                   b.scheduled_time AS arrive1,
                   a.walk_m AS start_walk_m, s_a.stop_name AS board_name
            FROM leg1_first a
            JOIN stops s_a ON s_a.stop_id = a.stop_id
            JOIN journey_stops b
              ON a.journey_id = b.journey_id
             AND b.stop_sequence > a.stop_sequence
             AND b.stop_id IN (SELECT stop_id FROM target)
            JOIN stops s_b ON s_b.stop_id = b.stop_id
        ),
        near_b AS (
            SELECT DISTINCT s2.stop_id
            FROM (SELECT DISTINCT transfer_lat1, transfer_lon1 FROM leg1) t
            JOIN stops s2
              ON s2.latitude BETWEEN t.transfer_lat1 - 0.004 AND t.transfer_lat1 + 0.004
             AND s2.longitude BETWEEN t.transfer_lon1 - 0.007 AND t.transfer_lon1 + 0.007
        ),
        leg2f AS (
            SELECT * FROM leg2 WHERE transfer_stop_id2 IN (SELECT stop_id FROM near_b)
        )
        SELECT leg1.line1, leg1.operator1, leg1.depart1, leg1.transfer_name, leg1.arrive1,
               leg1.transfer_lat1, leg1.transfer_lon1,
               leg2.line2, leg2.operator2, leg2.depart2, leg2.arrive2,
               leg2.transfer_lat2, leg2.transfer_lon2,
               leg1.journey1, leg1.from_stop_id1, leg1.transfer_stop_id1,
               leg2.journey2, leg2.transfer_stop_id2, leg2.to_stop_id2,
               leg1.start_walk_m, leg1.board_name
        FROM leg1
        JOIN leg2f AS leg2
          ON ABS(leg1.transfer_lat1 - leg2.transfer_lat2) < 0.004
         AND ABS(leg1.transfer_lon1 - leg2.transfer_lon2) < 0.007
         AND leg2.depart2 > leg1.arrive1
         AND EXTRACT(EPOCH FROM leg2.depart2::time) - EXTRACT(EPOCH FROM leg1.arrive1::time) BETWEEN 0 AND 2700
         AND leg1.journey1 != leg2.journey2
         AND leg1.arrive1 > leg1.depart1
         AND leg2.arrive2 > leg2.depart2
        ORDER BY leg2.arrive2
        LIMIT 600
    """, (from_stop, to_stop, current_time, current_time))
    results = cur.fetchall()

    if not results:
        cur.close()
        conn.close()
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    trips = []
    for r in results:
        (line1, operator1, depart1, transfer_stop, arrive1, lat1, lon1,
         line2, operator2, depart2, arrive2, lat2, lon2,
         journey1, from_stop_id1, transfer_stop_id1, journey2, transfer_stop_id2, to_stop_id2,
         start_walk_m, board_name) = r

        dist_m = distance_meters(lat1, lon1, lat2, lon2)
        if dist_m is not None and dist_m > 500:
            continue  # not a real transfer

        gap_minutes = minutes_between(arrive1, depart2)
        start_walk_min = (start_walk_m or 0) / 84.0
        total_duration = round(minutes_between(depart1, arrive2) + start_walk_min)

        walk_minutes = (dist_m / 84) if dist_m is not None else 0
        spare_minutes = gap_minutes - walk_minutes

        if spare_minutes < 1:
            status = "tight"
            status_label = "Tight connection"
            reason = f"Buses are only {round(gap_minutes)} min apart, leaving little time after the {max(1, round(walk_minutes))} min walk."
        elif gap_minutes > 25:
            status = "long_wait"
            status_label = "Long wait"
            reason = f"Works, but there's a {round(gap_minutes)} min wait between buses."
        else:
            status = "recommended"
            status_label = "Recommended"
            reason = f"About {max(1, round(walk_minutes))} min walk, {round(spare_minutes)} min to spare after walking."

        trips.append({
            "line1": line1, "operator1": operator1, "depart1": str(depart1),
            "transfer_stop": transfer_stop, "arrive1": str(arrive1),
            "line2": line2, "operator2": operator2, "depart2": str(depart2),
            "arrive2": str(arrive2),
            "connection_minutes": round(gap_minutes),
            "walk_meters": round(dist_m) if dist_m is not None else None,
            "board_stop": board_name,
            "start_walk_minutes": max(0, round(start_walk_min)),
            "duration_minutes": total_duration,
            "status": status,
            "status_label": status_label,
            "reason": reason,
            "legs": [
                {"journey_id": journey1, "from_stop_id": from_stop_id1, "to_stop_id": transfer_stop_id1},
                {"journey_id": journey2, "from_stop_id": transfer_stop_id2, "to_stop_id": to_stop_id2}
            ]
        })

    # one result per pair of buses (the earliest arrival), best connections first,
    # and at most 2 results per first bus so you see different options
    best = {}
    for t in trips:
        key = (t["line1"], t["line2"])
        if key not in best or (t["arrive2"], t["duration_minutes"]) < (best[key]["arrive2"], best[key]["duration_minutes"]):
            best[key] = t
    unique = list(best.values())
    rank = {"recommended": 0, "long_wait": 1, "tight": 2}
    unique.sort(key=lambda t: (rank.get(t["status"], 3), t["arrive2"]))

    picked = []
    per_line = {}
    for t in unique:
        if per_line.get(t["line1"], 0) >= 2:
            continue
        per_line[t["line1"]] = per_line.get(t["line1"], 0) + 1
        picked.append(t)
        if len(picked) >= 5:
            break
    trips = picked

    # only look up "how often does it run" for the few cards we actually show
    for t in trips:
        t["frequency"] = get_frequency_text(cur, t["board_stop"], t["line1"], t["depart1"])

    cur.close()
    conn.close()
    if not trips:
        return {"message": "No connecting route found (even with a change of bus) in our data."}

    return trips
@app.get("/journey_detail")
def journey_detail(journey_id: str, from_stop_id: str, to_stop_id: str):
    """Returns every stop, in order, that this specific bus run passes through
    between the boarding stop and the alighting stop — plus where to stand at
    the boarding stop."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT stop_sequence FROM journey_stops
        WHERE journey_id = %s AND stop_id = %s
    """, (journey_id, from_stop_id))
    from_row = cur.fetchone()

    cur.execute("""
        SELECT stop_sequence FROM journey_stops
        WHERE journey_id = %s AND stop_id = %s
    """, (journey_id, to_stop_id))
    to_row = cur.fetchone()

    if not from_row or not to_row:
        cur.close()
        conn.close()
        return {"message": "Could not find that leg of the journey in our data."}

    from_sequence, to_sequence = from_row[0], to_row[0]

    cur.execute("""
        SELECT js.stop_sequence, js.scheduled_time, s.stop_name, s.locality,
               s.latitude, s.longitude, s.street, s.indicator, s.bearing
        FROM journey_stops js
        JOIN stops s ON js.stop_id = s.stop_id
        WHERE js.journey_id = %s
          AND js.stop_sequence BETWEEN %s AND %s
        ORDER BY js.stop_sequence
    """, (journey_id, from_sequence, to_sequence))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    stops = []
    for i, row in enumerate(rows):
        seq, sched_time, stop_name, locality, lat, lon, street, indicator, bearing = row
        entry = {
            "stop_name": stop_name,
            "locality": locality,
            "scheduled_time": str(sched_time),
            "latitude": lat,
            "longitude": lon,
        }
        if i == 0:
            entry["stand_where"] = stand_where_text(street, indicator, bearing)
        stops.append(entry)

    return {"stops": stops, "stop_count": len(stops)}
@app.get("/live_delay")
def live_delay(line: str):
    conn = get_connection()
    cur = conn.cursor()
    # latest position of each bus on this line in the last 10 minutes
    cur.execute("""
        SELECT DISTINCT ON (vehicle_ref) vehicle_ref, latitude, longitude,
               (recorded_at AT TIME ZONE 'Europe/London')::time AS local_time,
               destination_name
        FROM vehicle_positions
        WHERE line_name = %s AND recorded_at > now() - interval '10 minutes'
        ORDER BY vehicle_ref, recorded_at DESC
    """, (line,))
    buses = cur.fetchall()

    out = []
    for vehicle_ref, lat, lon, local_time, dest in buses:
        # timetable stops within ~150 m of the bus, for this line, nearest scheduled time first
        cur.execute("""
            SELECT s.stop_name, js.scheduled_time,
                   EXTRACT(EPOCH FROM (%s::time - js.scheduled_time::time)) / 60 AS diff_min
            FROM stops s
            JOIN journey_stops js ON js.stop_id = s.stop_id
            WHERE ABS(s.latitude - %s) < 0.0015 AND ABS(s.longitude - %s) < 0.0025
              AND js.line_name = %s
              AND ABS(EXTRACT(EPOCH FROM (%s::time - js.scheduled_time::time))) <= 1800
            ORDER BY ABS(EXTRACT(EPOCH FROM (%s::time - js.scheduled_time::time)))
            LIMIT 1
        """, (local_time, lat, lon, line, local_time, local_time))
        row = cur.fetchone()

        if row is None:
            out.append({"vehicle": vehicle_ref, "destination": dest,
                        "latitude": lat, "longitude": lon,
                        "status": "unknown", "delay_minutes": None,
                        "label": "Live position only"})
            continue

        stop_name, scheduled, diff_min = row
        delay = round(diff_min)
        if delay > 2:
            status, label = "late", f"About {delay} min late"
        elif delay < -1:
            status, label = "early", f"About {abs(delay)} min early"
        else:
            status, label = "on_time", "On time"
        out.append({"vehicle": vehicle_ref, "destination": dest,
                    "latitude": lat, "longitude": lon,
                    "near_stop": stop_name, "scheduled": str(scheduled),
                    "status": status, "delay_minutes": delay, "label": label})

    cur.close()
    conn.close()
    return {"line": line, "count": len(out), "buses": out}

@app.get("/live_buses")
def live_buses(line: str):
    buses = get_live_buses_for_line(line)
    return {"line": line, "count": len(buses), "buses": buses}

@app.get("/stops/nearby")
def stops_nearby(lat: float, lon: float):
    conn = get_connection()
    cur = conn.cursor()
    current_time = london_time_after(0)

    cur.execute("""
        SELECT stop_id, stop_name, locality, latitude, longitude
        FROM stops
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
          AND ABS(latitude - %s) < 0.006 AND ABS(longitude - %s) < 0.009
        ORDER BY ((latitude - %s) * (latitude - %s)) + ((longitude - %s) * (longitude - %s))
        LIMIT 15
    """, (lat, lon, lat, lat, lon, lon))
    stops = cur.fetchall()

    out = []
    for stop_id, stop_name, locality, slat, slon in stops:
        cur.execute("""
            SELECT line_name, scheduled_time,
                   EXTRACT(EPOCH FROM (scheduled_time::time - %s::time)) / 60 AS due_min
            FROM journey_stops
            WHERE stop_id = %s AND scheduled_time::time > %s::time
            ORDER BY scheduled_time::time
            LIMIT 1
        """, (current_time, stop_id, current_time))
        nxt = cur.fetchone()
        if nxt is None:
            continue
        d = distance_meters(lat, lon, slat, slon)
        out.append({
            "stop_id": stop_id, "stop_name": stop_name, "locality": locality,
            "line_name": nxt[0], "departure_time": str(nxt[1]),
            "due_minutes": max(0, round(nxt[2])),
            "distance_m": round(d) if d is not None else None
        })
        if len(out) >= 3:
            break

    cur.close()
    conn.close()
    return out

@app.get("/stops/nearest")
def nearest_stop(lat: float, lon: float):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT stop_id, stop_name, locality,
               ((latitude - %s) * (latitude - %s)) + ((longitude - %s) * (longitude - %s)) AS distance
        FROM stops
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY distance ASC
        LIMIT 1
    """, (lat, lat, lon, lon))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        return {"message": "Could not find a nearby stop."}
    return {"stop_id": result[0], "stop_name": result[1], "locality": result[2]}

def _fmt12(t):
    try:
        h, m = str(t).split(":")[:2]
        h = int(h) % 24
        return f"{h % 12 or 12}:{m} {'PM' if h >= 12 else 'AM'}"
    except Exception:
        return str(t)


def _clean_place(s):
    s = s.strip(" ?.!,")
    return re.sub(r"\s+(please|now|today|right now)$", "", s, flags=re.I).strip()


def _find_stop(name):
    results = search_stops(name)
    return results[0] if results else None


def _answer_trip(from_name, to_name):
    f = _find_stop(from_name)
    t = _find_stop(to_name)
    if not f:
        return f"I couldn't find a stop called “{from_name}”."
    if not t:
        return f"I couldn't find a stop called “{to_name}”."
    fn, tn = f["stop_name"], t["stop_name"]

    trips = plan_trip(fn, tn, 0)
    if isinstance(trips, list) and trips:
        x = trips[0]
        return (f"Next direct bus: route {x['line_name']} leaves {fn} at {_fmt12(x['depart_time'])} "
                f"and arrives at {tn} at {_fmt12(x['arrive_time'])} ({x['duration_minutes']} min).")

    trips2 = plan_trip_transfer(fn, tn, 0)
    if isinstance(trips2, list) and trips2:
        x = trips2[0]
        walk = x.get("walk_meters")
        walk_text = f", walk about {max(1, round(walk / 84))} min," if walk else ""
        return (f"No direct bus. Take route {x['line1']} from {fn} at {_fmt12(x['depart1'])} "
                f"to {x['transfer_stop']}{walk_text} then route {x['line2']} at {_fmt12(x['depart2'])}, "
                f"arriving at {tn} at {_fmt12(x['arrive2'])} ({x['duration_minutes']} min in total).")

    return "I couldn't find a bus route between those stops in our data right now."


@app.get("/chat")
def chat(q: str, lat: float = None, lon: float = None):
    help_text = ("I can answer things like: “next bus from Clarence Road to Gracechurch Centre”, "
                 "“next bus to Selly Oak” (uses your location), “buses at Bibsworth Avenue”, "
                 "or “is the 11C late?”")
    try:
        text = q.strip()
        low = text.lower()
        quick = _answer_line_at_stop(text, low)
        if quick:
            return {"answer": quick}

        # 1. "... from A to B"
        m = re.search(r"\bfrom\s+(.+?)\s+to\s+(.+)$", text, flags=re.I)
        if m:
            return {"answer": _answer_trip(_clean_place(m.group(1)), _clean_place(m.group(2)))}

        # 2. "next bus to B" / "how do I get to B"  (starts from the user's location)
        m = re.search(r"\b(?:bus|buses|get|go|travel)\s+to\s+(.+)$", text, flags=re.I)
        if m:
            if lat is None or lon is None:
                return {"answer": "I need to know where you're starting. Allow location in your browser, "
                                  "or ask: “next bus from <your stop> to " + _clean_place(m.group(1)) + "”."}
            near = nearest_stop(lat, lon)
            if "stop_name" not in near:
                return {"answer": "I couldn't find a stop near you."}
            return {"answer": _answer_trip(near["stop_name"], _clean_place(m.group(1)))}

        # 3. "buses at A" / "departures from A"
        m = re.search(r"\b(?:buses|departures|bus|times)\s+(?:at|from)\s+(.+)$", text, flags=re.I)
        if m:
            stop = _find_stop(_clean_place(m.group(1)))
            if not stop:
                return {"answer": f"I couldn't find a stop called “{_clean_place(m.group(1))}”."}
            data = get_arrivals(stop["stop_id"])
            if "arrivals" not in data:
                return {"answer": f"No upcoming buses found at {stop['stop_name']} right now."}
            lines = [f"route {a['line_name']} at {_fmt12(a['departure_time'])}" for a in data["arrivals"][:4]]
            return {"answer": f"Next buses at {stop['stop_name']}: " + "; ".join(lines) + "."}

        # 4. "is the 11C late?" / "where is the 6"
        if re.search(r"\b(late|delay|delayed|on time|where is|running)\b", low):
            m = re.search(r"\b(x?\d{1,3}[a-z]?)\b", low)
            if m:
                line = m.group(1).upper()
                data = live_delay(line)
                buses = data.get("buses", [])
                if not buses:
                    return {"answer": f"No live buses are reporting for route {line} right now (it may be off-hours)."}
                known = [b for b in buses if b.get("delay_minutes") is not None]
                if known:
                    b = known[0]
                    return {"answer": f"Route {line}: {len(buses)} bus(es) tracked. One near {b['near_stop']} is "
                                      f"{b['label'].lower()} (estimated from its position)."}
                return {"answer": f"Route {line}: {len(buses)} bus(es) tracked live, but none is at a timetable stop "
                                  f"right now, so I can't say if they're late."}

        return {"answer": help_text}
    except Exception:
        import traceback
        traceback.print_exc()
        return {"answer": "Sorry, something went wrong answering that. " + help_text}

    _STOP_NAMES_CACHE = None


_STOP_NAMES_CACHE = None

def _stop_by_name_fuzzy(name):
    """Find (stop_name, lat, lon) for a typed name, forgiving small typos."""
    global _STOP_NAMES_CACHE
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT stop_name, latitude, longitude FROM stops
        WHERE stop_name ILIKE %s AND latitude IS NOT NULL
        ORDER BY (lower(stop_name) = lower(%s)) DESC, length(stop_name)
        LIMIT 1
    """, (f"%{name}%", name))
    row = cur.fetchone()
    if row is None:
        import difflib
        if _STOP_NAMES_CACHE is None:
            cur.execute("SELECT DISTINCT stop_name FROM stops WHERE stop_name IS NOT NULL")
            _STOP_NAMES_CACHE = {r[0].lower(): r[0] for r in cur.fetchall()}
        close = difflib.get_close_matches(name.lower(), list(_STOP_NAMES_CACHE.keys()), n=1, cutoff=0.75)
        if close:
            real = _STOP_NAMES_CACHE[close[0]]
            cur.execute("""
                SELECT stop_name, latitude, longitude FROM stops
                WHERE stop_name = %s AND latitude IS NOT NULL LIMIT 1
            """, (real,))
            row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def _add_minutes(t, mins):
    h, m = str(t).split(":")[:2]
    total = (int(h) * 60 + int(m) + mins) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}:00"


def _answer_line_at_stop(text, low):
    """Handles: 'where is the 76 ... when will it come to <stop>' / 'is the 11C late at <stop>'."""
    if not re.search(r"\b(where is|where's|when will|when does|when is|how far|late|delay|delayed|running)\b", low):
        return None
    lm = re.search(r"\b(x?\d{1,3}[a-z]?)\b(?!:)", low)
    if not lm:
        return None
    sm = re.search(r"\b(?:come to|arrive at|arrive in|reach|get to|at|to)\s+(.+?)(?:\s+(?:so|and|because|then|but)\b|[?.!]*$)",
                   text[lm.end():], flags=re.I)
    if not sm:
        return None

    line = lm.group(1).upper()
    stop_in = _clean_place(sm.group(1))
    stop = _stop_by_name_fuzzy(stop_in)
    if not stop:
        return f"I couldn't find a stop called “{stop_in}”."
    stop_name, slat, slon = stop

    current_time = london_time_after(0)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT js.scheduled_time
        FROM journey_stops js
        JOIN stops s ON s.stop_id = js.stop_id
        WHERE s.stop_name = %s AND js.line_name = %s AND js.scheduled_time::time > %s::time
        ORDER BY js.scheduled_time
        LIMIT 3
    """, (stop_name, line, current_time))
    times = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    if not times:
        return (f"I can't find any more scheduled buses of route {line} at {stop_name} right now. "
                f"Check the route number, or it may have finished for today.")

    parts = [f"Route {line} at {stop_name}: next scheduled {', '.join(_fmt12(t) for t in times)}."]

    buses = live_delay(line).get("buses", [])
    if not buses:
        parts.append("No live bus is reporting for this route right now, so these are timetable times only.")
        return " ".join(parts)

    def dist(b):
        d = distance_meters(slat, slon, b["latitude"], b["longitude"])
        return d if d is not None else 10**9

    def nice(d):
        return f"{d / 1000:.1f} km" if d >= 1000 else f"{round(d)} m"

    nearest = min(buses, key=dist)
    parts.append(f"The closest live bus on this route is about {nice(dist(nearest))} from the stop "
                 f"(I can't tell which direction it's heading).")

    known = [b for b in buses if b.get("delay_minutes") is not None and dist(b) < 10000]
    if known:
        kb = min(known, key=dist)
        delay = kb["delay_minutes"]
        expected = _fmt12(_add_minutes(times[0], delay))
        parts.append(f"A bus near the stop area is {kb['label'].lower()}, so the {_fmt12(times[0])} bus "
                     f"may arrive around {expected} (estimate).")
    else:
        parts.append("None of the nearby buses is at a timetable stop right now, so I can't say if they're late.")
    return " ".join(parts)